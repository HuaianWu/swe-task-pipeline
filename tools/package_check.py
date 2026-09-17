"""Offline port of the client's delivery-package 体检 (toml2base.py, 2026-09-10 revision).

Checks one package directory or zip the way the client's intake script does before it writes a
ledger row, but without lark-cli: structure (required files, non-empty trajectory, screenshots),
environment/Dockerfile (mars-base digest, pip/apt/go pins), task.toml (exactly the 16 keys, value
rules per key), tests/nl_rubric.yaml (>=5 items, id/type/text, f2p/p2p, at least one f2p) and
run_result (one `<id> 通过|未通过 [reason]` line per rubric, reasons for failures, no duplicates,
cross-checked with requirement_met).  The Bad Pattern bonus checks are not ported: they never fail
a package.

    python3 tools/package_check.py <dir-or-zip> [...]      # prints FAIL lines, exit 1 if any
    from package_check import check_package                # -> list[str] of failures

    python3 tools/package_check.py --options <work>/select-options.json <dir-or-zip> ...

Single-select options (主要语言 / 任务类型 / Harness / 是否完成需求) come from a select-options.json
dumped from the ledger schema (tools/dump_select_options.py, or SELECT_OPTIONS=<file>); without one
the constants below are used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

REQUIRED_FILES = ["task.toml", "instruction.md", "environment/Dockerfile", "tests/nl_rubric.yaml", "evidence/model.patch"]
TRAJECTORY_FILES = ["evidence/trajectory.jsonl", "evidence/trajectory.json", "evidence/trajectory.md"]
TRAE_HARNESSES = ("Trae", "TraeX")
BATCH_LANGUAGES = ["Python", "Go"]
MARS_BASE_IMAGE = "public.ecr.aws/x8v8d7g8/mars-base"
MARS_BASE_DIGEST = "sha256:91db850db926024eed328c4bf519d54986bc10aad75302cbb074f8e9d79b4c46"
MARS_BASE_REF = f"{MARS_BASE_IMAGE}@{MARS_BASE_DIGEST}"
VERDICTS = ("通过", "未通过")
PLACEHOLDER_MARKERS = ["写题面时注意"]
CJK_PLACEHOLDER = re.compile(r"<[^<>\n]{0,60}[一-鿿][^<>\n]{0,60}>")

DEFAULT_OPTIONS = {
    "主要语言": ["Python", "JavaScript/TypeScript", "Rust", "Go", "Java/Kotlin", "C/C++", "C#", "Ruby", "PHP",
             "Swift/Objective-C", "Dart", "Shell", "其他"],
    "任务类型": ["功能新增", "Bug 修复", "测试增强", "重构/性能", "配置/工具链", "其他"],
    "Harness": ["Trae", "TraeX", "miniswe"],
    "是否完成需求": ["完成", "部分完成", "未完成", "无法判断"],
}

# task.toml key -> (ledger column, check kind); the order is the client's report order
TOML_FIELDS = [
    ("title", "题目名称", "title"), ("submitter", "提交人", "submitter"), ("submit_date", "提交日期", "date"),
    ("language", "主要语言", "language"), ("task_type", "任务类型", "select"), ("repo_url", "Repo URL", "url"),
    ("base_commit", "Commit/版本", "sha"), ("realism_and_difficulty", "真实性与难度说明", "text"),
    ("modules", "可能涉及模块", "text"), ("trae_session_id", "Trae Session ID", "session_id"),
    ("effective_turns", "有效轮数", "turns"), ("harness", "Harness", "select"), ("seed_model", "Seed 模型/版本", "text"),
    ("requirement_met", "是否完成需求", "select"), ("run_result", "产物结果", "run_result"), ("notes", "备注", "optional_text"),
]
ALLOWED_KEYS = [k for k, _, _ in TOML_FIELDS]

_PIP_EXACT_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.+\-]*)(\[[^\]]+\])?==[0-9][0-9A-Za-z.\+\-]*$")
_APT_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.~_\-:]*=")
_GO_GET_PIN = re.compile(r"@[^@\s]+$")


def load_options(path: "str | Path | None" = None) -> dict:
    """DEFAULT_OPTIONS updated from a select-options.json: the explicit `path`, else $SELECT_OPTIONS,
    else the newest <root>/feishu-sync/work*/select-options.json."""
    opts = dict(DEFAULT_OPTIONS)
    candidates = [Path(path)] if path else ([Path(os.environ["SELECT_OPTIONS"])] if os.environ.get("SELECT_OPTIONS") else
                                            sorted(ROOT.glob("feishu-sync/work*/select-options.json"), key=lambda q: q.stat().st_mtime, reverse=True))
    for p in candidates:
        if p.exists():
            try:
                opts.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if k in opts and v})
            except Exception:
                pass
            break
    return opts


def placeholder_hits(value) -> list[str]:
    s = str(value)
    hits = [m.group(0) for m in CJK_PLACEHOLDER.finditer(s)] + [mk for mk in PLACEHOLDER_MARKERS if mk in s]
    return list(dict.fromkeys(hits))


# ---- Dockerfile ---------------------------------------------------------------------------
def _dockerfile_code_lines(text: str) -> list[str]:
    raw = []
    for line in text.splitlines():
        out, in_s, i = [], None, 0
        while i < len(line):
            c = line[i]
            if in_s:
                out.append(c)
                if c == in_s and (i == 0 or line[i - 1] != "\\"):
                    in_s = None
            elif c in ("'", '"'):
                in_s = c; out.append(c)
            elif c == "#":
                break
            else:
                out.append(c)
            i += 1
        raw.append("".join(out).rstrip())
    joined, buf = [], ""
    for line in raw:
        if buf:
            line = buf + " " + line.lstrip(); buf = ""
        if line.endswith("\\"):
            buf = line[:-1].rstrip(); continue
        if line.strip():
            joined.append(line.strip())
    if buf.strip():
        joined.append(buf.strip())
    return joined


def _shell_tokens(s: str) -> list[str]:
    return re.findall(r"""[^\s"']+|"(?:\\.|[^"])*"|'(?:\\.|[^'])*'""", s)


def _unquote(tok: str) -> str:
    return tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"') else tok


def _check_pip_install(cmd: str, errors: list[str]) -> None:
    m = re.search(r"(?:(?:python\d*(?:\.\d+)?\s+-m\s+)|(?:uv\s+))?pip(?:3)?\s+install\b(.*)$", cmd, re.I)
    if not m:
        return
    tokens, i, loose = _shell_tokens(m.group(1).strip()), 0, []
    while i < len(tokens):
        tok = tokens[i]; low = tok.lower()
        if low in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable"):
            i += 2; continue
        if tok.startswith("-"):
            i += 2 if ("=" not in tok and low in ("--python", "--prefix", "--root", "--src", "--target", "-t", "--upgrade-strategy",
                                                  "--index-url", "-i", "--extra-index-url", "--find-links", "-f")) else 1
            continue
        spec = _unquote(tok)
        if spec in (".", "./") or re.match(r"^\.\[", spec) or spec.startswith(("./", "/")) and "==" not in spec:
            i += 1; continue
        if _PIP_EXACT_PIN.match(spec):
            i += 1; continue
        loose.append(spec); i += 1
    if loose:
        errors.append("environment/Dockerfile 里 pip 依赖未钉死具体版本：%s" % "、".join(loose))


def _check_apt_install(cmd: str, errors: list[str]) -> None:
    m = re.search(r"apt-get\s+install\b(.*)$", cmd, re.I)
    if not m:
        return
    loose = []
    for tok in _shell_tokens(m.group(1).strip()):
        tok = _unquote(tok)
        if tok.startswith("-") or _APT_PIN.match(tok):
            continue
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9+.~_\-]*$", tok):
            loose.append(tok)
    if loose:
        errors.append("environment/Dockerfile 里 apt 包未钉版本：%s" % "、".join(loose))


