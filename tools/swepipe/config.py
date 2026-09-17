"""Configuration.  Precedence, highest first:
  1. command-line flags
  2. process environment variables
  3. <root>/.env                (KEY=VALUE lines; secrets live here, never committed)
  4. <root>/pipeline.toml       (non-secret defaults, committed; see pipeline.toml.example)
  5. built-in defaults

Keys (environment / .env spelling; pipeline.toml uses lower-case sections, see the example):
  SOURCE                 feishu | json                      (default feishu)
  DELIVERY               repo | zip                         (default repo)
                         repo: verified tasks become GitHub repos, the URL is written to the row
                         zip:  verified tasks become <题目名称>.zip attached to the row (交付包规范)
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BASE_TOKEN, FEISHU_TABLE_ID
  JSON_SOURCE_PATH       ledger file for the json source   (default <work>/source.json)
  GITHUB_OWNER           user or org that owns the task repos (default: `gh api user`)
  GITHUB_TOKEN           token for that owner; when set it is passed to gh/git as GH_TOKEN, so the
                         local `gh auth login` account is not needed and can differ
  GITHUB_VISIBILITY      public | private                   (default public)
  PLATFORMS              comma list                         (default linux/arm64,linux/amd64)
  BUILD_JOBS             concurrent docker builds per host  (default 3); BUILD_JOBS_<ARCH> overrides it
                         for the host that builds that platform (e.g. BUILD_JOBS_AMD64=4 for the x86 box)
  BUILD_DIRECT           1 = bypass the proxy Docker injects into builds (default 0)
  DOCKER_HOST_<ARCH>     docker endpoint for that platform, e.g. DOCKER_HOST_AMD64=ssh://root@x86-box
                         (builds + smokes of linux/amd64 run there and count as native)
  MAX_IMAGE_GB           skip + mark rows above this size   (default 12)
  IMAGE_PREFIX           local docker tag prefix            (default swe-task-pipeline)
  CLOUD_AGENT_URL, CLOUD_AGENT_TOKEN   optional x86 verification host (tools/cloud_agent.py)
  TASKS_DIR, WORK_DIR, OVERRIDES, REPOS_DIR   paths (defaults under <root>)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # .../swe-task-pipeline

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

DEFAULT_PLATFORMS = "linux/arm64,linux/amd64"


def read_dotenv(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_toml(path: Path) -> dict[str, str]:
    """Flatten pipeline.toml into ENV-style keys: [github] owner -> GITHUB_OWNER."""
    if not path.exists():
        return {}
    if tomllib is None:
        sys.exit("pipeline.toml present but no TOML parser: use Python >= 3.11 or `pip install tomli`")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    flat = {}
    special = {("source", "type"): "SOURCE", ("source", "json_path"): "JSON_SOURCE_PATH",
               ("delivery", "mode"): "DELIVERY",
               ("build", "jobs"): "BUILD_JOBS", ("paths", "tasks"): "TASKS_DIR", ("paths", "work"): "WORK_DIR",
               ("paths", "overrides"): "OVERRIDES", ("paths", "repos"): "REPOS_DIR",
               ("build", "platforms"): "PLATFORMS", ("build", "max_image_gb"): "MAX_IMAGE_GB",
               ("build", "image_prefix"): "IMAGE_PREFIX",
               ("build", "direct"): "BUILD_DIRECT", ("build", "full_tests"): "FULL_TESTS"}
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        for k, v in values.items():
            key = special.get((section, k), f"{section}_{k}".upper())
            flat[key] = ",".join(map(str, v)) if isinstance(v, list) else str(v)
    return flat


@dataclass
class Config:
    values: dict = field(default_factory=dict)
    root: Path = ROOT

    @classmethod
    def load(cls, root: Path = ROOT, cli: dict | None = None) -> "Config":
        layers = [read_toml(root / "pipeline.toml"), read_dotenv(root / ".env"), dict(os.environ),
                  {k.upper(): str(v) for k, v in (cli or {}).items() if v not in (None, "", False)}]
        merged: dict = {}
        for layer in layers:
            merged.update({k: v for k, v in layer.items() if v is not None})
        return cls(values=merged, root=root)

    def get(self, key: str, default: str | None = None) -> str | None:
        v = self.values.get(key)
        return v if v not in (None, "") else default

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not self.get(k)]
        if missing:
            sys.exit(f"missing configuration {missing}; set them in the environment, {self.root / '.env'} "
                     f"or {self.root / 'pipeline.toml'} (see README.md)")

    # ---- typed accessors
    @property
    def source(self) -> str:
        return self.get("SOURCE", "feishu")

    @property
    def delivery(self) -> str:
        v = self.get("DELIVERY", "repo")
        if v not in ("repo", "zip"):
            sys.exit(f"DELIVERY={v!r}: expected repo or zip")
        return v

    @property
    def tasks_dir(self) -> Path:
        return Path(self.get("TASKS_DIR", str(self.root / "tasks")))

    @property
    def work_dir(self) -> Path:
        return Path(self.get("WORK_DIR", str(self.root / "feishu-sync" / "work")))

    @property
    def overrides_path(self) -> Path:
        return Path(self.get("OVERRIDES", str(self.root / "task-overrides.json")))

    @property
    def repos_dir(self) -> Path:
        return Path(self.get("REPOS_DIR", str(self.root / "feishu-sync" / "_repos")))

    @property
    def platforms(self) -> list[str]:
        return [p.strip() for p in self.get("PLATFORMS", DEFAULT_PLATFORMS).split(",") if p.strip()]

    @property
    def build_jobs(self) -> int:
        return int(self.get("BUILD_JOBS", "3"))

    def build_jobs_for(self, platform: str, default: int | None = None) -> int:
        """Concurrent builds for one platform's docker host: BUILD_JOBS_<ARCH> ([build] jobs_amd64 = 4),
        else the general BUILD_JOBS / --jobs value.  Platforms on the same host share one pool."""
        arch = platform.split("/")[-1].upper()
        v = self.get(f"BUILD_JOBS_{arch}")
        return int(v) if v else (default if default is not None else self.build_jobs)

    @property
    def full_tests(self) -> bool:
        """Run the repository's real test suite offline on the native platform (FULL_TESTS, default on)."""
        return str(self.get("FULL_TESTS", "1")).lower() not in ("0", "false", "no")

    @property
    def build_direct(self) -> bool:
        """Pass empty HTTP(S)_PROXY build-args so RUN steps skip the proxy Docker Desktop injects."""
        return str(self.get("BUILD_DIRECT", "0")).lower() in ("1", "true", "yes")

    def docker_host(self, platform: str) -> str | None:
        """Docker endpoint for one platform's builds and smokes, e.g. DOCKER_HOST_AMD64=ssh://root@x86-box
        ([build] docker_host_amd64 in pipeline.toml).  Unset = the local daemon.  A platform with its own
        host is treated as native there (the full test suite runs instead of the light emulated smoke)."""
        arch = platform.split("/")[-1].upper()
        return self.get(f"DOCKER_HOST_{arch}") or self.get(f"BUILD_DOCKER_HOST_{arch}")

    @property
    def max_image_gb(self) -> float:
        return float(self.get("MAX_IMAGE_GB", "12"))

    @property
    def image_prefix(self) -> str:
        return self.get("IMAGE_PREFIX", "swe-task-pipeline")

    @property
    def github_owner(self) -> str | None:
        return self.get("GITHUB_OWNER")

    @property
    def github_token(self) -> str | None:
        return self.get("GITHUB_TOKEN") or self.get("GH_TOKEN")

    @property
    def github_visibility(self) -> str:
        return self.get("GITHUB_VISIBILITY", "public")

    def describe(self) -> str:
        secret = ("SECRET", "TOKEN")
        shown = {k: ("***" if any(s in k for s in secret) else v) for k, v in sorted(self.values.items())
                 if k.startswith(("SOURCE", "DELIVERY", "FEISHU_", "JSON_", "GITHUB_", "GH_", "PLATFORMS", "BUILD_", "MAX_IMAGE",
                                  "IMAGE_PREFIX", "DOCKER_HOST_", "CLOUD_AGENT_", "TASKS_DIR", "WORK_DIR", "OVERRIDES", "REPOS_DIR"))}
        return "\n".join(f"  {k}={v}" for k, v in shown.items())
