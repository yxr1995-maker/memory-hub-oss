from __future__ import annotations

import sys
import pathlib
_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts.automation_core.query_planner import SearchRequest
from scripts.automation_core.ranker import DEFAULT_TAU, render_human
from scripts.automation_core.schema import Mode, OperationContext, new_operation_id
from scripts.automation_core.scope import apply_backfill, plan_backfill
from scripts.automation_core.service import MemoryService


def _get_service() -> MemoryService:
    hub = Path(__file__).resolve().parents[1]
    wiki = Path(os.environ.get("WIKI_PATH", str(Path.home() / "llm-wiki"))).resolve()
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    return MemoryService(wiki, data, hub)


def _trigger_incremental_embed(wiki: Path, data: Path) -> None:
    """Task B: 成功管道末尾触发一次增量向量索引。

    只走 subprocess 调 scripts/embed.py index（增量，跳过已嵌入页）；
    显式透传 WIKI_PATH/MEMORY_HUB_DATA；timeout=60；失败只 stderr warn，
    绝不改变调用方退出码。调用方负责 --safe 不触发。"""
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "embed.py"), "index"]
    env = dict(os.environ, WIKI_PATH=str(wiki), MEMORY_HUB_DATA=str(data))
    try:
        proc = subprocess.run(cmd, env=env, timeout=60, capture_output=True, text=True)
    except Exception as exc:
        print(f"embed: 增量索引未触发（已忽略）: {exc}", file=sys.stderr)
        return
    if proc.returncode != 0:
        print(
            f"embed: 增量索引退出码 {proc.returncode}（已忽略）: "
            f"{(proc.stderr or '').strip()[-500:]}",
            file=sys.stderr,
        )
    else:
        print("embed: 增量索引完成", file=sys.stderr)


def _scope_backfill(args: argparse.Namespace) -> int:
    wiki = Path(os.environ.get("WIKI_PATH", str(Path.home() / "llm-wiki"))).resolve()
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    operation_id = os.environ.get("MEMORY_HUB_OPERATION_ID") or new_operation_id(
        datetime.now(timezone.utc), uuid4
    )
    ctx = OperationContext(
        operation_id=operation_id,
        command="scope-backfill",
        mode=Mode.AUTO,
        auto=True,
        apply=args.apply,
        wiki_path=wiki,
        data_path=data,
    )
    report = apply_backfill(plan_backfill(wiki, args.cursor, args.limit, ctx), ctx)
    payload = {
        "operation_id": operation_id,
        "apply": args.apply,
        "counts": report.counts,
        "next_cursor": report.next_cursor,
        "report": str(report.report_path.relative_to(data)),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"scope-backfill: {'apply' if args.apply else 'dry-run'} "
            f"counts={json.dumps(report.counts, sort_keys=True)} "
            f"next_cursor={report.next_cursor or '-'} report={payload['report']}"
        )
    return 0


