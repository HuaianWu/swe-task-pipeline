"""Source-independent data model.

A ledger row becomes a TaskRecord.  Every adapter (Feishu, JSON file, a future database or API)
maps its own columns onto these fields; everything downstream only ever sees TaskRecords.

Status rules (the ledger is the only state store; runs are idempotent):
  candidate  review == REVIEW_PASS and output_url empty
  skipped    remark starts with one of SKIP_MARKERS (we wrote it on an earlier run)
  done       output_url filled

`output_url` is "the pipeline's product as recorded on the row".  With DELIVERY=repo it is the
GitHub repository URL; with DELIVERY=zip it is the name of the zip attached to the row (the
delivery-package column).  Either way: non-empty means done.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

REVIEW_PASS = "初检通过"
TOO_LARGE_MARK = "镜像过大，暂不上传。"
SKIP_MARKERS = (TOO_LARGE_MARK,)

# TaskRecord field -> ledger column understood by tools/xlsx2task.py (COLS aliases) and by the
# delivery packager (docs/delivery-package.md).  Columns unknown to xlsx2task are simply ignored.
LEDGER_COLUMN = {
    "title": "题目名称", "seed_type": "Type", "submitter": "提交人", "submitted_at": "提交日期",
    "repo_url": "Repo URL", "base_sha": "Commit/版本", "language": "主要语言", "task_type": "任务类型",
    "prompt": "需求 Prompt（原文）", "difficulty": "真实性与难度说明", "modules": "可能涉及模块",
    "rubric": "Verify Rubric", "result": "产物结果", "result_extra": "产物补充材料",
    "solution_commit_url": "Commit URL", "patch_file": ".patch文件", "solution_sha": "解法 Commit",
    "done": "是否完成需求",
    # delivery-package spec (交付包规范) columns
    "trae_session_id": "Trae Session ID", "effective_turns": "有效轮数", "effective_turns_estimate": "有效轮数（预估）",
    "harness": "Harness",
    "seed_model": "Seed 模型/版本", "notes": "备注",
    "trajectory_url": "轨迹文件链接", "screenshot_url": "证明图片链接", "package": "交付包（zip）",
}


@dataclass
class TaskRecord:
    """One ledger row.  `key` identifies the row in its source (Feishu record_id, DB primary key,
    API id); it is "" for rows synthesized from a local task dir that the source does not know."""
    key: str
    title: str
    repo_url: str
    base_sha: str
    language: str = ""
    task_type: str = ""
    prompt: str = ""
    rubric: str = ""
    seq: str = ""                 # human-facing row number, for messages only
    review: str = ""              # 初检结果
    remark: str = ""              # 初检备注 (we prepend SKIP markers here)
    output_url: str = ""          # repo URL (DELIVERY=repo) or attached zip name (DELIVERY=zip)
    seed_type: str = ""
    submitter: str = ""
    submitted_at: str = ""
    difficulty: str = ""
    modules: str = ""
    result: str = ""
    result_extra: str = ""
    solution_commit_url: str = ""
    patch_file: str = ""          # attachment name(s); the files themselves are in `attachments`
    solution_sha: str = ""
    done: str = ""                # 是否完成需求
    trae_session_id: str = ""
    effective_turns: str = ""     # kept as text; the packager writes an integer when it parses as one
    effective_turns_estimate: str = ""  # 有效轮数（预估）: used when 有效轮数 is empty (user decision 2026-09-12)
    harness: str = ""
    seed_model: str = ""
    notes: str = ""
    trajectory_url: str = ""      # http(s) link to the trajectory file (md / jsonl / json)
    screenshot_url: str = ""      # http(s) link(s) to proof screenshots
    package: str = ""             # name(s) of the delivery zip attached to the row
    attachments: dict = field(default_factory=dict)   # field -> [source-specific descriptors]; see LedgerSource.fetch_file
    extra: dict = field(default_factory=dict)         # anything source-specific, never used by the pipeline

    # ---- derived
    @property
    def norm_title(self) -> str:
        return norm_title(self.title)

    @property
    def is_candidate(self) -> bool:
        return self.review.strip() == REVIEW_PASS and not self.output_url.strip()

    @property
    def is_skipped(self) -> bool:
        r = self.remark.lstrip()
        return any(r.startswith(m) for m in SKIP_MARKERS)

    @property
    def source_id(self) -> str:
        """Stable id used as the key in task-overrides.json (same formula as xlsx2task)."""
        url = self.repo_url.removesuffix(".git").rstrip("/").lower()
        sha = self.base_sha.lower().strip()
        m = re.match(r"^([0-9a-f]{40})", sha)
        sha = m.group(1) if m else sha
        return "bz" + hashlib.sha256("|".join((url, sha, self.title.strip())).encode()).hexdigest()[:30]

    @property
    def base_sha40(self) -> str:
        """The bare 40-hex commit (the ledger sometimes annotates it: 'abc... (v1.2.3)')."""
        m = re.match(r"^\s*([0-9a-fA-F]{40})", self.base_sha)
        return m.group(1).lower() if m else self.base_sha.strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRecord":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        known.setdefault("extra", {})
        known.setdefault("attachments", {})
        for k in ("key", "title", "repo_url", "base_sha"):
            known.setdefault(k, "")
        return cls(**{k: (str(v) if k not in ("extra", "attachments") and v is not None else v) for k, v in known.items()})


def norm_title(s: str) -> str:
    """Titles are compared without whitespace and case (the table has both '为 X' and '为X')."""
    return re.sub(r"\s+", "", s or "").lower()
