#!/usr/bin/env python3
"""xlsx2task — turn rows of the SWE-like task ledger (.xlsx) into deep-swe /
Harbor task directories (step 1 scope: task.toml + instruction.md + environment/Dockerfile).

Subcommands
  check   <ledger.xlsx>            validate every row, print a readiness report (no network)
  gen     <ledger.xlsx> --out DIR  write task dirs for the selected rows

Options shared by both
  --sheet NAME          worksheet name (default: first sheet)
  --overrides FILE      JSON keyed by stable source_id (preferred), exact source title, or ledger row.  Each entry may
                        contain source_title as a row-reordering guard plus task_id, display_title,
                        display_description, instruction_en, language, category, and install_block.
                        Values here win over anything derived from the ledger.
  --resolve             clone each repo (blobless) into --repos-dir, verify the base commit is on the
                        default branch, detect the build layout, and (Python + uv.lock) export pinned
                        test dependencies for the Dockerfile.  Needs git (+ uv for lockfile pins).
  --repos-dir DIR       where --resolve keeps clones (default: ./_repos); a private uv is downloaded
                        into <DIR>/.tools when uv is not on PATH
  --preflight           with --resolve: run the rendered install block as a dry-run inside the
                        mars-base image (docker required) and block rows whose recipe fails there.
                        --preflight-platform (default linux/amd64) picks the architecture.
  --instruction-mode M  original (default) copies 需求 Prompt（原文） verbatim;
                        english uses --instructions-dir / instruction_en overrides and appends the
                        DeepSWE branch-and-commit closing line

gen-only
  --rows 2,5-9          ledger row numbers to generate (1 = header); default: all rows
  --org NAME            task name prefix, [task].name = "<org>/<task_id>" (default: swe)
  --instructions-dir D  English instruction.md sources, one file per task: <D>/<task_id>.md
                        (fallback: an `instruction_en` column in the ledger)
  --force               overwrite the three generated files in an existing task dir
  --only-instructions   update only instruction.md; does not resolve or rewrite the environment
  --allow-incomplete    allow TODO scaffolds; strict complete generation is the default
  --report FILE         write a JSON generation report outside the task tree

Exit status of `check` is 1 when any row is blocked, so it can gate CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import openpyxl
except ImportError:  # pragma: no cover
    sys.exit("pip install openpyxl")

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"
sys.path.insert(0, str(HERE))
import pin_lint  # noqa: E402
CLOSING_LINE = "IMPORTANT: Please work on this in a new branch from main and commit everything when you are done."

# --------------------------------------------------------------------------- ledger columns
COLS = {  # canonical name -> accepted header aliases (first match wins)
    "title": ["题目名称", "title"],
    "seed_type": ["Type", "seed_type"],
    "submitter": ["提交人", "submitter"],
    "submitted_at": ["提交日期", "submitted_at"],
    "repo_url": ["Repo URL", "repo_url", "Repo"],
    "base_sha": ["Commit/版本", "base_commit", "commit"],
    "language": ["主要语言", "language"],
    "task_type": ["任务类型", "task_type"],
    "prompt_zh": ["需求 Prompt（原文）", "需求 Prompt(原文)", "prompt"],
    "difficulty_zh": ["真实性与难度说明", "difficulty"],
    "modules_zh": ["可能涉及模块", "modules"],
    "rubric_zh": ["Verify Rubric", "rubric"],
    "result_zh": ["产物结果", "result"],
    "result_extra_zh": ["产物补充材料", "result_extra"],
    "solution_commit_url": ["Commit URL", "solution_commit_url"],
    "patch_file": [".patch文件", "patch_file"],
    "solution_sha": ["解法 Commit", "solution_sha"],
    "done": ["是否完成需求", "done"],
    # optional columns we recommend ADDING to the ledger:
    "task_id": ["task_id", "slug"],
    "display_title": ["display_title"],
    "display_description": ["display_description"],
    "instruction_en": ["instruction_en", "instruction"],
    "source_id": ["source_id"],
}

LANG_MAP = {
    "python": "python", "go": "go", "golang": "go", "typescript": "typescript",
    "javascript": "javascript", "rust": "rust", "java": "java",
}
CATEGORY_MAP = {
    "功能新增": "feature_request", "功能增强": "enhancement", "bug 修复": "bugfix", "bug修复": "bugfix",
    "问题修复": "bugfix", "重构/性能": "enhancement", "测试增强": "enhancement", "配置/工具链": "enhancement",
    "其他": "enhancement", "feature_request": "feature_request",
    "bugfix": "bugfix", "enhancement": "enhancement",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
ANNOTATED_SHA40 = re.compile(r"^([0-9a-f]{40})(?:\s*[（(].*[）)]?)?$", re.I)
GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
TASK_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Row:
    n: int
    raw: dict
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    # derived
    repo_url: str = ""
    owner: str = ""
    repo: str = ""
    base_sha: str = ""
    language: str = ""
    category: str = ""
    task_id: str = ""
    source_id: str = ""
    ext_id: str = ""
    layout: dict = field(default_factory=dict)

    def get(self, key: str) -> str:
        return (self.raw.get(key) or "").strip()

    @property
    def readiness(self) -> str:
        return "blocked" if self.blockers else ("needs_fix" if self.warnings else "ready")


# --------------------------------------------------------------------------- reading
def read_ledger(path: Path, sheet: str | None) -> list[Row]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        sys.exit("ledger is empty")
    # Repo URLs in the supplied ledger are HYPERLINK formulas. openpyxl normally
    # returns their cached display values with data_only=True, but some spreadsheet
    # programs omit formula caches. Keep formulas as a deterministic fallback.
    formula_wb = openpyxl.load_workbook(path, data_only=False)
    formula_ws = formula_wb[ws.title]
    formula_rows = list(formula_ws.iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    index = {}
    for canon, aliases in COLS.items():
        for a in aliases:
            if a in header:
                index[canon] = header.index(a)
                break
    missing = [c for c in ("title", "repo_url", "base_sha", "language", "task_type", "prompt_zh", "rubric_zh") if c not in index]
    if missing:
        sys.exit(f"ledger is missing required columns: {missing}\nheaders seen: {header}")
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c not in (None, "") for c in r):
            continue
        raw = {}
        formula_row = formula_rows[n - 1] if n - 1 < len(formula_rows) else ()
        for canon, i in index.items():
            value = r[i] if i < len(r) else None
            if isinstance(value, str) and "linkLocation=" in value:
                # Some spreadsheet apps cache HYPERLINK cells as
                # 'HYPERLINK is not implemented. linkLocation=<url>, friendlyName=<text>'.
                link = re.search(r"linkLocation=([^,\s]+)", value)
                value = link.group(1) if link else value
            if value in (None, "") and i < len(formula_row):
                formula = formula_row[i]
                if isinstance(formula, str):
                    match = re.match(r'^=HYPERLINK\("((?:[^"]|"")*)"', formula, re.I)
                    if match:
                        value = match.group(1).replace('""', '"')
            raw[canon] = str(value) if value is not None else ""
        out.append(Row(n=n, raw=raw))
    return out


# --------------------------------------------------------------------------- derivation
def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def normalized_source_parts(row: Row) -> tuple[str, str, str]:
    url = row.get("repo_url").removesuffix(".git").rstrip("/").lower()
    sha_raw = row.get("base_sha").lower()
    match = ANNOTATED_SHA40.fullmatch(sha_raw)
    sha = match.group(1) if match else sha_raw
    return url, sha, row.get("title")


def source_id_for(row: Row) -> str:
    return "bz" + hashlib.sha256("|".join(normalized_source_parts(row)).encode()).hexdigest()[:30]


def row_override(row: Row, overrides: dict) -> dict:
    row.source_id = source_id_for(row)
    norm = re.sub(r"\s+", "", row.get("title")).lower()
    by_norm = {re.sub(r"\s+", "", k).lower(): v for k, v in overrides.items() if not k.startswith("bz") and not k.isdigit()}
    ov = (overrides.get(row.source_id) or overrides.get(row.get("title")) or by_norm.get(norm)
          or overrides.get(str(row.n)) or {})
    expected_title = ov.get("source_title")
    if expected_title and re.sub(r"\s+", "", expected_title) != re.sub(r"\s+", "", row.get("title")):
        message = f"override source_title mismatch for row {row.n}: expected {expected_title!r}, got {row.get('title')!r}"
        if message not in row.blockers:
            row.blockers.append(message)
        return {}
    return ov


def derive(row: Row, overrides: dict, org: str) -> Row:
    ov = row_override(row, overrides)
    # repository
    url = ov.get("repository_url") or row.get("repo_url")
    m = GITHUB.match(url)
    if not m:
        row.blockers.append(f"repo_url not a github.com/<owner>/<name> URL: {url!r}")
    else:
        row.owner, row.repo = m.group(1), m.group(2)
        row.repo_url = f"https://github.com/{row.owner}/{row.repo}"
    # base commit
    sha_raw = (ov.get("base_commit_hash") or row.get("base_sha")).strip().lower()
    annotated = ANNOTATED_SHA40.fullmatch(sha_raw)
    if annotated:
        row.base_sha = annotated.group(1)
    elif re.match(r"^[0-9a-f]{7,39}$", sha_raw):
        row.blockers.append(f"base commit is a short SHA ({sha_raw}); need the full 40-char SHA")
    else:
        row.blockers.append(f"base commit is not a SHA ({sha_raw!r}); tags/versions must be resolved to a commit")
    # language
    lang_raw = ov.get("language") or row.get("language")
    lang = LANG_MAP.get(lang_raw.strip().lower())
    if lang:
        row.language = lang
    elif "/" in lang_raw:
        row.blockers.append(f"language {lang_raw!r} is ambiguous; set overrides[{row.n}].language to typescript or javascript")
    else:
        row.blockers.append(f"language {lang_raw!r} has no environment recipe")
    # category
    cat_raw = ov.get("category") or row.get("task_type")
    cat = CATEGORY_MAP.get(cat_raw.strip().lower()) or CATEGORY_MAP.get(cat_raw.strip())
    if cat:
        row.category = cat
        if cat_raw.strip() == "重构/性能" and not ov.get("category"):
            row.warnings.append("任务类型 重构/性能 mapped to enhancement; confirm the task has observable behavior to test")
    else:
        row.blockers.append(f"任务类型 {cat_raw!r} has no deep-swe category (feature_request/bugfix/enhancement)")
    # task id
    tid = ov.get("task_id") or row.get("task_id")
    if tid:
        if not TASK_ID.fullmatch(tid):
            row.blockers.append(f"task_id {tid!r} must be kebab-case ascii")
        elif len(tid) > 56:
            row.blockers.append(f"task_id {tid!r} is {len(tid)} chars; DeepSWE allows at most 56")
        elif row.repo and not tid.startswith(slugify(row.repo)):
            row.warnings.append(f"task_id {tid!r} does not start with the repo name {slugify(row.repo)!r} (deep-swe convention)")
        row.task_id = tid
    else:
        row.task_id = f"{slugify(row.repo) or 'repo'}-row{row.n}"
        row.blockers.append("no task_id: add a task_id column or override; placeholders are not generated in strict mode")
    row.ext_id = "kh" + hashlib.sha256(
        f"{org}|{row.repo_url}|{row.base_sha}|{row.task_id}".encode()
    ).hexdigest()[:30]
    # English titles
    for key in ("display_title", "display_description"):
        if not (ov.get(key) or row.get(key)):
            row.blockers.append(f"no {key} (English); add a ledger column or an override")
    display_title = ov.get("display_title") or row.get("display_title")
    display_description = ov.get("display_description") or row.get("display_description")
    if len(display_title) > 120:
        row.blockers.append(f"display_title is {len(display_title)} chars; maximum is 120")
    if len(display_description) > 240:
        row.blockers.append(f"display_description is {len(display_description)} chars; maximum is 240")
    # title sanity
    if re.search(r"有效轮数|效果差|效果好", row.get("title")):
        row.warnings.append(f"题目名称 looks like a seed Type label ({row.get('title')!r}); the real title is missing")
    # prompt quality
    p = row.get("prompt_zh")
    if len(p) < 150 and not (ov.get("instruction_en") or row.get("instruction_en")):
        row.warnings.append(f"需求 Prompt is short ({len(p)} chars); the English instruction will need the rubric to be testable")
    if re.search(r"https?://github\.com/", p) and not (ov.get("instruction_en") or row.get("instruction_en")):
        row.warnings.append("需求 Prompt embeds a repo URL; must be stripped from instruction.md (the agent works offline in /app)")
    if re.search(r"\b(src|lib|pkg|cmd|internal)/[\w./-]+\.(py|go|ts|js|rs)\b", p):
        row.warnings.append("需求 Prompt names source files; deep-swe instructions avoid dictating file paths (leak / over-spec risk)")
    if not row.get("rubric_zh"):
        row.warnings.append("no Verify Rubric")
    # Solution references are intentionally not considered in step 1 and are never
    # copied into the public task.toml.
    return row


def check_license(row: Row, overrides: dict) -> None:
    ov = row_override(row, overrides)
    lic = ov.get("upstream_license") or row.layout.get("license", "")
    if lic.startswith(("UNKNOWN", "GPL", "AGPL")):
        row.warnings.append(f"upstream_license = {lic}; review before publishing")


# --------------------------------------------------------------------------- resolve (network)
BASE_IMAGE = "public.ecr.aws/x8v8d7g8/mars-base@sha256:91db850db926024eed328c4bf519d54986bc10aad75302cbb074f8e9d79b4c46"  # = :latest on 2026-09-08 (交付包规范: digest only)
IMAGE_PYTHON = "3.12.12"      # mars-base default interpreter
UV_PIN = "0.12.9"             # mars-base ships uv 0.9.18 (Dec 2025); 2026 lockfiles need a newer parser
# Client rule: every pip / apt / go dependency the Dockerfile adds must carry an exact version
# (bare names, >=, ~=, ==1.* are rejected; the repo's own `-e .`, `-r` files, lockfile installs and
# `go mod download` are exempt).  Versions resolved in mars-base on 2026-09-04 (Debian 12.12).
PIP_PINS = {"pytest": "9.1.1", "pytest-asyncio": "1.4.0", "pytest-mock": "3.15.1"}
APT_PINS = {"openjdk-17-jdk-headless": "17.0.20.1+1-1~deb12u1",
            "maven": "3.8.7-1",
            "rsync": "3.2.7-1+deb12u6",
            "libsecret-1-dev": "0.20.5-3",
            "libmagic1": "1:5.44-3",
            "libsm6": "2:1.2.3-1",
            "libxext6": "2:1.3.4-1+b1",
            "libopengl0": "1.6.0-1",
            "libosmesa6": "22.3.6-1+deb12u2",
            "libpango-1.0-0": "1.50.12+ds-1",
            "libpangocairo-1.0-0": "1.50.12+ds-1",
            "libgdk-pixbuf-2.0-0": "2.42.10+dfsg-1+deb12u4",
            "libcairo2": "1.16.0-7",
            "librsvg2-bin": "2.54.7+dfsg-1~deb12u1",
            "libffi-dev": "3.4.4-1"}


def pip_pin(spec: str) -> str:
    """Return `name==version` for bare names we know; leave already-exact specs untouched."""
    name = re.split(r"[<>=!~\[ ;@]", spec.strip(), 1)[0].lower()
    if "==" in spec and "*" not in spec:
        return spec
    return f"{name}=={PIP_PINS[name]}" if name in PIP_PINS and not re.search(r"[<>=~]", spec) else spec


def apt_pin(name: str) -> str:
    return f"{name}={APT_PINS[name]}" if name in APT_PINS and "=" not in name else name


PYTEST_PIN = f"pytest=={PIP_PINS['pytest']}"
TEST_GROUP_NAMES = ("tests", "test", "testing", "dev", "development")
TESTISH = re.compile(r"(^|[/_-])(test|tests|testing|ci)([._-]|$)", re.I)
DEVISH = re.compile(r"(^|[/_-])(dev|development)([._-]|$)", re.I)
REQUIREMENT_FILES = [  # ordered: base, test-ish, dev-ish (dev-ish is used only when no test-ish file exists)
    "requirements.txt", "requirements/base.txt", "requirements/requirements.txt",
    "requirements-test.txt", "requirements_test.txt", "test-requirements.txt", "test_requirements.txt",
    "requirements-testing.txt", "requirements/test.txt", "requirements/tests.txt", "requirements/testing.txt",
    "requirements/requirements-testing.txt", "requirements/test-requirements.txt",
    "tests/requirements.txt", "test/requirements.txt", "tests/requirements-test.txt",
    "tests/test_requirements.txt", "tests/test-requirements.txt", "tests/requirements_test.txt",
    "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt", "requirements/dev.txt",
    "requirements/requirements-dev.txt",
]


def run(cmd, cwd=None, check=True, capture=True, timeout=None):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def detect_license(repo_dir: Path) -> str:
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst", "LICENCE", "COPYING"):
        p = repo_dir / name
        if p.exists():
            head = p.read_text(errors="replace")[:600].lower()
            if "mit license" in head or "permission is hereby granted" in head:
                return "MIT"
            if "apache license" in head:
                return "Apache-2.0"
            if "bsd" in head and "3" in head[:200] or "neither the name" in head:
                return "BSD-3-Clause"
            if "bsd" in head or "redistribution and use in source and binary forms" in head:
                return "BSD-2-Clause"
            if "isc license" in head:
                return "ISC"
            if "mozilla public license" in head:
                return "MPL-2.0"
            if "agpl" in head or "affero" in head:
                return "AGPL-family (copyleft: REVIEW)"
            if "gnu general public license" in head or "gnu lesser" in head:
                return "GPL-family (copyleft: REVIEW)"
            if "unencumbered software released into the public domain" in head:
                return "Unlicense"
            return f"UNKNOWN (see {name})"
    return "UNKNOWN (no LICENSE file)"


def marker_ok(marker: str) -> bool:
    """Evaluate a PEP 508 marker for the image (linux, CPython 3.12). Falls back to heuristics."""
    try:
        from packaging.markers import Marker  # type: ignore
        env = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
               "python_version": IMAGE_PYTHON.rsplit(".", 1)[0], "python_full_version": IMAGE_PYTHON,
               "implementation_name": "cpython", "platform_python_implementation": "CPython",
               "platform_machine": "x86_64"}
        return bool(Marker(marker).evaluate(env))
    except Exception:
        return not any(t in marker for t in ("win32", "windows", "darwin", "< '3.11'", "< '3.12'", "pypy"))


def python_ok(spec: str) -> bool:
    """Does the repo's requires-python admit the image interpreter?"""
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet  # type: ignore
        return SpecifierSet(spec).contains(IMAGE_PYTHON, prereleases=True)
    except Exception:
        return True


