"""Unit tests for the graph-walk recall channel (HippoRAG-style one-hop boost).

Contract under test (from graph_recall.py):
- neighbor score = discovering hit score x decay (default 0.5), never outranks it
- a page reachable from several hits resolves to a single RecallHit with the best score
- explicit exclude sets plus the hit set itself are never re-emitted
- fan-out capped so a hub page cannot flood the pool
- deterministic ordering (sorted adjacency traversal)
"""
from scripts.automation_core.graph_recall import graph_neighbors
from scripts.automation_core.ranker import RecallHit


def _h(path, score=0.02, rank=1):
    return RecallHit(path=path, score=score, rank=rank, abstract="", content="")


def test_neighbor_score_is_bounded_fraction():
    out = graph_neighbors([_h("a.md", 0.02, 1)], {"a.md": {"b.md"}})
    assert len(out) == 1
    assert out[0].path == "b.md"
    assert out[0].score == 0.01  # 0.02 x 0.5 decay
    assert out[0].score < 0.02


def test_self_and_seed_paths_excluded():
    out = graph_neighbors(
        [_h("a.md", 0.02, 1)],
        {"a.md": {"a.md", "b.md"}},
        exclude={"b.md"},
    )
    assert out == []


def test_fanout_capped():
    links = {"a.md": {f"n{i}.md" for i in range(50)}}
    out = graph_neighbors([_h("a.md", 0.02, 1)], links, hub_degree=100)
    assert 0 < len(out) <= 6


def test_hub_pages_do_not_flood_the_graph_channel():
    """A hub page (hundreds of outlinks) must not donate its whole neighborhood.

    Real data: atoms/2026-08-06/_index.md has 2397 outlinks and batch drafts have
    ~700 each; letting them seed neighbors turned recall into random sampling
    (33.5% of result slots were noise).
    """
    hits = [RecallHit("atoms/2026-08-06/_index.md", 0.02, 1)]
    hub = {f"drafts/page-{i}.md" for i in range(500)}
    out = graph_neighbors(hits, {"atoms/2026-08-06/_index.md": hub})
    assert len(out) == 0, "hub page donated neighbors despite the noise guard"


def test_hub_threshold_is_configurable_and_tested():
    from scripts.automation_core.graph_recall import GRAPH_HUB_DEGREE, GRAPH_MAX_NEIGHBORS
    links = {"a.md": {f"n{i}.md" for i in range(GRAPH_HUB_DEGREE + 5)}}
    assert graph_neighbors([RecallHit("a.md", 0.02, 1)], links) == []
    small = {"a.md": {f"n{i}.md" for i in range(GRAPH_HUB_DEGREE - 1)}}
    out = graph_neighbors([RecallHit("a.md", 0.02, 1)], small)
    assert len(out) == GRAPH_MAX_NEIGHBORS  # fan-out cap still applies


def test_multi_hit_page_resolves_once_with_best_score():
    out = graph_neighbors(
        [_h("a.md", 0.02, 1), _h("c.md", 0.04, 2)],
        {"a.md": {"shared.md"}, "c.md": {"shared.md"}},
    )
    assert len(out) == 1 and out[0].path == "shared.md"
    assert out[0].score == 0.02  # 0.04 x 0.5 beats 0.02 x 0.5


def test_deterministic_ordering():
    links = {"a.md": {"z.md", "m.md", "k.md"}}
    first = [h.path for h in graph_neighbors([_h("a.md", 0.02, 1)], links)]
    second = [h.path for h in graph_neighbors([_h("a.md", 0.02, 1)], links)]
    assert first == second == ["k.md", "m.md", "z.md"]
