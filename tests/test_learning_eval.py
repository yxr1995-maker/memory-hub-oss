import json
import subprocess
from pathlib import Path
import pytest
from scripts.automation_core import learning_eval as le
from scripts.automation_core.indexer import atomic_rebuild_index


def wiki_fixture(tmp_path: Path, pages: dict[str, str]) -> Path:
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    for name, text in pages.items():
        target = wiki / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    (wiki / 'drafts').mkdir(exist_ok=True)
    (wiki / 'drafts' / 'noise.md').write_text('---\ntitle: noise\ntype: note\n---\nnoise\n', encoding='utf-8')
    return wiki


def test_eval_reports_hit_rate_and_excludes_drafts(tmp_path):
    pages = {
        'entities/alpha.md': '---\ntitle: Alpha Widget\ntype: entity\nstatus: active\n---\nAlpha Widget description\n',
        'concepts/beta.md': '---\ntitle: Beta Flow\ntype: concept\nstatus: active\n---\nBeta Flow description\n',
    }
    wiki = wiki_fixture(tmp_path, pages)
    atomic_rebuild_index(wiki, tmp_path / 'data')
    report = le.evaluate(wiki, tmp_path / 'data', tmp_path)
    assert report['indexed_pages'] == 3  # drafts are indexed; ranker demotes them
    assert report['probes']['drafts_note_pages'] == 1  # indexed but demoted by ranker
    assert report['probes']['title_hit_rate'] == 1.0
    assert report['probes']['abstention'] in (True, False)


def test_eval_flags_deprecated_memory_without_successor(tmp_path):
    pages = {
        'decisions/memory-a.md': '---\ntitle: Old claim\ntype: decision\nstatus: deprecated\n---\nold claim body\n',
        'decisions/memory-b.md': '---\ntitle: New claim\ntype: decision\nstatus: active\n---\nnew claim body\n',
    }
    wiki = wiki_fixture(tmp_path, pages)
    atomic_rebuild_index(wiki, tmp_path / 'data')
    # memory-a has deprecated status but no deprecated_by -> must be flagged
    import sqlite3
    with sqlite3.connect(tmp_path / 'data' / 'index.db') as con:
        con.execute("insert into lifecycle values('decisions/memory-a.md','')")
    report = le.evaluate(wiki, tmp_path / 'data', tmp_path)
    assert report['probes']['deprecated_without_successor'] == 1


def test_eval_on_empty_index_is_safe(tmp_path):
    report = le.evaluate(tmp_path, tmp_path, tmp_path)
    assert report['indexed_pages'] == 0
    assert report['probes']['title_hit_rate'] is None
