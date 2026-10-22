from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .frontmatter import parse_page, patch_frontmatter
from .operation import AutomationLock
from .schema import Mode, OperationContext

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ROOTS = ("concepts", "entities", "queries", "decisions", "comparisons", "failures", "communities", "moc", "atoms", "doba")
_EXCLUDED_FILES = {"log.md", "index.md", "SCHEMA.md", "LLM_Wiki_Rule.md", "NEWMAX_WRITE_PROTOCOL.md"}
_EXCLUDED_DIRS = {"raw", "drafts", "raw-drafts", "_archive", "extracts"}


def _ulid(timestamp_ms: int, used: set[str]) -> str:
    if timestamp_ms < 0 or timestamp_ms >= (1 << 48):
        raise ValueError("ULID timestamp outside 48-bit range")
    random_part = secrets.randbits(80)
    for _ in range(len(used) + 1):
        value = (timestamp_ms << 80) | random_part
        chars = ["0"] * 26
        for index in range(25, -1, -1):
            chars[index] = _ALPHABET[value & 31]
            value >>= 5
        result = "pg-" + "".join(chars)
        if result not in used:
            used.add(result)
            return result
        random_part = (random_part + 1) & ((1 << 80) - 1)
    raise ValueError("ULID collision space exhausted")


def new_page_id(timestamp_ms: int | None = None) -> str:
    """Fresh pg-<ULID> for a new page (distill/publish)."""
    import time
    return _ulid(int(time.time() * 1000) if timestamp_ms is None else timestamp_ms, set())


