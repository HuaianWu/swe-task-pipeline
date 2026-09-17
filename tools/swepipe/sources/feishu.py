"""Feishu (Lark) Bitable adapter.

Configuration: FEISHU_APP_ID, FEISHU_APP_SECRET (tenant app credentials), FEISHU_BASE_TOKEN (the
Bitable app token from its URL), FEISHU_TABLE_ID.  Column names are the ledger's Chinese headers
(see FIELDS / ALIASES); rename there if the table changes.

Status column: with DELIVERY=repo the row is done when "SWE-like Image Repo URL" is filled; with
DELIVERY=zip when the attachment column "交付包（zip）" holds a file.  Both map onto
TaskRecord.output_url so the selection logic is the same.

Attachments (.patch文件, 交付包) are kept in TaskRecord.attachments[field] as the raw Bitable
descriptors ({file_token, name, size, ...}); fetch_file downloads one, attach_output uploads the
delivery zip (drive upload_all, <= 20 MB) and links it to the row.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

SUBMIT_TZ = ZoneInfo("Asia/Shanghai")   # Feishu shows 提交日期 in the tenant's zone
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ..config import Config
from ..model import LEDGER_COLUMN, TaskRecord
from . import LedgerSource

FIELDS = {  # TaskRecord field -> Bitable column
    **LEDGER_COLUMN,
    "seq": "序号", "review": "初检结果", "remark": "初检备注", "output_url": "SWE-like Image Repo URL",
}
PACKAGE_COLUMN = LEDGER_COLUMN["package"]          # 交付包（zip）
# Alternative column names seen in newer tables; the first one present in the table wins.
ALIASES = {"seq": ["序号编码"]}
# Columns the pipeline cannot work without: selection reads review/output_url/remark, delivery
# writes output_url/remark.  A table missing one of these is refused instead of treating every
# row as new.
REQUIRED = ("title", "repo_url", "base_sha", "review", "output_url", "remark")
API = "https://open.feishu.cn/open-apis"
UPLOAD_ALL_LIMIT = 20 * 1024 * 1024


def cell_text(value) -> str:
    """Flatten a Bitable cell (text segments, url objects, users, attachments, numbers) to a string."""
    if value is None:
        return ""
    if isinstance(value, list):
        if value and isinstance(value[0], dict) and "file_token" in value[0]:
            return ", ".join(a.get("name", "") for a in value)
        return "".join(cell_text(v) for v in value)
    if isinstance(value, dict):
        if "link" in value:
            return str(value.get("link") or value.get("text") or "")
        if "text" in value:
            return str(value["text"])
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_attachment(value) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict) and "file_token" in value[0]


class FeishuSource(LedgerSource):
    name = "feishu"

    def __init__(self, config: Config):
        super().__init__(config)
        config.require("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BASE_TOKEN", "FEISHU_TABLE_ID")
        self.app_id, self.app_secret = config.get("FEISHU_APP_ID"), config.get("FEISHU_APP_SECRET")
        self.base, self.table = config.get("FEISHU_BASE_TOKEN"), config.get("FEISHU_TABLE_ID")
        self.delivery = config.delivery
        self.wanted = dict(FIELDS)
        if self.delivery == "zip":
            self.wanted["output_url"] = PACKAGE_COLUMN
        self._token, self._token_at = None, 0.0
        self._fields: dict[str, str] | None = None   # resolved TaskRecord field -> column (after aliases)

    def describe(self) -> str:
        return f"feishu bitable {self.base}/{self.table} (status column: {self.wanted['output_url']})"

    # ---- http
    def _token_value(self) -> str:
        if self._token and time.time() - self._token_at < 3600:
            return self._token
        d = self._call(f"{API}/auth/v3/tenant_access_token/internal",
                       {"app_id": self.app_id, "app_secret": self.app_secret}, auth=False)
        self._token, self._token_at = d["tenant_access_token"], time.time()
        return self._token

    def _call(self, url: str, payload=None, method: str | None = None, auth: bool = True,
              raw_body: bytes | None = None, content_type: str | None = None) -> dict:
        headers = {"Content-Type": content_type or "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = "Bearer " + self._token_value()
        body = raw_body if raw_body is not None else (
            json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
        req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"feishu {last} for {url}")
            except urllib.error.URLError as e:
                last = str(e)
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"feishu request failed for {url}: {e}")
        if data.get("code", 0) != 0:
            raise SystemExit(f"feishu error {data.get('code')} for {url}: {data.get('msg')}")
        return data

    def _records_url(self, record_id: str = "") -> str:
        return f"{API}/bitable/v1/apps/{self.base}/tables/{self.table}/records" + (f"/{record_id}" if record_id else "")

    # ---- mapping
    def columns(self) -> dict[str, str]:
        """Resolve the wanted columns against the live table schema (aliases applied); refuse
        tables missing a REQUIRED column."""
        if self._fields is None:
            d = self._call(f"{API}/bitable/v1/apps/{self.base}/tables/{self.table}/fields?page_size=200")["data"]
            present = {f["field_name"] for f in d.get("items", [])}
            resolved = {}
            for attr, col in self.wanted.items():
                for name in [col, *ALIASES.get(attr, [])]:
                    if name in present:
                        resolved[attr] = name
                        break
            missing = [self.wanted[a] for a in REQUIRED if a not in resolved]
            if missing:
                raise SystemExit(f"feishu table {self.table} lacks required column(s): {missing}. "
                                 "Add them to the table (or rename via FIELDS/ALIASES in sources/feishu.py).")
            self._fields = resolved
        return self._fields

    def _to_record(self, item: dict) -> TaskRecord:
        f = item.get("fields", {})
        values = {attr: "" for attr in FIELDS}
        attachments = {}
        for attr, col in self.columns().items():
            v = f.get(col)
            if attr == "submitted_at" and isinstance(v, (int, float)):
                # the table shows dates in the tenant's zone (Beijing); UTC would shift early-morning
                # submissions to the previous day and the package date must match the table
                v = dt.datetime.fromtimestamp(v / 1000, SUBMIT_TZ).strftime("%Y-%m-%d")
            if is_attachment(v):
                attachments[attr] = [{k: a.get(k) for k in ("file_token", "name", "size", "type")} for a in v]
            values[attr] = cell_text(v)
        return TaskRecord(key=item["record_id"], attachments=attachments, **values)

    # ---- LedgerSource
    def fetch(self) -> list[TaskRecord]:
        items, page_token = [], None
        while True:
            d = self._call(self._records_url() + "?page_size=500" + (f"&page_token={page_token}" if page_token else ""))["data"]
            items.extend(d.get("items", []))
            if not d.get("has_more"):
                break
            page_token = d["page_token"]
        recs = [self._to_record(i) for i in items]
        return sorted(recs, key=lambda r: float(r.seq or 0))

    def get(self, key: str) -> TaskRecord | None:
        try:
            return self._to_record(self._call(self._records_url(key))["data"]["record"])
        except SystemExit as e:
            if "RecordIdNotFound" in str(e) or "1254043" in str(e):
                return None
            raise

    def set_output_url(self, key: str, url: str) -> None:
        if self.delivery == "zip":
            raise SystemExit("DELIVERY=zip: the status column is an attachment column; use attach_output")
        self._call(self._records_url(key), {"fields": {self.columns()["output_url"]: url}}, method="PUT")

    def prepend_remark(self, key: str, text: str) -> None:
        live = self.get(key)
        if live is None:
            raise SystemExit(f"feishu row {key} no longer exists")
        self._call(self._records_url(key), {"fields": {self.columns()["remark"]: text + "\n" + live.remark}}, method="PUT")

    # ---- attachments
    def fetch_file(self, descriptor: dict, dest: Path) -> Path:
        tok = descriptor.get("file_token")
        if not tok:
            return super().fetch_file(descriptor, dest)
        extra = urllib.parse.quote(json.dumps({"bitablePerm": {"tableId": self.table, "rev": 1}}))
        d = self._call(f"{API}/drive/v1/medias/batch_get_tmp_download_url?file_tokens={tok}&extra={extra}")
        url = d["data"]["tmp_download_urls"][0]["tmp_download_url"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token_value()})
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        return dest

    def upload_file(self, path: Path) -> str:
        """Upload a local file into the Bitable's drive space; returns its file_token."""
        size = path.stat().st_size
        if size > UPLOAD_ALL_LIMIT:
            raise SystemExit(f"{path.name} is {size / 1e6:.1f} MB; feishu upload_all allows 20 MB")
        boundary = "----swepipe" + uuid.uuid4().hex
        fields = {"file_name": path.name, "parent_type": "bitable_file", "parent_node": self.base,
                  "size": str(size), "extra": json.dumps({"drive_route_token": self.base})}
        parts = []
        for k, v in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                      f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        d = self._call(f"{API}/drive/v1/medias/upload_all", raw_body=b"".join(parts),
                       content_type=f"multipart/form-data; boundary={boundary}", method="POST")
        return d["data"]["file_token"]

    def attach_output(self, key: str, path: Path) -> str:
        col = self.columns().get("package") or (self.columns()["output_url"] if self.delivery == "zip" else None)
        if not col:
            raise SystemExit(f"feishu table {self.table} has no '{PACKAGE_COLUMN}' column")
        token = self.upload_file(Path(path))
        self._call(self._records_url(key), {"fields": {col: [{"file_token": token}]}}, method="PUT")
        return token
