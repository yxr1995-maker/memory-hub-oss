from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.automation_core.cluster import (
    ClusterManifest,
    ClusterObservation,
    ClusterPlan,
    ManifestEntry,
    cluster_observations,
    commit_manifest,
    load_manifest,
    render_merge_page,
    scan_observations,
)
from scripts.automation_core.operation import (
    FailureHook,
    GitBaseline,
    TransactionContext,
    begin_transaction,
)
from scripts.automation_core.schema import Mode, OperationContext
from tests.helpers.full_auto_fixture import write_page


class FakeEmbedding:
    def __init__(self, cosine: float = 0.80) -> None:
        self._cosine = cosine

    def cosine(self) -> float:
        return self._cosine


class ClusterFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.wiki = tmp_path / "wiki"
        self.data = tmp_path / "data"
        self.staging = tmp_path / "staging"
        self.wiki.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

        self.manifest_path = self.data / "manifests" / "cluster-observations-v1.json"
        self.three_members_two_days = [
            ClusterObservation("id-1", "p", "text for item 1 in project p", 1788100000, "2026-08-30"),
            ClusterObservation("id-2", "p", "text for item 2 in project p", 1788100000, "2026-08-30"),
            ClusterObservation("id-3", "p", "text for item 3 in project p", 1788200000, "2026-08-31"),
        ]
        (self.data / "index.db").write_text("index-data", encoding="utf-8")

    def transaction(self, failure_hook: FailureHook | None = None) -> TransactionContext:
        ctx = OperationContext(
            operation_id="20260831T000000Z-cluster-test",
            command="maintain",
            mode=Mode.AUTO,
            auto=True,
            apply=True,
            wiki_path=self.wiki,
            data_path=self.data,
        )
        return begin_transaction(ctx, GitBaseline((), ()), failure_hook=failure_hook)

    def snapshot(self, pages: bool = True, frontmatter: bool = True, index: bool = True, manifest: bool = True) -> dict[str, str]:
        snap = {}
        for p in self.wiki.rglob("*.md"):
            snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        if (self.data / "index.db").is_file():
            snap["index"] = hashlib.sha256((self.data / "index.db").read_bytes()).hexdigest()
        if self.manifest_path.is_file():
            snap["manifest"] = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        return snap

    def publish(self, plan: ClusterPlan) -> Any:
        tx = self.transaction()
        tx.journal.checkpoint("PAGES_LIFECYCLE_PUBLISHED")
        tx.journal.checkpoint("INDEX_SWAPPED")
        tx.journal.checkpoint("LINT_PASSED")
        page_bytes = render_merge_page(plan)
        target = self.wiki / "moc" / f"cluster-{plan.key}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(page_bytes)

        entry = ManifestEntry(
            cluster_key=plan.key,
            observation_hashes=[hashlib.sha256(m.id.encode()).hexdigest() for m in plan.members],
            page_path=str(target.relative_to(self.wiki)),
            content_hash=hashlib.sha256(page_bytes).hexdigest(),
            operation_id=tx.operation.operation_id,
            created_at="2026-08-31T12:00:00Z",
        )
        commit_manifest(self.manifest_path, entry, tx)
        
        class Result:
            manifest = load_manifest(self.manifest_path)
            page_path = str(target.relative_to(self.wiki))
        return Result()

    def maintain_again(self) -> Any:
        tx = self.transaction()
        tx.journal.checkpoint("PAGES_LIFECYCLE_PUBLISHED")
        tx.journal.checkpoint("INDEX_SWAPPED")
        tx.journal.checkpoint("LINT_PASSED")
        plans = cluster_observations(self.three_members_two_days, FakeEmbedding(cosine=0.80))
        results = []
        for p in plans:
            entry = ManifestEntry(
                cluster_key=p.key,
                observation_hashes=[],
                page_path="",
                content_hash="",
                operation_id=tx.operation.operation_id,
                created_at="2026-08-31T12:00:00Z",
            )
            res = commit_manifest(self.manifest_path, entry, tx)
            results.append(res.result)
        
        class SecondResult:
            pass
        sr = SecondResult()
        sr.results = results
        return sr

    def cluster_page_count(self) -> int:
        return len(list(self.wiki.glob("moc/cluster-*.md")))


@pytest.fixture
def cluster_fixture(tmp_path: Path) -> ClusterFixture:
    return ClusterFixture(tmp_path)


