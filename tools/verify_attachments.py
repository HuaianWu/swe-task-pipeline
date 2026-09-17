#!/usr/bin/env python3
"""verify_attachments — download the delivery zip attached to each row and run the intake check.

After `swepipe.py deliver`, this is the independent proof that what is on the ledger row is a
valid package: it re-fetches the attachment through the source adapter (not the local copy),
unzips it and runs tools/package_check.py on it.

    python3 tools/verify_attachments.py --work feishu-sync/work-zip <task_id>[,<task_id>...]
    python3 tools/verify_attachments.py --work feishu-sync/work-zip --all      # every delivered row

Downloads land in <out>/dl/, unzipped packages in <out>/unz/ (default <work>/verify/).
Exit status 1 when any row has no attachment or fails the check.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task_ids", nargs="?", help="comma list of task ids (from <work>/records.json)")
    ap.add_argument("--all", action="store_true", help="every task id in <work>/deliver-state.json")
    ap.add_argument("--root", default=str(ROOT), help="directory holding .env / pipeline.toml")
    ap.add_argument("--work", default=str(ROOT / "feishu-sync" / "work"))
    ap.add_argument("--out", help="download / unzip directory (default <work>/verify)")
    args = ap.parse_args(argv)

    from package_check import check_package, load_options
    from swepipe.config import Config
    from swepipe.sources import get_source

    work = Path(args.work)
    out = Path(args.out) if args.out else work / "verify"
    config = Config.load(Path(args.root), {"WORK_DIR": str(work)})
    src = get_source(config)
    recs = {r["task_id"]: r for r in json.loads((work / "records.json").read_text(encoding="utf-8")) if r.get("task_id")}
    if args.all:
        state = json.loads((work / "deliver-state.json").read_text(encoding="utf-8")) if (work / "deliver-state.json").exists() else {}
        ids = [t for t in state if t in recs]
    else:
        ids = args.task_ids.split(",") if args.task_ids else []
    if not ids:
        ap.error("give task ids or --all")
    opts_file = work / "select-options.json"
    options = load_options(opts_file if opts_file.exists() else None)

    bad = 0
    for tid in ids:
        rec = recs.get(tid)
        if not rec:
            print(tid, "NOT IN records.json"); bad += 1; continue
        live = src.get(rec["key"])
        atts = (live.attachments.get("package") if live else None) or []
        if not atts:
            print(rec.get("seq"), tid, "NO ATTACHMENT"); bad += 1; continue
        for a in atts:
            name = a.get("name") or Path(str(a.get("path") or a.get("url") or "package.zip")).name
            dest = out / "dl" / f"{rec.get('seq')}-{name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.fetch_file(a, dest)
            unz = out / "unz" / str(rec.get("seq"))
            shutil.rmtree(unz, ignore_errors=True)
            zipfile.ZipFile(dest).extractall(unz)
            pkgs = [p for p in unz.iterdir() if p.is_dir() and p.name != "__MACOSX"]
            fails = check_package(pkgs[0], options) if len(pkgs) == 1 else [f"zip has {len(pkgs)} top-level directories"]
            print(rec.get("seq"), tid, name, f"{dest.stat().st_size / 1e6:.1f}MB", "OK" if not fails else "FAIL")
            for f in fails:
                print("     -", f)
            bad += bool(fails)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
