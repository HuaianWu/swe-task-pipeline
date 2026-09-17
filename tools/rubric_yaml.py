"""Turn the free-text "Verify Rubric" ledger cell into the delivery spec's tests/nl_rubric.yaml.

Spec shape (SWE-like Repo 交付包规范 §5):

    rubrics:
      - id: 1
        type: f2p        # or p2p
        text: 一句自然语言判分标准

Submitters write the cell in many shapes: "1. text", "1、text", "一、text", "R1 f2p：text",
"1. [f2p] text", "[f2p] 1 text", "R1（p2p）：text", "id: 1 type: f2p text: ...", a flattened
"rubrics:" YAML block, or everything on one line separated by "；".  `parse()` normalises all of
them into (id, type, text) items; `type` is None when the cell does not say f2p/p2p.

`to_yaml()` renders the spec shape.  Items without a type are rendered with the type decided by
the caller (`default_type`, or a keyword heuristic when `infer=True`); every inferred type is
reported so a human can review it.  Nothing here validates the spec's counting rules.

CLI:  python3 tools/rubric_yaml.py <json with [{title, rubric}]> [--infer] [--write-dir DIR]
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TYPE_RE = re.compile(r"\b([fp]2p)\b", re.I)
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
          "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15}
# Start of a rubric item: optional [type], optional R/id:, a number (arabic or Chinese), a separator.
ITEM_START = re.compile(
    r"(?:(?P<pre>\[?\s*[fp]2p\s*\]?)\s*)?"                      # "[f2p] " before the number
    r"(?:R|r|id:\s*)?(?P<num>\d{1,2}|[一二三四五六七八九十]{1,2})"  # R1 / id: 1 / 1 / 一
    r"(?:\s*[（(]\s*(?P<ptype>[fp]2p)\s*[)）])?"                  # R1（f2p）
    r"\s*(?P<sep>[.、．:：)）]|(?=\s*\[?\s*[fp]2p)|\s)\s*",         # separator, a type token, or a space
    re.I)
# "id: 1 type: f2p text: ..." (a YAML block flattened by the ledger)
YAML_LINE = re.compile(r"id:\s*(?P<num>\d+)\s*type:\s*(?P<type>[fp]2p)\s*text:\s*(?P<text>.*)", re.I | re.S)
HEAD_TYPE = re.compile(r"^\s*\[?\s*([fp]2p)\s*\]?\s*[:：\-–]?\s*", re.I)
P2P_HINT = re.compile(r"回归|既有|现有|原有|不变|保持|兼容|不破坏|不受影响|仍然|继续|不回归|默认关闭|未启用|未配置|不改变")


@dataclass
class Item:
    id: int
    type: str | None
    text: str
    inferred: bool = False


def _clean(text: str) -> str:
    text = text.strip().strip("；;").strip()
    text = text.strip("-").strip()  # YAML list dash flattened into the cell
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":  # a whole quoted YAML scalar
        text = text[1:-1].strip()
    return re.sub(r"\s+", " ", text)


def _split_items(raw: str) -> list[tuple[str | None, str]]:
    """Return (type, text) pairs found in the cell, in order."""
    text = raw.replace("\r", "")
    # flattened YAML block
    if re.search(r"id:\s*\d+\s*type:", text):
        out = []
        for m in re.finditer(r"id:\s*(\d+)\s*type:\s*([fp]2p)\s*text:\s*(.*?)(?=\s*-?\s*id:\s*\d+\s*type:|\Z)", text, re.I | re.S):
            out.append((m.group(2).lower(), _clean(m.group(3))))
        if out:
            return out
    # one item per numbered start; "；" separated one-liners are handled by finditer over the whole text
    starts = [m for m in ITEM_START.finditer(text)
              if (m.start() == 0 or text[m.start() - 1] in "\n；; \t")
              # a bare space may separate number and text only when a [type] precedes the number
              and not (m.group("sep").isspace() and not m.group("pre"))]
    # numbering must be sequential-ish from 1; drop matches that are just numbers inside prose
    seq, expected = [], 1
    for m in starts:
        n = int(m.group("num")) if m.group("num").isdigit() else CN_NUM.get(m.group("num"), 0)
        if n == expected or (n == expected - 1 and False):
            seq.append(m); expected += 1
    if not seq:
        return []
    out = []
    for i, m in enumerate(seq):
        end = seq[i + 1].start() if i + 1 < len(seq) else len(text)
        body = text[m.end():end]
        typ = (m.group("pre") or m.group("ptype") or "")
        typ = TYPE_RE.search(typ).group(1).lower() if TYPE_RE.search(typ) else None
        h = HEAD_TYPE.match(body)
        if h:
            typ = typ or h.group(1).lower()
            body = body[h.end():]
        out.append((typ, _clean(body)))
    return [o for o in out if o[1]]


def parse(raw: str) -> tuple[list[Item], list[str]]:
    """Cell text -> items plus warnings (empty text, numbering gaps, no items)."""
    warnings = []
    pairs = _split_items(raw or "")
    if not pairs:
        return [], ["no numbered rubric items found (free text?)"]
    items = [Item(i + 1, t, x) for i, (t, x) in enumerate(pairs)]
    if len(items) < 5:
        warnings.append(f"only {len(items)} items (spec asks for >= 5)")
    if all(it.type is None for it in items):
        warnings.append("no f2p/p2p types in the cell")
    elif any(it.type is None for it in items):
        warnings.append("some items lack a type")
    return items, warnings


def assign_types(items: list[Item], default_type: str | None = None, infer: bool = False) -> None:
    for it in items:
        if it.type:
            continue
        if infer:
            it.type = "p2p" if P2P_HINT.search(it.text) else "f2p"
            it.inferred = True
        elif default_type:
            it.type, it.inferred = default_type, True


SAFE_PLAIN = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`][^#]*$")


def _scalar(text: str) -> str:
    """Inline YAML scalar: plain when nothing in it can be mis-read, else double-quoted.
    Inline (not `>-`) because the client's package consistency checker reads `text:` values from
    the same line, while their toml2base.py uses PyYAML; both accept this form."""
    if (SAFE_PLAIN.match(text) and ": " not in text and not text.endswith(":") and " #" not in text
            and '"' not in text and "'" not in text and "\\" not in text):
        return text
    return json.dumps(text, ensure_ascii=False)


def to_yaml(items: list[Item]) -> str:
    """Block-style YAML: a single top-level `rubrics` list, each item `id` / `type` / `text` with
    the text inline on the `text:` line.  No flow style (`{...}` / `[...]`)."""
    lines = ["rubrics:"]
    for it in items:
        lines.append(f"  - id: {it.id}")
        if it.type:
            lines.append(f"    type: {it.type}")
        lines.append(f"    text: {_scalar(it.text)}")
    return "\n".join(lines) + "\n"


# ---- run_result (task.toml) ---------------------------------------------------------------
# Spec: one line per rubric, "<rubric id> 通过" or "<rubric id> 未通过 <reason>".  Submitters write
# "1. 通过：…", "1、通过。", "1.部分通过：…", plus summary sentences before/after the list.
RESULT_ITEM = re.compile(r"^\s*[Rr]?(?P<num>\d{1,2})\s*[.、．:：)）]?\s*(?P<body>.*)$")   # "1. …", "R1 通过：…"
RESULT_STATUS = re.compile(r"^(?P<st>已测通过|部分通过|未通过|不通过|通过)\s*[:：。，,、\-–]*\s*(?P<rest>.*)$")


def normalize_run_result(raw: str) -> tuple[list[str], list[str], list[str]]:
    """Cell text -> (spec lines, non-item sentences, warnings).

    Status words are mapped to the spec's two values: 不通过 -> 未通过; 部分通过 -> 未通过 with the
    original wording kept at the head of the reason (a partially met rubric is not passed);
    已测通过 -> 通过.  Lines that are not numbered items (summaries) are returned separately so the
    caller can keep them elsewhere (task.toml `notes`)."""
    lines, extras, warnings = [], [], []
    for line in (raw or "").replace("\r", "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = RESULT_ITEM.match(line)
        if not m:
            extras.append(line)
            continue
        num, body = m.group("num"), m.group("body").strip()
        st = RESULT_STATUS.match(body)
        if not st:
            warnings.append(f"item {num}: no 通过/未通过 status ({body[:30]})")
            lines.append(f"{num} {body}")
            continue
        word, rest = st.group("st"), st.group("rest").strip().rstrip("。 ")
        if word == "通过" or word == "已测通过":
            status = "通过"
        elif word == "部分通过":
            status, rest = "未通过", ("部分通过：" + rest) if rest else "部分通过"
        else:
            status = "未通过"
        lines.append(f"{num} {status}" + (f" {rest}" if rest else ""))
    if not lines:
        warnings.append("no numbered result items")
    return lines, extras, warnings


def convert(raw: str, default_type: str | None = None, infer: bool = False) -> tuple[str, list[Item], list[str]]:
    items, warnings = parse(raw)
    assign_types(items, default_type, infer)
    return to_yaml(items), items, warnings


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("json", help='[{"title":..., "rubric":...}, ...]')
    ap.add_argument("--infer", action="store_true", help="guess f2p/p2p for untyped items by keywords")
    ap.add_argument("--default-type", help="type for untyped items (instead of --infer)")
    ap.add_argument("--write-dir", help="write <title>.yaml files here")
    ap.add_argument("--show", type=int, default=0, help="print the first N conversions")
    a = ap.parse_args(argv)
    rows = json.loads(Path(a.json).read_text(encoding="utf-8"))
    stats = {"rows": len(rows), "unparsed": [], "untyped": [], "partly_typed": [], "short": [], "inferred_items": 0, "items": 0}
    for i, r in enumerate(rows):
        yaml_text, items, warns = convert(r["rubric"], a.default_type, a.infer)
        stats["items"] += len(items)
        stats["inferred_items"] += sum(1 for it in items if it.inferred)
        for w in warns:
            key = ("unparsed" if w.startswith("no numbered") else "untyped" if w.startswith("no f2p") else
                   "partly_typed" if w.startswith("some") else "short")
            stats[key].append(r["title"])
        if a.write_dir and items:
            Path(a.write_dir).mkdir(parents=True, exist_ok=True)
            (Path(a.write_dir) / f"{r['title']}.yaml").write_text(yaml_text, encoding="utf-8")
        if i < a.show:
            print(f"=== {r['title']}  warnings={warns}\n{yaml_text}")
    print(json.dumps({k: (v if isinstance(v, int) else len(v)) for k, v in stats.items()}, ensure_ascii=False))
    for k in ("unparsed", "untyped", "partly_typed", "short"):
        if stats[k]:
            print(f"{k}: " + " | ".join(stats[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
