"""State store: KV + temporal timeline tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.automation_core.state_store import StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path)


def test_kv_upsert_and_get(store):
    store.upsert_kv("user", "bob", "gateway_port", "7788", evidence_path="notes/gatewayx.md")
    entry = store.get_kv("user", "bob", "gateway_port")
    assert entry is not None and entry.value == "7788" and entry.evidence_path == "notes/gatewayx.md"
    assert entry.access_count == 1  # get_kv counts the access it performs


def test_kv_overwrite_preserves_last_updated(store):
    store.upsert_kv("user", "bob", "model", "group/route")
    first = store.get_kv("user", "bob", "model")
    store.upsert_kv("user", "bob", "model", "openai/gpt-4o")
    second = store.get_kv("user", "bob", "model")
    assert second is not None and second.value == "openai/gpt-4o"
    assert first is not None and first.updated <= second.updated  # monotonic


def test_kv_list_scoped(store):
    store.upsert_kv("user", "bob", "a", "1")
    store.upsert_kv("user", "bob", "b", "2")
    store.upsert_kv("user", "alice", "a", "9")
    assert {e.key for e in store.list_kv("user", "bob")} == {"a", "b"}
    assert {e.key for e in store.list_kv("user", "alice")} == {"a"}


def test_kv_delete(store):
    store.upsert_kv("user", "bob", "x", "1")
    assert store.delete_kv("user", "bob", "x") is True
    assert store.get_kv("user", "bob", "x") is None
    assert store.delete_kv("user", "bob", "x") is False


def test_kv_get_increments_access_count(store):
    store.upsert_kv("user", "bob", "p", "v")
    store.get_kv("user", "bob", "p")
    entry = store.get_kv("user", "bob", "p")
    assert entry is not None and entry.access_count >= 2


def test_timeline_record_first_edge_is_active(store):
    store.record_edge("bob", "works_at", "Hermes", evidence_path="bio.md")
    edges = store.active_edges(subject="bob", predicate="works_at")
    assert len(edges) == 1 and edges[0].object == "Hermes"


def test_timeline_exclusive_predicate_supersedes_open_edge(store):
    """Single-valued predicates (exclusive=True) close the previous value."""
    store.record_edge("bob", "works_at", "Acme", exclusive=True)
    store.record_edge("bob", "works_at", "Hermes", exclusive=True)
    active = store.active_edges(subject="bob", predicate="works_at")
    assert [e.object for e in active] == ["Hermes"]
    history = store.edge_history("bob", "works_at", "Acme")
    assert history[0].invalid_at is not None and history[0].superseded_by is not None


def test_timeline_multi_valued_predicate_keeps_siblings(store):
    """Multi-valued predicates (default) must never invalidate their siblings."""
    store.record_edge("bob", "manages", "team-x")
    store.record_edge("bob", "manages", "team-y")
    active = store.active_edges(subject="bob", predicate="manages")
    assert {e.object for e in active} == {"team-x", "team-y"}


def test_timeline_same_second_exclusive_rerecord_does_not_collide(store):
    """Re-recording the same triple in the same second must refresh, not crash."""
    store.record_edge("bob", "timezone", "UTC+8", exclusive=True, evidence_path="a.md")
    store.record_edge("bob", "timezone", "UTC+8", exclusive=True, evidence_path="b.md")
    active = store.active_edges(subject="bob", predicate="timezone")
    assert len(active) == 1 and active[0].evidence_path == "b.md"


def test_timeline_relationships_at_returns_active(store):
    store.record_edge("bob", "manages", "team-x")
    store.record_edge("bob", "manages", "team-y")
    rels = store.relationships_at("bob")
    assert {r.object for r in rels} == {"team-x", "team-y"}


def test_timeline_predicate_filter(store):
    store.record_edge("bob", "works_at", "Hermes")
    store.record_edge("bob", "manages", "team-x")
    work = store.active_edges(subject="bob", predicate="works_at")
    assert [e.object for e in work] == ["Hermes"]


def test_decay_marks_cold_keys(store):
    """Decay must NOT delete; it must mark rows as cold so the ranker can demote them."""
    store.upsert_kv("user", "bob", "hot", "1")
    # Insert a synthetic old timestamp via direct write to simulate age.
    from scripts.automation_core import state_store as ss
    import sqlite3
    with sqlite3.connect(store.db_path) as con:
        con.execute("UPDATE entity_kv SET updated='2020-01-01T00:00:00Z' WHERE key='hot'")
    marked = store.apply_decay(max_inactive_days=1)
    assert marked >= 1
    cold = store.cold_keys()
    assert ("user", "bob", "hot") in cold


def test_index_rebuild_preserves_state_tables(tmp_path):
    """Rebuilding the wiki index must carry KV + timeline forward (state survives)."""
    from scripts.automation_core.indexer import atomic_rebuild_index
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "entities").mkdir()
    (wiki / "entities" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: entity\n---\nalpha"
    )
    data = tmp_path / "data"
    atomic_rebuild_index(wiki, data)
    store = StateStore(data)
    store.upsert_kv("user", "bob", "k", "v1")
    store.record_edge("bob", "works_at", "Hermes")
    # Rebuild again.
    atomic_rebuild_index(wiki, data)
    store2 = StateStore(data)
    assert store2.get_kv("user", "bob", "k") is not None
    assert any(e.object == "Hermes" for e in store2.active_edges("bob", "works_at"))


def test_kv_requires_non_empty_fields(store):
    with pytest.raises(ValueError):
        store.upsert_kv("", "bob", "k", "v")
    with pytest.raises(ValueError):
        store.upsert_kv("user", "", "k", "v")
    with pytest.raises(ValueError):
        store.upsert_kv("user", "bob", "", "v")
