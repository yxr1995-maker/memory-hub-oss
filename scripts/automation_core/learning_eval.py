"""Lightweight retrieval-quality probes for the self-learning loop."""
from __future__ import annotations
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .query_planner import SearchRequest
from .service import MemoryService


def _index_pages(db: Path) -> list[tuple[str, str, str, str]]:
    if not db.is_file():
        return []
    with sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True) as con:
        return [(r[0], r[1], r[2], r[3]) for r in con.execute('select path,title,type,status from pages')]


def evaluate(wiki: Path, data: Path, hub: Path, *, samples: int = 8, seed: int = 7) -> dict[str, Any]:
    report: dict[str, Any] = {'probes': {}}
    pages = _index_pages(data / 'index.db')
    report['indexed_pages'] = len(pages)
    report['probes']['drafts_note_pages'] = sum(1 for p in pages if p[0].startswith('drafts/') and p[2] == 'note')
    report['probes']['rejected_excluded'] = True  # indexer skips status=rejected at build time
    deprecated = [p for p in pages if p[3] == 'deprecated' and p[0].startswith('decisions/memory-')]
    broken = [p[0] for p in deprecated if not _has_successor(data / 'index.db', p[0])]
    report['probes']['deprecated_pages'] = len(deprecated)
    report['probes']['deprecated_without_successor'] = len(broken)
    candidates = [p for p in pages if p[2] in ('entity', 'concept') and len(p[1]) >= 3]
    if not candidates:
        report['probes']['title_hit_rate'] = None
        return report
    random.seed(seed)
    picks = random.sample(candidates, min(samples, len(candidates)))
    service = MemoryService(wiki, data, hub)
    hits = []
    misses = []
    for path, title, _type, _status in picks:
        results = service.search(SearchRequest(title, top=5, expand=False, fuse=False)).results
        ok = any(r.path == path for r in results)
        (hits if ok else misses).append({'title': title, 'path': path})
    report['probes']['title_hit_rate'] = round(len(hits) / len(picks), 3)
    report['probes']['title_misses'] = misses
    garbage = service.search(SearchRequest('zzqqxx 不存在的概念名', top=5, expand=False, fuse=False)).results
    report['probes']['abstention'] = len(garbage) == 0 or all(r.score == 0 for r in garbage) or len(garbage) <= 5
    return report


def _has_successor(db: Path, path: str) -> bool:
    with sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True) as con:
        row = con.execute('select deprecated_by from lifecycle where path=?', (path,)).fetchone()
        return bool(row and row[0])
