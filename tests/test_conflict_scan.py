"""Contradiction scan across already-published pages.

The publish path already checks contradictions for *new* claims
(memory_worker.content_relation / semantic_relation + supersede_plan). This
module covers the other half: pages that were published weeks apart and now
disagree with each other. Nothing here writes to the wiki — it produces a
review list, because auto-resolving a contradiction means guessing which side
is the truth, and a wrong guess silently poisons recall.

Contract:
- deterministic and offline: no gateway, no LLM
- finds same-subject claims whose values differ, or one side negated
- never mutates any page
"""
from __future__ import annotations

from scripts.automation_core.conflict_scan import (
    extract_claims,
    scan_contradictions,
    text_of_page,
)


def _page(title, body, **fm):
    lines = ["---", f"title: {title}"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


def test_extract_claims_reads_port_and_tool_facts():
    body = "本地 GatewayX 网关跑在端口 7788，使用 group/route 模型。"
    claims = {c.attribute: c.value for c in extract_claims(body)}
    assert claims["port"] == "7788"


def test_extract_claims_marks_negation():
    claims = extract_claims("不要再走网关端口 7788 了。")
    assert any(c.negated and c.attribute == "port" for c in claims)


def test_contradiction_same_attribute_different_value():
    pages = {
        "entities/gatewayx.md": _page("GatewayX", "网关端口是 7788，运行正常。"),
        "failures/old-port.md": _page("旧配置", "网关端口是 10100，运行正常。"),
    }
    found = scan_contradictions(pages)
    assert found, "two pages claiming different port values must be flagged"
    pair = found[0]
    assert {pair.path_a, pair.path_b} == {"entities/gatewayx.md", "failures/old-port.md"}
    assert pair.attribute == "port"


def test_negation_counts_as_contradiction():
    """Same value, opposite instruction — the strongest contradiction signal."""
    pages = {
        "decisions/use-7788.md": _page("走 7788", "网关端口是 7788。"),
        "decisions/drop-7788.md": _page("弃用 7788", "不要再走网关端口 7788 了。"),
    }
    found = scan_contradictions(pages)
    assert any(c.negation_clash for c in found)


def test_agreement_is_not_a_contradiction():
    pages = {
        "entities/a.md": _page("A", "网关端口是 7788。"),
        "entities/b.md": _page("B", "这里同样记录网关端口是 7788。"),
    }
    assert scan_contradictions(pages) == []


def test_english_prose_is_not_a_model_fact():
    """Real-scan false positive: "model pricing" matched the loose 模型/\\w+ rule."""
    assert extract_claims("Sonnet 5 model pricing update") == []
    assert extract_claims("The model pricing page was updated.") == []


def test_scan_is_read_only(tmp_path):
    from pathlib import Path

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "x.md"
    target.write_text(_page("X", "端口 7788。"), encoding="utf-8")
    before = target.read_bytes()
    scan_contradictions({str(target): target.read_text(encoding="utf-8")})
    assert target.read_bytes() == before


def test_dates_and_zips_are_not_ports():
    """Real-scan false positives: "端口 08" (a date) and "37701" (a zip) must
    not register as ports, or the scan floods with noise."""
    assert extract_claims("记录于 2026-08-10，端口 08 变更。") == []
    assert extract_claims("邮编 37701 的地址。") == []


def test_real_port_is_detected():
    claims = {c.attribute: c.value for c in extract_claims("本地网关端口是 7788。")}
    assert claims["port"] == "7788"


def test_endpoints_are_not_tracked_as_facts():
    """Endpoints were removed: a page legitimately lists several services on
    one host, so endpoint comparison flagged 42 pairs and zero real conflicts."""
    claims = extract_claims("服务地址 http://127.0.0.1:7788 可用。")
    assert all(c.attribute != "endpoint" for c in claims)
    pages = {
        "entities/a.md": _page("A", "网关端口是 7788。"),
        "entities/b.md": _page("B", "网关端口是 10100。"),
    }
    assert any(c.attribute == "port" for c in scan_contradictions(pages))


def test_scan_caps_results_and_reports_total():
    pages = {f"entities/p{i}.md": _page(f"P{i}", f"网关端口是 {4000 + i}。") for i in range(20)}
    found = scan_contradictions(pages, limit=5)
    assert len(found) <= 5


def test_text_of_page_prefers_frontmatter_claim():
    text = _page("T", "正文里有很多其他端口 9999 的噪声。", decision="网关端口 7788")
    claim = text_of_page(text)
    assert claim is not None and "7788" in claim