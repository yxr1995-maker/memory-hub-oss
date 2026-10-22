from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.automation_core.id_backfill import apply_id_backfill, plan_id_backfill


def page(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_scans_only_allowed_markdown_and_uses_created_time(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    created = "2025-02-03T04:05:06-05:00"
    allowed = [
        "root.md",
        "concepts/one.md",
        "entities/one.md",
        "queries/nested/two.md",
        "decisions/three.md",
        "comparisons/four.md",
        "failures/five.md",
        "communities/six.md",
        "moc/seven.md",
        "atoms/eight.md",
        "doba/nine.md",
    ]
    for index, relative in enumerate(allowed):
        path = page(wiki, relative, f"---\ncreated: {created}\n---\nbody {index}\n")
        os.utime(path, (1, 1))
    excluded = [
        "raw/draft.md", "drafts/draft.md", "_archive/old.md", "archive-old/old.md",
        "extracts/item.md", ".hidden/secret.md", "entities/log.md", "entities/index.md",
        "entities/SCHEMA.md", "entities/LLM_Wiki_Rule.md",
        "entities/NEWMAX_WRITE_PROTOCOL.md", "entities/ignored.txt",
    ]
    for relative in excluded:
        page(wiki, relative, f"---\ncreated: {created}\n---\nexcluded\n")

    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))

    assert plan["total"] == len(allowed)
    assert {entry["path"] for entry in plan["entries"]} == set(allowed)
    assert all(entry["time_source"] == "created" for entry in plan["entries"])
    assert plan["generated_at"].endswith("Z")
    assert Path(plan["plan_file"]).name.startswith("id-backfill-")
    assert Path(plan["plan_file"]).suffix == ".json"
    saved = json.loads(Path(plan["plan_file"]).read_text(encoding="utf-8"))
    assert saved == plan
    assert plan["counts_by_dir"]["<root>"] == 1


def test_plan_ids_are_unique_crockford_ulids_and_skip_existing_or_parse_errors(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    existing = page(wiki, "entities/has-id.md", "---\nid: keep-me\ncreated: 2024-01-01\n---\nbody\n")
    malformed = page(wiki, "entities/bad.md", "---\ncreated: 2024-01-01\nmissing closer\n")
    page(wiki, "entities/same.md", "---\ncreated: 2024-01-01\n---\nsame body\n")
    page(wiki, "entities/same-copy.md", "---\ncreated: 2024-01-01\n---\nsame body\n")
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))

    ids = [entry["id"] for entry in plan["entries"]]
    assert len(ids) == len(set(ids)) == 2
    assert all(value.startswith("pg-") and len(value) == 29
               and set(value[3:]) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for value in ids)
    assert {entry["path"] for entry in plan["entries"]} == {"entities/same.md", "entities/same-copy.md"}
    skipped = {entry["path"]: entry["reason"] for entry in plan["skipped"]}
    assert skipped["entities/has-id.md"] == "has_id"
    assert skipped["entities/bad.md"].startswith("parse_error")
    assert "keep-me" in existing.read_text(encoding="utf-8")
    assert "missing closer" in malformed.read_text(encoding="utf-8")


