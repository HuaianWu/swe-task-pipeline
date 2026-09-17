"""build: docker build + smoke test + size for every task and platform, N concurrent.

Per (task, platform):
  1. `docker build --platform <p> -t <IMAGE_PREFIX>/<task_id>:<arch> tasks/<task_id>/environment`
  2. measure disk usage inside the image (du), the number the size rule is applied to
  3. run the smoke command with `bash -c` (never a login shell: mars-base's /etc/profile resets
     PATH and would hide venvs added via ENV PATH) inside `--network none`
  4. delete the image when everything passed; failed images are kept for diagnosis
Results accumulate in <work>/build-results.json: {task_id: {platform: {status, size_gb, ...}}}.
Tasks whose Dockerfile + smoke plan are byte-identical (several ledger rows on one upstream commit)
are verified once: the first is built, the others record the same result with `reused_from`.
Statuses: ok | build_failed | smoke_failed | error; a failed smoke carries reason = environment | tests | timeout.

Smoke commands: SMOKE by language, or the `smoke` key of the task's task-overrides.json entry.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import platform as platform_module
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Config

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import pin_lint  # noqa: E402

# Light smoke: proves the toolchain and dependencies are installed (used on the emulated platform).
SMOKE = {
    "python": "python -m pytest --collect-only -q -p no:cacheprovider 2>&1 | tail -n 5",
    "go": "go build ./... 2>&1 | tail -n 20",
    # the Cargo manifest may live below the repo root (e.g. src-tauri/Cargo.toml)
    "rust": "M=$(ls Cargo.toml */Cargo.toml 2>/dev/null | head -n 1); test -n \"$M\" && "
            "cargo metadata --manifest-path \"$M\" --format-version 1 --offline >/dev/null 2>&1 && echo cargo-metadata-ok",
    "typescript": "node -e \"require('./package.json'); console.log('node ok')\"",
    "javascript": "node -e \"require('./package.json'); console.log('node ok')\"",
    "java": "true",
}


def sh(cmd: str, log, timeout: int | None = None) -> int:
    log.write(f"\n$ {cmd}\n")
    log.flush()
    try:
        return subprocess.run(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log.write(f"\nTIMEOUT after {timeout}s\n")
        return 124


def parse_size_gb(text: str) -> float | None:
    m = re.match(r"([\d.]+)\s*([kMGT]?B)", text)
    return round(float(m.group(1)) * {"B": 1e-9, "kB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1e3}[m.group(2)], 2) if m else None


def image_disk_gb(tag: str, platform: str, log) -> float | None:
    """Unpacked filesystem size measured with du inside a container (docker's own size numbers
    depend on the image store and unpack state, so they are not comparable across builds)."""
    cmd = (f"docker run --rm --network none --platform {platform} {tag} "
           "du -sxk / --exclude=/proc --exclude=/sys --exclude=/dev 2>/dev/null | cut -f1")
    log.write(f"\n$ {cmd}\n")
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return None
    digits = re.findall(r"\d+", out.stdout)
    gb = round(int(digits[-1]) / 1e6, 2) if digits else None
    log.write(f"disk usage: {gb} GB\n")
    if gb is None:
        images = subprocess.run(["docker", "images", "--format", "{{.Size}}", tag], capture_output=True, text=True)
        gb = parse_size_gb(images.stdout.strip().splitlines()[0]) if images.stdout.strip() else None
    return gb


def task_language(task_dir: Path) -> str:
    m = re.search(r'^language = "(.*)"$', (task_dir / "task.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def smoke_overrides(overrides_path: Path) -> dict[str, str | dict]:
    if not overrides_path.exists():
        return {}
    data = json.loads(overrides_path.read_text(encoding="utf-8"))
    return {v["task_id"]: v["smoke"] for v in data.values() if isinstance(v, dict) and v.get("task_id") and v.get("smoke")}


INTERNAL_NETWORK = "swepipe-internal"   # docker network created with --internal: a NIC, no route out


def smoke_network(task_dir: Path, overrides_path: Path) -> str:
    """`--network` value for the smoke container: `none` unless the task's override sets
    smoke_network = "internal" (suites that need a non-loopback interface, e.g. pion/ice, still
    run offline on a docker network created with --internal)."""
    if not overrides_path.exists():
        return "none"
    data = json.loads(overrides_path.read_text(encoding="utf-8"))
    for v in data.values():
        if isinstance(v, dict) and v.get("task_id") == task_dir.name and v.get("smoke_network") == "internal":
            subprocess.run(["docker", "network", "inspect", INTERNAL_NETWORK], capture_output=True) .returncode == 0 or \
                subprocess.run(["docker", "network", "create", "--internal", INTERNAL_NETWORK], capture_output=True)
            return INTERNAL_NETWORK
    return "none"


def smoke_command(task_dir: Path, overrides_path: Path) -> str:
    ov = smoke_overrides(overrides_path).get(task_dir.name)
    if isinstance(ov, dict):
        ov = ov.get("native") or ov.get("emulated")
    return ov or SMOKE.get(task_language(task_dir), "true")


# Full smoke (the client's runnability bar, 2026-09-11): the repository's real test suite must run in an
# offline container without docker or external services.  It runs on the host's native platform (the
# emulated one keeps the light smoke: 2-3x slower, same environment).  Big suites need a per-task
# `smoke` override that narrows to the task-relevant modules within the 3600 s budget.
SMOKE_FULL = {
    "python": "python -m pytest -q -p no:cacheprovider 2>&1 | tail -n 40",
    "go": "go test ./... -count=1 2>&1 | tail -n 40",
}
# Failure signatures that mean "the environment is incomplete" rather than "a test is red".
ENV_FAILURE = re.compile(r"Connection refused|Errno 111|ConnectionRefused|could not connect to server|"
                         r"No such file or directory: 'docker'|docker: not found|FileNotFoundError: .*docker|"
                         r"Name or service not known|Temporary failure in name resolution|getaddrinfo|"
                         r"Network is unreachable|ModuleNotFoundError|ImportError|cannot find package|"
                         r"no required module|OperationalError|redis\.exceptions\.ConnectionError|"
                         r"no tests collected|INTERNALERROR|not found in `markers`", re.I)


def native_platform() -> str:
    m = platform_module.machine().lower()
    return "linux/arm64" if m in ("arm64", "aarch64") else "linux/amd64"


def smoke_plan(task_dir: Path, overrides_path: Path, platform: str, full_tests: bool) -> tuple[str, str]:
    """(kind, command) for this platform: an explicit override wins everywhere; otherwise the native
    platform runs the full suite and the emulated one the light smoke."""
    override = smoke_overrides(overrides_path).get(task_dir.name)
    if isinstance(override, dict):
        # {"native": cmd, "emulated": cmd}: heavy suites run only where they are fast enough
        override = override.get("native" if platform == native_platform() else "emulated")
    if override:
        return "override", override
    lang = task_language(task_dir)
    if full_tests and platform == native_platform() and lang in SMOKE_FULL:
        return "full", SMOKE_FULL[lang]
    return "light", SMOKE.get(lang, "true")


def classify_smoke_failure(log_tail: str, rc: int) -> str:
    if rc == 124 or "TIMEOUT after" in log_tail:
        return "timeout"
    return "environment" if ENV_FAILURE.search(log_tail) else "tests"


# Builds fetch from GitHub / proxy.golang.org / PyPI at build time; behind a flaky proxy (Docker Desktop
# forwards container traffic through the host's system proxy) TLS handshakes and downloads fail at
# random.  A build whose log ends with one of these signatures is retried before being recorded.
NETWORK_ERROR = re.compile(r"gnutls_handshake|TLS connection was non-properly terminated|SSL_ERROR|"
                           r"unable to access 'https?://|proxy\.golang\.org.*(EOF|timeout|reset)|"
                           r"storage\.googleapis\.com.*(EOF|timeout|reset)|sum\.golang\.org.*(EOF|timeout|reset)|"
                           r"Get \"https?://[^\"]+\": (EOF|.*timeout|.*reset)|Could not resolve host|"
                           r"Connection reset by peer|Temporary failure in name resolution|"
                           r"ReadTimeoutError|Failed to establish a new connection|from versions: none\)|Read timed out|"
                           r"GnuTLS recv error|fetch-pack: invalid index-pack output|early EOF|HTTP/2 stream .* not closed cleanly|curl: \((18|28|35|56)\)|"
                           r"verifying module: .*(EOF|timeout|reset)|dial tcp.*(timeout|refused|reset)")
NETWORK_RETRIES = 4
NETWORK_BACKOFF = 60


def build_one(task_id: str, platform: str, config: Config, logs: Path, timeout: int, keep: bool) -> dict:
    arch = platform.split("/")[-1]
    tag = f"{config.image_prefix}/{task_id}:{arch}"
    task_dir = config.tasks_dir / task_id
    started = time.time()
    result = {"task_id": task_id, "platform": platform, "tag": tag, "status": "build_failed",
              "size_gb": None, "seconds": 0, "log": str(logs / f"{task_id}-{arch}.log")}
    # Docker Desktop forwards the host proxy into every RUN step; when that proxy is flaky the
    # empty predefined build-args make the build go direct (measured 6/6 vs 2/12 on 2026-09-09).
    proxy_args = " --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy=" if config.build_direct else ""
    with open(result["log"], "w", encoding="utf-8") as log:
        for attempt in range(1, NETWORK_RETRIES + 2):
            rc = sh(f"docker build --platform {platform} --progress=plain{proxy_args} -t {tag} {task_dir / 'environment'}", log, timeout)
            if rc == 0 or attempt > NETWORK_RETRIES:
                break
            log.flush()
            tail = Path(result["log"]).read_text(encoding="utf-8", errors="replace")[-20000:]
            if not NETWORK_ERROR.search(tail):
                break
            log.write(f"\nRETRY {attempt}: transient network error during docker build, retrying in {NETWORK_BACKOFF}s\n")
            log.flush()
            time.sleep(NETWORK_BACKOFF)
        if rc == 0:
            result["size_gb"] = image_disk_gb(tag, platform, log)
            kind, smoke = smoke_plan(task_dir, config.overrides_path, platform, config.full_tests)
            result["smoke"] = kind
            log.write(f"\nSMOKE ({kind}): {smoke}\n")
            # Named so a timeout can remove the container: killing the docker client alone leaves the
            # test process running inside the VM, eating the memory of the builds that follow.
            cname = f"smoke-{task_dir.name}-{arch}"
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
            net = smoke_network(task_dir, config.overrides_path)
            if net != "none":
                log.write(f"SMOKE NETWORK: {net} (docker --internal network: interface present, no route out)\n")
            rc = sh(f"docker run --rm --name {cname} --network {net} --platform {platform} {tag} bash -c "
                    # 3600 s: `go build ./...` of large Go trees (toolhive, bigquery-emulator) exceeds
                    # 30 min under amd64 emulation when sharing the VM with two other builds.
                    f"{shlex.quote('set -o pipefail; ' + smoke)}", log, 3600)
            if rc == 124:
                subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
            result["status"] = "ok" if rc == 0 else "smoke_failed"
            if rc != 0:
                log.flush()
                result["reason"] = classify_smoke_failure(Path(result["log"]).read_text(encoding="utf-8", errors="replace")[-20000:], rc)
        result["seconds"] = int(time.time() - started)
        log.write(f"\nRESULT {result['status']} size={result['size_gb']}GB {result['seconds']}s\n")
        if not keep and result["status"] == "ok":
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
    return result


def task_fingerprint(task_dir: Path, overrides_path: Path, platforms: list[str], full_tests: bool) -> str:
    """Identity of what `build` verifies for a task: the Dockerfile bytes plus the smoke plan and
    network for every platform.  Tasks with equal fingerprints (same repo, commit and recipe, e.g.
    several ledger rows on one upstream commit) produce byte-identical images, so one verification
    covers them all."""
    h = hashlib.sha256((task_dir / "environment" / "Dockerfile").read_bytes())
    for p in platforms:
        h.update(repr(smoke_plan(task_dir, overrides_path, p, full_tests)).encode())
    h.update(smoke_network(task_dir, overrides_path).encode())
    return h.hexdigest()[:16]


def group_by_fingerprint(todo: list[tuple[str, str]], fingerprints: dict[str, str]) -> dict[tuple[str, str], list[str]]:
    """{(fingerprint, platform): [task_ids...]} preserving order; the first task of each group is built,
    the others reuse its result."""
    groups: dict[tuple[str, str], list[str]] = {}
    for t, p in todo:
        groups.setdefault((fingerprints[t], p), []).append(t)
    return groups


def load_results(work: Path) -> dict:
    p = work / "build-results.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def verified(results: dict, task_id: str, platforms: list[str]) -> bool:
    return all(results.get(task_id, {}).get(p, {}).get("status") == "ok" for p in platforms)


def run_build(config: Config, task_ids: list[str], platforms: list[str], jobs: int, timeout: int,
              rebuild: bool, keep_images: bool) -> int:
    work, tasks_dir = config.work_dir, config.tasks_dir
    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    missing = [t for t in task_ids if not (tasks_dir / t / "environment" / "Dockerfile").exists()]
    if missing:
        sys.exit(f"no generated task dir for {missing}; run `gen` first")
    unpinned = {t: v for t in task_ids if (v := pin_lint.lint(tasks_dir / t / "environment" / "Dockerfile"))}
    if unpinned:
        for t, v in unpinned.items():
            print(f"  unpinned      {t}: " + "; ".join(pin_lint.violations_as_text(v)))
        sys.exit(f"{len(unpinned)} task(s) violate the pinning rule; fix the Dockerfile before building")
    results_path = work / "build-results.json"
    results = load_results(work)
    todo = [(t, p) for t in task_ids for p in platforms
            if rebuild or results.get(t, {}).get(p, {}).get("status") != "ok"]
    fingerprints = {t: task_fingerprint(tasks_dir / t, config.overrides_path, platforms, config.full_tests) for t in task_ids}
    lock = threading.Lock()

    def record(t: str, p: str, res: dict) -> None:
        res["at"] = dt.datetime.now().isoformat(timespec="seconds")
        res["fingerprint"] = fingerprints[t]
        with lock:
            results.setdefault(t, {})[p] = res
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        extra = f"  smoke={res.get('smoke')}" + (f" ({res['reason']})" if res.get("reason") else "")
        if res.get("reused_from"):
            extra += f"  reused from {res['reused_from']}"
        print(f"  {res['status']:<13} {t:<48} {p:<12} {res.get('size_gb')} GB  {res.get('seconds')}s{extra}", flush=True)

    def reuse(res: dict, t: str, source: str) -> dict:
        out = {k: v for k, v in res.items() if k not in ("at", "fingerprint")}
        out.update(task_id=t, tag=f"{config.image_prefix}/{t}:{res['platform'].split('/')[-1]}", reused_from=source)
        return out

    # An earlier ok result of any task with the same fingerprint (same image, same smoke plan) already
    # verifies this (task, platform): reuse it instead of building, unless --rebuild.
    if not rebuild:
        verified_by = {(r["fingerprint"], p): (tid, r) for tid, per in results.items() for p, r in per.items()
                       if r.get("status") == "ok" and r.get("fingerprint")}
        for t, p in list(todo):
            hit = verified_by.get((fingerprints[t], p))
            if hit and hit[0] != t:
                record(t, p, reuse(hit[1], t, hit[0]))
                todo.remove((t, p))
    groups = group_by_fingerprint(todo, fingerprints)
    reps = [(members[0], p) for (_, p), members in groups.items()]
    print(f"build: {len(reps)} jobs ({len(task_ids)} tasks x {platforms}, {len(todo) - len(reps)} reuse an identical "
          f"Dockerfile), {jobs} concurrent", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(build_one, t, p, config, logs, timeout, keep_images): (t, p) for t, p in reps}
        for fut in concurrent.futures.as_completed(futs):
            t, p = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # pragma: no cover
                res = {"task_id": t, "platform": p, "status": "error", "error": str(e), "size_gb": None, "seconds": 0}
            record(t, p, res)
            for other in groups[(fingerprints[t], p)][1:]:
                record(other, p, reuse(res, other, t))
            if not keep_images:
                subprocess.run("docker builder prune -f --keep-storage 40GB", shell=True, capture_output=True)
    print(f"results: {results_path}")
    return 0 if all(verified(results, t, platforms) for t in task_ids) else 1
