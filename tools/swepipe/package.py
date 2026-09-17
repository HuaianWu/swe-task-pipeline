"""deliver (DELIVERY=zip): turn a verified task into the client's delivery package and attach it
to the ledger row.  Spec: docs/delivery-package.md (SWE-like Repo 交付包规范).

Package layout, top directory = 题目名称 (path-unsafe characters replaced by full-width ones):
  task.toml                 the 16 spec keys, values copied verbatim from the row, except run_result
                            (normalised to '<id> 通过|未通过 <reason>' lines; summary sentences dropped)
  instruction.md            需求 Prompt（原文）
  environment/Dockerfile    from tasks/<task_id>/environment/ (generated + verified by build)
  tests/nl_rubric.yaml      Verify Rubric column rewritten into the spec's YAML (rubrics: - id/type/text)
                            by tools/rubric_yaml.py; rows whose items carry no f2p/p2p type are
                            held back (`rubric_untyped`) unless --allow-untyped; likewise rows whose 产物结果 has no
                            per-rubric items (`result_unstructured`)
  solution/                 empty (allowed for this batch)
  evidence/model.patch      the row's .patch文件 attachment (extra attachments keep their names)
  evidence/trajectory.<ext> 轨迹文件链接 (md = Trae IDE export, jsonl = TraeX, json = miniswe)
  evidence/screenshots/     证明图片链接

The zip is <work>/deliver/<题目名称>.zip.  Downloads are cached in <work>/downloads/<row key>/.
No content validation is done here beyond the rubric shape (patch base, inclusion rule ...): the
pipeline organises what the table holds; the build/smoke gate is the only quality check applied.
`--redeliver` rebuilds and re-attaches packages that were delivered before (rows come from
<work>/deliver-state.json), e.g. after a format change like the rubric YAML.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from .build import load_results, verified
from .config import Config
from .ledger import load_records
from .model import TOO_LARGE_MARK, TaskRecord
from .sources import LedgerSource, download

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import pin_lint  # noqa: E402
import rubric_yaml  # noqa: E402
import package_check

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

# (task.toml key, TaskRecord field, multi-line?) in the order the spec lists them
TASK_TOML_KEYS = [
    ("title", "title", False), ("submitter", "submitter", False), ("submit_date", "submitted_at", False),
    ("language", "language", False), ("task_type", "task_type", False), ("repo_url", "repo_url", False),
    ("base_commit", "base_sha40", False), ("realism_and_difficulty", "difficulty", True),
    ("modules", "modules", True), ("trae_session_id", "trae_session_id", False),
    ("effective_turns", "effective_turns", False), ("harness", "harness", False),
    ("seed_model", "seed_model", False), ("requirement_met", "done", False),
    ("run_result", "result", True), ("notes", "notes", True),
]
UNSAFE = str.maketrans({"/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜"})


def safe_dirname(title: str) -> str:
    name = re.sub(r"[\x00-\x1f]", "", title.strip()).translate(UNSAFE).rstrip(". ")
    return name or "untitled"


def toml_string(value: str, multiline: bool) -> str:
    value = "" if value is None else str(value)
    esc = value.replace("\\", "\\\\")
    if multiline and "\n" in value:
        esc = esc.replace('"""', '\\"\\"\\"')
        return f'"""\n{esc}\n"""'
    esc = esc.replace('"', '\\"').replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    return f'"{esc}"'


def render_task_toml(rec: TaskRecord) -> str:
    """The 16 spec keys, values verbatim except: effective_turns becomes an integer when it parses
    as one; run_result is normalised to the spec's "<id> 通过|未通过 <reason>" lines (see
    rubric_yaml.normalize_run_result); summary sentences in that cell are not packaged."""
    # Summary sentences in the 产物结果 cell are dropped: they are not part of the spec and the
    # client's QC read one ("有效轮数 86…") as contradicting effective_turns.  They stay in the ledger.
    run_lines, _extras, _ = rubric_yaml.normalize_run_result(rec.result)
    lines = []
    for key, attr, multi in TASK_TOML_KEYS:
        value = getattr(rec, attr)
        if key == "run_result":
            value = "\n".join(run_lines) if run_lines else value
        if key == "effective_turns":
            # the ledger's 有效轮数 is authoritative; when submitters left it empty the estimate column is
            # used instead (user decision 2026-09-12) so the package is not blocked on a blank cell
            if not str(value or "").strip():
                value = getattr(rec, "effective_turns_estimate", "")
            m = re.fullmatch(r"\s*(\d+)(?:\.0+)?\s*", value or "")
            lines.append(f"{key} = {int(m.group(1)) if m else toml_string(value, False)}")
            continue
        lines.append(f"{key} = {toml_string(value, multi)}")
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # must round-trip
    return text