def ensure_uv(repos_dir: Path, row: Row) -> str | None:
    """uv on PATH, else a private copy under <repos-dir>/.tools (downloaded once)."""
    found = shutil.which("uv")
    if found:
        return found
    local = (repos_dir / ".tools" / "bin" / "uv").resolve()  # run() uses cwd=repo, so it must be absolute
    if local.exists():
        return str(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["sh", "-c", f'curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR="{local.parent}" UV_NO_MODIFY_PATH=1 sh'])
    except subprocess.CalledProcessError as e:
        row.warnings.append(f"uv is not installed and could not be downloaded ({e.stdout.strip()[-120:]}); "
                            "uv.lock repos fall back to in-image `uv sync`")
        return None
    return str(local) if local.exists() else None


def toml_loads(text: str) -> dict:
    """tomllib (3.11+), else tomli, else the copy pip vendors -- the ledger machine runs Python 3.10."""
    try:
        import tomllib  # type: ignore
        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore
        return tomli.loads(text)
    except ImportError:
        from pip._vendor import tomli as pip_tomli  # type: ignore
        return pip_tomli.loads(text)


def read_pyproject(repo_dir: Path, row: Row) -> dict:
    p = repo_dir / "pyproject.toml"
    if not p.is_file():
        return {}
    try:
        return toml_loads(p.read_text())
    except Exception as exc:
        row.warnings.append(f"could not parse pyproject.toml: {exc}")
        return {}