def _id_backfill(args: argparse.Namespace) -> int:
    from scripts.automation_core.id_backfill import apply_id_backfill, plan_id_backfill
    from scripts.automation_core.operation import LockBusy

    wiki = Path(args.wiki_root).expanduser()
    if args.plan:
        payload = plan_id_backfill(wiki, Path(args.plan_dir).expanduser())
        skipped_count = len(payload["skipped"])
        payload = {key: payload[key] for key in ("plan_file", "generated_at", "wiki_root", "counts_by_dir", "total")}
        payload["skipped_count"] = skipped_count
    else:
        if not args.plan_file:
            print("id-backfill: --plan-file is required with --apply", file=sys.stderr)
            return 2
        try:
            payload = apply_id_backfill(
                Path(args.plan_file), wiki, Path(args.transactions_dir).expanduser(),
                Path(args.lock_path).expanduser(), args.batch_size, args.max_batches,
            )
        except LockBusy as exc:
            print(f"id-backfill: {exc}", file=sys.stderr)
            return exc.exit_code
        except RuntimeError as exc:
            print(f"id-backfill: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _search(args: argparse.Namespace) -> int:
    service = _get_service()
    if len(args.query) > 500:
        print("search: query exceeds 500 characters", file=sys.stderr)
        return 2
    if args.top < 1 or args.top > 50:
        print("search: top must be between 1 and 50", file=sys.stderr)
        return 2
    if args.scope and args.scope not in ("user", "project", "agent"):
        print("search: invalid scope", file=sys.stderr)
        return 2

    # --no-fts means full-text only (no vector): in the unified service the
    # full-text backend already falls back to rg when no index exists.
    # --depth controls effort without changing the API shape: fast disables
    # expansions and the graph channel, deep currently behaves as standard
    # (expansion planner retains the budget decision).
    expand = args.expand and args.depth != "fast"
    request = SearchRequest(
        query=args.query,
        top=args.top,
        fuse=args.fuse and args.fts and args.depth != "fast",
        expand=expand,
        scope=args.scope,
        scope_id=args.scope_id,
        explain=args.explain,
    )
    response = service.search(request, tau=args.tau)

    if args.json:
        print(json.dumps(response.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        if args.explain:
            print(f"== Query Plan ({response.plan.get('planner')}): {json.dumps(response.plan)} ==")
        if not response.results:
            print("无命中")
        else:
            print(f"== 检索 ({'fuse' if request.fuse else 'fts'}): {request.query} ==")
            print(render_human(response.results))
    return 0


def _ask(args: argparse.Namespace) -> int:
    service = _get_service()
    if len(args.query) > 500:
        print("ask: query exceeds 500 characters", file=sys.stderr)
        return 2
    if args.top < 1 or args.top > 50:
        print("ask: top must be between 1 and 50", file=sys.stderr)
        return 2
    if args.scope and args.scope not in ("user", "project", "agent"):
        print("ask: invalid scope", file=sys.stderr)
        return 2

    request = SearchRequest(
        query=args.query,
        top=args.top,
        fuse=args.fuse,
        expand=args.expand,
        scope=args.scope,
        scope_id=args.scope_id,
        explain=args.explain,
    )
    ctx = service.ask_context(request)

    if args.json:
        print(json.dumps(ctx.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        if ctx.answer:
            print("== 回答 ==")
            print(ctx.answer)
            print("")
        else:
            print(f"answer_status={ctx.answer_status} reason={ctx.answer_error or 'no_context'}")
        print("== 引用页面 ==")
        for r in ctx.results:
            print(f"  - {r.path} — {r.title}")
    return 0


def _decide(args: argparse.Namespace) -> int:
    """Grounded decision: the model only picks among permitted options, never acts."""
    from scripts.automation_core.decision import DecisionEngine, DecisionError
    service = _get_service()
    engine = DecisionEngine(service)
    try:
        result = engine.decide(
            args.question,
            options=args.option or None,
            levels=args.level or None,
            constraints=args.constraint or None,
            top_k=args.top_k,
            instructions=args.instructions,
        )
    except DecisionError as exc:
        print(f"decide: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _sops(args: argparse.Namespace) -> int:
    """List repeated successful action chains (procedural-memory candidates)."""
    from scripts.automation_core.sop_extract import find_sops, load_tool_steps
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    steps = load_tool_steps(data / "codex-memory.db", limit=args.limit_rows)
    found = find_sops(steps, min_occurrences=args.min_occurrences, window=args.window)
    if args.json:
        print(json.dumps({"tool_steps": len(steps), "candidates": len(found),
                          "sops": [s.to_dict() for s in found]}, ensure_ascii=False, indent=2))
    else:
        from collections import Counter
        classes = Counter(s.tool for s in steps).most_common(5)
        print(f"工具观测 {len(steps)} 条；主要工具类别：{classes}")
        print(f"重复成功链（≥{args.min_occurrences} 次）：{len(found)} 组")
        for s in found:
            print(f"- {s.occurrences}x [{s.project}] {' → '.join(s.signature)}")
    return 0


def _conflict_scan(args: argparse.Namespace) -> int:
    """List contradictions between already-published pages (read-only)."""
    from scripts.automation_core.conflict_scan import scan_index_contradictions
    wiki = Path(os.environ.get("WIKI_PATH", str(Path.home() / "llm-wiki"))).resolve()
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    found = scan_index_contradictions(data / "index.db", wiki, limit=args.limit)
    # Collapse repeated value pairs: 300 pages mentioning two ports is one story.
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for c in found:
        low, high = sorted((c.value_a, c.value_b))
        key = (c.attribute, low, high)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    if args.json:
        print(json.dumps({"raw_pairs": len(found), "unique": len(unique),
                          "contradictions": [c.to_dict() for c in unique]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"扫描到 {len(found)} 组页面配对，去重后 {len(unique)} 组矛盾：")
        for c in unique:
            print(f"- [{c.attribute}] {c.value_a} ⇄ {c.value_b}"
                  + ("（否定冲突）" if c.negation_clash else ""))
            print(f"    {c.path_a}")
            print(f"    {c.path_b}")
    return 0


def _get_store():
    """StateStore shares the data dir with the service (KV + timeline)."""
    from scripts.automation_core.state_store import StateStore
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    return StateStore(data)


def _kv(args: argparse.Namespace) -> int:
    store = _get_store()
    if args.kv_action == "set":
        store.upsert_kv(args.scope, args.scope_id, args.key, args.value,
                        evidence_path=args.evidence, source="cli")
        print("ok")
    elif args.kv_action == "get":
        entry = store.get_kv(args.scope, args.scope_id, args.key)
        if entry is None:
            print("not found", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(asdict(entry), ensure_ascii=False))
        else:
            print(f"{entry.key} = {entry.value}")
            if entry.evidence_path:
                print(f"  evidence: {entry.evidence_path}")
    elif args.kv_action == "list":
        entries = store.list_kv(args.scope, args.scope_id)
        if args.json:
            print(json.dumps([asdict(e) for e in entries], ensure_ascii=False))
        else:
            for e in entries:
                print(f"{e.scope_id}\t{e.key} = {e.value}")
    elif args.kv_action == "rm":
        print("ok" if store.delete_kv(args.scope, args.scope_id, args.key) else "not found")
    return 0


def _timeline(args: argparse.Namespace) -> int:
    store = _get_store()
    def _edge_dict(e):
        return {"subject": e.subject, "predicate": e.predicate, "object": e.object,
                "valid_at": e.valid_at, "invalid_at": e.invalid_at,
                "superseded_by": e.superseded_by, "evidence_path": e.evidence_path,
                "confidence": e.confidence}
    if args.tl_action == "add":
        rowid = store.record_edge(
            args.subject, args.predicate, args.object,
            valid_at=args.valid_at, evidence_path=args.evidence,
            exclusive=args.exclusive,
        )
        print(rowid)
    elif args.tl_action == "active":
        edges = store.active_edges(args.subject or None, args.predicate or None, args.object or None)
        if getattr(args, "json", False):
            print(json.dumps({"edges": [_edge_dict(e) for e in edges]}, ensure_ascii=False))
        else:
            for e in edges:
                print(f"{e.subject}\t{e.predicate}\t{e.object}\t{e.valid_at}")
    elif args.tl_action == "history":
        edges = store.edge_history(args.subject, args.predicate, args.object)
        if getattr(args, "json", False):
            print(json.dumps({"edges": [_edge_dict(e) for e in edges]}, ensure_ascii=False))
        else:
            for e in edges:
                span = f"{e.valid_at} → {e.invalid_at or 'now'}"
                print(f"{span}\t{e.object}\t{e.evidence_path or ''}")
    elif args.tl_action == "at":
        edges = store.relationships_at(args.subject, args.when)
        if getattr(args, "json", False):
            print(json.dumps({"edges": [_edge_dict(e) for e in edges]}, ensure_ascii=False))
        else:
            for e in edges:
                print(f"{e.predicate}\t{e.object}\t{e.valid_at} → {e.invalid_at or 'now'}")
    return 0


def _state_sync(args: argparse.Namespace) -> int:
    """Export/import KV + timeline as JSONL for cross-machine sync via the wiki repo.

    Merge policy: KV is last-write-wins by `updated`; timeline rows are immutable
    facts unioned by primary key. Import never deletes local rows.
    """
    store = _get_store()
    path = Path(args.file)
    if args.sync_action == "export":
        rows = []
        with sqlite3.connect(store.db_path) as con:
            con.row_factory = sqlite3.Row
            for r in con.execute("SELECT * FROM entity_kv"):
                rows.append({"table": "entity_kv", **{k: r[k] for k in r.keys()}})
            for r in con.execute("SELECT * FROM entity_timeline"):
                rows.append({"table": "entity_timeline", **{k: r[k] for k in r.keys()}})
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        print(f"exported {len(rows)} rows → {path}")
    elif args.sync_action == "import":
        if not path.is_file():
            print(f"import: {path} not found", file=sys.stderr)
            return 1
        kv_n = tl_n = 0
        with sqlite3.connect(store.db_path) as con:
            con.execute("PRAGMA journal_mode=WAL")
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("table") == "entity_kv":
                    con.execute(
                        """INSERT INTO entity_kv(scope,scope_id,key,value,evidence_path,source,updated,last_accessed,access_count)
                           VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(scope,scope_id,key) DO UPDATE SET
                             value=excluded.value, evidence_path=excluded.evidence_path,
                             source=excluded.source, updated=excluded.updated,
                             last_accessed=COALESCE(entity_kv.last_accessed, excluded.last_accessed),
                             access_count=entity_kv.access_count + excluded.access_count
                           WHERE excluded.updated > entity_kv.updated""",
                        (r["scope"], r["scope_id"], r["key"], r["value"], r.get("evidence_path"),
                         r.get("source"), r["updated"], r.get("last_accessed"), r.get("access_count", 0)),
                    )
                    kv_n += 1
                elif r.get("table") == "entity_timeline":
                    cur = con.execute(
                        """INSERT OR IGNORE INTO entity_timeline(subject,predicate,object,valid_at,invalid_at,superseded_by,evidence_path,confidence,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (r["subject"], r["predicate"], r["object"], r["valid_at"], r.get("invalid_at"),
                         r.get("superseded_by"), r.get("evidence_path"), r.get("confidence", 1.0), r["created_at"]),
                    )
                    tl_n += cur.rowcount
        print(f"imported kv_rows={kv_n} timeline_new={tl_n} from {path}")
    return 0


def _maintain(args: argparse.Namespace) -> int:
    from scripts.automation_core.orchestrator import CliUsageError, MaintainStageRunner, maintain_pipeline, parse_mode
    from scripts.automation_core.operation import AutomationLock, GitBaseline, LockBusy, begin_transaction
    flags = [flag for name, flag in (("safe", "--safe"), ("no_auto", "--no-auto"),
             ("apply", "--apply"), ("commit", "--commit")) if getattr(args, name, False)]
    try:
        opts = parse_mode("maintain", flags)
    except CliUsageError as exc:
        print(f"maintain: {exc}", file=sys.stderr)
        return exc.exit_code
    wiki = Path(os.environ.get("WIKI_PATH", str(Path.home() / "llm-wiki"))).resolve()
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    operation_id = os.environ.get("MEMORY_HUB_OPERATION_ID") or new_operation_id(datetime.now(timezone.utc), uuid4)
    ctx = OperationContext(
        operation_id=operation_id,
        command="maintain",
        mode=opts.mode,
        auto=(opts.mode == Mode.AUTO),
        apply=opts.apply,
        wiki_path=wiki,
        data_path=data,
    )
    try:
        lock = AutomationLock.acquire(data, ctx)
    except LockBusy as exc:
        print(f"maintain: {exc}", file=sys.stderr)
        return exc.exit_code
    try:
        tx = begin_transaction(ctx, GitBaseline.capture(wiki))
        report = maintain_pipeline(tx, MaintainStageRunner(commit=opts.commit))
    finally:
        lock.release()
    if report.error:
        print(f"maintain: {report.error}", file=sys.stderr)
    try:
        from scripts.server import api_pending
        _pend = api_pending()
        _nc = sum(1 for _i in _pend["items"] if _i["source"] == "candidate")
        _ne = sum(1 for _i in _pend["items"] if _i["source"] == "experience")
        # 与 /api/pending + /api/overview 同源的待审计数（M6 评审 #7）。
        # report.counts 由 orchestrator.OperationReport（frozen，他人文件）构造，
        # 本层只做 stdout 机器可读行，不改 report 结构。
        _np = sum(1 for _i in _pend["items"] if _i["source"] == "page")
        print(f"maintain: pending candidates={_nc} experience_review={_ne} pages={_np} "
              f"total={len(_pend['items'])}")
    except Exception as exc:
        print(f"maintain: pending 计数不可用: {exc}", file=sys.stderr)
    ok = report.result in ("committed", "applied_no_commit", "safe")
    if ok and opts.apply:
        # Task B: 成功 apply 后增量嵌入；safe 模式 opts.apply 为 False，不触发。
        _trigger_incremental_embed(wiki, data)
    return 0 if ok else 1


def _run(args: argparse.Namespace) -> int:
    from scripts.automation_core.orchestrator import CliUsageError, RunStageRunner, run_pipeline, parse_mode
    from scripts.automation_core.operation import AutomationLock, GitBaseline, LockBusy, begin_transaction
    flags = [flag for name, flag in (("safe", "--safe"), ("no_auto", "--no-auto"),
             ("apply", "--apply"), ("commit", "--commit")) if getattr(args, name, False)]
    try:
        opts = parse_mode("run", flags)
    except CliUsageError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return exc.exit_code
    wiki = Path(os.environ.get("WIKI_PATH", str(Path.home() / "llm-wiki"))).resolve()
    data = Path(os.environ.get("MEMORY_HUB_DATA", str(Path.home() / ".memory-hub"))).resolve()
    operation_id = os.environ.get("MEMORY_HUB_OPERATION_ID") or new_operation_id(datetime.now(timezone.utc), uuid4)
    ctx = OperationContext(
        operation_id=operation_id,
        command="run",
        mode=opts.mode,
        auto=(opts.mode == Mode.AUTO),
        apply=opts.apply,
        wiki_path=wiki,
        data_path=data,
    )
    try:
        lock = AutomationLock.acquire(data, ctx)
    except LockBusy as exc:
        print(f"run: {exc}", file=sys.stderr)
        return exc.exit_code
    try:
        tx = begin_transaction(ctx, GitBaseline.capture(wiki))
        report = run_pipeline(tx, RunStageRunner(llm=args.llm))
    finally:
        lock.release()
    ok = report.result in ("committed", "applied_no_commit", "safe")
    if ok and opts.apply:
        # Task B: 成功 apply 后增量嵌入；safe 模式 opts.apply 为 False，不触发。
        _trigger_incremental_embed(wiki, data)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="memory-hub automation operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # scope-backfill
    scope = subparsers.add_parser("scope-backfill", help="plan or apply deterministic scope backfill")
    scope.add_argument("--apply", action="store_true", help="write scope frontmatter (default: dry-run)")
    scope.add_argument("--limit", type=int)
    scope.add_argument("--cursor")
    scope.add_argument("--json", action="store_true")
    scope.set_defaults(handler=_scope_backfill)

    id_backfill = subparsers.add_parser("id-backfill", help="plan or apply wiki page ID backfill")
    id_backfill_mode = id_backfill.add_mutually_exclusive_group(required=True)
    id_backfill_mode.add_argument("--plan", action="store_true", help="write a timestamped plan only")
    id_backfill_mode.add_argument("--apply", action="store_true", help="apply the required plan")
    id_backfill.add_argument("--wiki-root", default=str(Path.home() / "llm-wiki"))
    id_backfill.add_argument("--plan-dir", default=str(Path.home() / ".memory-hub" / "plans"))
    id_backfill.add_argument("--plan-file")
    id_backfill.add_argument("--batch-size", type=int, default=500)
    id_backfill.add_argument("--max-batches", type=int, default=None, help="stop after N batches (resumable)")
    id_backfill.add_argument("--lock-path", default=str(Path.home() / ".memory-hub" / "locks" / "automation.lock"))
    id_backfill.add_argument("--transactions-dir", default=str(Path.home() / ".memory-hub" / "transactions"))
    id_backfill.set_defaults(handler=_id_backfill)

    # search
    search_p = subparsers.add_parser("search", help="unified search operation")
    search_p.add_argument("query", help="query text")
    search_p.add_argument("--top", type=int, default=10)
    search_p.add_argument("--fuse", action="store_true", default=True)
    search_p.add_argument("--no-fuse", dest="fuse", action="store_false")
    search_p.add_argument("--expand", action="store_true", default=True)
    search_p.add_argument("--no-expand", dest="expand", action="store_false")
    search_p.add_argument("--no-fts", dest="fts", action="store_false", default=True,
                          help="skip FTS recall (vector only)")
    search_p.add_argument("--scope", choices=["user", "project", "agent"])
    search_p.add_argument("--scope-id")
    search_p.add_argument("--explain", action="store_true")
    search_p.add_argument("--json", action="store_true")
    search_p.add_argument("--tau", type=float, default=DEFAULT_TAU)
    search_p.add_argument("--depth", choices=["fast", "standard", "deep"], default="standard",
                          help="retrieval effort: fast skips expansions+graph, deep widens them")
    search_p.add_argument("--raw", action="store_true")
    search_p.add_argument("--all", action="store_true")
    search_p.add_argument("--gbrain", action="store_true")
    search_p.set_defaults(handler=_search)

    # ask
    ask_p = subparsers.add_parser("ask", help="unified ask operation")
    ask_p.add_argument("query", help="question text")
    ask_p.add_argument("--top", type=int, default=5)
    ask_p.add_argument("--fuse", action="store_true", default=True)
    ask_p.add_argument("--no-fuse", dest="fuse", action="store_false")
    ask_p.add_argument("--expand", action="store_true", default=True)
    ask_p.add_argument("--no-expand", dest="expand", action="store_false")
    ask_p.add_argument("--scope", choices=["user", "project", "agent"])
    ask_p.add_argument("--scope-id")
    ask_p.add_argument("--explain", action="store_true")
    ask_p.add_argument("--json", action="store_true")
    ask_p.set_defaults(handler=_ask)

    # decide
    decide_p = subparsers.add_parser("decide", help="decision-model choice/score grounded in the wiki")
    decide_p.add_argument("question", help="decision question")
    decide_p.add_argument("--option", action="append", default=[],
                          help="id=description (repeat, 2-12 options)")
    decide_p.add_argument("--level", action="append", default=[],
                          help="rubric level low->high (repeat, 2-10 levels)")
    decide_p.add_argument("--constraint", action="append", default=[])
    decide_p.add_argument("--top-k", "--top", dest="top_k", type=int, default=8)
    decide_p.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    decide_p.add_argument("--instructions")
    decide_p.set_defaults(handler=_decide)

    # kv
    kv_p = subparsers.add_parser("kv", help="entity key-value state (instant facts)")
    kv_sub = kv_p.add_subparsers(dest="kv_action", required=True)
    kv_set = kv_sub.add_parser("set")
    kv_set.add_argument("key"); kv_set.add_argument("value")
    kv_set.add_argument("--scope", default="user"); kv_set.add_argument("--scope-id", default="default")
    kv_set.add_argument("--evidence")
    kv_get = kv_sub.add_parser("get")
    kv_get.add_argument("key")
    kv_get.add_argument("--scope", default="user"); kv_get.add_argument("--scope-id", default="default")
    kv_get.add_argument("--json", action="store_true")
    kv_list = kv_sub.add_parser("list")
    kv_list.add_argument("--scope", default="user"); kv_list.add_argument("--scope-id")
    kv_list.add_argument("--json", action="store_true")
    kv_rm = kv_sub.add_parser("rm")
    kv_rm.add_argument("key")
    kv_rm.add_argument("--scope", default="user"); kv_rm.add_argument("--scope-id", default="default")
    kv_p.set_defaults(handler=_kv)

    # timeline
    tl_p = subparsers.add_parser("timeline", help="temporal relationship edges")
    tl_sub = tl_p.add_subparsers(dest="tl_action", required=True)
    tl_add = tl_sub.add_parser("add")
    tl_add.add_argument("subject"); tl_add.add_argument("predicate"); tl_add.add_argument("object")
    tl_add.add_argument("--valid-at"); tl_add.add_argument("--evidence")
    tl_add.add_argument("--exclusive", action="store_true",
                        help="single-valued predicate: close other open values")
    tl_act = tl_sub.add_parser("active")
    tl_act.add_argument("subject", nargs="?"); tl_act.add_argument("--predicate"); tl_act.add_argument("--object")
    tl_act.add_argument("--json", action="store_true")
    tl_hist = tl_sub.add_parser("history")
    tl_hist.add_argument("subject"); tl_hist.add_argument("predicate"); tl_hist.add_argument("object")
    tl_hist.add_argument("--json", action="store_true")
    tl_at = tl_sub.add_parser("at")
    tl_at.add_argument("subject"); tl_at.add_argument("--when")
    tl_at.add_argument("--json", action="store_true")
    tl_p.set_defaults(handler=_timeline)

    # sops
    sp_p = subparsers.add_parser("sops", help="procedural-memory candidates (repeated successful chains)")
    sp_p.add_argument("--min-occurrences", type=int, default=3)
    sp_p.add_argument("--window", type=int, default=4)
    sp_p.add_argument("--limit-rows", type=int, default=20000)
    sp_p.add_argument("--json", action="store_true")
    sp_p.set_defaults(handler=_sops)

    # conflicts
    cf_p = subparsers.add_parser("conflicts", help="scan published pages for contradictory config facts")
    cf_p.add_argument("--limit", type=int, default=300)
    cf_p.add_argument("--json", action="store_true")
    cf_p.set_defaults(handler=_conflict_scan)

    # state-sync
    ss_p = subparsers.add_parser("state-sync", help="export/import KV + timeline for cross-machine sync")
    ss_sub = ss_p.add_subparsers(dest="sync_action", required=True)
    ss_exp = ss_sub.add_parser("export"); ss_exp.add_argument("file")
    ss_imp = ss_sub.add_parser("import"); ss_imp.add_argument("file")
    ss_p.set_defaults(handler=_state_sync)

    # maintain
    maintain_p = subparsers.add_parser("maintain", help="maintain pipeline (default: apply and commit)",
        description="Maintain wiki pages and cross-day clusters. Default: apply and commit; use --safe to preview.")
    maintain_p.add_argument("--safe", action="store_true", help="preview without changing wiki, index, manifest or Git")
    maintain_p.add_argument("--no-auto", action="store_true", help="repairs only; requires --apply to write")
    maintain_p.add_argument("--apply", action="store_true", help="apply repairs in --no-auto mode")
    maintain_p.add_argument("--commit", action="store_true", help="commit repairs with --no-auto --apply")
    maintain_p.set_defaults(handler=_maintain)

    # run
    run_p = subparsers.add_parser("run", help="run pipeline")
    run_p.add_argument("--safe", action="store_true")
    run_p.add_argument("--no-auto", action="store_true")
    run_p.add_argument("--apply", action="store_true")
    run_p.add_argument("--commit", action="store_true")
    run_p.add_argument("--llm", action="store_true")
    run_p.set_defaults(handler=_run)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"{args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
