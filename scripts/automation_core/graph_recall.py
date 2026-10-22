"""Graph-walk recall channel (long-range associative memory, HippoRAG-style).

The index already has a links edge table (src_path -> dst_path). This module
expands the top literal FTS recall hits by walking one hop along those links,
so pages related to a hit but phrased differently can surface.

Scoring: a neighbor inherits a deterministic fraction of the hit's score,
so graph recalls always rank below the hit that discovered them and can
boost, never dominate, literal matches once fused through RRF.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .ranker import RecallHit

GRAPH_SCORE_DECAY = 0.5
GRAPH_MAX_NEIGHBORS = 6

#: A page above this out-degree is an index/hub page, not evidence. Real data:
#: atoms/2026-08-06/_index.md has 2397 outlinks and batch drafts ~700 each.
#: Letting hubs seed neighbors turned the channel into random sampling — 33.5%
#: of sampled result slots were noise before this guard.
GRAPH_HUB_DEGREE = 32


def graph_neighbors(
    hits: Sequence[RecallHit],
    links: Mapping[str, Iterable[str]],
    *,
    max_neighbors: int = GRAPH_MAX_NEIGHBORS,
    decay: float = GRAPH_SCORE_DECAY,
    exclude: set[str] | None = None,
    hub_degree: int = GRAPH_HUB_DEGREE,
) -> list[RecallHit]:
    """Expand `hits` one hop along the wikilink graph.

    Deterministic: neighbor order follows the sorted adjacency, scores decay by
    a fixed fraction of the discovering hit's score. Already-hit paths and
    pages in `exclude` are skipped. Pages whose out-degree exceeds `hub_degree`
    donate nothing — an index page's neighborhood is a directory, not evidence.
    """
    blocked = {h.path for h in hits} | set(exclude or set())
    best: dict[str, RecallHit] = {}
    for hit in hits:
        nbrs = sorted(links.get(hit.path, ()))
        if len(nbrs) > hub_degree:
            continue
        for nbr in nbrs:
            if nbr in blocked:
                continue
            cand = RecallHit(
                path=nbr,
                score=round(hit.score * decay, 4),
                rank=hit.rank + 1,
                abstract="",
                content="",
            )
            prev = best.get(nbr)
            if prev is None or cand.score > prev.score:
                best[nbr] = cand
    return [best[p] for p in sorted(best)][:max_neighbors]
