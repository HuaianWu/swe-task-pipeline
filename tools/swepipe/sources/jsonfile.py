"""JSON-file adapter — the reference implementation for "some other system owns the ledger".

File shape (JSON_SOURCE_PATH, default <work>/source.json):
    {"records": [ {TaskRecord fields ...}, ... ]}
Required per record: key, title, repo_url, base_sha (40-hex), language, task_type, prompt.
Optional: rubric, seq, review (must equal "初检通过" for the row to be processed), remark,
output_url, and the remaining TaskRecord fields.  The pipeline writes output_url / remark back
into the same file, so an API or database integration can be as small as: export rows to this
file, run the pipeline, import output_url / remark back.

Attachments (DELIVERY=zip): "attachments": {"patch_file": [{"path": "/local/x.patch"}]} or
{"url": "https://..."}.  attach_output stores the zip path in "package" and its name in
"output_url".
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..model import TaskRecord
from . import LedgerSource


class JsonFileSource(LedgerSource):
    name = "json"

    def __init__(self, config: Config):
        super().__init__(config)
        self.path = Path(config.get("JSON_SOURCE_PATH", str(config.work_dir / "source.json")))
        if not self.path.exists():
            raise SystemExit(f"JSON_SOURCE_PATH {self.path} does not exist")

    def describe(self) -> str:
        return f"json file {self.path}"

    def _load(self) -> list[dict]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["records"] if isinstance(data, dict) else data

    def _save(self, rows: list[dict]) -> None:
        self.path.write_text(json.dumps({"records": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    def fetch(self) -> list[TaskRecord]:
        return [TaskRecord.from_dict(r) for r in self._load()]

    def get(self, key: str) -> TaskRecord | None:
        for r in self._load():
            if str(r.get("key")) == key:
                return TaskRecord.from_dict(r)
        return None

    def _update(self, key: str, **fields) -> None:
        rows = self._load()
        for r in rows:
            if str(r.get("key")) == key:
                r.update(fields)
                self._save(rows)
                return
        raise SystemExit(f"json source: row {key} not found")

    def set_output_url(self, key: str, url: str) -> None:
        self._update(key, output_url=url)

    def prepend_remark(self, key: str, text: str) -> None:
        live = self.get(key)
        self._update(key, remark=text + "\n" + (live.remark if live else ""))

    def attach_output(self, key: str, path: Path) -> str:
        self._update(key, output_url=Path(path).name, package=str(path))
        return str(path)
