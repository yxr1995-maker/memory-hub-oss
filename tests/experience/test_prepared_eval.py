import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

from evaluation.experience_v1.prepared_eval import (
    FIXTURE_PATH,
    MockAlgorithmAdapter,
    prepare_adapter_request,
    run_evaluation,
    score_json_answer,
    strict_equal,
    write_result,
)

FROZEN_FIXTURE_SHA256 = "1cce7d53f2b5e4ab9ec6a65ea85bbdeb528f96c81ecc768037e03910dcec7b49"


def test_fixture_integrity_and_frozen_hash():
    assert FIXTURE_PATH.is_file(), f"Fixture file missing: {FIXTURE_PATH}"
    content = FIXTURE_PATH.read_bytes()
    computed_sha256 = hashlib.sha256(content).hexdigest()
    assert computed_sha256 == FROZEN_FIXTURE_SHA256, (
        f"Fixture SHA256 drift detected: expected {FROZEN_FIXTURE_SHA256}, got {computed_sha256}"
    )

    data = json.loads(content.decode("utf-8"))
    splits = data["splits"]
    train_cases = splits["train"]
    dev_cases = splits["dev"]
    test_cases = splits["test"]

    assert len(test_cases) == 6
    assert len(train_cases) >= 2
    assert len(dev_cases) >= 2

    # Check balanced categories in test split (3 categories x 2 cases)
    categories = [c["category"] for c in test_cases]
    for expected_cat in ["deduplicate_preserve_order", "retry_budget_calc", "field_sanitization"]:
        assert categories.count(expected_cat) == 2

    # Verify complete lack of ID and task text overlap across splits
    all_splits = [("train", train_cases), ("dev", dev_cases), ("test", test_cases)]
    id_map = {}
    task_map = {}
    for split_name, cases in all_splits:
        for c in cases:
            cid = c["case_id"]
            assert cid not in id_map, f"Duplicate case_id {cid} across {split_name} and {id_map[cid]}"
            id_map[cid] = split_name

            task_text = c["input"]["task"].strip().casefold()
            assert task_text not in task_map, f"Duplicate task text '{task_text}' in {split_name} and {task_map[task_text]}"
            task_map[task_text] = split_name


def test_adapter_request_payload_stripping_and_contract():
    sample_case = {
        "case_id": "test-sample-01",
        "category": "retry_budget_calc",
        "input": {"task": "Do task", "params": {"attempt_limit": 2}},
        "expected": {"max_retries": 1},
        "known_error": {"pattern": {"max_retries": 2}, "category": "off_by_one"},
    }
    memories = {
        "B": {"name": "Static Train", "type": "static_train_memory", "content": "train rules"}
    }
    contract = {"model_baseline": "unverified", "temperature": 0.0}

    stripped = prepare_adapter_request(sample_case, "B", memories, contract)
    assert "expected" not in stripped
    assert "known_error" not in stripped
    assert stripped["case_id"] == "test-sample-01"
    assert stripped["category"] == "retry_budget_calc"
    assert stripped["group"] == "B"
    assert stripped["memory"]["content"] == "train rules"
    assert stripped["contract"]["temperature"] == 0.0
    assert stripped["input"] == sample_case["input"]


def test_score_json_answer_strict_typing_and_errors():
    expected = {"flag": True, "count": 2, "items": ["a", "b"]}
    known_error = {"pattern": {"flag": False}, "category": "inverted_flag"}

    # 1. Exact match
    good = score_json_answer(json.dumps({"flag": True, "count": 2, "items": ["a", "b"]}), expected, known_error)
    assert good["is_success"] is True
    assert good["known_error_triggered"] is False
    assert good["failure_category"] == "none"

    # 2. Strict type mismatch: bool True is not integer 1
    bool_int_mismatch = score_json_answer(json.dumps({"flag": 1, "count": 2, "items": ["a", "b"]}), expected, known_error)
    assert bool_int_mismatch["is_success"] is False
    assert bool_int_mismatch["failure_category"] == "mismatched_output"

    # 3. List ordering matters: ["b", "a"] != ["a", "b"]
    order_mismatch = score_json_answer(json.dumps({"flag": True, "count": 2, "items": ["b", "a"]}), expected, known_error)
    assert order_mismatch["is_success"] is False

    # 4. Known error trigger
    bad_known = score_json_answer(json.dumps({"flag": False, "count": 2, "items": ["a", "b"]}), expected, known_error)
    assert bad_known["is_success"] is False
    assert bad_known["known_error_triggered"] is True
    assert bad_known["failure_category"] == "inverted_flag"

    # 5. Invalid JSON syntax
    bad_syntax = score_json_answer("not valid json", expected, known_error)
    assert bad_syntax["is_success"] is False
    assert bad_syntax["failure_category"] == "json_decode_error"


def test_adapter_exception_handling():
    class CrashingAdapter:
        def __call__(self, group: str, request: dict) -> str:
            raise RuntimeError("simulated model failure")

    result = run_evaluation(CrashingAdapter(), split="test", repetitions=1, seed=42)
    metrics = result["metrics"]
    for group in ("A", "B", "C"):
        assert metrics[group]["success_rate"] == 0.0
        assert metrics[group]["failure_categories"].get("adapter_exception") == 6


def test_positive_repetitions_validation():
    with pytest.raises(ValueError, match="repetitions must be positive integer"):
        run_evaluation(MockAlgorithmAdapter(), repetitions=0)


def test_mock_evaluation_condition_agnostic_and_deterministic(tmp_path):
    run_1 = run_evaluation(MockAlgorithmAdapter(), split="test", repetitions=2, seed=123)
    run_2 = run_evaluation(MockAlgorithmAdapter(), split="test", repetitions=2, seed=123)

    assert run_1["protocol"] == "experience-prepared-eval-v1"
    assert run_1["is_mock"] is True
    assert run_1["total_adapter_calls"] == 6 * 3 * 2  # 36 calls

    # Exclude measured real latency from exact equality assertion
    for g in ("A", "B", "C"):
        m1 = run_1["metrics"][g]
        m2 = run_2["metrics"][g]
        assert m1["success_rate"] == m2["success_rate"]
        assert m1["known_error_rate"] == m2["known_error_rate"]
        assert m1["failure_categories"] == m2["failure_categories"]
        assert m1["declared_planned_token_budget"] == m2["declared_planned_token_budget"]

    # Verify condition-agnostic mock achieves equal performance across groups (no fake C > B)
    scores = run_1["metrics"]
    assert scores["A"]["success_rate"] == scores["B"]["success_rate"] == scores["C"]["success_rate"] == 1.0
    assert scores["A"]["known_error_rate"] == scores["B"]["known_error_rate"] == scores["C"]["known_error_rate"] == 0.0

    # Test file writing
    out_file = tmp_path / "prepared_result.json"
    write_result(run_1, out_file)
    assert out_file.is_file()
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["protocol"] == "experience-prepared-eval-v1"


def test_cli_execution_with_output_flag(tmp_path):
    out_file = tmp_path / "cli_result.json"
    cmd = [
        sys.executable,
        "-m",
        "evaluation.experience_v1.prepared_eval",
        "--split",
        "test",
        "--repetitions",
        "1",
        "--seed",
        "42",
        "--output",
        str(out_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "Results successfully written" in res.stdout
    assert out_file.is_file()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["fixture_split"] == "test"
    assert data["total_adapter_calls"] == 18
    assert "metrics" in data