def tox_test_deps(repo_dir: Path, row: Row) -> list[str]:
    """Unconditional `deps` of [testenv] in tox.ini (env-conditional and -r/-e/git lines are skipped)."""
    ini = repo_dir / "tox.ini"
    if not ini.is_file():
        return []
    import configparser
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(ini)
        deps = cp.get("testenv", "deps", fallback="")
    except Exception as exc:
        row.warnings.append(f"could not parse tox.ini: {exc}")
        return []
    out = []
    for line in deps.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s or "{" in s or s.startswith(("-", "git+", "http")):
            continue
        if re.match(r"^[\w.,-]+\s*:", s):  # "py313: ..." / "cov: pytest-cov" env conditions
            continue
        out.append(s)
    return out


def has_native_ext(repo_dir: Path, pyproject: dict) -> bool:
    build_reqs = " ".join(pyproject.get("build-system", {}).get("requires", [])).lower()
    if "cython" in build_reqs:
        return True
    for p in repo_dir.glob("**/*.pyx"):
        if "node_modules" not in p.parts and ".git" not in p.parts:
            return True
    setup_py = repo_dir / "setup.py"
    return setup_py.is_file() and "ext_modules" in setup_py.read_text(errors="replace")


def detect_python_layout(repo_dir: Path, row: Row) -> None:
    lay = row.layout
    pyproject = read_pyproject(repo_dir, row)
    project = pyproject.get("project", {})
    lay["requires_python"] = project.get("requires-python", "")
    lay["requires_python_ok"] = python_ok(lay["requires_python"])
    extras = project.get("optional-dependencies", {})
    lay["python_extras"] = sorted(extras)
    lay["python_test_extra"] = next((n for n in TEST_GROUP_NAMES if n in extras), "")
    if not lay["python_test_extra"] and (repo_dir / "setup.py").is_file():
        m = re.search(r"extras_require\s*=\s*\{(.{0,4000})", (repo_dir / "setup.py").read_text(errors="replace"), re.S)
        if m:
            keys = re.findall(r"[\"']([A-Za-z_-]+)[\"']\s*:", m.group(1))
            lay["python_test_extra"] = next((n for n in TEST_GROUP_NAMES if n in keys), "")
    groups = pyproject.get("dependency-groups", {})
    lay["python_dependency_groups"] = sorted(groups)
    # Prefer a conventional test/dev group that actually pulls pytest; otherwise any group that does
    # (strictdoc keeps its unit-test deps in `check`), else the first conventional name.
    def has_pytest(deps) -> bool:
        return any("pytest" in str(d).lower() for d in (deps or []))
    conventional = [n for n in TEST_GROUP_NAMES if n in groups]
    lay["python_test_group"] = (next((n for n in conventional if has_pytest(groups[n])), "")
                                or next((n for n in sorted(groups) if has_pytest(groups[n])), "")
                                or (conventional[0] if conventional else ""))
    lay["uv_conflicts"] = bool(pyproject.get("tool", {}).get("uv", {}).get("conflicts"))
    poetry_cfg = pyproject.get("tool", {}).get("poetry", {})
    lay["poetry_groups"] = sorted(poetry_cfg.get("group", {}))
    # Apps often carry a [tool.poetry] name without any importable package; `poetry install`
    # then fails with "No file/folder found for package". Install such projects with --no-root.
    name = (poetry_cfg.get("name") or project.get("name") or "").replace("-", "_").lower()
    lay["python_project"] = name
    lay["poetry_has_root_package"] = bool(poetry_cfg.get("packages")) or bool(name) and any(
        (repo_dir / cand).exists() for cand in (name, f"src/{name}", f"{name}.py", f"src/{name}.py"))
    present = [n for n in REQUIREMENT_FILES if (repo_dir / n).is_file()]
    base = [n for n in present if n in ("requirements.txt", "requirements/base.txt", "requirements/requirements.txt")]
    testish = [n for n in present if TESTISH.search(n)]
    devish = [n for n in present if DEVISH.search(n) and n not in testish]
    lay["requirements"] = base + testish + (devish if not testish else [])
    pairs = []
    req_dir = repo_dir / "requirements"
    if req_dir.is_dir():
        for txt in sorted(req_dir.glob("*.txt")):
            src = txt.with_suffix(".in")
            if src.is_file() and TESTISH.search(txt.stem):
                pairs.append((f"requirements/{src.name}", f"requirements/{txt.name}"))
    pairs.sort(key=lambda p: (p[0] != "requirements/test.in", p[0]))
    lay["pip_compile_pairs"] = pairs[:1]
    lay["tox_test_deps"] = tox_test_deps(repo_dir, row)
    lay["native_ext"] = has_native_ext(repo_dir, pyproject)
    lock = repo_dir / "poetry.lock"
    if lock.is_file():
        text = lock.read_text(errors="replace")
        m = re.search(r"generated by Poetry (\d+\.\d+\.\d+)", text[:400])
        lay["poetry_version"] = m.group(1) if m else ""
        m = re.search(r'^lock-version = "([\d.]+)"', text, re.M)
        lay["poetry_lock_version"] = m.group(1) if m else ""
    uv_lock = repo_dir / "uv.lock"
    if uv_lock.is_file():
        m = re.search(r"^revision = (\d+)", uv_lock.read_text(errors="replace")[:400], re.M)
        lay["uv_lock_revision"] = int(m.group(1)) if m else 0