def test_cli_plan_emits_json_and_writes_only_explicit_plan_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.automation_cli import main

    wiki = tmp_path / "wiki"
    page(wiki, "entities/one.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    plan_dir = tmp_path / "plans"
    assert main(["id-backfill", "--plan", "--wiki-root", str(wiki), "--plan-dir", str(plan_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {"plan_file", "generated_at", "wiki_root", "counts_by_dir", "total", "skipped_count"}
    assert payload["total"] == 1
    assert Path(payload["plan_file"]).is_relative_to(plan_dir)
    assert list(plan_dir.glob("*.json")) == [Path(payload["plan_file"])]
    assert not (tmp_path / ".memory-hub").exists()


def test_apply_records_sha_mismatch_and_preserves_page_body_and_existing_staged_file(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    page_path = page(wiki, "entities/target.md", "---\ncreated: 2025-05-06\ntitle: A\n---\nbody stays once\n")
    unrelated = page(wiki, "unrelated.txt", "staged content\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/target.md", "unrelated.txt"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    before = page_path.read_bytes()
    page_path.write_bytes(before.replace(b"title: A", b"title: changed"))
    unrelated.write_text("new unrelated staged content\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "unrelated.txt"], cwd=wiki, check=True)
    unrelated_before = subprocess.run(["git", "diff", "--cached", "--", "unrelated.txt"], cwd=wiki,
                                      check=True, capture_output=True, text=True).stdout

    result = apply_id_backfill(
        plan["plan_file"], wiki, tmp_path / "transactions", tmp_path / "lock", batch_size=1
    )

    assert any(row.get("reason") == "sha_mismatch" for row in result["results"])
    assert "id:" not in page_path.read_text(encoding="utf-8")
    assert page_path.read_text(encoding="utf-8").count("body stays once") == 1
    assert subprocess.run(["git", "diff", "--cached", "--", "unrelated.txt"], cwd=wiki,
                          check=True, capture_output=True, text=True).stdout == unrelated_before
    assert unrelated.read_text(encoding="utf-8") == "new unrelated staged content\n"


def test_apply_does_not_commit_or_overwrite_staged_target_content(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/target.md", "---\ncreated: 2025-05-06\n---\noriginal\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/target.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    staged_text = "---\ncreated: 2025-05-06\n---\nuser staged content\n"
    target.write_text(staged_text, encoding="utf-8")
    subprocess.run(["git", "add", "--", "entities/target.md"], cwd=wiki, check=True)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wiki, check=True,
                                 capture_output=True, text=True).stdout.strip()

    with pytest.raises(ValueError, match="refusing pre-staged target"):
        apply_id_backfill(
            plan["plan_file"], wiki, tmp_path / "transactions", tmp_path / "lock", batch_size=1
        )
    assert target.read_text(encoding="utf-8") == staged_text
    cached = subprocess.run(["git", "diff", "--cached", "--", "entities/target.md"], cwd=wiki,
                            check=True, capture_output=True, text=True).stdout
    assert "user staged content" in cached
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wiki, check=True,
                                capture_output=True, text=True).stdout.strip()
    assert head_after == head_before


def test_apply_resumes_completed_batches_without_duplicating_body(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    files = [page(wiki, f"entities/{index}.md", f"---\ncreated: 2025-01-01\n---\nbody {index}\n")
             for index in range(3)]
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", *[f"entities/{index}.md" for index in range(3)]], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    kwargs = dict(wiki_root=wiki, transactions_dir=tmp_path / "transactions",
                  lock_path=tmp_path / "lock", batch_size=1)

    first = apply_id_backfill(plan["plan_file"], **kwargs)
    resumed = apply_id_backfill(plan["plan_file"], **kwargs)

    assert first["counts"]["written"] == 3
    assert resumed["processed"] == 3
    assert resumed["counts"]["written"] == 3
    for index, path in enumerate(files):
        text = path.read_text(encoding="utf-8")
        assert text.count(f"body {index}") == 1
        assert text.count("id:") == 1


def test_apply_works_in_git_repository_without_initial_commit(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/first.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))

    result = apply_id_backfill(
        plan["plan_file"], wiki, tmp_path / "transactions", tmp_path / "lock", batch_size=1
    )

    assert result["counts"]["written"] == 1
    assert "id: 'pg-" in target.read_text(encoding="utf-8")
    assert subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=wiki,
                          capture_output=True).returncode == 0


def test_apply_restores_original_if_readback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/readback.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/readback.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    original = target.read_bytes()
    parse = backfill.parse_page
    calls = 0

    def fail_readback(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected readback failure")
        return parse(path)

    monkeypatch.setattr(backfill, "parse_page", fail_readback)
    with pytest.raises(ValueError, match="injected readback failure"):
        apply_id_backfill(plan["plan_file"], wiki, tmp_path / "transactions", tmp_path / "lock")
    assert target.read_bytes() == original


def test_apply_recovers_after_write_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/retry.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/retry.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    commit = backfill._commit_batch

    def fail_once(root: Path, rels: list[str], operation_id: str, *_: object) -> None:
        monkeypatch.setattr(backfill, "_commit_batch", commit)
        raise RuntimeError("injected failure after page write")

    monkeypatch.setattr(backfill, "_commit_batch", fail_once)
    kwargs = dict(wiki_root=wiki, transactions_dir=tmp_path / "transactions",
                  lock_path=tmp_path / "lock", batch_size=1)
    with pytest.raises(RuntimeError, match="injected failure"):
        apply_id_backfill(plan["plan_file"], **kwargs)
    assert target.read_text(encoding="utf-8").count("id:") == 1


def test_apply_recovers_after_commit_before_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/retry.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/retry.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    atomic_json = backfill._atomic_json

    def fail_progress_once(path: Path, value: dict[str, object]) -> None:
        if path.name == "progress.json":
            monkeypatch.setattr(backfill, "_atomic_json", atomic_json)
            raise OSError("injected progress failure")
        atomic_json(path, value)

    monkeypatch.setattr(backfill, "_atomic_json", fail_progress_once)
    kwargs = dict(wiki_root=wiki, transactions_dir=tmp_path / "transactions",
                  lock_path=tmp_path / "lock", batch_size=1)
    with pytest.raises(OSError, match="injected progress failure"):
        apply_id_backfill(plan["plan_file"], **kwargs)
    head_after_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wiki, check=True,
                                       capture_output=True, text=True).stdout.strip()

    recovered = apply_id_backfill(plan["plan_file"], **kwargs)

    assert recovered["results"][0]["status"] == "recovered"
    assert target.read_text(encoding="utf-8").count("body\n") == 1
    assert target.read_text(encoding="utf-8").count("id:") == 1
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=wiki, check=True,
                          capture_output=True, text=True).stdout.strip() == head_after_commit


def test_apply_recovers_own_staged_after_image_after_git_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/retry.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/retry.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    run = backfill.subprocess.run

    def fail_first_commit(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[object]:
        if args[:2] == ["git", "commit"]:
            monkeypatch.setattr(backfill.subprocess, "run", run)
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="injected commit failure")
        return run(args, **kwargs)

    monkeypatch.setattr(backfill.subprocess, "run", fail_first_commit)
    kwargs = dict(wiki_root=wiki, transactions_dir=tmp_path / "transactions",
                  lock_path=tmp_path / "lock", batch_size=1)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        apply_id_backfill(plan["plan_file"], **kwargs)
    staged_before_retry = subprocess.run(["git", "diff", "--cached", "--", "entities/retry.md"],
                                         cwd=wiki, check=True, capture_output=True).stdout
    assert b"pg-" in staged_before_retry

    retried = apply_id_backfill(plan["plan_file"], **kwargs)

    assert retried["counts"]["written"] == 1
    assert retried["results"][0]["status"] == "recovered"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=wiki, check=True,
                          capture_output=True, text=True).stdout == ""
    assert target.read_text(encoding="utf-8").count("body\n") == 1


def test_apply_refuses_edited_after_image_with_id_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    target = page(wiki, "entities/retry.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "entities/retry.md"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    commit = backfill._commit_batch

    def fail_once(root: Path, rels: list[str], operation_id: str, *_: object) -> None:
        monkeypatch.setattr(backfill, "_commit_batch", commit)
        raise RuntimeError("injected failure after page write")

    monkeypatch.setattr(backfill, "_commit_batch", fail_once)
    kwargs = dict(wiki_root=wiki, transactions_dir=tmp_path / "transactions",
                  lock_path=tmp_path / "lock", batch_size=1)
    with pytest.raises(RuntimeError, match="injected failure"):
        apply_id_backfill(plan["plan_file"], **kwargs)
    current = target.read_text(encoding="utf-8")
    target.write_text(current.replace("body\n", "user edit after id\n"), encoding="utf-8")

    retried = apply_id_backfill(plan["plan_file"], **kwargs)

    assert retried["counts"]["sha_mismatch"] == 1
    assert retried["results"][0]["status"] == "skipped"
    assert "user edit after id" in target.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8").count("id:") == 1


def test_ulid_uses_created_datetime_timezone_or_mtime_fallback(tmp_path: Path) -> None:
    from scripts.automation_core.id_backfill import _ALPHABET

    wiki = tmp_path / "wiki"
    aware = page(wiki, "entities/aware.md", "---\ncreated: 2025-02-03T04:05:06-05:00\n---\nbody\n")
    fallback = page(wiki, "entities/fallback.md", "---\ncreated: invalid\n---\nbody\n")
    os.utime(fallback, (1_700_000_000, 1_700_000_000))
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    entries = {entry["path"]: entry for entry in plan["entries"]}

    def milliseconds(value: str) -> int:
        decoded = 0
        for char in value[3:13]:
            decoded = decoded * 32 + _ALPHABET.index(char)
        return decoded

    assert entries["entities/aware.md"]["time_source"] == "created"
    assert milliseconds(entries["entities/aware.md"]["id"]) == int(
        datetime.fromisoformat("2025-02-03T04:05:06-05:00").timestamp() * 1000
    )
    assert entries["entities/fallback.md"]["time_source"] == "mtime"
    assert milliseconds(entries["entities/fallback.md"]["id"]) == 1_700_000_000_000


def test_ulid_collision_retries_with_unique_random_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.automation_core.id_backfill as backfill

    wiki = tmp_path / "wiki"
    for index in range(2):
        page(wiki, f"entities/{index}.md", "---\ncreated: 2025-01-01\n---\nbody\n")
    monkeypatch.setattr(backfill.secrets, "randbits", lambda bits: 0)

    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))

    assert len({entry["id"] for entry in plan["entries"]}) == 2


def test_apply_rejects_traversal_and_symlink_plan_paths_before_writing(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    outside = page(tmp_path, "outside.md", "---\ncreated: 2025-01-01\n---\noutside\n")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    base = {"generated_at": "2026-10-13T00:00:00Z", "wiki_root": str(wiki),
            "counts_by_dir": {}, "total": 1, "skipped": []}
    for name, rel in (("traversal", "../outside.md"), ("symlink", "entities/escape.md")):
        entry_path = wiki / rel if name == "symlink" else wiki / "placeholder.md"
        if name == "symlink":
            entry_path.parent.mkdir(parents=True)
            entry_path.symlink_to(outside)
        plan_file = plan_dir / f"{name}.json"
        plan_file.write_text(json.dumps({**base, "entries": [{
            "path": rel, "id": "pg-" + "0" * 26, "sha256": "0" * 64,
            "time_source": "created",
        }]}), encoding="utf-8")
        expected = "invalid or duplicate plan path" if name == "traversal" else "symlink path refused"
        with pytest.raises(ValueError, match=expected):
            apply_id_backfill(plan_file, wiki, tmp_path / "transactions", tmp_path / "lock")
    assert outside.read_text(encoding="utf-8").endswith("outside\n")


def test_lifecycle_status_updater_preserves_id_and_body_in_temp_copy(tmp_path: Path) -> None:
    source = Path.home() / "llm-wiki" / ".scripts" / "lifecycle_scan.py"
    if not source.is_file():
        pytest.skip("本机没有 ~/llm-wiki/.scripts/lifecycle_scan.py")

    copied = tmp_path / "lifecycle_scan.py"
    shutil.copyfile(source, copied)
    spec = importlib.util.spec_from_file_location("temp_lifecycle_scan", copied)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    page_path = page(
        tmp_path,
        "temp-wiki/entities/with-id.md",
        "---\nid: pg-00000000000000000000000000\nstatus: fresh\n---\nbody preserved\n",
    )

    assert module.rewrite_status(str(page_path), "aging") is True

    text = page_path.read_text(encoding="utf-8")
    assert "id: pg-00000000000000000000000000" in text
    assert "status: aging" in text
    assert text.endswith("body preserved\n")
    frontmatter, body = module.parse_fm(text)
    assert frontmatter["id"] == "pg-00000000000000000000000000"
    assert body == "body preserved\n"


def test_apply_max_batches_commit_subject_has_batch_number_and_plan_hash(tmp_path: Path) -> None:
    import hashlib

    wiki = tmp_path / "wiki"
    for i in range(3):
        page(wiki, f"concepts/p{i}.md", f"---\ncreated: 2025-01-0{i + 1}\n---\nbody {i}\n")
    (wiki / ".tmp_orphans.json").parent.mkdir(parents=True, exist_ok=True)
    (wiki / ".tmp_orphans.json").write_text("{}")
    subprocess.run(["git", "init", "-q"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=wiki, check=True)
    subprocess.run(["git", "add", "--", "concepts", ".tmp_orphans.json"], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=wiki, check=True)
    (wiki / ".tmp_orphans.json").write_text('{"dirty": 1}')
    plan = plan_id_backfill(wiki, tmp_path / "plans", now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    phash = hashlib.sha256(Path(plan["plan_file"]).read_bytes()).hexdigest()
    kw = dict(wiki_root=wiki, transactions_dir=tmp_path / "tx", lock_path=tmp_path / "lock", batch_size=2)
    first = apply_id_backfill(plan["plan_file"], max_batches=1, **kw)
    assert first["remaining"] == 1 and [b["batch"] for b in first["batches_this_run"]] == [1]
    second = apply_id_backfill(plan["plan_file"], max_batches=1, **kw)
    assert second["remaining"] == 0 and [b["batch"] for b in second["batches_this_run"]] == [2]
    subjects = subprocess.run(["git", "log", "--format=%s", "-2"], cwd=wiki, capture_output=True, text=True,
                              check=True).stdout.splitlines()
    assert subjects[0].startswith(f"chore(wiki): id backfill batch 2 plan-sha256:{phash[:16]}")
    assert subjects[1].startswith(f"chore(wiki): id backfill batch 1 plan-sha256:{phash[:16]}")
    files = subprocess.run(["git", "log", "--name-only", "--format=", "-2"], cwd=wiki, capture_output=True,
                           text=True, check=True).stdout
    assert ".tmp_orphans.json" not in files
    assert subprocess.run(["git", "status", "--short"], cwd=wiki, capture_output=True, text=True,
                          check=True).stdout.strip() == "M .tmp_orphans.json"
