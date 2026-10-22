"""Notion 单向投影：只用 mock 客户端与临时库。"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.automation_core.notion_sync import migrations as m  # noqa: E402
from scripts.automation_core.notion_sync import projector as pj  # noqa: E402


class MockNotion:
    def __init__(self, fail_times=0, exc=RuntimeError):
        self.created, self.updated, self.fail_times, self.exc = [], [], fail_times, exc

    def create_page(self, properties):
        self._maybe_fail()
        self.created.append(properties)
        return f"notion-{len(self.created)}"

    def update_page(self, page_id, properties):
        self._maybe_fail()
        self.updated.append((page_id, properties))

    def _maybe_fail(self):
        if self.fail_times:
            self.fail_times -= 1
            raise self.exc("boom")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "notion-sync.sqlite3"
    m.migrate(p)
    return p


def put(db, key, oid="notes/a.md", status="active", version="v1"):
    with sqlite3.connect(db) as c:
        m.enqueue(c, key, "page", oid, "review.approved",
                  json.dumps({"status": status, "decision": "approve", "version": version}))
        c.execute("UPDATE outbox SET available_at='2000-01-01T00:00:00.000Z' WHERE idempotency_key=?", (key,))


def rows(db, sql):
    with sqlite3.connect(db) as c:
        return c.execute(sql).fetchall()


def test_success_creates_then_updates_and_maps(db):
    put(db, "k1")
    client = MockNotion()
    r = pj.project_once(db, client, now=Clock())
    assert r.done and not r.retried and not r.dead
    assert client.created[0]["object_id"] == "notes/a.md"
    assert rows(db, "SELECT status, attempts FROM outbox") == [("done", 1)]
    assert rows(db, "SELECT object_id, notion_page_id, local_version FROM sync_map") == [
        ("notes/a.md", "notion-1", "v1")]
    put(db, "k2", version="v2")
    pj.project_once(db, client, now=Clock())
    assert len(client.created) == 1 and client.updated[0][0] == "notion-1"
    assert rows(db, "SELECT local_version FROM sync_map") == [("v2",)]
    # done 事件不会被再次处理
    assert pj.project_once(db, client, now=Clock()).done == []


def test_retry_with_backoff_then_success(db):
    put(db, "k1")
    clock, client = Clock(), MockNotion(fail_times=1)
    r = pj.project_once(db, client, now=clock)
    assert r.retried and not r.done
    st, att, err, avail = rows(db, "SELECT status, attempts, last_error, available_at FROM outbox")[0]
    assert (st, att) == ("failed", 1) and "boom" in err
    assert avail == "2026-10-14T12:00:30.000Z"
    assert pj.project_once(db, client, now=clock).done == []  # 未到期不重试
    clock.t += timedelta(seconds=31)
    assert pj.project_once(db, client, now=clock).done
    assert rows(db, "SELECT status, attempts, last_error FROM outbox") == [("done", 2, None)]


def test_dead_after_max_attempts(db):
    put(db, "k1")
    clock, client = Clock(), MockNotion(fail_times=99)
    for _ in range(3):
        pj.project_once(db, client, now=clock, max_attempts=3)
        clock.t += timedelta(hours=2)
    assert rows(db, "SELECT status, attempts FROM outbox") == [("dead", 3)]
    assert rows(db, "SELECT count(*) FROM sync_map") == [(0,)]


def test_permanent_error_dead_immediately(db):
    put(db, "k1")
    r = pj.project_once(db, MockNotion(fail_times=1, exc=pj.PermanentProjectionError), now=Clock())
    assert r.dead and rows(db, "SELECT status, attempts FROM outbox") == [("dead", 1)]


def test_page_id_collision_marks_dead_without_corrupting_map(db):
    put(db, "k1", oid="notes/a.md")
    put(db, "k2", oid="notes/b.md")

    class SamePage(MockNotion):
        def create_page(self, properties):
            return "notion-dup"
    r = pj.project_once(db, SamePage(), now=Clock())
    assert len(r.done) == 1 and len(r.dead) == 1
    assert rows(db, "SELECT object_id FROM sync_map") == [("notes/a.md",)]


def test_stale_in_flight_requeued(db):
    put(db, "k1")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE outbox SET status='in_flight', updated_at='2026-10-14T00:00:00.000Z'")
    assert pj.requeue_stale_in_flight(db, Clock()()) == 1
    assert pj.project_once(db, MockNotion(), now=Clock()).done


def test_refuses_unmigrated_db(tmp_path):
    with pytest.raises(RuntimeError):
        pj.project_once(tmp_path / "none.sqlite3", MockNotion())