def detect_rust_layout(repo_dir: Path, row: Row) -> None:
    lay = row.layout
    root = repo_dir / "Cargo.toml"
    manifests = [p for p in repo_dir.glob("**/Cargo.toml")
                 if not {"target", "node_modules", ".git"} & set(p.parts) and len(p.relative_to(repo_dir).parts) <= 3]
    chosen = root if root.is_file() else None
    if chosen is None and manifests:
        workspaces = [m for m in manifests if "[workspace]" in m.read_text(errors="replace")]
        chosen = (workspaces or sorted(manifests, key=lambda p: len(p.parts)))[0]
        row.warnings.append(f"Cargo.toml is not at the repo root; using {chosen.relative_to(repo_dir).as_posix()}")
    if chosen is None:
        row.warnings.append("no Cargo.toml found (depth <= 3)")
        return
    rel = chosen.relative_to(repo_dir).as_posix()
    lay["rust_manifest"] = rel
    lay["rust_dir"] = str(Path(rel).parent.as_posix())
    lay["rust_locked"] = (chosen.parent / "Cargo.lock").is_file()
    lay["rust_toolchain_file"] = any((d / f).is_file() for d in (chosen.parent, repo_dir)
                                     for f in ("rust-toolchain.toml", "rust-toolchain"))


def detect_go_layout(repo_dir: Path, row: Row) -> None:
    if (repo_dir / "go.mod").is_file():
        row.layout["go_dir"] = "."
        return
    mods = [p for p in repo_dir.glob("*/go.mod")] + [p for p in repo_dir.glob("*/*/go.mod")]
    if mods:
        chosen = sorted(mods, key=lambda p: len(p.parts))[0]
        row.layout["go_dir"] = chosen.parent.relative_to(repo_dir).as_posix()
        row.warnings.append(f"go.mod is not at the repo root; using {row.layout['go_dir']}/")
    else:
        row.warnings.append("no go.mod found (depth <= 2)")


def detect_node_layout(repo_dir: Path, row: Row) -> None:
    lay = row.layout
    pkg_path = repo_dir / "package.json"
    if not pkg_path.is_file():
        return
    try:
        pkg = json.loads(pkg_path.read_text())
    except Exception as exc:
        row.warnings.append(f"could not parse package.json: {exc}")
        return
    scripts = pkg.get("scripts", {})
    lay["node_workspaces"] = bool(pkg.get("workspaces")) or (repo_dir / "pnpm-workspace.yaml").is_file()
    lay["node_root_hooks"] = [k for k in ("preinstall", "postinstall", "prepare") if k in scripts]
    lay["node_package_manager"] = pkg.get("packageManager", "")
    yarnrc = repo_dir / ".yarnrc.yml"
    lay["yarn_path"] = yarnrc.is_file() and bool(re.search(r"^yarnPath:", yarnrc.read_text(errors="replace"), re.M))
    lay["node_lockfile"] = next((n for n in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "bun.lockb", "bun.lock")
                                 if (repo_dir / n).is_file()), "")
    if lay["node_workspaces"] and lay["node_root_hooks"]:
        row.warnings.append(f"node monorepo with root {'/'.join(lay['node_root_hooks'])} script: the generic install "
                            "runs every workspace build; expect to need an install_block override (see joplin)")
    if not lay["node_lockfile"]:
        row.warnings.append("no node lockfile: `npm install` is not reproducible; consider committing a lockfile in the override")


def export_python_pins(repo_dir: Path, row: Row, uv_bin: str | None) -> list[str]:
    """['name==ver', ...] for runtime + test deps from uv.lock (image interpreter), or [] if unavailable."""
    if not (repo_dir / "uv.lock").exists():
        return []
    if not row.layout.get("requires_python_ok", True):
        row.warnings.append(f"requires-python {row.layout.get('requires_python')!r} excludes the image's {IMAGE_PYTHON}; "
                            "using in-image `uv sync` so uv can pick an interpreter")
        return []
    if uv_bin is None:
        return []
    chosen = row.layout.get("python_test_group") or ""
    test_extra = row.layout.get("python_test_extra") or ""
    cmd = [uv_bin, "export", "--frozen", "--no-default-groups", "--no-emit-project", "--no-hashes",
           "--no-annotate", "--no-header"]
    if chosen:
        cmd += ["--group", chosen]
    elif test_extra:
        cmd += ["--extra", test_extra]
        chosen = f"extra {test_extra}"
    else:
        row.warnings.append("uv.lock present but no tests/test/dev dependency group or extra found; "
                            "pins cover runtime deps only (pytest is added unpinned)")
    if "default" in row.layout.get("python_extras", []):
        cmd += ["--extra", "default"]  # projects such as yt-dlp keep their runtime deps in a `default` extra
    try:
        out = run(cmd, cwd=repo_dir).stdout
    except subprocess.CalledProcessError as e:
        row.warnings.append(f"uv export failed: {e.stdout.strip()[-300:]}")
        return []
    pins = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e ", ".")):
            continue
        req, _, marker = line.partition(";")
        if marker and not marker_ok(marker.strip()):
            continue
        pins.append(req.strip())
    if chosen and "pytest" not in " ".join(pins):
        row.warnings.append(f"dependency group {chosen!r} does not include pytest; check the test runner")
    return sorted(set(pins), key=str.lower)