def test_cross_day_cluster_key_and_manifest_replay(cluster_fixture: ClusterFixture) -> None:
    plans = cluster_observations(cluster_fixture.three_members_two_days, FakeEmbedding(cosine=0.80))
    expected_key = hashlib.sha256("id-1\nid-2\nid-3".encode()).hexdigest()[:16]
    assert [(p.key, len(p.members), p.method) for p in plans] == [(expected_key, 3, "embedding")]
    first = cluster_fixture.publish(plans[0])
    second = cluster_fixture.maintain_again()
    assert first.manifest.entries[expected_key].page_path == first.page_path
    assert second.results[0] == "manifest_skip"
    assert cluster_fixture.cluster_page_count() == 1


def test_short_similar_observations_cluster_via_local_jaccard() -> None:
    observations = [
        ClusterObservation("short-1", "p", "abcdefghijklmnopqrst", 1788100000, "2026-08-30"),
        ClusterObservation("short-2", "p", "abcdefghijklmnopqrsu", 1788100000, "2026-08-30"),
        ClusterObservation("short-3", "p", "abcdefghijklmnopqrsv", 1788200000, "2026-08-31"),
    ]

    plans = cluster_observations(observations)

    assert len(plans) == 1
    assert len(plans[0].members) == 3
    assert plans[0].method == "local"


def test_long_similar_observations_cluster_via_local_jaccard() -> None:
    first = "The experience retrieval pipeline records reliable evidence and prevents duplicate errors while preserving consistency across project workspaces and review checkpoints."
    second = first.replace("review checkpoints", "audit checkpoints")
    observations = [
        ClusterObservation("long-1", "p", first, 1788100000, "2026-08-30"),
        ClusterObservation("long-2", "p", second, 1788100000, "2026-08-30"),
        ClusterObservation("long-3", "p", first, 1788200000, "2026-08-31"),
    ]

    plans = cluster_observations(observations)

    assert len(plans) == 1
    assert len(plans[0].members) == 3


def _make_synthetic_observations(count: int = 502) -> list[ClusterObservation]:
    base_epoch = 1788100000
    items: list[ClusterObservation] = []
    for i in range(count):
        if i == 0:
            day = "2026-08-30"
            epoch = base_epoch + i
        else:
            day = "2026-08-31"
            epoch = base_epoch + 86400 + (i - 1) // 2
        items.append(
            ClusterObservation(
                id=f"obs-{i:04d}",
                project_id="p-synthetic",
                text="identical text across observations for cluster limit validation",
                created_at_epoch=epoch,
                created_at_date=day,
            )
        )
    return items


@pytest.mark.parametrize(
    ("env_value", "expected_count"),
    [
        (None, 500),
        ("3", 3),
        ("500", 500),
        ("501", 501),
        ("invalid", 500),
        ("", 500),
        ("0", 500),
        ("-1", 500),
        ("2", 500),
    ],
)
def test_cluster_max_obs_env_configuration_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    expected_count: int,
) -> None:
    if env_value is None:
        monkeypatch.delenv("MEMORY_HUB_CLUSTER_MAX_OBS", raising=False)
    else:
        monkeypatch.setenv("MEMORY_HUB_CLUSTER_MAX_OBS", env_value)

    observations = _make_synthetic_observations(count=502)
    plans = cluster_observations(observations)
    assert len(plans) == 1
    assert len(plans[0].members) == expected_count


@pytest.mark.parametrize("env_value", [None, "3", "500", "501", "invalid"])
def test_cluster_max_obs_truncation_ordering_and_reverse_stability(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
) -> None:
    if env_value is None:
        monkeypatch.delenv("MEMORY_HUB_CLUSTER_MAX_OBS", raising=False)
        effective_limit = 500
    else:
        monkeypatch.setenv("MEMORY_HUB_CLUSTER_MAX_OBS", env_value)
        effective_limit = int(env_value) if env_value in {"3", "500", "501"} else 500

    forward = _make_synthetic_observations(count=502)
    backward = list(reversed(forward))

    plans_forward = cluster_observations(forward)
    plans_backward = cluster_observations(backward)

    assert len(plans_forward) == 1
    assert len(plans_backward) == 1

    expected_members = sorted(forward, key=lambda item: (item.created_at_epoch, item.id))[:effective_limit]
    expected_ids = [m.id for m in expected_members]

    assert [m.id for m in plans_forward[0].members] == expected_ids
    assert [m.id for m in plans_backward[0].members] == expected_ids


@pytest.mark.parametrize("limit", [3, 500, 501])
def test_cluster_max_obs_exact_limit_preserves_input_order(
    monkeypatch: pytest.MonkeyPatch, limit: int,
) -> None:
    monkeypatch.setenv("MEMORY_HUB_CLUSTER_MAX_OBS", str(limit))
    observations = list(reversed(_make_synthetic_observations(count=limit)))

    plans = cluster_observations(observations)

    assert len(plans) == 1
    assert [member.id for member in plans[0].members] == [item.id for item in observations]
