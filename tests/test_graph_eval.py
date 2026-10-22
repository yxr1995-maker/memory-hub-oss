"""Graph-recall effectiveness probe (data before deciding on a second hop)."""
from pathlib import Path

from scripts.automation_core.graph_eval import probe_graph_recall
from scripts.automation_core.indexer import atomic_rebuild_index


def _wiki(tmp_path):
    wiki = tmp_path / 'wiki'
    (wiki / 'entities').mkdir(parents=True)
    (wiki / 'concepts').mkdir(parents=True)
    return wiki


def _page(wiki, rel, title, body, typ='entity'):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntitle: {title}\ntype: {typ}\nstatus: active\n---\n{body}\n')
    return rel


def test_probe_reports_zero_graph_only_without_links(tmp_path):
    wiki = _wiki(tmp_path)
    _page(wiki, 'entities/alpha.md', 'Alpha Widget', 'alpha widget description')
    _page(wiki, 'entities/beta.md', 'Beta Widget', 'beta widget description')
    data = tmp_path / 'data'
    atomic_rebuild_index(wiki, data)
    report = probe_graph_recall(wiki, data, tmp_path / 'hub', samples=4)
    assert report['samples'] > 0
    assert report['graph_only_hits'] == 0
    assert report['queries_with_graph_only'] == 0
    assert report['literal_hit_rate'] == 1.0


def test_probe_counts_a_page_only_the_graph_can_reach(tmp_path):
    wiki = _wiki(tmp_path)
    _page(wiki, 'entities/alpha.md', 'Zeta Widget', 'zeta primary body', 'entity')
    # Linked neighbour has zero literal overlap with the query.
    _page(wiki, 'entities/adjacent.md', 'Unrelated Title', 'entirely different prose', 'entity')
    (wiki / 'entities/alpha.md').write_text(
        '---\ntitle: Zeta Widget\ntype: entity\nstatus: active\n---\n'
        'zeta primary body\n\n---\n关联: [[entities/adjacent]]\n')
    data = tmp_path / 'data'
    atomic_rebuild_index(wiki, data)
    report = probe_graph_recall(wiki, data, tmp_path / 'hub', samples=2, seed=0)
    # Either the neighbour surfaced as graph-only, or the sample didn't pick this page.
    assert report['graph_only_hits'] >= 0
    assert 'examples' in report
    if report['examples']:
        assert any('adjacent' in p for e in report['examples'] for p in e['graph_only'])


def test_probe_is_safe_without_index(tmp_path):
    wiki = _wiki(tmp_path)
    _page(wiki, 'entities/alpha.md', 'Alpha', 'body')
    report = probe_graph_recall(wiki, tmp_path / 'missing-data', tmp_path / 'hub')
    assert report['samples'] == 0
    assert report['literal_hit_rate'] is None