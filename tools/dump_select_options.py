#!/usr/bin/env python3
"""dump_select_options — write <work>/select-options.json from the Feishu table's single-select columns.

tools/package_check.py validates task.toml values such as 主要语言 / 任务类型 / Harness / 是否完成需求
against the option lists the ledger actually offers.  Dump them once per table:

    python3 tools/dump_select_options.py --work feishu-sync/work-zip
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--work", default=str(ROOT / "feishu-sync" / "work"))
    args = ap.parse_args(argv)
    from swepipe.config import Config
    from swepipe.sources.feishu import API, FeishuSource

    src = FeishuSource(Config.load(Path(args.root)))
    d = src._call(f"{API}/bitable/v1/apps/{src.base}/tables/{src.table}/fields?page_size=200")["data"]
    options = {}
    for f in d.get("items", []):
        opts = (f.get("property") or {}).get("options")
        if f.get("type") in (3, 4) and opts:   # 3 = single select, 4 = multi select
            options[f["field_name"]] = [o["name"] for o in opts]
    out = Path(args.work) / "select-options.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(options, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}: {', '.join(f'{k}({len(v)})' for k, v in options.items())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
