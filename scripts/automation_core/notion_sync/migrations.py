"""Notion 投影状态库的 migration。

沿用 experience store 的机制：PRAGMA user_version 记版本 + store_meta 表，
已有库升级前先做 O_EXCL 备份，DDL 全部 IF NOT EXISTS，可重复执行。
独立库文件（默认 $MEMORY_HUB_DATA/notion-sync.sqlite3），不改 experience store 的 user_version。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

OUTBOX_DDL = """CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 idempotency_key TEXT NOT NULL UNIQUE,
 object_type TEXT NOT NULL,
 object_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','in_flight','done','failed','dead')),
 attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,
 available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""
OUTBOX_INDEX = "CREATE INDEX IF NOT EXISTS outbox_status_available ON outbox(status, available_at, id)"

SYNC_MAP_DDL = """CREATE TABLE IF NOT EXISTS sync_map(
 object_type TEXT NOT NULL,
 object_id TEXT NOT NULL,
 notion_page_id TEXT NOT NULL UNIQUE,
 notion_database_id TEXT,
 local_version TEXT,
 last_synced_at TEXT,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(object_type, object_id))"""

STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS store_meta(version INTEGER NOT NULL)",
    OUTBOX_DDL, OUTBOX_INDEX, SYNC_MAP_DDL,
)


def default_db_path() -> Path:
    data = os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))
    return Path(data) / "notion-sync.sqlite3"


def current_version(db_path: Path) -> int:
    if not Path(db_path).exists():
        return 0
    with sqlite3.connect(db_path) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def migrate(db_path: Path) -> int:
    """建 outbox/sync_map；已是最新版本直接返回。返回迁移后版本。"""
    db_path = Path(db_path)
    if db_path.exists():
        version = current_version(db_path)
        if version == SCHEMA_VERSION:
            return version
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"notion-sync db version {version} newer than {SCHEMA_VERSION}")
        backup = db_path.with_name(f"{db_path.name}.v{version}-backup")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(db_path) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    try:
        c.execute("BEGIN IMMEDIATE")
        for stmt in STATEMENTS:
            c.execute(stmt)
        c.execute("DELETE FROM store_meta")
        c.execute("INSERT INTO store_meta VALUES(?)", (SCHEMA_VERSION,))
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return SCHEMA_VERSION


def rollback(db_path: Path) -> int:
    """删除 outbox/sync_map/store_meta 并把 user_version 置 0。可重复执行。"""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    c = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DROP INDEX IF EXISTS outbox_status_available")
        c.execute("DROP TABLE IF EXISTS outbox")
        c.execute("DROP TABLE IF EXISTS sync_map")
        c.execute("DROP TABLE IF EXISTS store_meta")
        c.execute("PRAGMA user_version=0")
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return 0


def enqueue(c: sqlite3.Connection, idempotency_key: str, object_type: str, object_id: str,
            event_type: str, payload_json: str = "{}") -> bool:
    """幂等入队：同一 idempotency_key 重复写入不新增、不报错。返回是否新插入。"""
    cur = c.execute(
        "INSERT INTO outbox(idempotency_key,object_type,object_id,event_type,payload_json) "
        "VALUES(?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
        (idempotency_key, object_type, object_id, event_type, payload_json))
    return cur.rowcount == 1


def upsert_mapping(c: sqlite3.Connection, object_type: str, object_id: str,
                   notion_page_id: str, notion_database_id: str | None = None,
                   local_version: str | None = None) -> None:
    """幂等写映射：同一本地对象重复写入只更新，不新增。"""
    c.execute(
        "INSERT INTO sync_map(object_type,object_id,notion_page_id,notion_database_id,local_version,last_synced_at) "
        "VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT(object_type,object_id) DO UPDATE SET "
        "notion_page_id=excluded.notion_page_id, notion_database_id=excluded.notion_database_id, "
        "local_version=excluded.local_version, last_synced_at=excluded.last_synced_at, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (object_type, object_id, notion_page_id, notion_database_id, local_version))