def _check_go_get(cmd: str, errors: list[str]) -> None:
    m = re.search(r"\bgo\s+get\b(.*)$", cmd)
    if not m:
        return
    loose = [_unquote(t) for t in _shell_tokens(m.group(1).strip()) if not _unquote(t).startswith("-") and not _GO_GET_PIN.search(_unquote(t))]
    if loose:
        errors.append("environment/Dockerfile 里 go get 未钉版本：%s" % "、".join(loose))


def check_dockerfile(text: str) -> list[str]:
    errors, lines, froms = [], _dockerfile_code_lines(text), []
    for line in lines:
        if re.match(r"^FROM\b", line, re.I):
            for p in _shell_tokens(re.sub(r"^FROM\b", "", line, flags=re.I).strip()):
                if p.startswith("--"):
                    continue
                if p.upper() == "AS":
                    break
                froms.append(_unquote(p)); break
    if not froms:
        errors.append("environment/Dockerfile 缺少 FROM 行，基线镜像应为 %s" % MARS_BASE_REF)
    else:
        if not froms[0].startswith(MARS_BASE_IMAGE):
            errors.append("environment/Dockerfile 首条 FROM 必须是 mars-base（钉 digest），收到 %s" % froms[0])
        for img in froms:
            if img.startswith(MARS_BASE_IMAGE) and img != MARS_BASE_REF:
                errors.append("environment/Dockerfile 的 FROM 必须钉 digest %s，收到 %s" % (MARS_BASE_DIGEST, img))
    for line in lines:
        if not re.match(r"^RUN\b", line, re.I):
            continue
        for piece in re.split(r"&&", re.sub(r"^RUN\b", "", line, flags=re.I).strip()):
            piece = piece.strip()
            if re.search(r"(?:(?:python\d*(?:\.\d+)?\s+-m\s+)|(?:uv\s+))?pip(?:3)?\s+install\b", piece, re.I):
                _check_pip_install(piece, errors)
            if re.search(r"apt-get\s+install\b", piece, re.I):
                _check_apt_install(piece, errors)
            if re.search(r"\bgo\s+get\b", piece):
                _check_go_get(piece, errors)
    return list(dict.fromkeys(errors))


