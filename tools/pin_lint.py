#!/usr/bin/env python3
"""pin_lint — check environment/Dockerfile files against the client's pinning rule:
pip / apt / go get|install dependencies must carry an exact version (bare names, >=, ~=, ==1.*
all fail).  Exempt: the repo itself (`-e .`, `-e ".[extra]"`), the repo's own `-r`/`-c` files,
`go mod download`, lockfile installs (uv sync / poetry install / yarn / cargo).

  python3 tools/pin_lint.py [tasks-dir] [--json out]
"""
from __future__ import annotations
import json, re, shlex, sys
from pathlib import Path

EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?==\d[\w.]*(?:[+][\w.]+)?$")
STAR = re.compile(r"==.*\*")


def logical_lines(text: str):
    buf, n0 = "", 0
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            n0 = n
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        yield n0, (buf + line).strip()
        buf = ""
    if buf:
        yield n0, buf.strip()


def check_pip(args: list[str]) -> list[str]:
    bad, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("-r", "-c", "-e", "--index-url", "--extra-index-url", "--constraint", "--requirement", "--editable"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        if a.startswith((".", "/", "git+", "http")) or a.startswith("-e"):
            continue
        if re.search(r"@ *git\+.*@[0-9a-f]{40}$", a):  # `pkg @ git+url@<full sha>` is exact
            continue
        if not EXACT.match(a) or STAR.search(a):
            bad.append(a)
    return bad


def check_apt(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-") and "=" not in a]


def check_go(args: list[str]) -> list[str]:
    # packages of the repository itself (`go install ./cmd/foo`, `.`, `./...`) are exempt like `-e .`
    return [a for a in args if not a.startswith("-") and "/" in a and not a.startswith(("./", "../"))
            and not re.search(r"@v\d+\.\d+\.\d+", a)]


def lint_text(text: str) -> list[dict]:
    out = []
    for n, line in logical_lines(text):
        if not line.startswith("RUN"):
            continue
        for seg in re.split(r"\s*(?:&&|\|\||;)\s*", line[3:].strip()):
            seg = re.sub(r"^\(|\)$", "", seg.strip())
            try:
                toks = shlex.split(seg)
            except ValueError:
                toks = seg.split()
            if not toks:
                continue
            # strip env assignments / sudo / python -m prefixes
            while toks and re.match(r"^[A-Z_]+=", toks[0]):
                toks = toks[1:]
            if toks[:3] == ["python", "-m", "pip"]:
                toks = ["pip"] + toks[3:]
            if not toks:
                continue
            if toks[0] in ("pip", "pip3") and len(toks) > 1 and toks[1] == "install":
                if "--dry-run" in toks:
                    continue
                bad = check_pip(toks[2:])
                if bad:
                    out.append({"line": n, "kind": "pip", "bad": bad, "seg": seg})
            elif toks[0] == "apt-get" and "install" in toks:
                bad = check_apt(toks[toks.index("install") + 1:])
                if bad:
                    out.append({"line": n, "kind": "apt", "bad": bad, "seg": seg})
            elif toks[0] == "go" and len(toks) > 1 and toks[1] in ("get", "install"):
                bad = check_go(toks[2:])
                if bad:
                    out.append({"line": n, "kind": "go", "bad": bad, "seg": seg})
    return out


def lint(dockerfile: Path) -> list[dict]:
    return lint_text(dockerfile.read_text(encoding="utf-8"))


def violations_as_text(vs: list[dict]) -> list[str]:
    return [f"unpinned {v['kind']} dependency (line {v['line']}): {' '.join(v['bad'])}" for v in vs]


def main(argv):
    tasks = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("--") else Path(__file__).resolve().parent.parent / "tasks"
    report = {}
    for df in sorted(tasks.glob("*/environment/Dockerfile")):
        v = lint(df)
        if v:
            report[df.parent.parent.name] = v
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    kinds = {}
    for t, vs in report.items():
        for v in vs:
            kinds.setdefault(v["kind"], set()).add(t)
    total = len(list(tasks.glob("*/environment/Dockerfile")))
    print(f"tasks={total} violating={len(report)} " + " ".join(f"{k}={len(v)}" for k, v in sorted(kinds.items())))
    for t, vs in report.items():
        for v in vs:
            print(f"  {t:<52} {v['kind']:<4} {' '.join(v['bad'])[:90]}")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
