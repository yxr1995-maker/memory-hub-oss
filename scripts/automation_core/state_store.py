"""State store: entity KV + temporal relationship timeline.

A separate SQLite DB so writes never block wiki index rebuilds. Schema matches
indexer.KV_DDL / indexer.TIMELINE_DDL so the same DDL lives in one place.

Decay fields (last_accessed, access_count) live only here; the indexer's
pages/meta/vec are not aware of them.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .indexer import KV_DDL, TIMELINE_DDL, TIMELINE_IDX_DDL

# Use IF NOT EXISTS so re-opening an existing state.db (which indexer rebuild
# also opens) is idempotent. The indexer's CREATE TABLE without IF NOT EXISTS
# is the canonical first-create; ours is the safe re-open.
SCHEMA = (
    KV_DDL.replace("CREATE TABLE entity_kv(", "CREATE TABLE IF NOT EXISTS entity_kv(")
    + "\n"
    + TIMELINE_DDL.replace("CREATE TABLE entity_timeline(", "CREATE TABLE IF NOT EXISTS entity_timeline(")
    + "\n"
    + "\n".join(idx.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS") for idx in TIMELINE_IDX_DDL)
)


@dataclass(frozen=True)
class KVEntry:
    scope: str
    scope_id: str
    key: str
    value: str
    evidence_path: str | None
    source: str | None
    updated: str
    last_accessed: str | None
    access_count: int


@dataclass(frozen=True)
class TimelineEdge:
    subject: str
    predicate: str
    object: str
    valid_at: str
    invalid_at: str | None
    superseded_by: int | None
    evidence_path: str | None
    confidence: float
    created_at: str

    def is_active(self, now: str | None = None) -> bool:
        return self.invalid_at is None or self.invalid_at == ""


class StateStore:
    """Thin facade over state.db; opens one connection per call (write paths are short)."""

    def __init__(self, data: Path) -> None:
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / "state.db"
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def _now() -> str:
        # ISO-8601 UTC with seconds; stable for SQL string compares.
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # -- KV ---------------------------------------------------------------

    def upsert_kv(
        self,
        scope: str,
        scope_id: str,
        key: str,
        value: str,
        *,
        evidence_path: str | None = None,
        source: str | None = None,
    ) -> None:
        if not scope or not scope_id or not key:
            raise ValueError("scope, scope_id, and key are required")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    """
                    INSERT INTO entity_kv(scope, scope_id, key, value, evidence_path, source, updated)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(scope, scope_id, key) DO UPDATE SET
                      value=excluded.value,
                      evidence_path=COALESCE(excluded.evidence_path, entity_kv.evidence_path),
                      source=COALESCE(excluded.source, entity_kv.source),
                      updated=excluded.updated
                    """,
                    (scope, scope_id, key, value, evidence_path, source, self._now()),
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    def get_kv(self, scope: str, scope_id: str, key: str) -> KVEntry | None:
        with self._connect() as con:
            # Increment first so the returned entry reflects this access; the
            # read-then-write order returned a stale counter (off by one).
            cur = con.execute(
                "UPDATE entity_kv SET last_accessed=?, access_count=access_count+1 "
                "WHERE scope=? AND scope_id=? AND key=?",
                (self._now(), scope, scope_id, key),
            )
            if cur.rowcount == 0:
                return None
            row = con.execute(
                "SELECT * FROM entity_kv WHERE scope=? AND scope_id=? AND key=?",
                (scope, scope_id, key),
            ).fetchone()
        return _row_to_kv(row)

    def list_kv(self, scope: str, scope_id: str | None = None) -> list[KVEntry]:
        sql = "SELECT * FROM entity_kv WHERE scope=?"
        args: tuple[Any, ...] = (scope,)
        if scope_id is not None:
            sql += " AND scope_id=?"
            args = (scope, scope_id)
        sql += " ORDER BY updated DESC"
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [_row_to_kv(r) for r in rows]

    def delete_kv(self, scope: str, scope_id: str, key: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM entity_kv WHERE scope=? AND scope_id=? AND key=?",
                (scope, scope_id, key),
            )
            return cur.rowcount > 0

    # -- Timeline ---------------------------------------------------------

    def record_edge(
        self,
        subject: str,
        predicate: str,
        object_: str,
        *,
        valid_at: str | None = None,
        evidence_path: str | None = None,
        confidence: float = 1.0,
        exclusive: bool = False,
    ) -> int:
        """Add a (subject, predicate, object) edge valid from valid_at onward.

        exclusive=True marks a single-valued predicate (works_at, lives_in): the
        new edge closes any other open edge for the same (subject, predicate),
        whatever its object. Multi-valued predicates (manages, member_of) keep
        their siblings, so the default is False — history is never invalidated
        silently. Returns the edge's rowid.
        """
        va = valid_at or self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                if exclusive:
                    # Close open siblings with a different object (or an older
                    # same-object row) so exactly one value stays open.
                    con.execute(
                        "UPDATE entity_timeline SET invalid_at=? "
                        "WHERE subject=? AND predicate=? AND invalid_at IS NULL "
                        "AND (object<>? OR valid_at<?)",
                        (va, subject, predicate, object_, va),
                    )
                existing = con.execute(
                    "SELECT rowid FROM entity_timeline WHERE subject=? AND predicate=? AND object=? AND valid_at=?",
                    (subject, predicate, object_, va),
                ).fetchone()
                if existing is not None:
                    # Same triple in the same instant: refresh evidence instead of
                    # colliding on the primary key.
                    con.execute(
                        "UPDATE entity_timeline SET evidence_path=COALESCE(?, evidence_path), "
                        "confidence=?, invalid_at=NULL WHERE rowid=?",
                        (evidence_path, confidence, existing["rowid"]),
                    )
                    new_id = existing["rowid"]
                else:
                    cur = con.execute(
                        """
                        INSERT INTO entity_timeline(
                            subject, predicate, object, valid_at, invalid_at,
                            evidence_path, confidence, created_at
                        ) VALUES(?,?,?,?,NULL,?,?,?)
                        """,
                        (subject, predicate, object_, va, evidence_path, confidence, self._now()),
                    )
                    new_id = cur.lastrowid
                # Back-link superseded siblings so callers can walk the chain.
                con.execute(
                    "UPDATE entity_timeline SET superseded_by=? "
                    "WHERE subject=? AND predicate=? AND rowid<>? AND invalid_at=?",
                    (new_id, subject, predicate, new_id, va),
                )
                con.execute("COMMIT")
                return int(new_id) if new_id is not None else 0
            except Exception:
                con.execute("ROLLBACK")
                raise

    def active_edges(
        self, subject: str | None = None, predicate: str | None = None, object_: str | None = None
    ) -> list[TimelineEdge]:
        clauses, args = [], []
        if subject is not None:
            clauses.append("subject=?")
            args.append(subject)
        if predicate is not None:
            clauses.append("predicate=?")
            args.append(predicate)
        if object_ is not None:
            clauses.append("object=?")
            args.append(object_)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM entity_timeline {where} "  # noqa: S608 — clause names fixed
                "ORDER BY subject, predicate, object, valid_at DESC",
                args,
            ).fetchall()
        return [_row_to_edge(r) for r in rows if r["invalid_at"] is None or r["invalid_at"] == ""]

    def edge_history(self, subject: str, predicate: str, object_: str) -> list[TimelineEdge]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM entity_timeline WHERE subject=? AND predicate=? AND object=? "
                "ORDER BY valid_at DESC",
                (subject, predicate, object_),
            ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def relationships_at(
        self, subject: str, when: str | None = None
    ) -> list[TimelineEdge]:
        """Edges true for `subject` at time `when` (default: now)."""
        t = when or self._now()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM entity_timeline "
                "WHERE subject=? AND valid_at<=? AND (invalid_at IS NULL OR invalid_at>=?) "
                "ORDER BY predicate, valid_at DESC",
                (subject, t, t),
            ).fetchall()
        return [_row_to_edge(r) for r in rows]

    # -- Decay ------------------------------------------------------------

    def apply_decay(
        self,
        *,
        access_half_life_days: float = 30.0,
        max_inactive_days: int = 180,
    ) -> int:
        """Mark cold rows. The ranker can then demote them; this does NOT delete
        anything. A row is cold when:
          - never accessed AND updated > max_inactive_days ago, OR
          - last_accessed > max_inactive_days ago.
        Returns the number of rows marked (we set a `decay_tag` via last_accessed
        prefix; intentionally light so we can roll back).
        """
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        cutoff = now - _dt.timedelta(days=max_inactive_days)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as con:
            cur = con.execute(
                "UPDATE entity_kv SET last_accessed=? "
                "WHERE (last_accessed IS NULL AND updated<?) OR last_accessed<?",
                (f"cold:{cutoff_iso}", cutoff_iso, cutoff_iso),
            )
            return cur.rowcount

    def cold_keys(self) -> list[tuple[str, str, str]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT scope, scope_id, key FROM entity_kv "
                "WHERE substr(IFNULL(last_accessed,''),1,5)='cold:'"
            ).fetchall()
        return [(r["scope"], r["scope_id"], r["key"]) for r in rows]

    def hot_keys(self) -> list[tuple[str, str, str, int]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT scope, scope_id, key, access_count FROM entity_kv "
                "WHERE access_count > 0 ORDER BY access_count DESC LIMIT 50"
            ).fetchall()
        return [(r["scope"], r["scope_id"], r["key"], r["access_count"]) for r in rows]


def _row_to_kv(row: sqlite3.Row) -> KVEntry:
    return KVEntry(
        scope=row["scope"],
        scope_id=row["scope_id"],
        key=row["key"],
        value=row["value"],
        evidence_path=row["evidence_path"],
        source=row["source"],
        updated=row["updated"],
        last_accessed=row["last_accessed"],
        access_count=row["access_count"],
    )


def _row_to_edge(row: sqlite3.Row) -> TimelineEdge:
    return TimelineEdge(
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        valid_at=row["valid_at"],
        invalid_at=row["invalid_at"],
        superseded_by=row["superseded_by"],
        evidence_path=row["evidence_path"],
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
    )