def compile_python_pins(repo_dir: Path, row: Row, uv_bin: str | None) -> list[str]:
    """Lockfile-less project: resolve runtime + test deps with `uv pip compile`, excluding releases newer
    than the base commit so the image matches what upstream CI saw at that commit and never drifts.
    Returns ['name==ver', ...] or [] (caller falls back to the unpinned recipe, which the lint may reject)."""
    lay = row.layout
    files = set(lay.get("files", []))
    if uv_bin is None or not (files & {"pyproject.toml", "setup.py", "setup.cfg"}):
        return []
    if files & {"uv.lock", "poetry.lock", "pdm.lock"} or lay.get("pip_compile_pairs"):
        return []
    if not lay.get("requires_python_ok", True):
        return []
    date = run(["git", "show", "-s", "--format=%cI", row.base_sha], cwd=repo_dir, check=False).stdout.strip()
    inputs = []
    if "pyproject.toml" in files:
        inputs.append("pyproject.toml")
    elif "setup.py" in files or "setup.cfg" in files:
        inputs.append("setup.py" if "setup.py" in files else "setup.cfg")
    # Every test-ish / dev-ish requirement file the repo ships is an input too (upstream CI installs
    # them next to the package), regardless of what detect_python_layout chose for the plain recipe.
    for req in sorted(set(lay.get("requirements", []) or [])
                      | {n for n in REQUIREMENT_FILES if (repo_dir / n).is_file() and ("test" in n or "dev" in n)}):
        inputs.append(req)
    extra_reqs = [d for d in lay.get("tox_test_deps", []) if d]
    tmp = repo_dir / ".swepipe-extra-requirements.txt"
    if extra_reqs:
        tmp.write_text("\n".join(extra_reqs) + "\n")
        inputs.append(tmp.name)
    # Resolved for the image's interpreter and Linux only (no --universal): universal output carries
    # markers such as `python_full_version < '3.11'` that pip would still try to satisfy verbatim.
    base = [uv_bin, "pip", "compile", *inputs, "--python-version", IMAGE_PYTHON.rsplit(".", 1)[0],
            "--python-platform", "x86_64-unknown-linux-gnu",
            "--no-header", "--no-annotate", "--no-emit-package", "pip", "-q"]
    if lay.get("python_test_extra"):
        base += ["--extra", lay["python_test_extra"]]
    if lay.get("python_test_group"):
        base += ["--group", lay["python_test_group"]]
    attempts = ([base + ["--exclude-newer", date]] if date else []) + [base]
    out, used_date = None, False
    for i, cmd in enumerate(attempts):
        r = run(cmd, cwd=repo_dir, check=False)
        if r.returncode == 0:
            out, used_date = r.stdout, (i == 0 and bool(date))
            break
        last_err = (r.stdout or "").strip()[-300:]
    if tmp.exists():
        tmp.unlink()
    if out is None:
        row.warnings.append(f"uv pip compile failed; falling back to the unpinned recipe: {last_err}")
        return []
    if date and not used_date:
        row.warnings.append(f"pins could not be resolved as of the base commit ({date[:10]}); resolved without a date cap")
    pins = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e ", ".", "-r ")):
            continue
        req, _, marker = line.partition(";")
        if marker and not marker_ok(marker.strip()):
            continue
        pins.append(req.strip())
    lay["pins_source"] = f"uv pip compile ({'as of ' + date[:10] if used_date else 'undated'}; inputs: {', '.join(inputs)}"
    lay["pins_source"] += (f"; extra {lay['python_test_extra']}" if lay.get("python_test_extra") else "")
    lay["pins_source"] += (f"; group {lay['python_test_group']}" if lay.get("python_test_group") else "") + ")"
    return sorted(set(pins), key=str.lower)


def resolve(row: Row, repos_dir: Path) -> None:
    if row.blockers:
        return
    repo_dir = repos_dir / f"{row.owner}__{row.repo}"
    try:
        if not (repo_dir / ".git").exists():
            repos_dir.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "-q", "--filter=blob:none", row.repo_url, str(repo_dir)])
        if run(["git", "cat-file", "-t", row.base_sha], cwd=repo_dir, check=False).returncode != 0:
            # Reuse an already verified cache without a network fetch on every run,
            # but give older or partial caches one chance to acquire this commit.
            run(["git", "fetch", "-q", "origin", row.base_sha], cwd=repo_dir, check=False)
            if run(["git", "cat-file", "-t", row.base_sha], cwd=repo_dir, check=False).returncode != 0:
                row.blockers.append(f"base commit {row.base_sha[:12]} does not exist in {row.repo_url}")
                return
        default = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_dir).stdout.strip().split("/")[-1]
        row.layout["default_branch"] = default
        run(["git", "checkout", "-q", "--force", row.base_sha], cwd=repo_dir)
        run(["git", "clean", "-fdxq"], cwd=repo_dir, check=False)
        if (repo_dir / ".gitmodules").is_file():
            run(["git", "submodule", "update", "--init", "--recursive", "-q"], cwd=repo_dir, check=False)
        files = {p.name for p in repo_dir.iterdir()}
        row.layout["files"] = sorted(f for f in files if f in {
            "pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "pdm.lock", "requirements.txt",
            "requirements-dev.txt", "requirements_dev.txt", "tox.ini", "noxfile.py", "go.mod", "go.sum", "package.json",
            "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb", "Cargo.toml", "Cargo.lock", "Makefile",
            ".gitmodules", "conftest.py", "pytest.ini", ".yarnrc.yml", "pom.xml", "mvnw", "gradlew",
            "build.gradle", "build.gradle.kts"})
        row.layout["license"] = detect_license(repo_dir)
        if row.language == "python":
            detect_python_layout(repo_dir, row)
            uv_bin = ensure_uv(repos_dir, row)
            row.layout["pins"] = export_python_pins(repo_dir, row, uv_bin)
            if not row.layout["pins"]:
                row.layout["pins"] = compile_python_pins(repo_dir, row, uv_bin)
        elif row.language == "rust":
            detect_rust_layout(repo_dir, row)
        elif row.language == "go":
            detect_go_layout(repo_dir, row)
        elif row.language in ("typescript", "javascript"):
            detect_node_layout(repo_dir, row)
    except subprocess.CalledProcessError as e:
        row.blockers.append(f"git failed for {row.repo_url}: {e.stdout.strip()[-300:]}")


# --------------------------------------------------------------------------- preflight (docker)
def preflight_script(install: str) -> tuple[str, list[str]]:
    """Rewrite a rendered install block into a dry-run shell script for the base image.

    pip/uv/poetry installs become --dry-run resolutions (tool installs such as uv==/poetry== stay
    real), cargo fetch becomes cargo metadata, go mod download becomes go list, node package
    installs are skipped (too slow to dry-run) and the pristine-checkout guards are dropped."""
    logical: list[str] = []
    for raw in install.splitlines():
        if logical and logical[-1].endswith("\\"):
            logical[-1] = logical[-1][:-1].rstrip() + " " + raw.strip()
        else:
            logical.append(raw.rstrip())
    lines, skipped = ["set -euo pipefail", "cd /app"], []
    for line in logical:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("ENV "):
            lines.append("export " + " ".join(shlex.quote(kv) if "=" in kv else kv for kv in shlex.split(s[4:])))
            continue
        if not s.startswith("RUN "):
            continue
        segments = []
        for seg in re.split(r"\s*&&\s*", s[4:]):
            seg = seg.strip()
            if not seg:
                continue
            if re.search(r"\b(npm|yarn|pnpm|bun)\s+(install|ci|i|rebuild)\b|workspaces focus", seg):
                skipped.append(seg[:80])
                continue
            if seg.startswith(("test -z", "git checkout", "git config", "python -c", "python3 -c")):
                continue  # post-install assertions cannot hold after a dry-run
            if re.match(r"^(python -m )?pip install\b", seg):
                if not re.search(r"\b(uv|poetry|pipx|poetry-plugin-export)==", seg) and "--dry-run" not in seg:
                    seg = seg.replace("pip install", "pip install --dry-run", 1)
            elif "uv sync" in seg:
                seg += " --dry-run"
            elif seg.startswith("poetry install"):
                seg += " --dry-run"
            elif seg.startswith("cargo fetch"):
                seg = seg.replace("cargo fetch", "cargo metadata --format-version 1 --no-deps", 1).replace("--locked", "") + " > /dev/null"
            elif seg.startswith("go mod download"):
                seg = "go list -m all > /dev/null"
            elif seg.startswith("go install"):
                continue
            segments.append(seg)
        if segments:  # one subshell per RUN: a `cd` must not leak into the next instruction
            lines.append("( " + " && ".join(segments) + " )")
    return "\n".join(lines) + "\n", skipped