def url_filename(url: str, default: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    return re.sub(r"[\x00-\x1f/\\]", "", name) or default


def trajectory_name(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".") or "md"
    return f"trajectory.{ext}"


def split_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s,;，；]+", text or "")


def cached(cache: Path, name: str, fetch) -> Path:
    dest = cache / name
    if not dest.exists() or dest.stat().st_size == 0:
        tmp = dest.with_suffix(dest.suffix + ".part")
        fetch(tmp)
        tmp.replace(dest)
    return dest


def build_package(rec: TaskRecord, task_dir: Path, source: LedgerSource, work: Path) -> tuple[Path, list[str]]:
    """Assemble <work>/deliver/<题目名称>/ and zip it.  Returns (zip path, notes)."""
    notes = []
    cache = work / "downloads" / (rec.key or rec.source_id)
    cache.mkdir(parents=True, exist_ok=True)
    name = safe_dirname(rec.title)
    stage = work / "deliver" / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    (stage / "task.toml").write_text(render_task_toml(rec), encoding="utf-8")
    (stage / "instruction.md").write_text(rec.prompt.rstrip() + "\n", encoding="utf-8")
    env_src = task_dir / "environment"
    if not (env_src / "Dockerfile").exists():
        raise RuntimeError(f"{task_dir} has no environment/Dockerfile")
    shutil.copytree(env_src, stage / "environment", ignore=shutil.ignore_patterns(".DS_Store"))
    (stage / "tests").mkdir()
    _, _, result_warnings = rubric_yaml.normalize_run_result(rec.result)
    notes.extend("run_result: " + w for w in result_warnings)
    yaml_text, items, rubric_warnings = rubric_yaml.convert(rec.rubric)
    if items:
        (stage / "tests" / "nl_rubric.yaml").write_text(yaml_text, encoding="utf-8")
        notes.extend("rubric: " + w for w in rubric_warnings)
    else:  # unparseable cell: keep the text so the package is not silently empty
        (stage / "tests" / "nl_rubric.yaml").write_text(rec.rubric.rstrip() + "\n", encoding="utf-8")
        notes.append("rubric: cell not parseable, written verbatim")
    (stage / "solution").mkdir()
    ev = stage / "evidence"
    (ev / "screenshots").mkdir(parents=True)
    # patch attachment(s)
    patches = rec.attachments.get("patch_file") or []
    if not patches:
        notes.append("no .patch attachment: evidence/model.patch missing")
    for i, att in enumerate(patches):
        fname = att.get("name") or f"patch-{i}.patch"
        f = cached(cache, f"patch-{i}-{fname}", lambda tmp, att=att: source.fetch_file(att, tmp))
        shutil.copyfile(f, ev / ("model.patch" if i == 0 else fname))
    # trajectory
    turls = split_urls(rec.trajectory_url)
    if not turls:
        notes.append("no trajectory link")
    for i, u in enumerate(turls):
        f = cached(cache, f"trajectory-{i}-{url_filename(u, 'trajectory')}", lambda tmp, u=u: download(u, tmp))
        shutil.copyfile(f, ev / (trajectory_name(u) if i == 0 else url_filename(u, f"trajectory-{i}")))
    # screenshots
    surls = split_urls(rec.screenshot_url)
    if not surls:
        notes.append("no screenshot link")
    for i, u in enumerate(surls):
        fname = url_filename(u, f"screenshot-{i}.png")
        f = cached(cache, f"shot-{i}-{fname}", lambda tmp, u=u: download(u, tmp))
        shutil.copyfile(f, ev / "screenshots" / fname)
    # zip
    zip_path = work / "deliver" / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(stage.rglob("*")):
            arc = f"{name}/{p.relative_to(stage).as_posix()}"
            if p.is_dir():
                zf.writestr(zipfile.ZipInfo(arc + "/"), "")
            else:
                zf.write(p, arc)
    return zip_path, notes


def lint_text(tasks_dir: Path, task_id: str) -> str:
    df = tasks_dir / task_id / "environment" / "Dockerfile"
    return "; ".join(pin_lint.violations_as_text(pin_lint.lint(df))) if df.exists() else ""


