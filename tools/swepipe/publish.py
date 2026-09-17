"""publish / push: put verified task dirs on GitHub and write the URL back to the ledger.

GitHubPublisher works through the `gh` CLI and git.  Which account it acts as is configuration:
  GITHUB_OWNER        user or organisation that will own <owner>/<task_id>
  GITHUB_TOKEN        optional; exported as GH_TOKEN to gh and used by git via gh's credential
                      helper, so switching accounts = changing two variables (no `gh auth login`)
  GITHUB_VISIBILITY   public (default) | private
Without GITHUB_TOKEN the locally logged-in `gh` account is used and GITHUB_OWNER defaults to it.

A task repo holds exactly environment/, instruction.md and task.toml on `main`.  Existing repos
are cloned and fast-forwarded with one commit (their .gitignore is kept); nothing is force-pushed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .build import load_results, verified
from .config import Config
from .ledger import load_records
from .model import TOO_LARGE_MARK
from .sources import LedgerSource

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import pin_lint  # noqa: E402

TASK_FILES = ("environment", "instruction.md", "task.toml")


class GitHubPublisher:
    def __init__(self, config: Config):
        self.env = dict(os.environ)
        if config.github_token:
            self.env["GH_TOKEN"] = config.github_token
        self.owner = config.github_owner or self.gh("api", "user", "--jq", ".login").stdout.strip()
        self.visibility = config.github_visibility
        self.git_env = dict(self.env, GIT_TERMINAL_PROMPT="0")

    def describe(self) -> str:
        return f"github.com/{self.owner} ({self.visibility}, {'GITHUB_TOKEN' if 'GH_TOKEN' in self.env else 'local gh login'})"

    def gh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, env=self.env)
        if check and p.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
        return p

    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args]
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=self.git_env)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
        return p

    def repo_url(self, task_id: str) -> str:
        return f"https://github.com/{self.owner}/{task_id}"

    def exists(self, task_id: str) -> bool:
        return self.gh("repo", "view", f"{self.owner}/{task_id}", "--json", "url", check=False).returncode == 0

    def publish(self, task_id: str, task_dir: Path, title: str) -> str:
        """Create or update <owner>/<task_id>; returns the repo URL."""
        for name in TASK_FILES:
            if not (task_dir / name).exists():
                raise RuntimeError(f"{task_id}: missing {name}")
        exists = self.exists(task_id)
        with tempfile.TemporaryDirectory(prefix="swepipe-publish-") as tmp:
            dst = Path(tmp) / task_id
            if exists:
                self.gh("repo", "clone", f"{self.owner}/{task_id}", str(dst), "--", "-q")
                for child in dst.iterdir():
                    if child.name not in (".git", ".gitignore"):
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
            else:
                dst.mkdir()
                self._git(dst, "init", "-q", "-b", "main")
            for name in TASK_FILES:
                src = task_dir / name
                if src.is_dir():
                    shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns(".DS_Store", ".git"))
                else:
                    shutil.copy2(src, dst / name)
            self._git(dst, "add", "-A")
            dirty = self._git(dst, "status", "--porcelain").stdout.strip()
            if dirty:
                self._git(dst, "-c", "user.name=swe-task-pipeline", "-c", "user.email=swe-task-pipeline@users.noreply.github.com",
                          "commit", "-q", "-m", f"{'Update' if exists else 'Add'} SWE-like task environment: {task_id}")
            if not exists:
                self.gh("repo", "create", f"{self.owner}/{task_id}", f"--{self.visibility}", "--source", str(dst),
                        "--push", "--description", f"SWE-like task environment: {title}"[:350])
            elif dirty:
                self._git(dst, "push", "origin", "HEAD")
        return self.repo_url(task_id)


def task_title(tasks_dir: Path, task_id: str) -> str:
    m = re.search(r'^original_title = "(.*)"$', (tasks_dir / task_id / "task.toml").read_text(encoding="utf-8"), re.M)
    return json.loads('"' + m.group(1) + '"') if m else task_id


def lint_text(tasks_dir: Path, task_id: str) -> str:
    df = tasks_dir / task_id / "environment" / "Dockerfile"
    return "; ".join(pin_lint.violations_as_text(pin_lint.lint(df))) if df.exists() else ""


def run_publish(config: Config, source: LedgerSource, publisher: GitHubPublisher, platforms: list[str],
                max_image_gb: float, dry_run: bool) -> int:
    """For every pulled row: verified on all platforms and small enough -> publish + write URL;
    verified but too large -> prepend TOO_LARGE_MARK to the remark; otherwise leave untouched."""
    work, tasks_dir = config.work_dir, config.tasks_dir
    records, results = load_records(work), load_results(work)
    state_path = work / "publish-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    summary = {"uploaded": [], "too_large": [], "not_verified": [], "already_done": []}
    now = lambda: dt.datetime.now().isoformat(timespec="seconds")  # noqa: E731
    for rec in records:
        tid, key = rec.get("task_id"), rec["record_id"]
        if not key:
            summary["already_done"].append((rec["seq"], f"{tid} (not in the source; use push)"))
            continue
        if not tid:
            summary["not_verified"].append((rec["seq"], "no task_id"))
            continue
        live = source.get(key)
        if live is None:
            summary["already_done"].append((rec["seq"], f"{tid} (row no longer in the source)"))
            continue
        if live.output_url.strip() or live.is_skipped:
            summary["already_done"].append((rec["seq"], tid))
            continue
        if (why := lint_text(tasks_dir, tid)):
            summary["not_verified"].append((rec["seq"], f"{tid} unpinned: {why}"))
            continue
        per = {p: results.get(tid, {}).get(p, {}) for p in platforms}
        ok = verified(results, tid, platforms)
        sizes = [per[p].get("size_gb") or 0 for p in platforms]
        if ok and max(sizes) > max_image_gb:
            if not dry_run:
                source.prepend_remark(key, TOO_LARGE_MARK)
            state[tid] = {"status": "too_large", "sizes_gb": sizes, "at": now()}
            summary["too_large"].append((rec["seq"], tid, sizes))
            continue
        if not ok:
            why = ", ".join(f"{p}: {per[p].get('status', 'not built')}" for p in platforms)
            summary["not_verified"].append((rec["seq"], f"{tid} {{{why}}}"))
            continue
        if dry_run:
            summary["uploaded"].append((rec["seq"], tid, f"(dry-run) {publisher.repo_url(tid)}"))
            continue
        try:
            url = publisher.publish(tid, tasks_dir / tid, rec["title"])
            source.set_output_url(key, url)
        except Exception as e:
            summary["not_verified"].append((rec["seq"], f"{tid} publish error: {e}"))
            continue
        state[tid] = {"status": "uploaded", "url": url, "sizes_gb": sizes, "record_id": key, "at": now()}
        summary["uploaded"].append((rec["seq"], tid, url))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, items in summary.items():
        print(f"{k} ({len(items)}):")
        for it in items:
            print("   ", *it)
    return 0


def run_push(config: Config, publisher: GitHubPublisher, platforms: list[str], task_ids: list[str] | None,
             no_verify: bool, dry_run: bool) -> int:
    """Re-publish task dirs to their existing repos (after a recipe fix); no ledger writes."""
    tasks_dir = config.tasks_dir
    results = load_results(config.work_dir)
    ids = task_ids or sorted(p.name for p in tasks_dir.iterdir() if (p / "task.toml").exists())
    done, skipped = [], []
    for t in ids:
        if (why := lint_text(tasks_dir, t)):
            skipped.append((t, f"unpinned: {why}"))
        elif not no_verify and not verified(results, t, platforms):
            skipped.append((t, "no ok build result on every platform (use --no-verify to override)"))
        elif not publisher.exists(t):
            skipped.append((t, "no GitHub repo yet (use publish)"))
        elif dry_run:
            done.append((t, "(dry-run)"))
        else:
            try:
                done.append((t, publisher.publish(t, tasks_dir / t, task_title(tasks_dir, t))))
            except Exception as e:
                skipped.append((t, f"push error: {e}"))
    print(f"pushed ({len(done)}):")
    for t, u in done:
        print("   ", t, u)
    print(f"skipped ({len(skipped)}):")
    for t, why in skipped:
        print("   ", t, why)
    return 0 if not skipped else 1