def _created_ms(value: object) -> int | None:
    if not isinstance(value, (str, date, datetime)):
        return None
    try:
        parsed = value if isinstance(value, (date, datetime)) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(parsed, datetime):
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _atomic_page_write(path: Path, content: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp.exists():
            temp.unlink()


def _safe_page(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    cursor = root
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink path refused: {rel}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes wiki root: {rel}")
    return rel


def _in_scope(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if len(parts) == 1:
        return parts[0].endswith(".md") and parts[0] not in _EXCLUDED_FILES
    return parts[0] in _ROOTS and parts[-1].endswith(".md") and parts[-1] not in _EXCLUDED_FILES and not any(
        part.startswith(".") or part in _EXCLUDED_DIRS or part.startswith("archive-") for part in parts[:-1]
    )


def _candidates(root: Path) -> list[Path]:
    paths: set[Path] = set(root.glob("*.md"))
    for directory in _ROOTS:
        base = root / directory
        if base.is_dir() and not base.is_symlink():
            paths.update(base.rglob("*.md"))
    out = []
    for path in paths:
        rel = path.relative_to(root)
        if path.name in _EXCLUDED_FILES or any(part.startswith(".") or part in _EXCLUDED_DIRS or part.startswith("archive-") for part in rel.parts[:-1]):
            continue
        out.append(path)
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def plan_id_backfill(wiki_root: Path, plan_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    root = Path(wiki_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"wiki root is not a directory: {root}")
    generated = now or datetime.now(timezone.utc)
    generated = (generated.replace(tzinfo=timezone.utc) if generated.tzinfo is None
                 else generated.astimezone(timezone.utc))
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    plan_file = Path(plan_dir).expanduser() / f"id-backfill-{stamp}.json"
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for path in _candidates(root):
        rel = _safe_page(root, path)
        try:
            page = parse_page(path)
        except Exception as exc:
            skipped.append({"path": rel, "reason": f"parse_error: {exc}"})
            continue
        current_id = page.frontmatter.get("id")
        if current_id is not None and str(current_id).strip():
            skipped.append({"path": rel, "reason": "has_id"})
            continue
        raw = path.read_bytes()
        created_ms = _created_ms(page.frontmatter.get("created"))
        source = "created" if created_ms is not None else "mtime"
        if created_ms is None or not 0 <= created_ms < (1 << 48):
            source = "mtime"
            timestamp_ms = int(path.stat().st_mtime * 1000)
        else:
            timestamp_ms = created_ms
        if not 0 <= timestamp_ms < (1 << 48):
            skipped.append({"path": rel, "reason": "invalid_mtime"})
            continue
        entries.append({"path": rel, "id": _ulid(timestamp_ms, used), "sha256": _sha(raw), "time_source": source})
        parent = path.parent.relative_to(root).parts
        directory = parent[0] if parent else "<root>"
        counts[directory] = counts.get(directory, 0) + 1
    plan = {"generated_at": generated.isoformat().replace("+00:00", "Z"), "wiki_root": str(root),
            "counts_by_dir": counts, "total": len(entries), "entries": entries, "skipped": skipped,
            "plan_file": str(plan_file.resolve())}
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with plan_file.open("x", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise ValueError(f"plan already exists: {plan_file}") from exc
    return plan


def _batch_subject(operation_id: str, batch_no: int, plan_hash: str) -> str:
    return f"chore(wiki): id backfill batch {batch_no} plan-sha256:{plan_hash[:16]} {operation_id}"


def _head_matches_batch(root: Path, rels: list[str], subject_expected: str) -> bool:
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True)
    if head.returncode:
        return False
    subject = subprocess.run(["git", "show", "-s", "--format=%s", "HEAD"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
    paths = subprocess.run(["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"],
                           cwd=root, capture_output=True, check=True).stdout.decode(errors="surrogateescape").split("\0")
    if subject != subject_expected or set(filter(None, paths)) != set(rels):
        return False
    return all(subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, check=True).stdout
               == (root / rel).read_bytes() for rel in rels)


def _commit_batch(root: Path, rels: list[str], operation_id: str, batch_no: int = 1, plan_hash: str = "") -> None:
    if not rels:
        return
    subject_expected = _batch_subject(operation_id, batch_no, plan_hash)
    subprocess.run(["git", "add", "--", *rels], cwd=root, check=True, capture_output=True)
    if _head_matches_batch(root, rels, subject_expected):
        return
    proc = subprocess.run(["git", "commit", "--only", "-m", subject_expected, "--", *rels],
                          cwd=root, check=False, capture_output=True, text=True)
    if proc.returncode:
        if not _head_matches_batch(root, rels, subject_expected):
            raise RuntimeError(f"git commit failed: {proc.stderr.strip()[:300]}")


def apply_id_backfill(plan_file: Path, wiki_root: Path, transactions_dir: Path, lock_path: Path,
                      batch_size: int = 500, max_batches: int | None = None) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    plan_path = Path(plan_file).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root = Path(wiki_root).expanduser().resolve()
    if root != Path(plan["wiki_root"]).resolve():
        raise ValueError("wiki root does not match plan")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise ValueError("invalid plan entries")
    seen: set[str] = set()
    seen_ids: set[str] = set()
    raw_plan = plan_path.read_bytes()
    plan_hash = _sha(raw_plan)
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("path", "id", "sha256")):
            raise ValueError("invalid plan entry")
        rel = entry["path"]
        parsed = PurePosixPath(rel)
        if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts or parsed.as_posix() != rel or rel in seen:
            raise ValueError(f"invalid or duplicate plan path: {rel}")
        if not _in_scope(rel):
            raise ValueError(f"plan path outside id backfill scope: {rel}")
        if len(entry["id"]) != 29 or not entry["id"].startswith("pg-") or entry["id"][3] not in _ALPHABET[:8] or any(c not in _ALPHABET for c in entry["id"][3:]):
            raise ValueError(f"invalid plan id: {rel}")
        if entry["id"] in seen_ids:
            raise ValueError(f"duplicate plan id: {entry['id']}")
        if len(entry["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in entry["sha256"]):
            raise ValueError(f"invalid plan hash: {rel}")
        seen.add(rel)
        seen_ids.add(entry["id"])
        _safe_page(root, root.joinpath(*parsed.parts))
    if entries and subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
                                  capture_output=True, text=True).returncode:
        raise ValueError("wiki root must be a Git worktree for apply")
    if entries:
        git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                  capture_output=True, text=True, check=True).stdout.strip()
        if Path(git_root).resolve() != root:
            raise ValueError("wiki root must be the Git worktree root")
    tx_root = Path(transactions_dir).expanduser() / plan_path.stem
    tx_root.mkdir(parents=True, exist_ok=True)
    progress_file = tx_root / "progress.json"
    op = OperationContext(plan_path.stem, "id-backfill", Mode.NO_AUTO, False, True, root, tx_root.parent)
    lock = AutomationLock.acquire(tx_root.parent, op, lock_path=Path(lock_path).expanduser())
    try:
        progress = json.loads(progress_file.read_text(encoding="utf-8")) if progress_file.exists() else {
            "cursor": 0, "results": [], "plan_sha256": plan_hash}
        if progress.get("plan_sha256") != plan_hash:
            raise ValueError("plan changed after apply started")
        cursor = int(progress.get("cursor", 0))
        results = list(progress.get("results", []))
        if cursor < 0 or cursor > len(entries) or len(results) != cursor:
            raise ValueError("invalid transaction progress")
        staged = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"], cwd=root,
                                capture_output=True, check=True).stdout.decode(errors="surrogateescape").split("\0")
        by_path = {entry["path"]: entry for entry in entries}
        for rel in seen.intersection(staged):
            entry = by_path[rel]
            backup = tx_root / "before-images" / (hashlib.sha256(rel.encode()).hexdigest() + ".before")
            try:
                previous = backup.read_bytes()
                old_page = parse_page(backup)
                expected = patch_frontmatter(old_page, {"id": entry["id"]})
                current = (root / rel).read_bytes()
                index = subprocess.run(["git", "show", f":{rel}"], cwd=root,
                                       capture_output=True, check=True).stdout
                if _sha(previous) != entry["sha256"] or current != expected or index != expected:
                    raise ValueError("staged bytes do not match this plan")
            except Exception as exc:
                raise ValueError(f"refusing pre-staged target {rel}: {exc}") from exc
        run_batches: list[dict[str, Any]] = []
        while cursor < len(entries) and (max_batches is None or len(run_batches) < max_batches):
            batch_no = cursor // batch_size + 1
            batch = entries[cursor:cursor + batch_size]
            changed: list[str] = []
            batch_results: list[dict[str, str]] = []
            for entry in batch:
                rel = entry["path"]
                path = root / rel
                safe_rel = _safe_page(root, path)
                if safe_rel != rel or not path.is_file():
                    batch_results.append({"path": rel, "status": "skipped", "reason": "missing_or_unsafe"})
                    continue
                original = path.read_bytes()
                backup = tx_root / "before-images" / (hashlib.sha256(rel.encode()).hexdigest() + ".before")
                staged_result = subprocess.run(["git", "diff", "--cached", "--", rel], cwd=root,
                                               capture_output=True, check=True).stdout
                if staged_result:
                    try:
                        previous = backup.read_bytes()
                        old_page = parse_page(backup)
                        expected = patch_frontmatter(old_page, {"id": entry["id"]})
                        index = subprocess.run(["git", "show", f":{rel}"], cwd=root,
                                               capture_output=True, check=True).stdout
                        if _sha(previous) != entry["sha256"] or original != expected or index != expected:
                            raise ValueError("staged target is not owned by this operation")
                        changed.append(rel)
                        batch_results.append({"path": rel, "status": "recovered"})
                        continue
                    except Exception as exc:
                        raise ValueError(f"refusing pre-staged target {rel}: {exc}") from exc
                if _sha(original) != entry["sha256"]:
                    try:
                        previous = backup.read_bytes()
                        if _sha(previous) != entry["sha256"]:
                            raise ValueError("backup hash mismatch")
                        old_page = parse_page(backup)
                        expected = patch_frontmatter(old_page, {"id": entry["id"]})
                        owned = original == expected
                    except Exception:
                        owned = False
                    if owned:
                        changed.append(rel)
                        batch_results.append({"path": rel, "status": "recovered"})
                    else:
                        batch_results.append({"path": rel, "status": "skipped", "reason": "sha_mismatch"})
                    continue
                try:
                    page = parse_page(path)
                    if str(page.frontmatter.get("id", "")).strip():
                        batch_results.append({"path": rel, "status": "skipped", "reason": "has_id"})
                        continue
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if not backup.exists():
                        _atomic_page_write(backup, original)
                    if _sha(backup.read_bytes()) != entry["sha256"]:
                        raise ValueError(f"backup verification failed: {rel}")
                    rendered = patch_frontmatter(page, {"id": entry["id"]})
                    try:
                        _atomic_page_write(path, rendered)
                        check = parse_page(path)
                        if check.frontmatter.get("id") != entry["id"]:
                            raise ValueError("readback id mismatch")
                    except Exception:
                        if path.exists() and path.read_bytes() == rendered:
                            _atomic_page_write(path, original)
                        raise
                    changed.append(rel)
                    batch_results.append({"path": rel, "status": "applied"})
                except Exception as exc:
                    batch_results.append({"path": rel, "status": "failed", "reason": str(exc)})
                    raise
            _commit_batch(root, changed, plan_path.stem, batch_no, plan_hash)
            run_batches.append({"batch": batch_no, "changed": len(changed), "processed": len(batch),
                                "skipped": [r for r in batch_results if r["status"] == "skipped"]})
            cursor += len(batch)
            results.extend(batch_results)
            _atomic_json(progress_file, {"cursor": cursor, "results": results, "plan_sha256": plan_hash})
        counts = {"written": sum(r["status"] in ("applied", "recovered") for r in results),
                  "sha_mismatch": sum(r.get("reason") == "sha_mismatch" for r in results),
                  "skipped": sum(r["status"] == "skipped" for r in results)}
        return {"plan_file": str(plan_path), "wiki_root": str(root), "processed": len(results),
                "remaining": len(entries) - cursor, "plan_sha256": plan_hash, "batches_this_run": run_batches,
                "counts": counts, "results": results}
    finally:
        lock.release()
