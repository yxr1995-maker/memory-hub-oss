"""Notion 单向投影：消费 outbox，调用可注入的 Notion 客户端，更新 sync_map。

原则：Markdown 是知识唯一来源、SQLite 是运行状态唯一来源，Notion 只是投影；
这里只读 outbox/写 sync_map 与 outbox 状态，从不回写 Markdown。
本模块不含任何真实 Notion 客户端，也不读取凭证；真实客户端需另行注入。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import migrations

TS = "%Y-%m-%dT%H:%M:%S.%fZ"


class NotionClient(Protocol):
    """注入接口：create_page 返回新页面 id；update_page 更新已有页面。"""

    def create_page(self, properties: dict) -> str: ...

    def update_page(self, page_id: str, properties: dict) -> None: ...


class PermanentProjectionError(Exception):
    """不可重试的错误（如 400/权限/对象不存在）：直接置 dead。"""


@dataclass
class ProjectionReport:
    done: list[int] = field(default_factory=list)
    retried: list[int] = field(default_factory=list)
    dead: list[int] = field(default_factory=list)


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS)[:-4] + "Z"  # 毫秒精度，与 DDL 默认值同格式


def default_backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * (2 ** (attempts - 1))))


def build_properties(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"] or "{}")
    return {"object_type": row["object_type"], "object_id": row["object_id"],
            "event_type": row["event_type"], "status": payload.get("status"),
            "decision": payload.get("decision"), "version": payload.get("version"),
            "outbox_id": row["id"], "occurred_at": row["created_at"]}


def _claim(c: sqlite3.Connection, now: str, limit: int) -> list[sqlite3.Row]:
    c.execute("BEGIN IMMEDIATE")
    rows = c.execute(
        "SELECT * FROM outbox WHERE status IN ('pending','failed') AND available_at<=? "
        "ORDER BY id LIMIT ?", (now, limit)).fetchall()
    for r in rows:
        c.execute("UPDATE outbox SET status='in_flight', updated_at=? WHERE id=?", (now, r["id"]))
    c.execute("COMMIT")
    return rows


def project_once(db_path: Path, client: NotionClient, *, limit: int = 50,
                 max_attempts: int = 5, now: Callable[[], datetime] | None = None,
                 backoff: Callable[[int], timedelta] = default_backoff) -> ProjectionReport:
    """处理一批到期事件。成功→done + sync_map；可重试失败→failed+退避；超限或永久错误→dead。"""
    now = now or (lambda: datetime.now(timezone.utc))
    db_path = Path(db_path)
    if migrations.current_version(db_path) != migrations.SCHEMA_VERSION:
        raise RuntimeError("notion-sync db 未迁移；请先显式执行 migration")
    report = ProjectionReport()
    c = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        for row in _claim(c, _ts(now()), limit):
            props = build_properties(row)
            existing = c.execute(
                "SELECT notion_page_id FROM sync_map WHERE object_type=? AND object_id=?",
                (row["object_type"], row["object_id"])).fetchone()
            try:
                if existing:
                    page_id = existing[0]
                    client.update_page(page_id, props)
                else:
                    page_id = client.create_page(props)
                    if not isinstance(page_id, str) or not page_id:
                        raise PermanentProjectionError("create_page 未返回页面 id")
            except Exception as e:  # noqa: BLE001 - 任何客户端异常都要落库
                attempts = row["attempts"] + 1
                permanent = isinstance(e, PermanentProjectionError)
                status = "dead" if permanent or attempts >= max_attempts else "failed"
                t = now()
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE outbox SET status=?, attempts=?, last_error=?, available_at=?, "
                          "updated_at=? WHERE id=?",
                          (status, attempts, f"{type(e).__name__}: {e}"[:1000],
                           _ts(t + backoff(attempts)) if status == "failed" else _ts(t),
                           _ts(t), row["id"]))
                c.execute("COMMIT")
                (report.dead if status == "dead" else report.retried).append(row["id"])
                continue
            try:
                c.execute("BEGIN IMMEDIATE")
                migrations.upsert_mapping(c, row["object_type"], row["object_id"], page_id,
                                          local_version=props.get("version"))
                c.execute("UPDATE outbox SET status='done', attempts=?, last_error=NULL, updated_at=? "
                          "WHERE id=?", (row["attempts"] + 1, _ts(now()), row["id"]))
                c.execute("COMMIT")
            except sqlite3.IntegrityError as e:  # 例如 Notion 页面 id 已映射到别的对象
                c.execute("ROLLBACK")
                c.execute("UPDATE outbox SET status='dead', attempts=?, last_error=?, updated_at=? "
                          "WHERE id=?", (row["attempts"] + 1, f"sync_map: {e}"[:1000],
                                         _ts(now()), row["id"]))
                report.dead.append(row["id"])
                continue
            report.done.append(row["id"])
    finally:
        c.close()
    return report


def requeue_stale_in_flight(db_path: Path, older_than: datetime) -> int:
    """进程崩溃留下的 in_flight 超时后放回 failed 以便重试。"""
    with sqlite3.connect(db_path) as c:
        return c.execute("UPDATE outbox SET status='failed' WHERE status='in_flight' AND updated_at<?",
                         (_ts(older_than),)).rowcount