def preflight(row: Row, repos_dir: Path, install: str, platform: str, timeout: int) -> None:
    repo_dir = repos_dir / f"{row.owner}__{row.repo}"
    if not (repo_dir / ".git").exists():
        row.warnings.append("preflight skipped: repo not resolved")
        return
    script, skipped = preflight_script(install)
    active = [l for l in script.splitlines() if not l.startswith(("set ", "cd ", "export "))]
    if not active:
        row.layout["preflight"] = {"platform": platform, "rc": None, "skipped": skipped, "tail": "nothing to dry-run"}
        row.warnings.append("preflight skipped: install block has no dry-runnable step (node installs are not preflighted)")
        return
    cmd = ["docker", "run", "--rm", "--platform", platform, "-v", f"{repo_dir}:/app", "-w", "/app",
           BASE_IMAGE, "bash", "-c", script]
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        rc, out = res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = 124, f"timeout after {timeout}s\n" + ((e.stdout or "") if isinstance(e.stdout, str) else "")
    except FileNotFoundError:
        row.warnings.append("preflight skipped: docker not found")
        return
    finally:
        run(["git", "checkout", "-q", "--", "."], cwd=repo_dir, check=False)
        run(["git", "clean", "-fdxq"], cwd=repo_dir, check=False)
    tail = "\n".join(l for l in out.splitlines() if "WARNING: Running pip as the 'root'" not in l and "[notice]" not in l)[-2500:]
    row.layout["preflight"] = {"platform": platform, "rc": rc, "skipped": skipped, "tail": tail}
    if rc != 0:
        row.blockers.append(f"preflight failed in {BASE_IMAGE} ({platform}), rc={rc}: {tail.strip().splitlines()[-1][:160] if tail.strip() else ''}")
    elif skipped:
        row.warnings.append(f"preflight passed but skipped {len(skipped)} node install step(s)")


# --------------------------------------------------------------------------- rendering
def toml_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_task_toml(row: Row, ov: dict, org: str) -> str:
    tmpl = (TEMPLATES / "task.toml.tmpl").read_text()
    values = {
        "org": org, "task_id": row.task_id, "ext_id": row.ext_id,
        "display_title": toml_str(ov.get("display_title") or row.get("display_title") or f"TODO: English title for {row.task_id}"),
        "display_description": toml_str(ov.get("display_description") or row.get("display_description") or "TODO: one-sentence English description (95-177 chars)."),
        "original_title": toml_str(row.get("title")),
        "category": row.category, "language": row.language, "repository_url": row.repo_url,
        "base_commit_hash": row.base_sha,
        "docker_image_line": ("docker_image = \"%s\"" % ov["docker_image"]) if ov.get("docker_image") else
            "# docker_image is omitted in step 1: Harbor/Pier builds environment/Dockerfile.\n"
            "# After the image is built and pushed, set e.g.\n"
            f"# docker_image = \"<registry>/{org}-swe:{row.ext_id}-v1\"",
    }
    return tmpl.format(**values)


def python_install_block(row: Row) -> tuple[str, list[str]]:
    lay, todo = row.layout, []
    files = set(lay.get("files", []))
    has_pkg = bool(files & {"pyproject.toml", "setup.py", "setup.cfg"})
    guard = '    && test -z "$(git status --porcelain)"\n'
    reporter = ("\n# v1.1 node-id scoring: pytest ships a native JUnit XML reporter (--junitxml),\n"
                "# so no extra reporter dependency is required.\n")
    extra_pkgs = [d for d in lay.get("tox_test_deps", []) if "pytest" not in d.lower() or True]
    pins = lay.get("pins") or []
    if pins:
        if lay.get("pins_source"):
            comment = (f"No lockfile upstream: pinned with `{lay['pins_source']}`,\n"
                       "# i.e. every release newer than the base commit is excluded, resolved for Linux / CPython 3.12\n"
                       "# (the mars-base default interpreter), so the image matches what upstream CI saw at this commit.")
        else:
            comment = ("Pinned from the repo's uv.lock (`uv export --frozen --no-default-groups --group <tests>`) at\n"
                       "# BASE_SHA, resolved for Linux / CPython 3.12 (the mars-base default interpreter), so the\n"
                       "# image matches upstream CI for this commit and does not drift when PyPI moves.")
        lines = "".join(f'        "{p}" \\\n' for p in pins)
        block = (TEMPLATES / "install.python.tmpl").read_text().format(install_comment=comment, pinned_lines=lines.rstrip("\n"))
        # The project under test may itself be pytest: then the editable install *is* the runner.
        if not any(p.lower().startswith("pytest==") for p in pins) and lay.get("python_project") != "pytest":
            block = block.replace("    && pip install --no-cache-dir --no-deps -e . \\\n",
                                  "    && pip install --no-cache-dir --no-deps -e . \\\n" f"    && pip install --no-cache-dir \"{PYTEST_PIN}\" \\\n", 1)
        return block, todo
    if "uv.lock" in files:
        group = lay.get("python_test_group", "")
        select = f"--no-default-groups --group {group}" if group else "--no-default-groups"
        if not group and lay.get("uv_conflicts"):
            row.warnings.append("uv.lock with [tool.uv] conflicts and no test group: sync installs runtime deps only")
        block = (f"# uv.lock is newer than the uv shipped in mars-base (0.9.18, Dec 2025), so a current uv is\n"
                 f"# installed first. The project environment lives outside /app so it can never be committed.\n"
                 f"ENV UV_PROJECT_ENVIRONMENT=/opt/venv\n"
                 f"RUN pip install --no-cache-dir \"uv=={UV_PIN}\" \\\n"
                 f"    && python -m uv sync --frozen {select} \\\n"
                 + ("" if group else f"    && python -m uv pip install \"{PYTEST_PIN}\" \\\n")
                 + guard + 'ENV PATH="/opt/venv/bin:${PATH}"\n')
        return block + reporter, todo
    if "poetry.lock" in files:
        ver = lay.get("poetry_version", "")
        groups = [g for g in lay.get("poetry_groups", []) if g in TEST_GROUP_NAMES]
        withs = f" --with {','.join(groups)}" if groups else ""
        install_poetry = (f"RUN pip install --no-cache-dir \"poetry=={ver}\"\n" if ver and ver != "1.8.2" else "")
        no_root = "" if lay.get("poetry_has_root_package", True) else " --no-root"
        if no_root:
            row.warnings.append("poetry project has no importable package; installed with --no-root (tests run from the checkout)")
        block = ((f"# poetry.lock was written by Poetry {ver}; " if ver else "# ")
                 + "mars-base ships Poetry 1.8.2, which cannot read lock-version 2.x files.\n"
                 "# `poetry check --lock` fails when upstream edited pyproject after locking; the lock is refreshed\n"
                 "# in that case, dependencies installed into the system interpreter, then the tracked lock file is\n"
                 "# restored so the checkout stays pristine.\n"
                 + install_poetry +
                 "RUN poetry config virtualenvs.create false \\\n"
                 "    && (poetry check --lock || poetry lock) \\\n"
                 f"    && poetry install --no-interaction --no-ansi{withs}{no_root} \\\n"
                 "    && git checkout -- poetry.lock \\\n"
                 + ("" if groups else f"    && pip install --no-cache-dir \"{PYTEST_PIN}\" \\\n")
                 + guard)
        return block + reporter, todo
    if "pdm.lock" in files:
        return ("RUN pdm install --frozen-lockfile\nENV PATH=\"/app/.venv/bin:${PATH}\"\n" + reporter), todo
    pairs = lay.get("pip_compile_pairs") or []
    if pairs:
        src, txt = pairs[0]
        needs_pytest = "pytest" not in (Path(txt).name + " ")  # refined below by caller when repo is available
        block = (f"# pip-compile layout: {src} is the requirement set and {txt} its pinned lock. Upstream CI\n"
                 "# installs them as requirements + constraints, so pins that do not apply to this interpreter\n"
                 "# (for example py3.10-only backports) are simply skipped instead of failing resolution.\n"
                 f"RUN pip install --no-cache-dir -r {src} -c {txt}" + (" -e ." if has_pkg else "") + " \\\n"
                 "    && python -c \"import pytest\" \\\n" + guard)
        return block + reporter, todo
    reqs = lay.get("requirements") or []
    if reqs:
        req_lines = " \\\n".join(f"    -r {name}" for name in reqs)
        editable = "    && pip install --no-cache-dir -e . \\\n" if has_pkg else ""
        extras = "".join(f"    && pip install --no-cache-dir {shlex.quote(pip_pin(d))} \\\n" for d in lay.get("tox_test_deps", []))
        block = ("RUN pip install --no-cache-dir \\\n" f"{req_lines} \\\n" f"{editable}{extras}"
                 f"    && pip install --no-cache-dir \"{PYTEST_PIN}\" \\\n" + guard)
        return block + reporter, todo
    if has_pkg:
        extra = lay.get("python_test_extra", "")
        group = lay.get("python_test_group", "")
        spec = f".[{extra}]" if extra else "."
        group_flag = f" --group {group}" if group else ""
        extras = "".join(f"    && pip install --no-cache-dir {shlex.quote(pip_pin(d))} \\\n" for d in lay.get("tox_test_deps", []))
        comment = ("# No lockfile or requirements file: install the package"
                   + (f" with its `{extra}` extra" if extra else "")
                   + (f" and the `{group}` dependency group" if group else "")
                   + (" plus the unconditional [testenv] deps from tox.ini" if lay.get("tox_test_deps") else "")
                   + ". Unpinned: verify against upstream CI.\n")
        if not (extra or group or lay.get("tox_test_deps")):
            row.warnings.append("no test dependencies detected (extras/dependency-groups/tox.ini); only pytest is added")
        block = (comment + f"RUN pip install --no-cache-dir -e \"{spec}\"{group_flag} \\\n" f"{extras}"
                 f"    && pip install --no-cache-dir \"{PYTEST_PIN}\" \\\n" + guard)
        return block + reporter, todo
    todo.append("Dockerfile install block is a TODO (python packaging metadata not found)")
    return "# TODO(env): no supported Python packaging metadata was found.\n", todo


