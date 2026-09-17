"""Command line: `python3 tools/swepipe.py <command> [options]`.

Commands, in pipeline order (every one is idempotent and safe to re-run):
  status    counts from the source (total / url filled / candidates / skipped)
  pull      read the source, select rows, write <work>/ledger.xlsx + records.json + overrides.stub.json
  gen       generate tasks/<task_id>/ for the pulled rows (clones repos, resolves pins, lints)
  build     docker build + smoke on every platform; results in <work>/build-results.json
  publish   DELIVERY=repo: verified & small enough -> GitHub repo + URL written back; too large -> remark marker
  deliver   DELIVERY=zip:  verified & small enough -> <题目名称>.zip attached to the row (交付包规范)
  push      re-publish existing repos after a recipe fix (no ledger writes)
  lint      check every tasks/*/environment/Dockerfile against the pinning rule
  config    print the effective configuration (secrets masked)

Global options (before the command): --root DIR, --source feishu|json, --work DIR, --tasks DIR,
--overrides FILE, --owner NAME, --platforms a,b.  Anything not given falls back to environment,
.env, pipeline.toml, then defaults (see config.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ROOT, Config


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="swepipe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"swepipe {__version__}")
    ap.add_argument("--root", default=str(ROOT), help="swe-task-pipeline directory (holds .env, pipeline.toml, tasks/)")
    ap.add_argument("--source", dest="SOURCE", help="ledger source: feishu | json")
    ap.add_argument("--delivery", dest="DELIVERY", help="repo (GitHub repos) | zip (delivery packages attached to rows)")
    ap.add_argument("--json-source", dest="JSON_SOURCE_PATH", help="ledger file for --source json")
    ap.add_argument("--work", dest="WORK_DIR")
    ap.add_argument("--tasks", dest="TASKS_DIR")
    ap.add_argument("--overrides", dest="OVERRIDES")
    ap.add_argument("--repos-dir", dest="REPOS_DIR")
    ap.add_argument("--owner", dest="GITHUB_OWNER", help="GitHub user/org that owns the task repos")
    ap.add_argument("--platforms", dest="PLATFORMS", help="comma list, default linux/arm64,linux/amd64")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("config")
    sub.add_parser("lint")
    pl = sub.add_parser("pull")
    pl.add_argument("--all-local", action="store_true",
                    help="also include already-published rows / local-only dirs so they can be regenerated")
    g = sub.add_parser("gen")
    g.add_argument("--task-ids", help="comma list: regenerate only these (existing dirs need --force)")
    g.add_argument("--force", action="store_true", help="overwrite existing task dirs")
    g.add_argument("--preflight", action="store_true", help="dry-run each install block in mars-base first (docker)")
    b = sub.add_parser("build")
    b.add_argument("--task-ids", help="comma list (default: every pulled record with a task_id)")
    b.add_argument("--jobs", type=int, help="concurrent builds (default BUILD_JOBS / 3)")
    b.add_argument("--timeout", type=int, default=5400, help="seconds per docker build")
    b.add_argument("--rebuild", action="store_true", help="rebuild even when a previous result was ok")
    b.add_argument("--keep-images", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("--max-image-gb", type=float, help="default MAX_IMAGE_GB / 12")
    p.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("deliver")
    d.add_argument("--task-ids", help="comma list (default: every pulled record)")
    d.add_argument("--max-image-gb", type=float, help="default MAX_IMAGE_GB / 12")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--no-upload", action="store_true", help="build the zips under <work>/deliver/ but do not attach them")
    d.add_argument("--no-verify", action="store_true", help="package without an ok build result (not for delivery)")
    d.add_argument("--redeliver", action="store_true", help="rebuild + re-attach packages already delivered (rows from deliver-state.json)")
    d.add_argument("--allow-untyped", action="store_true", help="package rows whose rubric items lack f2p/p2p types or whose 产物结果 is prose only (default: hold them back)")
    d.add_argument("--allow-invalid", action="store_true",
                   help="upload even when the package fails the client's intake checks (tools/package_check.py)")
    u = sub.add_parser("push")
    u.add_argument("--task-ids", help="comma list (default: every task dir)")
    u.add_argument("--no-verify", action="store_true", help="push without an ok build result")
    u.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cli = {k: v for k, v in vars(args).items() if k.isupper()}
    config = Config.load(Path(args.root), cli)
    tools = Path(__file__).resolve().parents[1]
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))

    if args.cmd == "config":
        print(config.describe())
        return 0
    if args.cmd == "lint":
        import pin_lint
        return pin_lint.main(["pin_lint", str(config.tasks_dir)])
    if args.cmd == "gen":
        from .generate import run_gen
        return run_gen(config, args.task_ids.split(",") if args.task_ids else None, args.force, args.preflight)
    if args.cmd == "build":
        from .build import run_build
        from .ledger import load_records
        ids = args.task_ids.split(",") if args.task_ids else [r["task_id"] for r in load_records(config.work_dir) if r.get("task_id")]
        return run_build(config, ids, config.platforms, args.jobs or config.build_jobs, args.timeout, args.rebuild, args.keep_images)
    if args.cmd == "push":
        from .publish import GitHubPublisher, run_push
        pub = GitHubPublisher(config)
        print("publisher:", pub.describe())
        return run_push(config, pub, config.platforms, args.task_ids.split(",") if args.task_ids else None, args.no_verify, args.dry_run)

    from .sources import get_source
    source = get_source(config)
    if args.cmd == "status":
        recs = source.fetch()
        cand = [r for r in recs if r.is_candidate]
        print(f"source={source.describe()}")
        print(f"delivery={config.delivery} records={len(recs)} done={sum(1 for r in recs if r.output_url.strip())} "
              f"candidates={len(cand)} skipped_by_marker={sum(1 for r in cand if r.is_skipped)}")
        return 0
    if args.cmd == "pull":
        from .ledger import write_ledger
        from .select import local_titles, select_records
        selection = select_records(source.fetch(), local_titles(config.tasks_dir), args.all_local, config.tasks_dir)
        records, stub = write_ledger(selection, config.work_dir, config.overrides_path,
                                     auto_metadata=config.delivery == "zip")
        print(f"source: {source.describe()}")
        print("pull:", selection.counts)
        for seq, why, ref in selection.duplicates:
            print(f"  dup  #{seq:<4} {why}: {ref}")
        for rec in records:
            flag = "existing" if rec.get("existing") else "new"
            print(f"  row {rec['ledger_row']:<3} #{rec['seq']:<4} {flag:<8} {rec['language']:<10} "
                  f"{rec['task_id'] or '(no override)':<48} {rec['title'][:40]}")
        print(f"ledger: {config.work_dir / 'ledger.xlsx'}  records: {len(records)}  rows needing overrides: {len(stub)}"
              + (f"  -> fill {config.work_dir / 'overrides.stub.json'} into {config.overrides_path}" if stub else ""))
        return 0
    if args.cmd == "deliver":
        from .package import run_deliver
        if config.delivery != "zip":
            print("deliver needs DELIVERY=zip (pipeline.toml [delivery] mode, or --delivery zip)")
            return 2
        return run_deliver(config, source, config.platforms, args.max_image_gb or config.max_image_gb,
                           args.task_ids.split(",") if args.task_ids else None, args.dry_run, args.no_upload, args.no_verify,
                           redeliver=args.redeliver, allow_untyped=args.allow_untyped)
    if args.cmd == "publish":
        from .publish import GitHubPublisher, run_publish
        if config.delivery != "repo":
            print("publish needs DELIVERY=repo (this configuration delivers zips: use deliver)")
            return 2
        pub = GitHubPublisher(config)
        print("publisher:", pub.describe())
        return run_publish(config, source, pub, config.platforms,
                           args.max_image_gb or config.max_image_gb, args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
