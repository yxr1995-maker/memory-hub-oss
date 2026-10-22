# memory-hub

Local agent memory built around Markdown, SQLite FTS5, and a command-line pipeline.
Capture supported conversation logs, distill them into reviewable pages, publish
those pages to a Markdown knowledge base, and retrieve context through the CLI,
MCP, or an optional REST service. Markdown is the source of truth; search indexes
can be rebuilt.

中文简介：本地会话采集、分层蒸馏、Markdown 知识库发布与检索工具。模型调用和外部集成需单独配置，公开前请审查自己的数据。

## Features

- **Capture:** incremental import from Codex, Claude Code, WorkBuddy, or a legacy
  claude-mem database, with cursors and observation deduplication.
- **Distill:** project-oriented pages with L0 summaries, L1 overviews, L2 details,
  provenance metadata, and candidate handling for conflicts. Optional LLM summaries
  require review.
- **Publish:** frontmatter validation and type-based routing into an llm-wiki-style
  Markdown knowledge base. Standalone publication previews by default; applying it
  updates the knowledge base index and log without overwriting existing pages.
- **Retrieve:** SQLite FTS5 trigram search, optional local embeddings, reciprocal
  rank fusion, scope filters, and time-aware ranking.
- **Connect:** a stdio MCP server, context injection, structured export, and a REST
  interface sharing the query service.
- **Operate:** explicit automation modes, maintenance reports, recovery journals,
  and metrics. Automatic modes can write data and create Git commits.

## Architecture

```text
Supported local conversation logs
             |
          capture
             v
    staging/observations-*.jsonl
             |
     distill [optional LLM]
             v
        staging/pages/*.md
             |
     publish [preview/apply]
             v
    Markdown knowledge base (WIKI_PATH)
             |
           index
             v
    SQLite FTS5 (MEMORY_HUB_DATA/index.db)
             |
    CLI / MemoryService / MCP / REST
             + optional embeddings and model-assisted queries
```

The CLI entry point is `memory-hub.sh`. Bash adapters live in `scripts/`;
`scripts/automation_core/` implements indexing, retrieval, publication workflows,
and automation. `mcp/server.py` exposes MCP tools, and `scripts/server.py` provides
the REST interface. `plugins/memory-hub/` contains the native integration hooks
and launcher.

## Requirements and Installation

Use a local checkout of the source and run commands from its root.

The shell workflow needs Bash, Python 3.11 or newer, `jq`, `rg` (ripgrep), `curl`,
and the `sqlite3` command-line tool. Python's SQLite build must support FTS5 and
the trigram tokenizer. Dependencies are not installed automatically.

```bash
cd /absolute/path/to/memory-hub
python3 -m venv .venv
. .venv/bin/activate

# Optional: stdio MCP server (includes FastMCP).
python3 -m pip install mcp

# Optional: local vector indexing and fused retrieval.
python3 -m pip install fastembed numpy

# Optional: development checks.
python3 -m pip install -r requirements-test.txt
```

Embedding setup may download model files. LLM-assisted distillation, question
answering, and query expansion use the configured runtime/provider route; no
specific hosted model is required by this guide. Follow your runtime's secure
configuration process and keep credentials outside source control.

## Quickstart: Local Search

This example creates a synthetic page and an index under the checkout. It does
not capture personal conversations or request model assistance.

```bash
export WIKI_PATH="$PWD/demo/wiki"
export MEMORY_HUB_DATA="$PWD/demo/data"
mkdir -p "$WIKI_PATH"
cat > "$WIKI_PATH/getting-started.md" <<'PAGE'
---
title: Getting started
type: concept
created: 2026-01-01
updated: 2026-01-01
abstract: A synthetic memory-hub example.
tags: [example]
---
# Getting started
Memory-hub keeps searchable notes in Markdown.
PAGE

bash memory-hub.sh index
bash memory-hub.sh search "searchable" --no-expand --no-fuse --top 5
```

Use a fresh demo directory: the example writes `getting-started.md`. For your own
knowledge base, set `WIKI_PATH` and `MEMORY_HUB_DATA` to its Markdown and runtime
storage directories before running commands. Defaults are `~/llm-wiki` and
`~/.memory-hub`. Staging defaults to the checkout's `staging/`; capture supports
`MEMORY_HUB_STAGING` to override its destination.

## Capture, Review, and Publish

Only import conversations you are authorized to process. Review staged content
before applying publication, especially if the destination is shared.

```bash
# Reads the configured local Codex session directory by default.
bash memory-hub.sh capture
# Also supported: --source claude-code, --source workbuddy, --source claude-mem.

# Select an actual observations file produced by capture.
bash memory-hub.sh distill staging/observations-YYYY-MM-DD.jsonl

# Preview first, then review staging/pages before applying.
bash memory-hub.sh publish
bash memory-hub.sh publish --apply
bash memory-hub.sh index
```

