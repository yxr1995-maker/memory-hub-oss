"""distill/publish 稳定页面 ID：新页生成 pg-<ULID>，覆盖时沿用旧 id，发布后校验唯一。只用临时目录。"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

from scripts.automation_core.frontmatter import parse_page
from scripts.automation_core.id_backfill import new_page_id

ROOT = pathlib.Path(__file__).resolve().parents[1]
ID_RE = re.compile(r"^pg-[0-7][0-9A-HJKMNP-TV-Z]{25}$")

PAGE = """---
type: concept
title: 'Demo'
created: '2026-10-13'
tags:
  - demo
sources:
  - x
abstract: 'a'
{extra}---

# Demo

body
"""


def _env(tmp: pathlib.Path) -> dict[str, str]:
    home = tmp / "home"; home.mkdir(exist_ok=True)
    return {**os.environ, "HOME": str(home), "WIKI_PATH": str(tmp / "wiki"),
            "MEMORY_HUB_STAGING": str(tmp / "staging"), "PYTHONPATH": str(ROOT)}


def _publish(tmp: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(ROOT / "scripts" / "publish.sh"), "--apply"], cwd=ROOT,
                          env=_env(tmp), text=True, capture_output=True, check=False)


def _setup(tmp: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    wiki = tmp / "wiki"; (wiki / "concepts").mkdir(parents=True)
    (wiki / "index.md").write_text("# index\n"); (wiki / "log.md").write_text("# log\n")
    pages = tmp / "staging" / "pages"; pages.mkdir(parents=True)
    return wiki, pages


def test_new_page_id_format_and_unique() -> None:
    ids = {new_page_id() for _ in range(2000)}
    assert len(ids) == 2000 and all(ID_RE.match(i) for i in ids)


def test_publish_assigns_id_to_new_page(tmp_path: pathlib.Path) -> None:
    wiki, pages = _setup(tmp_path)
    (pages / "demo.md").write_text(PAGE.format(extra=""))
    proc = _publish(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = parse_page(wiki / "concepts" / "demo.md")
    assert ID_RE.match(str(out.frontmatter["id"]))
    assert (wiki / "concepts" / "demo.md").read_text().count("body") == 1


def test_publish_keeps_existing_staging_id(tmp_path: pathlib.Path) -> None:
    wiki, pages = _setup(tmp_path)
    pid = new_page_id()
    (pages / "demo.md").write_text(PAGE.format(extra=f"id: {pid}\n"))
    proc = _publish(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert parse_page(wiki / "concepts" / "demo.md").frontmatter["id"] == pid


def test_publish_rejects_duplicate_id(tmp_path: pathlib.Path) -> None:
    wiki, pages = _setup(tmp_path)
    pid = new_page_id()
    (wiki / "concepts" / "other.md").write_text(PAGE.format(extra=f"id: {pid}\n"))
    (pages / "demo.md").write_text(PAGE.format(extra=f"id: {pid}\n"))
    proc = _publish(tmp_path)
    assert proc.returncode != 0
    assert "页面 id 不唯一" in proc.stderr


def test_distill_python_block_adds_id_only_when_missing() -> None:
    text = (ROOT / "scripts" / "distill.sh").read_text()
    assert "new_page_id" in text and 'document.frontmatter.get("id")' in text


def test_publish_requires_id_field() -> None:
    text = (ROOT / "scripts" / "publish.sh").read_text()
    assert 'REQUIRED_FIELDS="title type created tags sources abstract id"' in text
