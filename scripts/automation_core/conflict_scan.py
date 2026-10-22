"""Contradiction scan across already-published pages.

The publish path already checks contradictions for *new* claims
(memory_worker.content_relation / semantic_relation + supersede_plan). This
module covers the other half: pages that were published weeks apart and now
disagree with each other. Nothing here writes to the wiki — it produces a
review list, because auto-resolving a contradiction means guessing which side
is the truth, and a wrong guess silently poisons recall.

Deliberately deterministic and offline: no gateway, no LLM. A cheap regex
pass catches the config-shaped facts that actually rot (ports, models, tools,
endpoints); free-form prose is left to the publish path, which has evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

#: (attribute, value pattern). Values are captured groups; the first group wins.
#:
#: Every pattern requires *explicit configuration context* on both sides.
#: A bare `端口\s*(\d+)` matched dates ("端口 08") and zip codes ("37701");
#: a bare `模型\s*(\w+)` matched English prose ("model pricing"). On the real
#: wiki the loose forms produced 100% false positives, so the vocabulary —
#: not number ranges — is what actually separates config from chatter.
_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("port", re.compile(
        r"(?:网关|gateway|代理|proxy|服务|service|监听|listen|本地)?\s*"
        r"(?:端口|port)\s*(?:是|为|在|改成|改为|:|：|=)?\s*(\d{2,5})(?!\d)", re.I)),
    ("model", re.compile(
        r"(?:默认模型|使用模型|模型名|default model|model provider)\s*"
        r"(?:为|是|用|走|改成|改为|:|：|=)?\s*([A-Za-z][\w./-]{2,40})", re.I)),
)

# Endpoints were tried and removed. A page legitimately lists several services
# on one host (9222 chrome debug, 8787 memory-hub REST, 10100 gateway), so
# "same host, different port" flagged 42 unique pairs and zero real conflicts.
# An endpoint without a named service is not a claim. The `port` rule already
# covers the case that matters because it requires gateway/service context.

#: Ports below this are almost always dates, versions or ids in practice.
_PORT_MIN = 1024

_NEGATION = re.compile(
    r"(?:不要|不再|不用|不应|禁止|不能|别|not|no longer|don't|do not|stop using)\s*"
    r"[\w一-鿿. -]{0,12}$"
)

#: Frontmatter keys that carry the page's authoritative claim.
_CLAIM_KEYS = ("decision", "abstract", "lesson", "source_quote")


@dataclass(frozen=True)
class Claim:
    attribute: str
    value: str
    negated: bool


@dataclass(frozen=True)
class Contradiction:
    path_a: str
    path_b: str
    attribute: str
    value_a: str
    value_b: str
    negation_clash: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "path_a": self.path_a,
            "path_b": self.path_b,
            "attribute": self.attribute,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "negation_clash": self.negation_clash,
        }


def text_of_page(text: str) -> str | None:
    """The page's authoritative claim: frontmatter decision/abstract first."""
    if not text.startswith("---\n"):
        return None
    header = text[4:].split("\n---\n", 1)
    if len(header) < 2:
        return None
    for key in _CLAIM_KEYS:
        m = re.search(rf"^{key}:\s*(.+)$", header[0], re.M)
        if m:
            value = m.group(1).strip().strip("'\"")
            if value:
                return value
    return None


def extract_claims(text: str) -> list[Claim]:
    """Config-shaped facts in `text`, with negation flag.

    Only the first occurrence of each attribute counts: a page mentioning both
    an old and a new port is describing a change, not asserting a conflict.

    `uses` is deliberately excluded. The verb pattern matches arbitrary
    identifiers ("用 exec_command"), so on the real wiki it produced only
    false positives (skill vs Skill, python vs Python) and no real conflicts.
    Tools and workflows belong to the procedural-memory extractor, which knows
    what a tool name actually looks like.
    """
    claims: list[Claim] = []
    seen: set[str] = set()
    for attribute, pattern in _FACT_PATTERNS:
        if attribute in seen or attribute == "uses":
            continue
        m = pattern.search(text)
        if not m:
            continue
        value = m.group(1).strip()
        if not value:
            continue
        if attribute == "port" and int(value) < _PORT_MIN:
            continue
        prefix = text[max(0, m.start() - 14):m.start()]
        negated = bool(_NEGATION.search(prefix))
        claims.append(Claim(attribute=attribute, value=value, negated=negated))
        seen.add(attribute)
    return claims


def _negation_mentions_other(text: str, value: str) -> bool:
    """True when `text` explicitly says not to use `value`."""
    pattern = re.compile(
        r"(?:不要|不再|不用|不应|禁止|不能|别|not|no longer|don't|do not)\s*"
        r"[\w一-鿿.-]{0,12}?" + re.escape(value), re.I)
    return bool(pattern.search(text))


def scan_index_contradictions(
    db: Path,
    wiki: Path,
    *,
    limit: int = 100,
    max_pages: int | None = None,
) -> list[Contradiction]:
    """Scan only pages the index knows about (FTS content column).

    Reading 15k files costs ~50s; the index already holds the text, and a
    contradiction is about what a page *claims*, which lives in the same
    content column. Drafts are included on purpose: stale drafts are exactly
    where retired config values survive.
    """
    import sqlite3

    if not db.is_file():
        return []
    uri = f"file:{db.resolve()}?mode=ro"
    sql = "SELECT path, content FROM pages"
    params: tuple[object, ...] = ()
    if max_pages:
        sql += " LIMIT ?"
        params = (max_pages,)
    pages: dict[str, str] = {}
    with sqlite3.connect(uri, uri=True) as con:
        for path, content in con.execute(sql, params):
            text = content or ""
            # Rebuild a minimal frontmatter so text_of_page can still work on
            # pages whose claim lives in the header.
            pages[path] = text
    return scan_contradictions(pages, limit=limit)


def scan_contradictions(
    pages: Mapping[str, str],
    *,
    limit: int = 50,
) -> list[Contradiction]:
    """Find page pairs asserting different values for the same attribute.

    Read-only. `pages` maps relative path -> file text.
    """
    indexed: dict[str, list[tuple[str, str, bool]]] = {}
    for path, text in pages.items():
        # Scan the whole page, not just the authoritative claim: a negation
        # ("stop using <Service>") usually lives in the body while the fact
        # ("use <Service>") sits in frontmatter, so both must be considered.
        claims = extract_claims(text)
        for claim in claims:
            negated = claim.negated
            # A negation about *this* value counts even if the regex missed it.
            if not negated and _negation_mentions_other(text, claim.value):
                negated = True
            indexed.setdefault(claim.attribute, []).append((path, claim.value, negated))

    found: list[Contradiction] = []
    for attribute, rows in indexed.items():
        for i, (path_a, val_a, neg_a) in enumerate(rows):
            for path_b, val_b, neg_b in rows[i + 1:]:
                if path_a == path_b:
                    continue
                related = val_a.lower() in val_b.lower() or val_b.lower() in val_a.lower()
                # A negation clash is the strongest signal and is checked before
                # value equality: "用 X" and "不要用 X" agree on the value while
                # disagreeing completely on the instruction.
                if related and neg_a != neg_b:
                    found.append(Contradiction(path_a, path_b, attribute, val_a, val_b, True))
                    if len(found) >= limit:
                        return found
                    continue
                if val_a == val_b:
                    continue
                # Same attribute, different value: only config-ish facts can contradict.
                if attribute not in ("port", "model"):
                    continue
                found.append(Contradiction(path_a, path_b, attribute, val_a, val_b))
                if len(found) >= limit:
                    return found
    return found