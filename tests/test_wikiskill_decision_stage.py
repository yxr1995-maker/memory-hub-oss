from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.automation_core.operation import GitBaseline, begin_transaction
from scripts.automation_core.experience import AccessContext
from scripts.automation_core.experience.cli import extract_pattern
from scripts.automation_core.orchestrator import (
    WIKISKILL_CHECKPOINT,
    WIKISKILL_STAGE,
    RunStageRunner,
    StageRunner,
    StageOutcome,
    run_pipeline,
)
from scripts.automation_core.schema import Mode, OperationContext

SCORECARD = json.loads((ROOT / "evaluation" / "wikiskill" / "scorecard.json").read_text(encoding="utf-8"))


def _episode(index: int, status: str, narrative: str) -> dict:
    return {
        "schema_version": 1,
        "collection_id": "fixture",
        "event_kind": "episode",
        "synthetic": True,
        "goal": "第 %d 次改写文案" % index,
        "narrative": narrative,
        "explicit_reason": "原因 %d" % index,
        "source_kind": "synthetic_fixture",
        "conditions": {"task_kind": "creative"},
        "outcome": {"status": status},
        "source_refs": [{"kind": "fixture", "ref": "development/creative-%02d" % index}],
    }


def test_extract_pattern_records_real_source_trace_chars(tmp_path):
    root = tmp_path / "wikiskill"
    root.mkdir(parents=True, exist_ok=True)
    (root / "scorecard.json").write_text(json.dumps(SCORECARD, ensure_ascii=False), encoding="utf-8")
    success = _episode(1, "success", "成功的叙述保留完整上下文")
    failure = _episode(2, "failure", "失败的叙述同样长")
    episodes = tmp_path / "episodes.jsonl"
    episodes.write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in (success, failure)) + "\n", encoding="utf-8"
    )
    ctx = AccessContext("owner", ("fixture",), "owner", (str(tmp_path),))
    result = extract_pattern(tmp_path / "unused.sqlite3", ctx, file_path=episodes,
                             output_root=root, owner=True)
    assert len(result["candidates"]) == 1
    score = result["candidates"][0]["score"]
    expected = (len(success["narrative"]) + len(success["explicit_reason"])
                + len(failure["narrative"]) + len(failure["explicit_reason"]))
    assert score["source_trace_chars"] == expected
    assert score["retained_trace_chars"] == len(success["narrative"]) + len(failure["narrative"])
    assert score["retained_trace_chars"] <= score["source_trace_chars"]
    frontmatter = (root / "candidates" / (result["candidates"][0]["candidate_id"] + ".md")).read_text(encoding="utf-8")
    assert '"source_trace_chars": %d' % expected in frontmatter


def _score(**overrides):
    score = {
        "retrieval_hit_at_5": 0.9,
        "retrieval_mrr": 0.7,
        "retrieval_ratio": 0.95,
        "retrieval_passed": True,
        "paired_host_gate": {field: True for field in SCORECARD["metrics"]["paired_host_gate"]["fields"]},
        "compression_guard": "pending_threshold",
        "command_echo_guard": "pass",
        "retained_trace_chars": 4000,
        "source_trace_chars": 4000,
        "R_val": 0.91,
        "R_best": 0.88,
    }
    score.update(overrides)
    return score


