"""Ledger source adapters.

A source is anything that can list task rows and accept the write-backs the pipeline makes:
  set_output_url   DELIVERY=repo   the published repo URL goes on the row
  attach_output    DELIVERY=zip    the delivery zip is attached to the row
  prepend_remark   both            a skip marker in front of the remark
plus, for zip delivery, hand out the row's own files (patch attachment) via fetch_file.

Implement `LedgerSource` for a database or an HTTP API and register it in `get_source`; nothing
else in the pipeline changes.

    class MySource(LedgerSource):
        name = "mydb"
        def fetch(self) -> list[TaskRecord]: ...
        def get(self, key) -> TaskRecord | None: ...
        def set_output_url(self, key, url): ...
        def prepend_remark(self, key, text): ...
        def attach_output(self, key, path) -> str: ...      # only needed for DELIVERY=zip
        def fetch_file(self, descriptor, dest) -> Path: ... # only if attachments are not plain path/url
"""
from __future__ import annotations

import shutil
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Config
from ..model import TaskRecord


class LedgerSource(ABC):
    name = "abstract"

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def fetch(self) -> list[TaskRecord]:
        """Every row of the ledger (the pipeline does its own selection)."""

    @abstractmethod
    def get(self, key: str) -> TaskRecord | None:
        """Fresh copy of one row, or None when the row no longer exists."""

    @abstractmethod
    def set_output_url(self, key: str, url: str) -> None:
        """Record the published repository URL on the row (marks it done).  DELIVERY=repo."""

    @abstractmethod
    def prepend_remark(self, key: str, text: str) -> None:
        """Put `text` (a SKIP marker line) in front of the row's remark."""

    def attach_output(self, key: str, path: Path) -> str:
        """Attach the delivery zip to the row (marks it done) and return a reference to it.  DELIVERY=zip."""
        raise NotImplementedError(f"{self.name} source cannot attach files")

    def fetch_file(self, descriptor: dict, dest: Path) -> Path:
        """Materialise one entry of TaskRecord.attachments[...] at `dest`.
        Default descriptors: {"path": local file} or {"url": http(s) link}; adapters override for
        their own storage (Feishu: file_token)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if descriptor.get("path"):
            shutil.copyfile(descriptor["path"], dest)
        elif descriptor.get("url"):
            download(descriptor["url"], dest)
        else:
            raise ValueError(f"cannot fetch attachment {descriptor!r}")
        return dest

    def describe(self) -> str:
        return self.name


def download(url: str, dest: Path, timeout: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "swepipe/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


def get_source(config: Config) -> LedgerSource:
    from .feishu import FeishuSource
    from .jsonfile import JsonFileSource
    sources = {s.name: s for s in (FeishuSource, JsonFileSource)}
    name = config.source
    if name not in sources:
        raise SystemExit(f"unknown SOURCE={name!r}; available: {sorted(sources)}")
    return sources[name](config)
