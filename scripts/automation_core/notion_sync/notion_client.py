"""真实 Notion REST 客户端（标准库 urllib）。实现 projector.NotionClient 接口。

- 凭证只从 env 文件解析（默认 ~/.agents/secrets/notion-memoryhub.env），只需
  NOTION_TOKEN 与 NOTION_DATABASE_ID；token 从不出现在异常、日志或 repr 里。
- 429 / 5xx / 网络错误 → RetryableNotionError（投影器按退避重试）；
  400 / 401 / 403 / 404 → PermanentProjectionError（投影器直接置 dead）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .projector import PermanentProjectionError

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_ENV_FILE = Path.home() / ".agents" / "secrets" / "notion-memoryhub.env"
PERMANENT_STATUSES = {400, 401, 403, 404}
_TOKENISH = re.compile(r"\b(secret_|ntn_)[A-Za-z0-9]+")
log = logging.getLogger("memory_hub.notion")


class NotionConfigError(Exception):
    """env 文件缺失或缺字段。消息里只有文件路径和字段名，不含值。"""


class RetryableNotionError(Exception):
    """429 / 5xx / 网络错误：可重试。"""


def load_env_config(path: Path | str = DEFAULT_ENV_FILE) -> dict:
    path = Path(path).expanduser()
    if not path.is_file():
        raise NotionConfigError(f"Notion env 文件不存在：{path}（需要 NOTION_TOKEN 与 NOTION_DATABASE_ID）")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip("'\"")
    missing = [k for k in ("NOTION_TOKEN", "NOTION_DATABASE_ID") if not values.get(k)]
    if missing:
        raise NotionConfigError(f"Notion env 文件 {path} 缺少字段：{', '.join(missing)}")
    return {"token": values["NOTION_TOKEN"], "database_id": values["NOTION_DATABASE_ID"],
            "title_property": values.get("NOTION_TITLE_PROPERTY") or "Name"}


def _text(value) -> list:
    s = "" if value is None else str(value)
    return [{"type": "text", "text": {"content": s[:2000]}}]


def build_notion_properties(props: dict, title_property: str = "Name") -> dict:
    """把投影器的扁平属性转成 Notion properties：object_id 作标题，其余为 rich_text。

    目标数据库需有同名列（title 列名可用 NOTION_TITLE_PROPERTY 配置）。
    """
    out = {title_property: {"title": _text(props.get("object_id"))}}
    for k, v in props.items():
        if k != "object_id":
            out[k] = {"rich_text": _text(v)}
    return out


class NotionRestClient:
    def __init__(self, token: str, database_id: str, *, title_property: str = "Name",
                 opener: Callable = urllib.request.urlopen, timeout: float = 30.0,
                 api_base: str = API_BASE) -> None:
        self.__token = token
        self.database_id = database_id
        self.title_property = title_property
        self._open = opener
        self._timeout = timeout
        self._base = api_base.rstrip("/")

    @classmethod
    def from_env_file(cls, path: Path | str = DEFAULT_ENV_FILE, **kw) -> "NotionRestClient":
        cfg = load_env_config(path)
        return cls(cfg["token"], cfg["database_id"], title_property=cfg["title_property"], **kw)

    def __repr__(self) -> str:
        return f"NotionRestClient(database_id={self.database_id!r}, token=***)"

    def _redact(self, text: str) -> str:
        text = str(text)
        if self.__token:
            text = text.replace(self.__token, "***")
        return _TOKENISH.sub(r"\1***", text)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.__token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            msg = self._redact(f"Notion {method} {path} -> HTTP {e.code}: {detail}")
            log.warning(msg)
            if e.code == 429 or 500 <= e.code < 600:
                raise RetryableNotionError(msg) from None
            if e.code in PERMANENT_STATUSES:
                raise PermanentProjectionError(msg) from None
            raise RetryableNotionError(msg) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            msg = self._redact(f"Notion {method} {path} 网络错误: {e}")
            log.warning(msg)
            raise RetryableNotionError(msg) from None
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise RetryableNotionError(f"Notion {method} {path} 返回非 JSON") from None

    def create_page(self, properties: dict) -> str:
        res = self._request("POST", "/pages", {
            "parent": {"database_id": self.database_id},
            "properties": build_notion_properties(properties, self.title_property)})
        page_id = res.get("id")
        if not page_id:
            raise PermanentProjectionError("Notion create_page 响应缺少 id")
        return page_id

    def update_page(self, page_id: str, properties: dict) -> None:
        self._request("PATCH", f"/pages/{page_id}",
                      {"properties": build_notion_properties(properties, self.title_property)})

    def read_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")