def _write_scorecard(root: Path, *, calibrated: bool) -> Path:
    card = json.loads(json.dumps(SCORECARD))
    if calibrated:
        guard = card["metrics"]["local_red_lines"]["compression_guard"]
        guard["status"] = "active"
        guard["parameters"] = {"max_compression_ratio": 0.3, "min_retained_trace_chars": 1000}
    path = root / "scorecard.json"
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_candidate(root: Path, candidate_id: str, *, diff_body: str, score: dict,
                    calibrated: bool = False) -> Path:
    candidates = root / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)
    _write_scorecard(root, calibrated=calibrated)
    lines = [
        "---",
        "candidate_id: " + json.dumps(candidate_id),
        "pattern_source: \"dev/01::dev/02\"",
        "score: " + json.dumps(score, sort_keys=True),
        "---",
        "",
        "# " + candidate_id,
        "",
        "## proposal",
        "",
        "keep the validated source trace intact",
        "",
        "## diff",
        "",
        "```diff",
        diff_body,
        "```",
        "",
    ]
    path = candidates / (candidate_id + ".md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _begin(tmp_path: Path, root: Path, *, apply: bool = True):
    wiki = tmp_path / "wiki"
    data = tmp_path / "data"
    staging = tmp_path / "staging"
    for path in (wiki, data, staging):
        path.mkdir(parents=True, exist_ok=True)
    (wiki / "index.md").write_text("---\ntitle: Index\n---\nindex\n", encoding="utf-8")
    op = OperationContext(
        operation_id="wikiskill-op",
        command="run",
        mode=Mode.AUTO if apply else Mode.NO_AUTO,
        auto=True,
        apply=apply,
        wiki_path=wiki,
        data_path=data,
    )
    tx = begin_transaction(op, GitBaseline.capture(wiki))
    return tx, RunStageRunner(staging), data


@pytest.fixture(autouse=True)
def _clean_env():
    os.environ.pop("MEMORY_HUB_WIKISKILL_DECISION", None)
    yield
    os.environ.pop("MEMORY_HUB_WIKISKILL_DECISION", None)
    os.environ.pop("MEMORY_HUB_WIKISKILL_ROOT", None)


def _run_stage(tmp_path, root, *, apply=True):
    tx, runner, data = _begin(tmp_path, root, apply=apply)
    os.environ["MEMORY_HUB_WIKISKILL_ROOT"] = str(root)
    os.environ["MEMORY_HUB_WIKISKILL_DECISION"] = "1"
    return tx, runner.apply(WIKISKILL_STAGE, tx), data


def _run_order():
    from scripts.automation_core import orchestrator as orch
    return orch.RUN_ORDER


class _StubRunner(StageRunner):
    def apply(self, stage_name, tx):
        if stage_name == WIKISKILL_STAGE:
            return StageOutcome.ok_outcome(
                message="stub wikiskill",
                data={"candidates": 1, "operation_id": tx.operation.operation_id},
            )
        return StageOutcome.ok_outcome(message="stub " + stage_name)

    def index_swap_once_and_finalize(self, tx):
        tx.journal.checkpoint("INDEX_SWAPPED")
        return StageOutcome.ok_outcome(checkpoint_owned=True)


def test_stage_absent_from_default_run_plan():
    from scripts.automation_core import orchestrator as orch
    assert orch._wikiskill_run_plan() == (orch.RUN_ORDER, orch.RUN_CHECKPOINTS)
    assert WIKISKILL_STAGE not in orch.RUN_ORDER
    assert WIKISKILL_CHECKPOINT not in orch.RUN_CHECKPOINTS


def test_checkpoint_appended_only_when_enabled(monkeypatch):
    from scripts.automation_core import orchestrator as orch
    monkeypatch.setenv("MEMORY_HUB_WIKISKILL_DECISION", "1")
    order, checkpoints = orch._wikiskill_run_plan()
    assert order == (*orch.RUN_ORDER, WIKISKILL_STAGE)
    assert checkpoints == (*orch.RUN_CHECKPOINTS, WIKISKILL_CHECKPOINT)
    assert order[:-1] == orch.RUN_ORDER
    assert len(order) == len(checkpoints)


def test_uncalibrated_compression_guard_fails_closed(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-clean", diff_body="+keep source trace", score=_score())
    assert SCORECARD["metrics"]["local_red_lines"]["compression_guard"]["status"] == "pending_threshold"
    _tx, outcome, _data = _run_stage(tmp_path, root)
    assert outcome.ok, outcome.message
    result = json.loads((root / "results" / "candidate-clean.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rejected"
    assert result["checks_failed"] == ["compression_guard:threshold_pending"]
    assert outcome.data["decisions"]["rejected"] == 1


def test_passing_candidate_is_accepted_and_journaled(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-ok", diff_body="+keep source trace",
                    score=_score(), calibrated=True)
    tx, outcome, _data = _run_stage(tmp_path, root)
    assert outcome.ok, outcome.message
    assert outcome.data["decisions"] == {"accepted": 1, "rejected": 0, "rolled_back": 0}
    result = json.loads((root / "results" / "candidate-ok.json").read_text(encoding="utf-8"))
    for field in SCORECARD["artifacts"]["result_required"]:
        assert field in result
    assert result["operation_id"] == tx.operation.operation_id
    assert result["diff"] == "+keep source trace"
    assert result["proposal"] == "keep the validated source trace intact"


def test_command_echo_and_compression_red_lines_reject(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(
        root,
        "candidate-bad",
        diff_body="$ git commit -am echo-residue\ncat raw.md",
        score=_score(retained_trace_chars=694, source_trace_chars=24002),
        calibrated=True,
    )
    _tx, outcome, _data = _run_stage(tmp_path, root)
    assert outcome.ok, outcome.message
    result = json.loads((root / "results" / "candidate-bad.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rejected"
    assert "command_echo_guard" in result["guard_hits"]
    assert "compression_guard" in result["guard_hits"]
    assert "compression_guard:over_compressed" in result["checks_failed"]
    assert "compression_guard:retained_below_minimum" in result["checks_failed"]
    assert "command_echo_guard:hit" in result["checks_failed"]
    assert outcome.data["decisions"]["rejected"] == 1


def test_command_echo_patterns_match_scorecard_rules(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-prompt", diff_body="> sudo rm -rf /",
                    score=_score(), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((root / "results" / "candidate-prompt.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rejected"
    assert "command_echo_guard:hit" in result["checks_failed"]
    assert result["checks_failed"] == ["command_echo_guard:hit"]
    assert outcome.data["guard_hits"] == ["command_echo_guard"]


def test_non_improving_candidate_is_rejected(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-flat", diff_body="+neutral change",
                    score=_score(R_val=0.88), calibrated=True)
    _tx, _outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((root / "results" / "candidate-flat.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rejected"
    assert "R_val_or_R_best:not_improved" in result["checks_failed"]


def test_missing_required_field_is_rejected(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-thin", diff_body="+x", score=_score(), calibrated=True)
    path = root / "candidates" / "candidate-thin.md"
    text = path.read_text(encoding="utf-8").replace('pattern_source: "dev/01::dev/02"\n', "")
    path.write_text(text, encoding="utf-8")
    _tx, _outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((root / "results" / "candidate-thin.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rejected"
    assert "pattern_source:missing" in result["checks_failed"]


def test_regression_after_acceptance_rolls_back(tmp_path):
    root = tmp_path / "wikiskill"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "candidate-drift.json").write_text(
        json.dumps({"candidate_id": "candidate-drift", "decision": "accepted"}), encoding="utf-8"
    )
    _write_candidate(root, "candidate-drift", diff_body="+drifted change",
                    score=_score(R_val=0.4), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((results / "candidate-drift.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rolled_back"
    assert outcome.data["decisions"]["rolled_back"] == 1


def test_rollback_stays_rolled_back_on_the_next_run(tmp_path):
    root = tmp_path / "wikiskill"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "candidate-stay.json").write_text(
        json.dumps({"candidate_id": "candidate-stay", "decision": "rolled_back"}), encoding="utf-8"
    )
    _write_candidate(root, "candidate-stay", diff_body="+still failing",
                    score=_score(R_val=0.4), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((results / "candidate-stay.json").read_text(encoding="utf-8"))
    assert result["decision"] == "rolled_back"
    assert outcome.data["decisions"]["rolled_back"] == 1

    _tx2, outcome2, _data2 = _run_stage(tmp_path / "round2", root)
    rerun = json.loads((results / "candidate-stay.json").read_text(encoding="utf-8"))
    assert rerun["decision"] == "rolled_back"
    assert outcome2.data["decisions"]["rolled_back"] == 1


def test_corrupt_result_ledger_is_counted_and_fails_closed(tmp_path):
    root = tmp_path / "wikiskill"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "broken.json").write_text("{not json", encoding="utf-8")
    (results / "candidate-guard.json").write_text(
        json.dumps({"candidate_id": "candidate-guard", "decision": "accepted"}), encoding="utf-8"
    )
    _write_candidate(root, "candidate-guard", diff_body="+drifted",
                    score=_score(R_val=0.4), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root)
    result = json.loads((results / "candidate-guard.json").read_text(encoding="utf-8"))
    assert outcome.ok
    assert outcome.data["invalid_result_records"] == 1
    assert outcome.data["invalid_result_files"] == ["broken.json"]
    assert result["decision"] == "rolled_back"
    assert (results / "broken.json").read_text(encoding="utf-8") == "{not json"


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-dry", diff_body="+dry", score=_score(), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root, apply=False)
    assert outcome.ok
    assert outcome.data["skipped"] is True
    assert not (root / "results" / "candidate-dry.json").exists()


def test_preexisting_rejected_result_is_not_touched(tmp_path):
    root = tmp_path / "wikiskill"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {
            "candidate_id": "rejected-x",
            "decision": "rejected",
            "reason": "no_failure_counterexample",
            "recorded_at": "2026-09-26T00:00:00Z",
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    (results / "rejected-x.json").write_text(original, encoding="utf-8")
    _write_candidate(root, "candidate-x", diff_body="+x", score=_score(), calibrated=True)
    _tx, outcome, _data = _run_stage(tmp_path, root)
    assert outcome.ok
    assert (results / "rejected-x.json").read_text(encoding="utf-8") == original
    assert (results / "candidate-x.json").is_file()
    assert "rejected-x.json" not in outcome.data["results_written"]


def test_result_files_registered_for_rollback(tmp_path):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-tx", diff_body="+tx", score=_score(), calibrated=True)
    tx, outcome, _data = _run_stage(tmp_path, root)
    result_path = root / "results" / "candidate-tx.json"
    assert outcome.ok
    assert str(result_path.absolute()) in tx.journal.before_images
    assert str(result_path.absolute()) in tx.journal.after_hashes


def test_run_pipeline_records_checkpoint_only_when_enabled(tmp_path, monkeypatch):
    root = tmp_path / "wikiskill"
    _write_candidate(root, "candidate-pipe", diff_body="+pipe", score=_score(), calibrated=True)
    monkeypatch.setenv("MEMORY_HUB_WIKISKILL_ROOT", str(root))

    monkeypatch.delenv("MEMORY_HUB_WIKISKILL_DECISION", raising=False)
    tx_off, _r, data_off = _begin(tmp_path / "off", root, apply=False)
    report_off = run_pipeline(tx_off, _StubRunner())
    assert report_off.stage_names == list(_run_order())
    assert WIKISKILL_STAGE not in report_off.stage_names
    journal_off = (data_off / "transactions" / tx_off.operation.operation_id / "operation-journal.jsonl").read_text(encoding="utf-8")
    assert WIKISKILL_CHECKPOINT not in journal_off

    monkeypatch.setenv("MEMORY_HUB_WIKISKILL_DECISION", "1")
    tx_on, _r2, data_on = _begin(tmp_path / "on", root, apply=False)
    report_on = run_pipeline(tx_on, _StubRunner())
    assert report_on.stage_names == [*_run_order(), WIKISKILL_STAGE]
    assert report_on.checkpoints[-1] == WIKISKILL_CHECKPOINT
    journal_on = (data_on / "transactions" / tx_on.operation.operation_id / "operation-journal.jsonl").read_text(encoding="utf-8")
    assert WIKISKILL_CHECKPOINT in journal_on
    report_payload = json.loads((data_on / "reports" / ("run-" + tx_on.operation.operation_id + ".json")).read_text(encoding="utf-8"))
    assert report_payload["stage_data"][WIKISKILL_STAGE]["operation_id"] == tx_on.operation.operation_id