`CODEX_SESSIONS_DIR` overrides the Codex capture input. LLM distillation is opt-in
with `distill --llm`. Candidate conflicts require review; do not treat generated
summaries as verified facts.

## MCP Integration

Install `mcp` in the Python environment used by the client. For a client accepting
an `mcpServers` JSON object, adapt the following entry. Replace every
`/absolute/path/to/...` placeholder with an absolute path; JSON clients may not
expand shell variables or `~`.

```json
{
  "mcpServers": {
    "memory-hub": {
      "command": "/absolute/path/to/memory-hub/.venv/bin/python3",
      "args": ["/absolute/path/to/memory-hub/mcp/server.py"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/memory-hub",
        "WIKI_PATH": "/absolute/path/to/wiki",
        "MEMORY_HUB_DATA": "/absolute/path/to/runtime-data"
      }
    }
  }
}
```

This starts a stdio server; it does not require a public listener. Client-specific
configuration locations vary. The native plugin launcher is
`plugins/memory-hub/mcp/launch.sh` and requires its installation runtime settings;
it is separate from the direct server example above.

Available tools include:

| Tool | Purpose |
| --- | --- |
| `service_health` | Report build, process, and capability metadata. |
| `recall_for_decision` | Recall scoped experience for a task. |
| `read_evidence` | Read authorized evidence using its returned ID and revision. |
| `record_episode` | Append an observable episode with an idempotency key. |
| `memory_search` | Query indexed pages; expansion and fusion are configurable. |
| `memory_ask` | Return knowledge-base question-answering results. |
| `memory_inject` | Produce context for agent use. |

Experience tools require an authorized experience store and host configuration;
installing the server alone does not supply private memory. Legacy query tools
can append access logs. Tool annotations do not replace client permissions.

## Command Reference

Run `bash memory-hub.sh help` for the entry-point command list.

| Command | Purpose and behavior |
| --- | --- |
| `capture` | Incremental observation import; `--all`, `--since`, `--source`, `--watch`. |
| `distill [file] [--llm]` | Generate staged Markdown pages; optional model summaries. |
| `publish [--apply]` | Preview publication; apply only when explicitly requested. |
| `index [--with-raw]` | Rebuild the SQLite FTS5 index from Markdown. |
| `embed index` | Build/update the optional local vector index. |
| `search "query"` | Search; use `--no-expand --no-fuse` for lexical retrieval without model expansion. |
| `ask "question"` | Retrieve context and request a model-generated answer. |
| `export` | Export pages as JSONL, JSON, or Markdown, with project/type/tier filters. |
| `inject` | Print context; `--apply --file PATH` writes a marked section. |
| `scope-backfill` | Preview scope metadata backfill; `--apply` writes changes. |
| `archive` | Preview observation archiving; `--apply` moves selected files. |
| `run --safe` | Preview automation; may still write diagnostic reports/journals. |
| `run` | Automatic pipeline; defaults to applying changes and committing owned paths. |
| `maintain --safe` | Preview maintenance; may still write diagnostic reports/journals. |
| `maintain` | Apply maintenance and clustering, with scoped Git commits. |
| `status`, `usage`, `metrics` | Inspect local state and usage. |
| `serve --port 8787` | Start the optional REST query service. |
| `watch` | Repeated capture and distillation. |
| `codex`, `memory-worker` | Native integration management and background processing. |
| `eval` | Run a locally supplied evaluation set; private evaluation data is not distributed. |
| `verify` | Host-specific configuration checks; not a portable installation smoke test. |

## Security and Privacy

Conversation capture and staged pages can contain personal or confidential
information. A local index, vector store, access log, export, or diagnostic report
can retain that information too. Keep these artifacts out of distributed source.
`.gitignore` is a guard for untracked files; it cannot remove already tracked data
or erase past commits.

Model-assisted operations send selected text to the configured endpoint. Optional
integrations may contact external services. For local lexical retrieval, disable
query expansion and vector fusion as shown in the quickstart. Review integration
configuration before enabling capture, background workers, automatic publication,
or any network-facing REST deployment.

Before publishing a repository, review its tracked files and complete Git history
using tools and permissions appropriate to the environment. Review Markdown
content, private paths, email addresses, and project identifiers in addition to
credential findings. Do not publish scan logs containing secret values. Never
assume an ignore rule or a clean working-tree scan proves the history is clean.

## Development

```bash
python3 -m pytest -q tests/ --disable-warnings --maxfail=5
```

Some integration tests and `verify` depend on host-specific configuration. Run
those only in an isolated environment with synthetic data and explicit paths.

## License

[MIT](LICENSE), copyright 2026 memory-hub contributors.
