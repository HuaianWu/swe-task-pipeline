"""Offline checks for tools/rubric_yaml.py: every cell shape seen in the ledger must parse."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rubric_yaml as R  # noqa: E402

CASES = {
    "numbered": ("1. 甲\n2. 乙\n3. 丙", [(None, "甲"), (None, "乙"), (None, "丙")]),
    "cn-number": ("一、甲\n二、乙", [(None, "甲"), (None, "乙")]),
    "R-type-colon": ("R1 f2p：甲\nR2 p2p：乙", [("f2p", "甲"), ("p2p", "乙")]),
    "num-type-colon-oneline": ("1. f2p：甲；2. p2p：乙；3. f2p：丙", [("f2p", "甲"), ("p2p", "乙"), ("f2p", "丙")]),
    "num-bracket": ("1. [f2p] 甲\n2. [p2p] 乙", [("f2p", "甲"), ("p2p", "乙")]),
    "bracket-num": ("[f2p] 1 甲\n[p2p] 2 乙", [("f2p", "甲"), ("p2p", "乙")]),
    "bracket-num-colon": ("[f2p] 1：甲\n[p2p] 2：乙", [("f2p", "甲"), ("p2p", "乙")]),
    "R-paren": ("R1（f2p）：甲；R2（p2p）：乙", [("f2p", "甲"), ("p2p", "乙")]),
    "num-type-space": ("1 f2p 甲\n2 p2p 乙", [("f2p", "甲"), ("p2p", "乙")]),
    "flat-yaml": ("rubrics:\n\nid: 1 type: f2p text: 甲\nid: 2 type: p2p text: 乙", [("f2p", "甲"), ("p2p", "乙")]),
    "flat-yaml-dash": ("rubrics: - id: 1 type: f2p text: 甲 - id: 2 type: p2p text: 乙", [("f2p", "甲"), ("p2p", "乙")]),
    "prose-numbers-inside": ("1. 含 3 条消息的批次\n2. 乙", [(None, "含 3 条消息的批次"), (None, "乙")]),
}


def main() -> int:
    bad = 0
    for name, (raw, expect) in CASES.items():
        items, _ = R.parse(raw)
        got = [(it.type, it.text) for it in items]
        if got != expect:
            bad += 1
            print(f"FAIL {name}: {got} != {expect}")
    free, warn = R.parse("完成，已通过 6 条 rubric；go test 通过。")
    if free or not warn:
        bad += 1; print("FAIL free text should not parse")
    y = R.to_yaml(R.parse("R1 f2p：甲：乙 \"引号\"\nR2 p2p：丙")[0])
    if 'text: "甲：乙 \\"引号\\""' not in y or "type: p2p" not in y or not y.startswith("rubrics:\n  - id: 1\n"):
        bad += 1; print("FAIL yaml rendering:\n" + y)
    try:
        import yaml
        doc = yaml.safe_load(y)
        if doc != {"rubrics": [{"id": 1, "type": "f2p", "text": '甲：乙 "引号"'}, {"id": 2, "type": "p2p", "text": "丙"}]}:
            bad += 1; print("FAIL yaml round-trip:", doc)
    except ImportError:
        pass
    lines, extras, warn = R.normalize_run_result("总结一句。\n1. 通过：甲\n2、不通过。\n3.部分通过：乙\n4 未通过 丙\n5. 已测通过：丁\n收尾一句")
    exp = ["1 通过 甲", "2 未通过", "3 未通过 部分通过：乙", "4 未通过 丙", "5 通过 丁"]
    r_lines, _, _ = R.normalize_run_result("R1 通过：甲\nR2 部分通过：乙")
    if r_lines != ["1 通过 甲", "2 未通过 部分通过：乙"]:
        bad += 1; print("FAIL R-prefixed run_result:", r_lines)
    if lines != exp or extras != ["总结一句。", "收尾一句"] or warn:
        bad += 1; print("FAIL run_result:", lines, extras, warn)
    print("rubric_yaml self-test:", "ok" if not bad else f"{bad} failures")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