def render_dockerfile(row: Row, ov: dict) -> tuple[str, list[str], str]:
    """Returns (dockerfile, todo, install_block)."""
    todo: list[str] = []
    head = (TEMPLATES / "Dockerfile.head.tmpl").read_text().format(base_commit_hash=row.base_sha, repository_url=row.repo_url)
    tail = (TEMPLATES / "Dockerfile.tail.tmpl").read_text()
    lay = row.layout
    files = set(lay.get("files", []))
    if ov.get("install_block"):
        install = ov["install_block"].rstrip() + "\n"
    elif row.language == "python":
        install, todo = python_install_block(row)
        if lay.get("native_ext") and lay.get("files"):
            row.warnings.append("repo builds native extensions (Cython/ext_modules): verify the build in the base image "
                                "or provide an install_block (for example <PKG>_NO_EXTENSIONS=1, see aiohttp)")
    elif row.language == "go":
        go_dir = lay.get("go_dir", ".")
        install = (TEMPLATES / "install.go.tmpl").read_text()
        if go_dir != ".":
            install = install.replace("RUN go mod download", f"RUN cd {go_dir} && go mod download")
    elif row.language == "rust":
        manifest = lay.get("rust_manifest", "Cargo.toml" if not lay.get("files") else "")
        if not manifest:
            install = "# TODO(env): no Cargo.toml found.\n"
            todo.append("Dockerfile install block is a TODO (rust manifest not found)")
        else:
            rust_dir = lay.get("rust_dir", ".")
            locked = " --locked" if lay.get("rust_locked") else ""
            pre = (f"# rust-toolchain.toml pins the toolchain; `cargo --version` inside the crate makes rustup fetch\n"
                   f"# it now (network is only available at build time).\nRUN cd {rust_dir} && cargo --version\n"
                   if lay.get("rust_toolchain_file") else "")
            install = pre + f"RUN cargo fetch --manifest-path {manifest}{locked}\n"
    elif row.language == "java":
        prefix = (
            "RUN apt-get update \\\n"
            f"    && apt-get install -y --no-install-recommends {apt_pin('openjdk-17-jdk-headless')} {apt_pin('maven')} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
        )
        if "mvnw" in files:
            install = prefix + "RUN chmod +x ./mvnw && ./mvnw -q -DskipTests dependency:go-offline\n"
        elif "pom.xml" in files:
            install = prefix + "RUN mvn -q -DskipTests dependency:go-offline\n"
        elif "gradlew" in files:
            install = prefix + "RUN chmod +x ./gradlew && ./gradlew --no-daemon dependencies\n"
        else:
            install = "# TODO(env): determine the Java build tool and dependency install command.\n"
            todo.append("Dockerfile install block is a TODO (java)")
    elif row.language in ("typescript", "javascript"):
        prefix = "ENV NODE_ENV=development NPM_CONFIG_PRODUCTION=false"
        if lay.get("node_workspaces") and lay.get("node_root_hooks"):
            prefix += " HUSKY=0 YARN_ENABLE_INLINE_BUILDS=1"
        prefix += "\n\n"
        lock = lay.get("node_lockfile") or next((n for n in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json") if n in files), "")
        if lock == "pnpm-lock.yaml":
            install = prefix + "RUN corepack enable && pnpm install --frozen-lockfile\n"
        elif lock == "yarn.lock":
            if ".yarnrc.yml" in files:
                yarn = "yarn" if lay.get("yarn_path") else "corepack enable && yarn"
                install = prefix + f"RUN {yarn} install --immutable\n"
            else:
                install = prefix + "RUN yarn install --frozen-lockfile\n"
        elif lock == "package-lock.json":
            install = prefix + "RUN npm ci --include=dev\n"
        elif "package.json" in files:
            install = prefix + "RUN npm install --include=dev\n"
        else:
            install = f"# TODO(env): determine the {row.language} package manager.\n"
            todo.append(f"Dockerfile install block is a TODO ({row.language})")
    else:
        install = f"# TODO(env): write the {row.language} install block (see templates/ and deep-swe examples)\n"
        todo.append(f"Dockerfile install block is a TODO ({row.language})")
    return head + install + tail, todo, install


def find_instruction(row: Row, instructions_dir: Path | None, ov: dict, mode: str) -> str | None:
    if mode == "original":
        return row.get("prompt_zh") or None
    if instructions_dir:
        p = instructions_dir / f"{row.task_id}.md"
        if p.exists():
            return p.read_text()
    return ov.get("instruction_en") or row.get("instruction_en") or None


def normalize_instruction(text: str, row: Row, mode: str) -> str:
    if mode == "original":
        # Preserve the ledger prompt as the task instruction.  Normalizing the
        # outer trailing newline keeps generated files deterministic without
        # changing the prompt itself.
        return text.rstrip() + "\n"
    lines = [line for line in text.rstrip().splitlines() if line.strip() != CLOSING_LINE]
    text = "\n".join(lines).rstrip() + "\n\n" + CLOSING_LINE + "\n"
    if re.search(r"[\u4e00-\u9fff]", text):
        row.warnings.append("instruction.md contains CJK characters; deep-swe instructions are English only")
    if re.search(r"https?://github\.com/", text):
        row.warnings.append("instruction.md contains a GitHub URL; remove it (agent works offline in /app)")
    return text


# --------------------------------------------------------------------------- commands
def parse_rows(spec: str | None, rows: list[Row]) -> list[Row]:
    if not spec:
        return rows
    wanted = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        wanted.update(range(int(a), int(b or a) + 1))
    return [r for r in rows if r.n in wanted]


def report(rows: list[Row]) -> dict:
    counts = {"ready": 0, "needs_fix": 0, "blocked": 0}
    for r in rows:
        counts[r.readiness] += 1
    return {"counts": counts, "rows": [{"row": r.n, "source_id": r.source_id,
                                       "title": r.get("title")[:50], "task_id": r.task_id,
                                       "readiness": r.readiness, "blockers": r.blockers, "warnings": r.warnings,
                                       "layout": r.layout} for r in rows]}


def load_overrides(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise SystemExit("overrides must be a JSON object whose values are objects")
    return data


def validate_batch(rows: list[Row]) -> None:
    by_task_id: dict[str, list[int]] = {}
    by_ext_id: dict[str, list[int]] = {}
    for row in rows:
        by_task_id.setdefault(row.task_id, []).append(row.n)
        by_ext_id.setdefault(row.ext_id, []).append(row.n)
    for label, groups in (("task_id", by_task_id), ("ext_id", by_ext_id)):
        for value, row_numbers in groups.items():
            if value and len(row_numbers) > 1:
                message = f"duplicate {label} {value!r} in rows {row_numbers}"
                for row in rows:
                    if row.n in row_numbers:
                        row.blockers.append(message)


def prepare_rows(args) -> tuple[list[Row], dict, Path | None, list[dict]]:
    rows = parse_rows(args.rows, read_ledger(Path(args.ledger), args.sheet))
    overrides = load_overrides(args.overrides)
    instr_dir = Path(args.instructions_dir) if args.instructions_dir else None
    prepared = []
    for row in rows:
        derive(row, overrides, args.org)
        if args.resolve:
            resolve(row, Path(args.repos_dir))
        check_license(row, overrides)
        ov = row_override(row, overrides)
        instruction = find_instruction(row, instr_dir, ov, args.instruction_mode)
        normalized = normalize_instruction(instruction, row, args.instruction_mode) if instruction else None
        if not normalized:
            source = "需求 Prompt（原文）" if args.instruction_mode == "original" else "instruction_en or --instructions-dir"
            row.blockers.append(f"instruction.md missing: provide {source}")
        dockerfile, todo, install = render_dockerfile(row, ov)
        if todo and not args.allow_incomplete:
            row.blockers.extend(todo)
        # Client rule: no unpinned pip / apt / go dependency may reach a task repo.
        row.blockers.extend(pin_lint.violations_as_text(pin_lint.lint_text(dockerfile)))
        if args.preflight and args.resolve and not row.blockers:
            preflight(row, Path(args.repos_dir), install, args.preflight_platform, args.preflight_timeout)
        prepared.append({"row": row, "override": ov, "instruction": normalized,
                         "dockerfile": dockerfile, "todo": todo, "install": install})
    validate_batch(rows)
    return rows, overrides, instr_dir, prepared


def cmd_check(args) -> int:
    rows, _overrides, _instr_dir, _prepared = prepare_rows(args)
    rep = report(rows)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"rows={len(rows)}  ready={rep['counts']['ready']}  needs_fix={rep['counts']['needs_fix']}  blocked={rep['counts']['blocked']}")
    from collections import Counter
    c = Counter(re.sub(r"\(.*?\)|'.*?'|\d+", "", x).strip() for r in rows for x in r.blockers + r.warnings)
    for k, v in c.most_common():
        print(f"{v:4}  {k}")
    if args.verbose:
        for r in rows:
            print(f"\n[{r.n}] {r.readiness:9} {r.task_id}  {r.get('title')[:40]}")
            for b in r.blockers:
                print(f"      BLOCK  {b}")
            for w in r.warnings:
                print(f"      warn   {w}")
    return 1 if rep["counts"]["blocked"] else 0


def cmd_gen_instructions(args) -> int:
    """Update only instruction.md files without resolving or rebuilding environments."""
    rows = parse_rows(args.rows, read_ledger(Path(args.ledger), args.sheet))
    overrides = load_overrides(args.overrides)
    instr_dir = Path(args.instructions_dir) if args.instructions_dir else None
    out = Path(args.out)
    prepared = []
    for row in rows:
        derive(row, overrides, args.org)
        ov = row_override(row, overrides)
        instruction = find_instruction(row, instr_dir, ov, args.instruction_mode)
        if instruction:
            instruction = normalize_instruction(instruction, row, args.instruction_mode)
        else:
            source = "需求 Prompt（原文）" if args.instruction_mode == "original" else "instruction_en or --instructions-dir"
            row.blockers.append(f"instruction.md missing: provide {source}")
        target = out / row.task_id
        if not target.is_dir():
            row.blockers.append(f"target task directory does not exist: {target}")
        elif (target / "instruction.md").exists() and not args.force:
            row.blockers.append("instruction.md exists; use --force to replace it")
        prepared.append((row, target, instruction))
    validate_batch(rows)
    if any(row.blockers for row in rows):
        for row, _target, _instruction in prepared:
            if row.blockers:
                print(f"[{row.n}] blocked            {row.task_id}")
                for reason in row.blockers:
                    print(f"      - {reason}")
        print("\nno files written: instruction batch preflight failed")
        return 1
    for row, target, instruction in prepared:
        (target / "instruction.md").write_text(instruction, encoding="utf-8")
        print(f"[{row.n}] updated            {row.task_id}/instruction.md")
    return 0


def cmd_gen(args) -> int:
    if args.only_instructions:
        return cmd_gen_instructions(args)
    rows, _overrides, _instr_dir, prepared = prepare_rows(args)
    out = Path(args.out)
    statuses = [{"row": r.n, "source_id": r.source_id, "task_id": r.task_id,
                 "status": r.readiness, "reasons": r.blockers + r.warnings} for r in rows]
    if any(r.blockers for r in rows):
        for status in statuses:
            print(f"[{status['row']}] {status['status']:18} {status['task_id']}")
            for reason in status["reasons"]:
                print(f"      - {reason}")
        print("\nno files written: batch preflight failed")
        return 1
    # Render every artifact before creating any directory. This keeps generation
    # all-or-nothing for template and serialization failures as well as validation.
    for item in prepared:
        item["task_toml"] = render_task_toml(
            item["row"], item["override"], args.org
        )
    existing = [out / item["row"].task_id for item in prepared
                if (out / item["row"].task_id).exists()]
    if existing and not args.force:
        for path in existing:
            print(f"blocked: target exists: {path}")
        print("\nno files written: batch preflight failed; use --force after reviewing existing targets")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for item in prepared:
        r = item["row"]
        tdir = out / r.task_id
        (tdir / "environment").mkdir(parents=True, exist_ok=True)
        (tdir / "task.toml").write_text(item["task_toml"], encoding="utf-8")
        (tdir / "environment" / "Dockerfile").write_text(item["dockerfile"], encoding="utf-8")
        (tdir / "instruction.md").write_text(item["instruction"], encoding="utf-8")
    if args.report:
        Path(args.report).write_text(json.dumps(statuses, ensure_ascii=False, indent=1), encoding="utf-8")
    for s in statuses:
        print(f"[{s['row']}] {s['status']:18} {s['task_id']}")
        for x in s["reasons"]:
            print(f"      - {x}")
    if args.report:
        print(f"\nreport: {args.report}")
    return 0 if all(not r.blockers for r in rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "gen"):
        p = sub.add_parser(name)
        p.add_argument("ledger")
        p.add_argument("--sheet")
        p.add_argument("--overrides")
        p.add_argument("--rows")
        p.add_argument("--org", default="swe")
        p.add_argument("--resolve", action="store_true")
        p.add_argument("--repos-dir", default="_repos")
        p.add_argument("--instructions-dir")
        p.add_argument("--instruction-mode", choices=("original", "english"), default="original")
        p.add_argument("--allow-incomplete", action="store_true")
        p.add_argument("--preflight", action="store_true",
                       help="with --resolve: dry-run the rendered install block inside the base image via docker")
        p.add_argument("--preflight-platform", default="linux/amd64",
                       help="docker --platform for --preflight (default linux/amd64, the Pier/Modal target)")
        p.add_argument("--preflight-timeout", type=int, default=1500, help="seconds per row for --preflight")
        if name == "check":
            p.add_argument("--json", help="write the full report here")
            p.add_argument("-v", "--verbose", action="store_true")
        else:
            p.add_argument("--out", required=True)
            p.add_argument("--force", action="store_true")
            p.add_argument("--only-instructions", action="store_true")
            p.add_argument("--report")
    args = ap.parse_args(argv)
    return cmd_check(args) if args.cmd == "check" else cmd_gen(args)


if __name__ == "__main__":
    sys.exit(main())
