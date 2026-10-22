"""Graph-recall effectiveness probes.

Answers one question with data: does the one-hop graph channel actually add
value, or is it dead weight? A graph-only page is one that no literal channel
could reach (zero literal overlap) yet still surfaced into the result set.

Without these numbers the second hop is guesswork.
"""
from __future__ import annotations

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


def _literal_paths(service: MemoryService, query: str, top: int) -> set[str]:
    """Paths reachable without the graph channel (FTS + vector only).

    expand=False on both sides of the comparison: query expansion is a
    separate channel family that also calls the gateway, and including it here
    would make the probe measure the planner rather than the graph channel.
    """
    from .query_planner import plan_query

    request = SearchRequest(query, top=top, fuse=True, expand=False)
    plan = plan_query(request, service.recall)
    recalls: dict[str, Any] = {
        'original_fts': service.recall.fts(query, 15),
        'original_vec': service.recall.vector(query, 15),
    }
    for idx, term in enumerate(plan.expansions):
        recalls[f'expansion_{idx}_fts'] = service.recall.fts(term.text, 10)
        recalls[f'expansion_{idx}_vec'] = service.recall.vector(term.text, 10)
    hit_paths: set[str] = set()
    for hits in recalls.values():
        hit_paths.update(h.path for h in hits)
    return hit_paths


def probe_graph_recall(wiki: Path, data: Path, hub: Path, *, samples: int = 12,
                       seed: int = 11, top: int = 5) -> dict[str, Any]:
    """Measure graph-channel contribution on sampled real titles."""
    pages = _index_pages(data / 'index.db')
    titles = [t for p, t, ty, st in pages
              if ty in ('entity', 'concept') and st != 'deprecated' and len(t or '') >= 3]
    if not titles or not (data / 'index.db').is_file():
        return {'samples': 0, 'graph_only_hits': 0, 'queries_with_graph_only': 0,
                'literal_hit_rate': None, 'notes': 'no index or no candidate titles'}
    random.seed(seed)
    picks = random.sample(titles, min(samples, len(titles)))
    service = MemoryService(wiki, data, hub)

    graph_only_total = 0
    queries_with = 0
    literal_ok = 0
    examples: list[dict[str, Any]] = []
    for title in picks:
        results = service.search(SearchRequest(title, top=top, fuse=True, expand=False)).results
        literal = _literal_paths(service, title, top)
        graph_only = [r.path for r in results if r.path not in literal]
        if graph_only:
            queries_with += 1
            graph_only_total += len(graph_only)
            examples.append({'query': title, 'graph_only': graph_only[:3]})
        if any(r.path in literal for r in results):
            literal_ok += 1

    n = len(picks)
    return {
        'samples': n,
        'graph_only_hits': graph_only_total,
        'queries_with_graph_only': queries_with,
        'graph_only_share': round(graph_only_total / (n * top), 4),
        'literal_hit_rate': round(literal_ok / n, 3),
        'examples': examples[:5],
    }