"""Regression coverage for literal mixed-language recall."""
from pathlib import Path
import sqlite3

from scripts.automation_core.indexer import PAGES_DDL
from scripts.automation_core.service import SqliteRecallBackend


def backend(tmp_path, rows):
    with sqlite3.connect(tmp_path / 'index.db') as con:
        con.executescript(PAGES_DDL)
        con.executemany('INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?)', [
            (path, title, kind, '', text, text, 'project', 'test', 'high', 'active')
            for path, title, kind, text in rows
        ])
    return SqliteRecallBackend(tmp_path, tmp_path, Path(__file__).parents[1])


def test_mixed_query_preserves_short_chinese_constraint(tmp_path):
    b = backend(tmp_path, [
        ('entities/wrong.md', 'LUNARA', 'entity', 'LUNARA 商品图集与售价'),
        ('concepts/right.md', 'LUNARA 巡检', 'concept', 'LUNARA 每日巡检流程'),
    ])
    assert [h.path for h in b.fts('LUNARA 巡检')] == ['concepts/right.md']


def test_short_query_uses_index_without_ripgrep(tmp_path):
    b = backend(tmp_path, [('concepts/right.md', '广告', 'concept', '广告预算')])
    # No Markdown files exist: any result must come from the index.
    assert [h.path for h in b.fts('广告')] == ['concepts/right.md']


def test_cluster_flood_cannot_hide_curated_candidate(tmp_path):
    rows = [(f'moc/cluster-{i}.md', 'LUNARA', 'moc', 'LUNARA 巡检') for i in range(30)]
    rows.append(('entities/lunara.md', '店铺规范', 'entity', 'LUNARA 巡检流程 ' + '商品说明 ' * 200))
    b = backend(tmp_path, rows)
    hits = b.fts('LUNARA 巡检', limit=12)
    assert 'entities/lunara.md' in [h.path for h in hits]
    assert len(hits) <= 12


def test_search_collapses_identical_bodies_before_top_limit(tmp_path, monkeypatch):
    from scripts.automation_core.indexer import atomic_rebuild_index
    from scripts.automation_core.query_planner import SearchRequest
    from scripts.automation_core.service import MemoryService
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    repeated = 'LUNARA 每日巡检流程：先核对店铺身份，记录数据来源，无法获取时保留未知状态。' * 5
    for name, body in [('a', repeated), ('b', repeated), ('c', 'LUNARA' * 200 + ' 巡检后的独立价格检查流程。' * 200)]:
        (wiki / (name + '.md')).write_text(
            f'---\ntitle: {name}\ntype: concept\n---\n{body}', encoding='utf-8')
    atomic_rebuild_index(wiki, tmp_path / 'data')
    service = MemoryService(wiki, tmp_path / 'data', Path(__file__).parents[1])
    from scripts.automation_core.ranker import RecallHit
    monkeypatch.setattr(service.recall, 'fts', lambda *args: [RecallHit('a.md', 1, 1), RecallHit('b.md', 1, 2), RecallHit('c.md', 1, 3)])
    results = service.search(SearchRequest('LUNARA', top=2, fuse=False, expand=False)).results
    paths = [r.path for r in results]
    assert len(paths) == 2
    assert 'c.md' in paths
    assert len(set(paths) & {'a.md', 'b.md'}) == 1


def test_long_cjk_query_recalls_via_trigram_or(tmp_path):
    """A long mixed CJK question must not collapse to zero hits when every term
    fails the AND match; OR-trigram recall is the last resort before rg."""
    b = backend(tmp_path, [
        ('entities/fulicang.md', '海外仓', 'entity', '海外仓时效快但滞销风险高'),
    ])
    assert b.fts('海外仓还是国内直发好', 5), 'CJK question lost all recall'


def test_exact_title_precedes_pages_only_mentioning_entity():
    from dataclasses import replace
    from scripts.automation_core.indexer import IndexedPage
    from scripts.automation_core.query_planner import QueryPlan, SearchRequest
    from scripts.automation_core.ranker import RecallHit, rank_results
    p = IndexedPage('entities/claude-code.md', 'Claude Code', 'entity', '', '', 'Claude Code agent',
                    'project', 'p', 'high', 'active', '', '', '', '')
    other = replace(p, path='entities/other.md', title='Other', content='Mentions Claude Code')
    plan = QueryPlan('Claude Code', 'h', (), 'original-only', None, (), 0)
    recalls = {'original_fts': [RecallHit(other.path, 1, 1), RecallHit(p.path, 1, 2)]}
    ranked = rank_results(SearchRequest('Claude Code'), plan, recalls, {p.path:p, other.path:other})
    assert ranked[0].path == p.path
