"""RED tests: newer explicit claims supersede machine memory pages."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
import pytest


def _git(wiki: Path):
    for args in (["init"], ["config", "user.name", "Fixture"], ["config", "user.email", "f@x.invalid"], ["add", "."], ["commit", "-m", "base"]):
        subprocess.run(["git", *args], cwd=wiki, check=True, capture_output=True)


def _old_page(wiki: Path, body_quote: str, *, scope_id="proj", created="2026-01-01"):
    rel = "decisions/memory-oldclaim01.md"
    (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
    (wiki / rel).write_text(
        "---\ntitle: 历史标题\ntype: decision\ncreated: '" + created + "'\nupdated: '" + created + "'\nstatus: active\nscope: project\nscope_id: " + scope_id + "\ndecision: '" + body_quote + "'\nsources:\n  - 'codex-hook'\n---\n# 历史标题\n\n" + body_quote + "\n\n## Evidence\n- session=s; turn=t; source=codex-hook\n\n[[index]] [[SCHEMA]]\n",
        encoding="utf-8")
    return wiki / rel


def _fixture(tmp_path, publish=True):
    from scripts.automation_core.codex_integration import configure
    from scripts.automation_core import memory_worker as w
    wiki, data = tmp_path / "wiki", tmp_path / "data"
    wiki.mkdir(parents=True); data.mkdir(parents=True)
    for name in ("index.md", "log.md", "SCHEMA.md"):
        (wiki / name).write_text("# " + name + "\n", encoding="utf-8")
    _git(wiki)
    configure(data, publish=publish)
    return w, wiki, data


def _turn(turn_id, role, text, created_at="2026-09-08T01:00:00Z"):
    return {"turn_id": turn_id, "role": role, "text": text, "session_id": "sess-1",
            "project": "proj", "source": "codex-hook", "created_at": created_at}


def test_newer_explicit_claim_supersedes_machine_memory(tmp_path, monkeypatch):
    w, wiki, data = _fixture(tmp_path)
    old = _old_page(wiki, "我长期偏好报告使用紫色标题。")
    w.enqueue_turns(data, [_turn("t", "user", "我长期偏好报告使用绿色标题。")])
    monkeypatch.setattr(w, "propose", lambda rows: [
        {"kind": "preference", "title": "标题颜色", "quote": rows[0]["text"], "queue_ids": [rows[0]["id"]]}])
    result = w.run_once(wiki, data)
    assert result["published"] == 1 and result.get("superseded") == 1
    old_text = old.read_text()
    assert "status: deprecated" in old_text
    new_pages = sorted(p for p in (wiki / "decisions").glob("memory-*.md") if p != old)
    assert len(new_pages) == 1
    stem = new_pages[0].stem
    assert f"deprecated_by: '[[decisions/{stem}]]'" in old_text
    with w.connect(data) as c:
        assert c.execute("select status from queue").fetchone()[0] == "done"
    from scripts.automation_core.query_planner import SearchRequest
    from scripts.automation_core.service import MemoryService
    hits = MemoryService(wiki, data, tmp_path).search(SearchRequest("报告使用", expand=False, fuse=False)).results
    assert hits and hits[0].path == f"decisions/{stem}.md"


def test_conflict_with_curated_page_stays_candidate(tmp_path, monkeypatch):
    w, wiki, data = _fixture(tmp_path)
    curated = wiki / "concepts/curated-rule.md"
    curated.parent.mkdir(parents=True)
    curated.write_text("---\ntitle: 人工规范\ntype: concept\ncreated: '2026-01-01'\nupdated: '2026-01-01'\nstatus: active\nscope: project\nscope_id: proj\nabstract: '我长期偏好报告使用紫色标题。'\n---\n# 人工规范\n\n我长期偏好报告使用紫色标题。\n", encoding="utf-8")
    _git(wiki)
    before = curated.read_text()
    w.enqueue_turns(data, [_turn("t", "user", "我长期偏好报告使用绿色标题。")])
    monkeypatch.setattr(w, "propose", lambda rows: [
        {"kind": "preference", "title": "标题颜色", "quote": rows[0]["text"], "queue_ids": [rows[0]["id"]]}])
    result = w.run_once(wiki, data)
    assert result["published"] == 0 and result["candidates"] == 1
    assert curated.read_text() == before


def test_older_claim_cannot_supersede(tmp_path, monkeypatch):
    w, wiki, data = _fixture(tmp_path)
    old = _old_page(wiki, "我长期偏好报告使用紫色标题。", created="2026-10-18")
    before = old.read_text()
    w.enqueue_turns(data, [_turn("t", "user", "我长期偏好报告使用绿色标题。", "2026-09-08T01:00:00Z")])
    monkeypatch.setattr(w, "propose", lambda rows: [
        {"kind": "preference", "title": "标题颜色", "quote": rows[0]["text"], "queue_ids": [rows[0]["id"]]}])
    result = w.run_once(wiki, data)
    assert result["published"] == 0 and old.read_text() == before
    with w.connect(data) as c:
        assert c.execute("select status from queue").fetchone()[0] == "candidate"


def test_semantic_conflict_supersedes_when_deterministic_says_new(tmp_path, monkeypatch):
    w, wiki, data = _fixture(tmp_path)
    old = _old_page(wiki, "我长期偏好报告使用紫色标题。")
    w.enqueue_turns(data, [_turn("t", "user", "我长期偏好报告使用绿色标题。")])
    monkeypatch.setattr(w, "propose", lambda rows: [
        {"kind": "preference", "title": "标题颜色", "quote": rows[0]["text"], "queue_ids": [rows[0]["id"]]}])
    monkeypatch.setattr(w, "content_relation", lambda *a, **k: "new")
    monkeypatch.setattr(w, "semantic_relation", lambda *a, **k: {
        "relation": "conflict", "path": str(old), "matched_quote": "我长期偏好报告使用紫色标题。"})
    result = w.run_once(wiki, data)
    assert result["published"] == 1 and result.get("superseded") == 1
    assert "status: deprecated" in old.read_text()
