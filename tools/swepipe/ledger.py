"""Bridge between TaskRecords and the generator (tools/xlsx2task.py), which reads an .xlsx ledger.

pull writes three files into the work dir:
  ledger.xlsx           the selected rows in the column layout xlsx2task understands
  records.json          one entry per selected row: ledger_row, key, seq, title, source_id, task_id,
                        existing, local_only — the hand-off used by gen / build / publish
  overrides.stub.json   rows whose task-overrides.json entry still lacks task_id / display_title /
                        display_description (English metadata a human or an LLM must author)

With auto_metadata=True (DELIVERY=zip) those three values are derived instead of authored:
task_id = <repo slug>-<6 chars of source_id>, display_* = the Chinese title.  The delivery zip
carries its own 16-key task.toml, so the English Harbor metadata is internal only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .model import LEDGER_COLUMN
from .select import Selection

try:
    import openpyxl
except ImportError:  # pragma: no cover
    raise SystemExit("pip install openpyxl")

LEDGER_COLUMNS = list(LEDGER_COLUMN.values())
AUTO_COLUMNS = ["task_id", "display_title", "display_description"]


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def auto_task_id(repo_url: str, source_id: str) -> str:
    repo = repo_url.removesuffix(".git").rstrip("/").rsplit("/", 1)[-1]
    return f"{slugify(repo) or 'repo'}-{source_id[2:8]}"


def load_overrides(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def find_override(overrides: dict, source_id: str, title: str) -> dict:
    return overrides.get(source_id) or overrides.get(title.strip()) or {}


def write_ledger(selection: Selection, work: Path, overrides_path: Path,
                 auto_metadata: bool = False) -> tuple[list[dict], dict]:
    work.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(overrides_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ledger"
    ws.append(LEDGER_COLUMNS + ["record_id", "序号"] + (AUTO_COLUMNS if auto_metadata else []))
    records, stub = [], {}
    for n, s in enumerate(selection.selected, start=2):
        r = s.record
        ov = find_override(overrides, r.source_id, r.title)
        auto = []
        if auto_metadata:
            tid = s.existing_task_id or ov.get("task_id") or auto_task_id(r.repo_url, r.source_id)
            auto = [tid, ov.get("display_title") or r.title.strip()[:120],
                    ov.get("display_description") or f"SWE-like task: {r.title.strip()}"[:240]]
        ws.append([getattr(r, attr) for attr in LEDGER_COLUMN] + [r.key, r.seq] + auto)
        records.append({"ledger_row": n, "record_id": r.key, "seq": r.seq, "title": r.title.strip(),
                        "source_id": r.source_id, "repo_url": r.repo_url, "base_sha": r.base_sha,
                        "language": r.language,
                        "task_id": s.existing_task_id or ov.get("task_id", "") or (auto[0] if auto else ""),
                        "existing": s.existing, "local_only": s.local_only})
        if s.existing or auto_metadata:
            continue
        if not all(ov.get(k) for k in ("task_id", "display_title", "display_description")):
            stub[r.source_id] = {"source_title": r.title.strip(), "_seq": r.seq, "_repo": r.repo_url,
                                 "_language": r.language, "task_id": ov.get("task_id", ""),
                                 "display_title": ov.get("display_title", ""),
                                 "display_description": ov.get("display_description", "")}
            if "/" in r.language:
                stub[r.source_id]["language"] = ov.get("language", "")
    wb.save(work / "ledger.xlsx")
    (work / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "overrides.stub.json").write_text(json.dumps(stub, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "pull-summary.json").write_text(json.dumps(
        {"at": dt.datetime.now().isoformat(timespec="seconds"), "counts": selection.counts,
         "duplicates": selection.duplicates}, ensure_ascii=False, indent=1), encoding="utf-8")
    return records, stub


def load_records(work: Path) -> list[dict]:
    p = work / "records.json"
    if not p.exists():
        raise SystemExit(f"{p} missing: run `pull` first")
    return json.loads(p.read_text(encoding="utf-8"))


def save_records(work: Path, records: list[dict]) -> None:
    (work / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
