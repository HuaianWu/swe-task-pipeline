#!/usr/bin/env python3
"""add_override — merge per-task recipe entries into task-overrides.json, keyed by source_id.

task-overrides.json is keyed by the row's stable source_id (see docs/task-overrides.md), but while
working you think in task_ids.  This helper takes a JSON file (or stdin) shaped

    {"<task_id>": {"install_block": "...", "smoke": "...", "_note": "..."}, ...}

looks up each task_id in <work>/records.json (written by `swepipe.py pull`), and merges the entry
into the source_id's override record, stamping task_id and source_title so the row can be
re-identified when the ledger is reordered.

    python3 tools/add_override.py spec.json --work feishu-sync/work-zip
    cat spec.json | python3 tools/add_override.py - --work feishu-sync/work-zip
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="JSON file keyed by task_id, or - for stdin")
    ap.add_argument("--work", default=str(ROOT / "feishu-sync" / "work"), help="work dir holding records.json")
    ap.add_argument("--overrides", default=str(ROOT / "task-overrides.json"))
    args = ap.parse_args(argv)

    spec = json.load(sys.stdin) if args.spec == "-" else json.loads(Path(args.spec).read_text(encoding="utf-8"))
    ov_path = Path(args.overrides)
    ov = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    recs = {r["task_id"]: r for r in json.loads((Path(args.work) / "records.json").read_text(encoding="utf-8")) if r.get("task_id")}

    missing = [t for t in spec if t not in recs]
    if missing:
        sys.exit(f"task ids not in {args.work}/records.json (run `swepipe.py pull` for that table first): {missing}")
    for tid, entry in spec.items():
        rec = recs[tid]
        cur = ov.get(rec["source_id"], {})
        cur.update(entry)
        cur["task_id"] = tid
        cur["source_title"] = rec["title"]
        ov[rec["source_id"]] = cur
        print("set", tid, rec["source_id"], sorted(entry.keys()))
    ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
