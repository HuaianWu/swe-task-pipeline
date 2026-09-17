"""Row selection and de-duplication — pure functions, no I/O.

    selection = select_records(records, local_titles, all_local=False)

Rules (in this order, per row):
  * rows that are not candidates are ignored (unless all_local and the title has a local task dir,
    in which case they are included as `existing` so the recipe can be regenerated / re-pushed)
  * skipped rows (remark carries a SKIP marker) are ignored
  * title matches a local task dir           -> existing (reuse the dir: build / publish only)
  * title already has an output_url elsewhere -> duplicate, ignored
  * title seen earlier in this batch          -> duplicate, ignored
  * otherwise                                 -> new
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import TaskRecord, norm_title


@dataclass
class Selected:
    record: TaskRecord
    existing_task_id: str = ""      # set when a local task dir is reused
    local_only: bool = False        # synthesized from a local dir the source does not know

    @property
    def existing(self) -> bool:
        return bool(self.existing_task_id)


@dataclass
class Selection:
    selected: list[Selected] = field(default_factory=list)
    duplicates: list[tuple[str, str, str]] = field(default_factory=list)   # (seq, why, ref)
    counts: dict = field(default_factory=dict)


def local_titles(tasks_dir: Path) -> dict[str, str]:
    """normalized original_title -> task_id for every generated task dir."""
    out = {}
    for toml in sorted(tasks_dir.glob("*/task.toml")):
        m = re.search(r'^original_title = "(.*)"$', toml.read_text(encoding="utf-8"), re.M)
        if m:
            out[norm_title(json.loads('"' + m.group(1) + '"'))] = toml.parent.name
    return out


def local_record(tasks_dir: Path, task_id: str) -> TaskRecord:
    """A TaskRecord reconstructed from task.toml + instruction.md (for dirs the source lacks)."""
    toml = (tasks_dir / task_id / "task.toml").read_text(encoding="utf-8")
    get = lambda k: (re.search(rf'^{k} = "(.*)"$', toml, re.M) or [None, ""])[1]  # noqa: E731
    return TaskRecord(key="", seq=f"local:{task_id}", title=json.loads('"' + get("original_title") + '"'),
                      repo_url=get("repository_url"), base_sha=get("base_commit_hash"), language=get("language"),
                      task_type=get("category"),
                      prompt=(tasks_dir / task_id / "instruction.md").read_text(encoding="utf-8").strip())


def select_records(records: list[TaskRecord], local: dict[str, str], all_local: bool = False,
                   tasks_dir: Path | None = None) -> Selection:
    filled = {r.norm_title: r.output_url.strip() for r in records if r.output_url.strip()}
    counts = {"total": len(records), "review_pass": 0, "candidate": 0, "skipped_marker": 0,
              "dup_local": 0, "dup_filled": 0, "dup_batch": 0, "local_only": 0, "selected": 0}
    sel = Selection(counts=counts)
    seen: dict[str, str] = {}
    for r in records:
        key = r.norm_title
        if r.review.strip() == "初检通过":
            counts["review_pass"] += 1
        if not r.is_candidate:
            if all_local and key in local and key not in seen:
                seen[key] = r.seq
                sel.selected.append(Selected(r, existing_task_id=local[key]))
            continue
        counts["candidate"] += 1
        if r.is_skipped:
            counts["skipped_marker"] += 1
            continue
        if key in local:
            counts["dup_local"] += 1
            sel.duplicates.append((r.seq, "local task exists (reused, not regenerated)", local[key]))
            seen[key] = r.seq
            sel.selected.append(Selected(r, existing_task_id=local[key]))
            continue
        if key in filled:
            counts["dup_filled"] += 1
            sel.duplicates.append((r.seq, "another row already has a URL", filled[key]))
            continue
        if key in seen:
            counts["dup_batch"] += 1
            sel.duplicates.append((r.seq, "duplicate title within this batch", f"#{seen[key]}"))
            continue
        seen[key] = r.seq
        sel.selected.append(Selected(r))
    if all_local and tasks_dir is not None:
        in_source = {r.norm_title for r in records}
        for key, tid in sorted(local.items()):
            if key not in in_source:
                sel.selected.append(Selected(local_record(tasks_dir, tid), existing_task_id=tid, local_only=True))
                counts["local_only"] += 1
    counts["selected"] = len(sel.selected)
    return sel
