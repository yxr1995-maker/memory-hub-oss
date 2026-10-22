"""Procedural memory: extract repeated successful action sequences (SOPs).

Declarative memory answers "what is true". Procedural memory answers "how do
I do this". The wiki currently only holds the former, so a solved problem has
to be rediscovered from raw transcripts every time.

The signal available here is weak but real: a tool-call chain that ends in a
successful result, seen more than once, is a procedure the agent has actually
executed. This module finds those chains deterministically and proposes them
as SOP drafts. It never writes pages — a draft is a claim about what a
procedure is, and only a human (or the publish path with evidence) should
promote it.

Design constraints:
- offline and deterministic: no gateway, no LLM
- occurrence-gated: one occurrence is an anecdote, N is a pattern
- success-gated: a chain ending in failure teaches nothing
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: A chain must repeat at least this many times before it is a procedure.
DEFAULT_MIN_OCCURRENCES = 3

#: Window of consecutive tool observations forming one chain.
DEFAULT_WINDOW = 6

#: Failure markers in tool output.
_FAIL_RE = re.compile(r"(?:Traceback \(most recent call last\)|\bexit_code=[1-9]\d*\b|"
                      r"\b(?:FAILED|Error|error:)\b|command not found|No such file or directory)", re.I)
_PASS_RE = re.compile(r"(?:\bexit_code=0\b|\b\d+ passed\b|PASSED|OK\b|Successfully)", re.I)


@dataclass(frozen=True)
class ToolStep:
    turn_id: str
    project: str
    tool: str
    ok: bool


@dataclass
class SopCandidate:
    project: str
    signature: tuple[str, ...]
    occurrences: int
    successes: int
    example_turns: list[str] = field(default_factory=list)
    session_count: int = 0

    @property
    def success_rate(self) -> float:
        return round(self.successes / self.occurrences, 3) if self.occurrences else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "signature": list(self.signature),
            "occurrences": self.occurrences,
            "successes": self.successes,
            "success_rate": self.success_rate,
            "sessions": self.session_count,
            "example_turns": self.example_turns[:5],
        }


def classify_success(text: str) -> bool | None:
    """True/False for a known outcome, None when the output says nothing."""
    if _FAIL_RE.search(text or ""):
        return False
    if _PASS_RE.search(text or ""):
        return True
    return None


def _normalize_tool(tool: str) -> str:
    """Collapse tool names to a coarse class so chains match across sessions."""
    name = (tool or "").strip().lower()
    if not name:
        return "unknown"
    if name in ("terminal", "bash", "execute_command", "exec", "run_command"):
        return "shell"
    if "search" in name or "grep" in name:
        return "search"
    if name.startswith(("read", "view", "open", "cat")):
        return "read"
    if name.startswith(("write", "edit", "patch", "create", "update")):
        return "write"
    if name.startswith(("web", "fetch", "browse", "extract")):
        return "web"
    if name.startswith(("delegate", "task", "agent")):
        return "delegate"
    return name.split("_")[0]


def load_tool_steps(db: Path, *, limit: int = 20000) -> list[ToolStep]:
    """Recent tool observations as (tool class, success) steps.

    Tool identity is NOT taken from `queue.source`: for hermes-native rows that
    column holds a `file://...#messages.id=N` provenance URI, which normalizes
    to a unique garbage token and makes every chain look identical. Identity
    comes from the observation text (the command or output names the tool) and
    falls back to `tool-names.json`, which hermes_capture writes per turn.
    """
    if not db.is_file():
        return []
    names = _load_tool_names(db.resolve().parent.parent / "staging" / "tool-names.json")
    steps: list[ToolStep] = []
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, turn_id, project, text FROM queue "
            "WHERE role='tool' ORDER BY rowid ASC LIMIT ?",
            (max(1, min(int(limit), 100000)),),
        ).fetchall()
    for row in rows:
        text = row["text"] or ""
        turn = str(row["turn_id"] or row["id"])
        tool = names.get(turn) or _tool_from_text(text)
        steps.append(
            ToolStep(
                turn_id=turn,
                project=str(row["project"] or "unknown"),
                tool=_normalize_tool(tool),
                ok=bool(classify_success(text)),
            )
        )
    return steps


def _load_tool_names(path: Path) -> dict[str, str]:
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


_TOOL_HINT_RE = re.compile(
    r"(?:^|\n)(?:Process exited with code \d+|Command:|tool[=: ]\s*(\w+))", re.I
)


def _tool_from_text(text: str) -> str:
    """Best-effort tool identity from the observation text itself."""
    head = text[:400]
    for marker in ("pytest", "npm test", "git ", "make ", "bash", "rg ", "ls "):
        if marker in head:
            return "shell"
    if "Tool output" in head or head.startswith("Chunk ID"):
        return "shell"
    return ""


def find_sops(
    steps: Iterable[ToolStep],
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    window: int = DEFAULT_WINDOW,
) -> list[SopCandidate]:
    """Find tool-class chains that repeat and end in success.

    A chain is the `window` consecutive tool classes. Only chains whose last
    step succeeded are counted — a procedure that reliably fails is a bug
    report, not a procedure.
    """
    steps = list(steps)
    counts: dict[tuple[str, tuple[str, ...]], list[SopCandidate]] = defaultdict(list)
    sessions: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)

    for start in range(len(steps)):
        chunk = steps[start:start + window]
        if len(chunk) < 2 or not chunk[-1].ok:
            continue
        # A chain that contains a failure is not a procedure — it is a bug
        # report. Only keep the window when every step in it succeeded.
        if any(not s.ok for s in chunk):
            continue
        signature = tuple(s.tool for s in chunk)
        if all(t == "unknown" for t in signature):
            continue
        key = (chunk[0].project, signature)
        if not counts[key]:
            counts[key].append(SopCandidate(project=chunk[0].project, signature=signature,
                                            occurrences=0, successes=0))
        cand = counts[key][0]
        cand.occurrences += 1
        cand.successes += 1
        if len(cand.example_turns) < 5:
            cand.example_turns.append(chunk[-1].turn_id)
        sessions[key].add(chunk[-1].turn_id)

    out: list[SopCandidate] = []
    for key, cands in counts.items():
        cand = cands[0]
        cand.session_count = len(sessions[key])
        if cand.occurrences >= min_occurrences:
            out.append(cand)
    # Most-repeated first; deterministic tie-break on the signature.
    out.sort(key=lambda c: (-c.occurrences, c.project, c.signature))
    return out