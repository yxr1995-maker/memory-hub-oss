"""notion-sync migration：建表、回滚、幂等写入、重复迁移。只用临时数据库。"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.automation_core.notion_sync import migrations as m  # noqa: E402


def tables(db):
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_DATA", str(tmp_path / "hub"))
    p = tmp_path / "hub" / "notion-sync.sqlite3"
    assert m.default_db_path() == p
    return p


def test_migrate_creates_tables(db):
    assert m.migrate(db) == 1
    assert {"outbox", "sync_map", "store_meta"} <= tables(db)
    assert m.current_version(db) == 1
    with sqlite3.connect(db) as c:
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "outbox_status_available" in idx


def test_rollback_drops_tables(db):
    m.migrate(db)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "notion_sync_rollback.py"),
                          "--db", str(db)], capture_output=True, text=True, check=True)
    assert "user_version=0" in out.stdout
    assert not ({"outbox", "sync_map", "store_meta"} & tables(db))
    assert m.current_version(db) == 0
    m.rollback(db)  # 重复回滚也安全
    assert m.migrate(db) == 1  # 回滚后可重新迁移
    assert {"outbox", "sync_map"} <= tables(db)


def test_idempotent_writes(db):
    m.migrate(db)
    with sqlite3.connect(db) as c:
        assert m.enqueue(c, "k1", "page", "notes/a.md", "upsert") is True
        assert m.enqueue(c, "k1", "page", "notes/a.md", "upsert") is False
        m.upsert_mapping(c, "page", "notes/a.md", "notion-1", local_version="v1")
        m.upsert_mapping(c, "page", "notes/a.md", "notion-1", local_version="v2")
        assert c.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        rows = c.execute("SELECT notion_page_id, local_version FROM sync_map").fetchall()
        assert rows == [("notion-1", "v2")]
        with pytest.raises(sqlite3.IntegrityError):  # 一个 Notion 页只能映射一个本地对象
            m.upsert_mapping(c, "page", "notes/b.md", "notion-1")


def test_migrate_rerun_is_safe(db):
    m.migrate(db)
    with sqlite3.connect(db) as c:
        m.enqueue(c, "k1", "page", "notes/a.md", "upsert")
    assert m.migrate(db) == 1
    assert m.migrate(db) == 1
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert not list(db.parent.glob("*-backup"))  # 已是最新版本不再备份


def test_upgrade_existing_v0_db_backs_up(db):
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE other(x)")
    m.migrate(db)
    assert (db.parent / "notion-sync.sqlite3.v0-backup").exists()
    assert {"other", "outbox", "sync_map"} <= tables(db)