# ---- structure ----------------------------------------------------------------------------
def check_structure(pkg: Path) -> list[str]:
    errors = []
    for rel in REQUIRED_FILES:
        p = pkg / rel
        if not p.is_file():
            errors.append("交付包缺少必需文件：%s" % rel)
        elif p.stat().st_size == 0:
            errors.append("%s 是空文件" % rel)
    if not any((pkg / rel).is_file() and (pkg / rel).stat().st_size > 0 for rel in TRAJECTORY_FILES):
        errors.append("交付包缺少运行轨迹：%s 有其一即可，且不能是空文件" % " 或 ".join(TRAJECTORY_FILES))
    df = pkg / "environment" / "Dockerfile"
    if df.is_file() and df.stat().st_size > 0:
        errors.extend(check_dockerfile(df.read_text(encoding="utf-8")))
    shots = pkg / "evidence" / "screenshots"
    if not shots.is_dir():
        errors.append("交付包缺少 evidence/screenshots/ 目录，至少要放一张截图")
    elif not [f for f in shots.iterdir() if not f.name.startswith(".") and f.is_file()]:
        errors.append("evidence/screenshots/ 是空的，至少要放一张运行截图")
    return errors


# ---- rubric + run_result ------------------------------------------------------------------
def parse_rubric(path: Path) -> tuple[list[str], list[str]]:
    """-> (rubric ids in order, errors)."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [], ["tests/nl_rubric.yaml 不是合法 YAML：%s" % e]
    if not isinstance(doc, dict) or not isinstance(doc.get("rubrics"), list):
        return [], ["tests/nl_rubric.yaml 顶层必须是 rubrics 列表"]
    items, errors, seen, types, ids = doc["rubrics"], [], set(), [], []
    if len(items) < 5:
        errors.append("tests/nl_rubric.yaml 至少 5 条 rubric，当前只有 %d 条" % len(items))
    for i, it in enumerate(items, 1):
        pos = "tests/nl_rubric.yaml 第 %d 条" % i
        if not isinstance(it, dict):
            errors.append("%s 不是一个 id/type/text 对象" % pos); continue
        missing = [k for k in ("id", "type", "text") if it.get(k) in (None, "")]
        if missing:
            errors.append("%s 缺少字段：%s" % (pos, "、".join(missing))); continue
        rid, rtype, text = it["id"], str(it["type"]).strip().lower(), str(it["text"]).strip()
        if rtype not in ("f2p", "p2p"):
            errors.append('%s的 type 收到 "%s"，合法值为 f2p / p2p' % (pos, it["type"])); continue
        if rid in seen:
            errors.append("%s的 id「%s」与前面重复" % (pos, rid)); continue
        if placeholder_hits(text):
            errors.append("%s的 text 还是模板占位内容" % pos); continue
        seen.add(rid); types.append(rtype); ids.append(rid)
    if ids and "f2p" not in types:
        errors.append("tests/nl_rubric.yaml 至少要有 1 条 f2p，当前全是 p2p")
    if errors:
        return [], errors
    ordered = sorted(ids, key=lambda r: (0, int(r), "") if str(r).isdigit() else (1, 0, str(r)))
    return [str(r) for r in ordered], []


def parse_run_result(raw: str, rubric_ids: list[str], requirement_met: str) -> list[str]:
    if not rubric_ids:
        return ["tests/nl_rubric.yaml 没解析出 rubric，无法核对产物结果"]
    lines_in = [l.strip() for l in str(raw).splitlines() if l.strip()]
    if lines_in and not any(l.split(None, 1)[0].rstrip("：:.、") in rubric_ids for l in lines_in):
        return ["产物结果要逐条给出 rubric 结论，不能写成整段说明（每行 `<rubric id> <通过|未通过> [说明]`）"]
    seen, handled, errors = {}, set(), []
    for lineno, line in enumerate(lines_in, 1):
        parts = line.split(None, 2)
        rid = parts[0].rstrip("：:.、")
        verdict = parts[1].strip() if len(parts) > 1 else ""
        reason = parts[2].strip() if len(parts) > 2 else ""
        pos = "产物结果第 %d 行" % lineno
        if rid not in rubric_ids:
            errors.append('%s的 rubric id「%s」在 tests/nl_rubric.yaml 里不存在' % (pos, rid)); continue
        if rid in handled:
            errors.append("%s的 rubric id「%s」重复了" % (pos, rid)); continue
        handled.add(rid)
        if verdict not in VERDICTS:
            errors.append('%s收到「%s」，每行格式为 `<rubric id> <通过|未通过> [说明]`' % (pos, line[:40])); continue
        if verdict == "未通过" and not reason:
            errors.append("%s判为未通过，必须在同一行补上失败原因" % pos); continue
        seen[rid] = (verdict, reason)
    missing = [rid for rid in rubric_ids if rid not in handled]
    if missing:
        errors.append("产物结果漏了 rubric %s 的结论" % "、".join(missing))
    if errors:
        return errors
    failed = [rid for rid in rubric_ids if seen[rid][0] == "未通过"]
    if requirement_met == "完成" and failed:
        errors.append("产物结果里 rubric %s 未通过，但 requirement_met 填的是「完成」，两者矛盾" % "、".join(failed))
    elif requirement_met and requirement_met != "完成" and not failed:
        errors.append('requirement_met 填的是「%s」，但产物结果里 %d 条 rubric 全部通过，两者矛盾' % (requirement_met, len(rubric_ids)))
    return errors


# ---- task.toml ----------------------------------------------------------------------------
def check_toml(pkg: Path, pkg_name: str, options: dict) -> list[str]:
    errors = []
    path = pkg / "task.toml"
    if not path.is_file():
        return ["交付包里没有 task.toml"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return ["task.toml 不是合法 TOML：%s" % e]
    lowered = {k.lower(): k for k in ALLOWED_KEYS}
    for key in data:
        if key not in ALLOWED_KEYS:
            errors.append("task.toml 的键 %s 大小写不对，应写作 %s" % (key, lowered[key.lower()]) if key.lower() in lowered
                          else "task.toml 出现规范外的键：%s" % key)
    rub = pkg / "tests" / "nl_rubric.yaml"
    rubric_ids, rub_errors = parse_rubric(rub) if rub.is_file() else ([], ["交付包缺少 tests/nl_rubric.yaml"])
    errors.extend(rub_errors)
    harness = str(data.get("harness") or "").strip()
    for key, column, kind in TOML_FIELDS:
        raw = data.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if kind == "optional_text" or (kind == "session_id" and harness and harness not in TRAE_HARNESSES):
                continue
            errors.append("task.toml 缺少 %s 或值为空（底稿列「%s」）" % (key, column)); continue
        value = raw.strip() if isinstance(raw, str) else raw
        if kind == "run_result":
            errors.extend(parse_run_result(value, rubric_ids, str(data.get("requirement_met") or "").strip())); continue
        if isinstance(value, str) and placeholder_hits(value):
            errors.append("%s 还是模板里的占位内容" % key); continue
        if kind == "title":
            if value != pkg_name:
                errors.append('title「%s」与交付包目录名「%s」不一致' % (value, pkg_name))
        elif kind in ("select", "language"):
            opts = options.get(column, [])
            if value not in opts:
                errors.append('%s 收到「%s」，合法值为 %s' % (key, value, " / ".join(opts)))
            elif kind == "language" and value not in BATCH_LANGUAGES:
                errors.append('%s 收到「%s」，本批次只收 %s' % (key, value, " / ".join(BATCH_LANGUAGES)))
        elif kind == "date":
            s = str(value).strip().replace("/", "-").replace("T", " ")
            if not (re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s) or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?", s)):
                errors.append('submit_date 收到「%s」，格式应为 YYYY-MM-DD' % value)
        elif kind == "turns":
            try:
                n = int(str(value).strip())
            except (TypeError, ValueError):
                errors.append('effective_turns 收到「%s」，应为整数' % value); continue
            if n < 1:
                errors.append("effective_turns 收到 %d，有效轮数至少为 1" % n)
        elif kind == "url":
            if not re.match(r"^https?://\S+$", str(value)):
                errors.append('repo_url 收到「%s」，应为 http(s) 开头的仓库地址' % value)
        elif kind == "sha":
            if not re.fullmatch(r"[0-9a-f]{40}", str(value)):
                errors.append('base_commit 收到「%s」，应为 40 位小写完整 commit SHA' % value); continue
            df = pkg / "environment" / "Dockerfile"
            m = re.search(r"ARG\s+BASE_SHA\s*=\s*([0-9a-fA-F]{7,40})", df.read_text(encoding="utf-8")) if df.is_file() else None
            if not m:
                errors.append("environment/Dockerfile 里没找到 ARG BASE_SHA=<40 位 SHA>")
            elif m.group(1).lower() != str(value).lower():
                errors.append("task.toml 的 base_commit 与 Dockerfile 的 ARG BASE_SHA 不一致")
    inst = pkg / "instruction.md"
    if inst.is_file():
        text = inst.read_text(encoding="utf-8").strip()
        if not text:
            errors.append("instruction.md 是空的")
        elif placeholder_hits(text):
            errors.append("instruction.md 里还留着模板占位内容")
    return errors


def check_dir(pkg: Path, pkg_name: str, options: dict | None = None) -> list[str]:
    options = options or load_options()
    return check_structure(pkg) + check_toml(pkg, pkg_name, options)


def check_package(path: str | Path, options: dict | None = None) -> list[str]:
    """Package directory or zip -> list of failure strings (empty = 体检通过)."""
    path = Path(path)
    if path.is_dir():
        return check_dir(path, path.name, options)
    tmp = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(path) as zf:
            tops = {n.split("/")[0] for n in zf.namelist() if n.split("/")[0] not in ("", "__MACOSX")}
            if len(tops) != 1:
                return ["zip 里必须只有一个顶层目录（即题目名称），当前有 %d 个" % len(tops)]
            zf.extractall(tmp)
        name = tops.pop()
        return check_dir(tmp / name, name, options)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="offline delivery-package intake check (体检)")
    ap.add_argument("packages", nargs="+", help="package directories or <题目名称>.zip files")
    ap.add_argument("--options", help="select-options.json dumped from the ledger (default: $SELECT_OPTIONS or newest work dir)")
    args = ap.parse_args(argv)
    options = load_options(args.options)
    bad = 0
    for p in args.packages:
        errs = check_package(p, options)
        print(f"{'FAIL' if errs else 'OK  '}  {p}")
        for e in errs:
            print("      -", e)
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
