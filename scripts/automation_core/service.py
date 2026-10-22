"""Unified search and ask service for memory-hub."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import socket
import urllib.error
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .frontmatter import parse_page
from .indexer import IndexedPage, load_page_records
from .query_planner import (
    AuditSink,
    ExpansionTerm,
    L0Snippet,
    QueryPlan,
    SearchRequest,
    plan_query,
)
from .ranker import (
    DEFAULT_TAU,
    MetricSink,
    NullMetrics,
    RecallHit,
    SearchResult,
    rank_results,
    render_human,
)


@dataclass(frozen=True)
class SearchResponse:
    request: SearchRequest
    plan: dict[str, Any]
    results: tuple[SearchResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.request.query,
            "plan": self.plan,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass(frozen=True)
class AskContext:
    results: tuple[SearchResult, ...]
    pages: dict[str, IndexedPage]
    context_text: str
    answer: str | None = None
    answer_status: str = "no_context"
    answer_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_paths": [r.path for r in self.results],
            "answer": self.answer,
            "answer_status": self.answer_status,
            "answer_error": self.answer_error,
            "results": [r.to_dict() for r in self.results],
        }


class SqliteRecallBackend:
    def __init__(self, wiki_path: Path, data_path: Path, hub_path: Path) -> None:
        self.wiki_path = wiki_path
        self.data_path = data_path
        self.hub_path = hub_path
        self.db_path = data_path / "index.db"

    @staticmethod
    def _literal_terms(text: str) -> list[str]:
        # Keep identifiers and short Chinese words; never invent cross-word grams.
        return re.findall(r"\w+(?:[+#][\w+#]*)?", text, flags=re.UNICODE)

    def _ngram_match(self, text: str) -> str:
        terms = []
        for w in text.split():
            for w2 in w.replace("-", " ").split():
                clean = re.sub(r'[*|&~()"]', " ", w2).strip()
                if len(clean) >= 3:
                    ngrams = [clean[i:i+3] for i in range(len(clean) - 2)]
                    if ngrams:
                        terms.append(f"({' AND '.join(ngrams)})")
        if not terms:
            cjk = "".join(c for c in text if '\u4e00' <= c <= '\u9fff')
            if len(cjk) >= 3:
                ngrams = [cjk[i:i+3] for i in range(len(cjk) - 2)]
                terms.append(f"({' OR '.join(ngrams)})")
        return " AND ".join(terms)

    def _ngram_or_match(self, text: str) -> str:
        terms = []
        for w in text.split():
            for w2 in w.replace("-", " ").split():
                clean = re.sub(r'[*|&~()"]', " ", w2).strip()
                if len(clean) >= 3:
                    ngrams = [clean[i:i+3] for i in range(len(clean) - 2)]
                    if ngrams:
                        terms.append(f"({' AND '.join(ngrams)})")
        if not terms:
            cjk = "".join(c for c in text if '\u4e00' <= c <= '\u9fff')
            if len(cjk) >= 3:
                ngrams = [cjk[i:i+3] for i in range(len(cjk) - 2)]
                terms.append(f"({' OR '.join(ngrams)})")
        return " OR ".join(terms)

    def _cjk_or_expr(self, query: str) -> str:
        """OR-joined CJK trigrams — the widest recall net for a CJK question.

        A long Chinese question has no whitespace boundaries, so AND matching over
        its trigrams demands every sliding window to co-occur and usually returns
        nothing. This is the last resort before the rg fallback.
        """
        cjk = "".join(c for c in query if '一' <= c <= '鿿')
        if len(cjk) < 3:
            return ""
        return "(" + " OR ".join(cjk[i:i+3] for i in range(len(cjk) - 2)) + ")"

    def fts(self, query: str, limit: int = 12) -> Sequence[RecallHit]:
        if not self.db_path.is_file():
            return self._rg_fallback(query, limit)
        terms = self._literal_terms(query)
        if not terms:
            return []
        long_terms = [term for term in terms if len(term) >= 3]
        short_terms = [term for term in terms if len(term) < 3]
        or_expr = ""
        # FTS5 trigram cannot match fewer than three characters. Use literal
        # SQLite predicates for those constraints instead of silently dropping them.
        match_expr = " AND ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
        short_sql = " AND ".join(
            "instr(lower(title || ' ' || abstract || ' ' || content), lower(?)) > 0"
            for _ in short_terms
        )
        try:
            with sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True) as con:
                clauses = (["pages MATCH ?"] if match_expr else []) + ([short_sql] if short_sql else [])
                params = ([match_expr] if match_expr else []) + short_terms
                score_expr = "bm25(pages)" if match_expr else "0.0"
                sql = f"SELECT path, {score_expr}, abstract, content FROM pages WHERE " + " AND ".join(clauses)
                order_sql = f" ORDER BY {score_expr}, path LIMIT ?"
                rows = con.execute(sql + order_sql, (*params, limit)).fetchall()
                # Reserve half the candidate budget for curated types so long
                # reference pages reach the ranker even under a cluster flood.
                curated = con.execute(
                    sql + " AND type IN ('entity','concept','comparison','decision','failure')" + order_sql,
                    (*params, max(1, limit // 2)),
                ).fetchall()
                seen_paths = {row[0] for row in curated}
                rows = (curated + [row for row in rows if row[0] not in seen_paths])[:limit]
                if not rows and not short_terms:
                    or_expr = self._ngram_or_match(query)
                    if or_expr and or_expr != match_expr:
                        rows = con.execute(
                            "SELECT path, bm25(pages), abstract, content FROM pages WHERE pages MATCH ? ORDER BY bm25(pages), path LIMIT ?",
                            (or_expr, limit),
                        ).fetchall()
                if not rows and re.search(r"[一-鿿]", query):
                                    # Hook subclasses override _ngram_match to widen CJK questions
                                    # into OR trigrams; honor that contract, then fall back to a
                                    # pure CJK trigram OR net so a long question never zeroes out.
                                    tried = {match_expr, or_expr}
                                    for expr in (self._ngram_match(query), self._cjk_or_expr(query)):
                                        if not expr or expr in tried:
                                            continue
                                        rows = con.execute(
                                            "SELECT path, bm25(pages), abstract, content FROM pages WHERE pages MATCH ? ORDER BY bm25(pages), path LIMIT ?",
                                            (expr, limit),
                                        ).fetchall()
                                        if rows:
                                            break
                if not rows:
                    return self._rg_fallback(query, limit)
                return [RecallHit(r[0], float(r[1]), rank=idx+1, abstract=r[2] or "", content=r[3] or "") for idx, r in enumerate(rows)]
        except Exception:
            return self._rg_fallback(query, limit)

    def vector(self, query: str, limit: int = 12) -> Sequence[RecallHit]:
        embed_script = self.hub_path / "scripts" / "embed.py"
        if not embed_script.is_file():
            return []
        try:
            env = dict(os.environ, WIKI_PATH=str(self.wiki_path), MEMORY_HUB_DATA=str(self.data_path))
            out = subprocess.run(
                [sys.executable, str(embed_script), "search", query, "-n", str(limit)],
                capture_output=True, text=True, env=env, timeout=10
            ).stdout
            hits = []
            for line in out.splitlines():
                m = re.match(r"^\[(-?\d*\.?\d+)\]\s+(\S+?\.md)", line.strip())
                if m:
                    hits.append(RecallHit(m.group(2), float(m.group(1)), rank=len(hits)+1))
            return hits
        except Exception:
            return []

    def _rg_fallback(self, query: str, limit: int = 12) -> Sequence[RecallHit]:
        if not self.wiki_path.is_dir():
            return []
        try:
            cmd = ["rg", "-i", "-l", "-F", "-g", "*.md", "-g", "!**/raw/**", "-g", "!**/_legacy-para/**", "-g", "!**/_archive/**", "--", query, str(self.wiki_path)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            hits = []
            for line in out.splitlines():
                if line.strip():
                    rel = Path(line.strip()).relative_to(self.wiki_path).as_posix()
                    hits.append(RecallHit(rel, 1.0, rank=len(hits)+1))
                    if len(hits) >= limit:
                        break
            return hits
        except Exception:
            return []


class MemoryService:
    def __init__(self, wiki_path: Path, data_path: Path, hub_path: Path) -> None:
        self.wiki_path = wiki_path.resolve()
        self.data_path = data_path.resolve()
        self.hub_path = hub_path.resolve()
        self.db_path = self.data_path / "index.db"
        self.recall = SqliteRecallBackend(self.wiki_path, self.data_path, self.hub_path)
        self.audit = AuditSink(str(self.data_path / "reports" / "query-plan.jsonl"))
        self.metrics = MetricSink()

    def _load_pages(self) -> dict[str, IndexedPage]:
        return load_page_records(self.db_path) if self.db_path.is_file() else {}

    def _load_links(self) -> dict[str, set[str]]:
        """Bidirectional adjacency from the links edge table (empty if absent)."""
        adj: dict[str, set[str]] = defaultdict(set)
        if not self.db_path.is_file():
            return {}
        try:
            with sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True) as con:
                if not con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='links'"
                ).fetchone():
                    return {}
                for src, dst in con.execute("SELECT src_path, dst_path FROM links"):
                    adj[src].add(dst)
                    adj[dst].add(src)
        except (sqlite3.Error, OSError):
            return {}
        return dict(adj)

    def _drop_rejected_disk(
        self, results: Sequence[SearchResult], keep: int | None = None
    ) -> list[SearchResult]:
        """Drop results whose on-disk frontmatter says status: rejected.

        Single choke point after ranking (pre-truncation): covers recall
        paths that bypass the index (rg fallback, stale index records)
        and any future recall path. Scans in rank order and stops once
        `keep` non-rejected results are collected, so disk reads stay
        bounded (callers cap the pool at N+50). Unreadable or
        out-of-tree paths fail open (kept) to preserve prior behavior
        for non-rejected pages.
        """
        kept: list[SearchResult] = []
        for r in results:
            try:
                cand = (self.wiki_path / r.path).resolve()
                try:
                    cand.relative_to(self.wiki_path)
                except ValueError:
                    kept.append(r)
                    if keep is not None and len(kept) >= keep:
                        break
                    continue
                if not cand.is_file():
                    kept.append(r)
                    if keep is not None and len(kept) >= keep:
                        break
                    continue
                doc = parse_page(cand)
                if str(doc.frontmatter.get("status") or "") == "rejected":
                    continue
            except Exception:
                pass
            kept.append(r)
            if keep is not None and len(kept) >= keep:
                break
        return kept

    def search(self, request: SearchRequest, tau: float = DEFAULT_TAU) -> SearchResponse:
        # Validate request
        if len(request.query) > 500:
            raise ValueError("Query exceeds 500 characters")
        if request.top < 1 or request.top > 50:
            raise ValueError("Top must be between 1 and 50")
        if request.scope and request.scope not in ("user", "project", "agent"):
            raise ValueError("Invalid scope")

        plan = plan_query(request, self.recall, audit=self.audit)

        # Widen the candidate pool so rejected pages are filtered
        # pre-truncation: a top-ranked rejected hit must not swallow
        # the slot (top=1 would otherwise return empty). Pool capped
        # at N+50 (request.top <= 50), which bounds the post-filter
        # disk reads.
        wider_top = min(request.top + 50, 50)
        rank_request = (
            replace(request, top=wider_top) if wider_top != request.top else request
        )

        recalls: dict[str, Sequence[RecallHit]] = {}
        if request.fuse:
            recalls["original_fts"] = self.recall.fts(request.query, 15)
            recalls["original_vec"] = self.recall.vector(request.query, 15)
            for idx, term in enumerate(plan.expansions):
                recalls[f"expansion_{idx}_fts"] = self.recall.fts(term.text, 10)
                recalls[f"expansion_{idx}_vec"] = self.recall.vector(term.text, 10)
        else:
            recalls["original_fts"] = self.recall.fts(request.query, wider_top)
            for idx, term in enumerate(plan.expansions):
                recalls[f"expansion_{idx}_fts"] = self.recall.fts(term.text, wider_top)

        # Graph-walk boost: one deterministic hop from the top literal hits so
        # thematically adjacent pages (zero literal overlap) can surface with a
        # strictly weaker score. Failure here must not fail the query.
        try:
            from .graph_recall import graph_neighbors
            links = self._load_links()
            seed = list(recalls.get("original_fts", ()))
            if links and seed:
                excluded = {h.path for hits in recalls.values() for h in hits}
                nbrs = graph_neighbors(seed, links, exclude=excluded)
                if nbrs:
                    recalls["original_graph"] = nbrs
        except Exception:
            pass

        pages = self._load_pages()
        links = self._load_links()
        results = rank_results(rank_request, plan, recalls, pages, self.metrics, tau=tau, links=links)
        # Collapse identical substantial bodies, preserving the best ranked
        # source within each scope. Distinct pages sharing an abstract survive.
        seen_bodies: set[tuple[str, str, str]] = set()
        unique_results = []
        for result in results:
            page = pages.get(result.path)
            body = re.sub(r"\s+", " ", page.content).strip() if page else ""
            key = (result.scope, result.scope_id, body)
            if len(body) >= 80 and key in seen_bodies:
                continue
            if len(body) >= 80:
                seen_bodies.add(key)
            unique_results.append(result)
        results = tuple(self._drop_rejected_disk(unique_results, keep=request.top)[:request.top])
        self.audit.finish(plan, final_hits=len(results))

        return SearchResponse(request=request, plan=plan.public_explain(request), results=results)

    def ask_context(self, request: SearchRequest) -> AskContext:
        response = self.search(request)
        pages = self._load_pages()
        loaded = {r.path: pages.get(r.path) for r in response.results if r.path in pages}

        # Build context
        blocks = []
        for r in response.results[:3]:
            p = pages.get(r.path)
            title = p.title if p else r.title
            abstract = p.abstract if p else r.abstract
            body = (p.content[:800] if p else "").replace("\n", " ")
            blocks.append(f"页面 {r.path}（{title}）\n摘要: {abstract}\n正文: {body}")
        
        context_text = "\n\n".join(blocks)
        answer = None
        answer_status = "no_context"
        answer_error = None
        proxy_url = os.environ.get("OPENCODEX_URL", "http://127.0.0.1:3425/v1")
        model = os.environ.get("CLAUDE_MEM_MODEL", "group/route")
        if context_text.strip():
            answer_status = "unavailable"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是知识库问答助手。基于给定页面内容回答问题，只基于页面内容，不要编造。回答后标注来源页面（用 path）。简洁中文。"},
                    {"role": "user", "content": f"问题: {request.query}\n\n相关页面:\n{context_text}"},
                ],
                "temperature": 0.2,
            }
            try:
                import urllib.request
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(f"{proxy_url}/chat/completions", data=data, headers={"Content-Type": "application/json"})
                timeout = float(os.environ.get("MEMORY_HUB_ASK_TIMEOUT", "15"))
                if not math.isfinite(timeout) or not 0 < timeout <= 120:
                    raise ValueError("invalid answer timeout")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        parsed = json.loads(resp.read().decode("utf-8"))
                        answer = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if not isinstance(answer, str) or not answer.strip():
                            answer = None
                            answer_error = "empty_response"
                        else:
                            answer_status = "answered"
                    else:
                        answer_error = "http_error"
            except (TimeoutError, socket.timeout):
                answer_error = "timeout"
            except urllib.error.HTTPError:
                answer_error = "http_error"
            except urllib.error.URLError as exc:
                answer_error = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection_error"
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                answer_error = "invalid_response_or_config"
            except Exception:
                answer = None
                answer_error = "provider_error"

        return AskContext(results=response.results, pages=loaded, context_text=context_text, answer=answer,
                          answer_status=answer_status, answer_error=answer_error)
