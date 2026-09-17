#!/usr/bin/env python3
"""Offline self-test of the pipeline layers with the JSON source (no Feishu, no Docker, no GitHub).

    python3 tools/tests/test_swepipe.py
"""
import json
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import zipfile  # noqa: E402

from swepipe.build import group_by_fingerprint, task_fingerprint  # noqa: E402
from swepipe.config import Config  # noqa: E402
from swepipe.ledger import load_records, write_ledger  # noqa: E402
from swepipe.package import build_package, render_task_toml, safe_dirname  # noqa: E402
from swepipe.select import local_titles, select_records  # noqa: E402
from swepipe.sources import get_source  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_fingerprint_dedupe(root: Path) -> None:
    """Rows sharing a Dockerfile + smoke plan collapse to one build; a different smoke plan splits them."""
    tasks = root / "fp"
    for name, smoke in (("a-1", "x"), ("a-2", "x"), ("b-1", "y")):
        (tasks / name / "environment").mkdir(parents=True)
        (tasks / name / "environment" / "Dockerfile").write_text("FROM scratch\nRUN true\n")
        (tasks / name / "task.toml").write_text('language = "go"\n')
    ov = root / "fp-ov.json"
    ov.write_text(json.dumps({"s1": {"task_id": "a-1", "smoke": "x"}, "s2": {"task_id": "a-2", "smoke": "x"},
                              "s3": {"task_id": "b-1", "smoke": "y"}}))
    plats = ["linux/arm64", "linux/amd64"]
    fps = {n: task_fingerprint(tasks / n, ov, plats, True) for n in ("a-1", "a-2", "b-1")}
    assert fps["a-1"] == fps["a-2"] != fps["b-1"], fps
    groups = group_by_fingerprint([(n, p) for n in ("a-1", "a-2", "b-1") for p in plats], fps)
    assert len(groups) == 4 and groups[(fps["a-1"], plats[0])] == ["a-1", "a-2"], groups


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_fingerprint_dedupe(root)
        tasks = root / "tasks"
        (tasks / "demo-existing").mkdir(parents=True)
        (tasks / "demo-existing" / "task.toml").write_text('original_title = "已有 任务"\nrepository_url = "https://github.com/x/y"\n'
                                                            f'base_commit_hash = "{SHA}"\nlanguage = "go"\ncategory = "feature_request"\n')
        (tasks / "demo-existing" / "instruction.md").write_text("prompt\n")
        rows = [
            {"key": "1", "seq": "1", "title": "已有任务", "repo_url": "https://github.com/x/y", "base_sha": SHA,
             "language": "Go", "task_type": "功能新增", "prompt": "p", "review": "初检通过"},
            {"key": "2", "seq": "2", "title": "新任务", "repo_url": "https://github.com/x/z", "base_sha": SHA,
             "language": "Python", "task_type": "功能新增", "prompt": "p", "review": "初检通过"},
            {"key": "3", "seq": "3", "title": "新任务", "repo_url": "https://github.com/x/z", "base_sha": SHA,
             "language": "Python", "task_type": "功能新增", "prompt": "p", "review": "初检通过"},   # batch dup
            {"key": "4", "seq": "4", "title": "完成", "repo_url": "https://github.com/x/q", "base_sha": SHA,
             "language": "Go", "task_type": "功能新增", "prompt": "p", "review": "初检通过", "output_url": "https://github.com/o/q"},
            {"key": "5", "seq": "5", "title": "跳过", "repo_url": "https://github.com/x/s", "base_sha": SHA,
             "language": "Go", "task_type": "功能新增", "prompt": "p", "review": "初检通过", "remark": "镜像过大，暂不上传。\n"},
            {"key": "6", "seq": "6", "title": "打回", "repo_url": "https://github.com/x/r", "base_sha": SHA,
             "language": "Go", "task_type": "功能新增", "prompt": "p", "review": "初检打回"},
        ]
        (root / "source.json").write_text(json.dumps({"records": rows}, ensure_ascii=False))
        cfg = Config.load(root, {"SOURCE": "json", "JSON_SOURCE_PATH": str(root / "source.json"),
                                 "WORK_DIR": str(root / "work"), "TASKS_DIR": str(tasks), "OVERRIDES": str(root / "ov.json")})
        src = get_source(cfg)
        sel = select_records(src.fetch(), local_titles(tasks))
        assert sel.counts["candidate"] == 4 and sel.counts["skipped_marker"] == 1, sel.counts
        assert sel.counts["dup_local"] == 1 and sel.counts["dup_batch"] == 1 and sel.counts["selected"] == 2, sel.counts
        records, stub = write_ledger(sel, cfg.work_dir, cfg.overrides_path)
        assert [r["task_id"] for r in records] == ["demo-existing", ""], records
        assert len(stub) == 1 and list(stub.values())[0]["source_title"] == "新任务"
        assert load_records(cfg.work_dir) == records
        src.set_output_url("2", "https://github.com/o/z")
        src.prepend_remark("1", "镜像过大，暂不上传。")
        assert src.get("2").output_url.endswith("/z") and src.get("1").is_skipped and src.get("nope") is None
        assert select_records(src.fetch(), {}).counts["selected"] == 0

    # ---- DELIVERY=zip: auto task ids, 16-key task.toml, package layout, attach_output
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.patch").write_text("From abc\n---\n")
        tasks = root / "tasks"
        rows = [{"key": "r1", "seq": "1", "title": "泛型 Join/Preload 条件", "repo_url": "https://github.com/go-gorm/gorm",
                 "base_sha": f"{SHA} (v1.0.0)", "language": "Go", "task_type": "功能新增", "prompt": "需求原文\n第二行",
                 "rubric": "R1 f2p：a\nR2 p2p：b", "review": "初检通过", "submitter": "张三", "submitted_at": "2026-09-04",
                 "difficulty": "多行\n说明", "modules": "a、b", "trae_session_id": "sid", "effective_turns": "137",
                 "harness": "Trae", "seed_model": "Seed-Evolving", "done": "部分完成", "result": "1 通过\n2 未通过 x",
                 "notes": "", "trajectory_url": "", "screenshot_url": "",
                 "attachments": {"patch_file": [{"path": str(root / "a.patch"), "name": "a.patch"}]}}]
        (root / "source.json").write_text(json.dumps({"records": rows}, ensure_ascii=False))
        cfg = Config.load(root, {"SOURCE": "json", "DELIVERY": "zip", "JSON_SOURCE_PATH": str(root / "source.json"),
                                 "WORK_DIR": str(root / "work"), "TASKS_DIR": str(tasks), "OVERRIDES": str(root / "ov.json")})
        src = get_source(cfg)
        sel = select_records(src.fetch(), {})
        records, stub = write_ledger(sel, cfg.work_dir, cfg.overrides_path, auto_metadata=True)
        assert records[0]["task_id"].startswith("gorm-") and not stub, records
        rec = src.get("r1")
        toml_text = render_task_toml(rec)
        assert 'base_commit = "' + SHA + '"' in toml_text and "effective_turns = 137" in toml_text, toml_text
        assert 'requirement_met = "部分完成"' in toml_text and 'run_result = """' in toml_text
        assert safe_dirname(rec.title) == "泛型 Join／Preload 条件"
        tdir = tasks / records[0]["task_id"]
        (tdir / "environment").mkdir(parents=True)
        (tdir / "environment" / "Dockerfile").write_text("FROM x@sha256:0\n")
        zip_path, notes = build_package(rec, tdir, src, cfg.work_dir)
        names = set(zipfile.ZipFile(zip_path).namelist())
        top = safe_dirname(rec.title)
        for want in ("task.toml", "instruction.md", "environment/Dockerfile", "tests/nl_rubric.yaml", "solution/",
                     "evidence/model.patch", "evidence/screenshots/"):
            assert f"{top}/{want}" in names, (want, names)
        assert zip_path.name == f"{top}.zip", zip_path
        assert notes == ["rubric: only 2 items (spec asks for >= 5)", "no trajectory link", "no screenshot link"], notes
        rubric = zipfile.ZipFile(zip_path).read(f"{top}/tests/nl_rubric.yaml").decode()
        assert rubric.startswith("rubrics:\n  - id: 1\n    type: f2p\n    text: a\n") and "type: p2p\n    text: b\n" in rubric, rubric
        src.attach_output("r1", zip_path)
        live = src.get("r1")
        assert live.output_url == zip_path.name and not live.is_candidate
    print("swepipe self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
