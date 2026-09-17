"""gen: run the generator (tools/xlsx2task.py) for the pulled rows.

The generator clones each repo (blobless) into REPOS_DIR, checks the base commit, detects the
build layout, resolves pinned dependencies and writes tasks/<task_id>/{task.toml,instruction.md,
environment/Dockerfile}.  It refuses (all-or-nothing) when any row is blocked, including any
Dockerfile that violates the pinning rule (tools/pin_lint.py).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .config import Config
from .ledger import load_records, save_records

TOOLS = Path(__file__).resolve().parents[1]


def run_gen(config: Config, task_ids: list[str] | None, force: bool, preflight: bool) -> int:
    work = config.work_dir
    records = load_records(work)
    if task_ids:
        wanted = set(task_ids)
        chosen = [r for r in records if r.get("task_id") in wanted]
        missing = wanted - {r.get("task_id") for r in chosen}
        if missing:
            sys.exit(f"not in records.json (run `pull`, maybe with --all-local): {sorted(missing)}")
    else:
        chosen = [r for r in records if not r.get("existing")]
    if not chosen:
        print("nothing to generate (every pulled row reuses an existing task dir)")
        return 0
    cmd = [sys.executable, str(TOOLS / "xlsx2task.py"), "gen", str(work / "ledger.xlsx"),
           "--rows", ",".join(str(r["ledger_row"]) for r in chosen),
           "--overrides", str(config.overrides_path), "--out", str(config.tasks_dir),
           "--resolve", "--repos-dir", str(config.repos_dir), "--report", str(work / "gen-report.json")]
    if force:
        cmd.append("--force")
    if preflight:
        cmd.append("--preflight")
    print("+", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc == 0:
        rep = {r["source_id"]: r for r in json.loads((work / "gen-report.json").read_text(encoding="utf-8"))}
        for rec in records:
            if rec["source_id"] in rep:
                rec["task_id"] = rep[rec["source_id"]]["task_id"]
        save_records(work, records)
    return rc