def rubric_ready(text: str) -> tuple[bool, str]:
    """(ok, reason): every parsed item carries an f2p/p2p type."""
    items, warnings = rubric_yaml.parse(text)
    if not items:
        return False, "rubric cell not parseable"
    if any(it.type is None for it in items):
        return False, f"{sum(1 for it in items if it.type is None)}/{len(items)} rubric items lack f2p/p2p"
    return True, ""


def result_ready(text: str) -> tuple[bool, str]:
    """(ok, reason): the 产物结果 cell has per-rubric "<id> 通过/未通过" items (prose only fails QC)."""
    lines, _, warnings = rubric_yaml.normalize_run_result(text)
    if not lines:
        return False, "产物结果 has no per-rubric result items (prose only)"
    if warnings:
        return False, "; ".join(warnings)
    return True, ""


def run_deliver(config: Config, source: LedgerSource, platforms: list[str], max_image_gb: float,
                task_ids: list[str] | None, dry_run: bool, no_upload: bool, no_verify: bool,
                redeliver: bool = False, allow_untyped: bool = False, allow_invalid: bool = False) -> int:
    """For every pulled row: verified on all platforms and small enough -> package + attach zip;
    verified but too large -> prepend TOO_LARGE_MARK to the remark; otherwise leave untouched."""
    work, tasks_dir = config.work_dir, config.tasks_dir
    records, results = load_records(work), load_results(work)
    state_path = work / "deliver-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    summary = {"delivered": [], "packaged_only": [], "too_large": [], "not_verified": [], "rubric_untyped": [],
               "result_unstructured": [], "package_invalid": [], "already_done": []}
    now = lambda: dt.datetime.now().isoformat(timespec="seconds")  # noqa: E731
    if redeliver:  # previously delivered packages, regardless of what the current pull holds
        records = [{"task_id": tid, "record_id": st.get("record_id", ""), "seq": st.get("seq", "")}
                   for tid, st in state.items() if st.get("status") == "delivered"]
    for rec in records:
        tid, key = rec.get("task_id"), rec["record_id"]
        if task_ids and tid not in task_ids:
            continue
        if not key:
            summary["already_done"].append((rec["seq"], f"{tid} (not in the source)"))
            continue
        if not tid:
            summary["not_verified"].append((rec["seq"], "no task_id"))
            continue
        live = source.get(key)
        if live is None:
            summary["already_done"].append((rec["seq"], f"{tid} (row no longer in the source)"))
            continue
        if (live.output_url.strip() and not redeliver) or live.is_skipped:
            summary["already_done"].append((rec["seq"], f"{tid} ({live.output_url.strip() or 'skipped'})"))
            continue
        if not allow_untyped:
            ready, why = rubric_ready(live.rubric)
            if not ready:
                summary["rubric_untyped"].append((rec["seq"] or live.seq, tid, why))
                continue
            ready, why = result_ready(live.result)
            if not ready:
                summary["result_unstructured"].append((rec["seq"] or live.seq, tid, why))
                continue
        if (why := lint_text(tasks_dir, tid)):
            summary["not_verified"].append((rec["seq"], f"{tid} unpinned: {why}"))
            continue
        per = {p: results.get(tid, {}).get(p, {}) for p in platforms}
        ok = no_verify or verified(results, tid, platforms)
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
            summary["delivered"].append((rec["seq"], tid, f"(dry-run) {safe_dirname(live.title)}.zip"))
            continue
        try:
            zip_path, notes = build_package(live, tasks_dir / tid, source, work)
        except Exception as e:
            summary["not_verified"].append((rec["seq"], f"{tid} package error: {e}"))
            continue
        # the client's intake 体检 (tools/package_check.py): a package that would fail it is not uploaded
        problems = package_check.check_package(zip_path)
        if problems and not allow_invalid:
            summary["package_invalid"].append((rec["seq"] or live.seq, tid, "; ".join(problems)))
            continue
        if no_upload:
            state[tid] = {"status": "packaged", "zip": str(zip_path), "notes": notes, "at": now()}
            summary["packaged_only"].append((rec["seq"], tid, str(zip_path), *notes))
            continue
        try:
            ref = source.attach_output(key, zip_path)
        except Exception as e:
            summary["not_verified"].append((rec["seq"], f"{tid} upload error: {e}"))
            continue
        state[tid] = {"status": "delivered", "zip": str(zip_path), "ref": ref, "record_id": key, "seq": live.seq,
                      "notes": notes, "sizes_gb": sizes, "at": now()}
        summary["delivered"].append((rec["seq"], tid, zip_path.name, *notes))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, items in summary.items():
        print(f"{k} ({len(items)}):")
        for it in items:
            print("   ", *it)
    return 0
