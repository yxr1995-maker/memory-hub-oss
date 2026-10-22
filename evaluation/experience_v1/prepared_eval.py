"""Deterministic offline A/B/C prepared evaluation runner and scoring protocol.

This module provides a contract-compliant evaluation pipeline for synthetic tasks.
It uses a condition-agnostic algorithmic solver for mock execution, enforces strict
separation of expected answers from the adapter request payload, and computes exact
JSON match scores, known error rates, measured latency, and failure classifications.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

FIXTURE_PATH = Path(__file__).with_name("synthetic_tasks_v1.json")
GROUPS = ("A", "B", "C")


class MockAlgorithmAdapter:
    """Condition-agnostic algorithmic mock adapter.

    Executes a uniform heuristic parser and solver across all conditions (A/B/C).
    It does not read group labels, does not pre-bake C > B superiority, and operates
    solely on the sanitized input payload.
    """

    def __call__(self, group: str, request: dict) -> str:
        category = request.get("category", "")
        params = request.get("input", {}).get("params", {})

        if category == "deduplicate_preserve_order":
            items = params.get("items", [])
            unique = list(dict.fromkeys(items))
            return json.dumps({"unique_items": unique, "count": len(unique)}, ensure_ascii=False)

        elif category == "retry_budget_calc":
            attempt_limit = int(params.get("attempt_limit", 1))
            base_timeout = int(params.get("base_timeout_s", 1))
            max_retries = max(0, attempt_limit - 1)
            can_retry = attempt_limit > 1
            budget_seconds = attempt_limit * base_timeout
            return json.dumps(
                {"max_retries": max_retries, "can_retry": can_retry, "budget_seconds": budget_seconds},
                ensure_ascii=False,
            )

        elif category == "field_sanitization":
            record = copy.deepcopy(params.get("record", {}))
            sensitive = set(params.get("sensitive_keys", []))
            masked_count = 0
            for k in record:
                if k in sensitive:
                    record[k] = "[REDACTED]"
                    masked_count += 1
            return json.dumps({"sanitized_record": record, "masked_count": masked_count}, ensure_ascii=False)

        return json.dumps({"error": f"unsupported category: {category}"}, ensure_ascii=False)


def strict_equal(a: Any, b: Any) -> bool:
    """Strict type-sensitive equality: bool is not int, list order matters, dict keys match."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def pattern_matches(parsed: Any, pattern: Any) -> bool:
    """Check if all key-values in pattern are strictly matched in parsed dictionary."""
    if not isinstance(parsed, dict) or not isinstance(pattern, dict):
        return strict_equal(parsed, pattern)
    for k, v in pattern.items():
        if k not in parsed or not strict_equal(parsed[k], v):
            return False
    return True


def score_json_answer(raw_answer: str, expected: dict, known_error: dict) -> dict:
    """Score raw answer string against expected structure and known error patterns."""
    try:
        parsed = json.loads(raw_answer)
    except Exception as exc:
        return {
            "parsed": None,
            "is_success": False,
            "known_error_triggered": False,
            "failure_category": "json_decode_error",
            "error_detail": str(exc),
        }

    is_success = strict_equal(parsed, expected)
    known_error_triggered = False
    failure_category = "none" if is_success else "mismatched_output"

    pattern = known_error.get("pattern")
    if pattern and pattern_matches(parsed, pattern):
        known_error_triggered = True
        failure_category = known_error.get("category", "known_error")

    return {
        "parsed": parsed,
        "is_success": is_success,
        "known_error_triggered": known_error_triggered,
        "failure_category": failure_category,
    }


def prepare_adapter_request(case: dict, group: str, memory_definitions: dict, contract: dict) -> dict:
    """Strip expected answers and known error definitions; attach group, memory, and contract."""
    mem = memory_definitions.get(group, {})
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "group": group,
        "memory": {
            "name": mem.get("name", ""),
            "type": mem.get("type", "none"),
            "content": mem.get("content", ""),
        },
        "contract": copy.deepcopy(contract),
        "input": copy.deepcopy(case["input"]),
    }


def run_evaluation(
    adapter: Optional[Callable[[str, dict], str]] = None,
    fixture_path: Path = FIXTURE_PATH,
    split: str = "test",
    repetitions: int = 1,
    seed: int = 42,
) -> dict:
    if repetitions <= 0:
        raise ValueError(f"repetitions must be positive integer, got {repetitions}")

    if adapter is None:
        adapter = MockAlgorithmAdapter()

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    contract = fixture.get("contract", {})
    memory_definitions = fixture.get("memory_definitions", {})
    cases = fixture["splits"].get(split, [])
    if not cases:
        raise ValueError(f"Split {split} contains no cases in {fixture_path}")

    rng = random.Random(seed)
    results_rows = []
    group_stats = {
        g: {
            "successes": 0,
            "known_errors": 0,
            "latencies_ms": [],
            "failure_categories": {},
            "total": 0,
        }
        for g in GROUPS
    }

    for rep in range(repetitions):
        shuffled_cases = list(cases)
        rng.shuffle(shuffled_cases)

        for case in shuffled_cases:
            # Randomize group execution order to avoid position bias
            shuffled_groups = list(GROUPS)
            rng.shuffle(shuffled_groups)

            for group in shuffled_groups:
                request = prepare_adapter_request(case, group, memory_definitions, contract)
                start_t = time.perf_counter()
                try:
                    raw_answer = adapter(group, request)
                    duration_ms = (time.perf_counter() - start_t) * 1000.0
                    scored = score_json_answer(raw_answer, case["expected"], case.get("known_error", {}))
                except Exception as exc:
                    duration_ms = (time.perf_counter() - start_t) * 1000.0
                    scored = {
                        "parsed": None,
                        "is_success": False,
                        "known_error_triggered": False,
                        "failure_category": "adapter_exception",
                        "error_detail": str(exc),
                    }

                stat = group_stats[group]
                stat["total"] += 1
                if scored["is_success"]:
                    stat["successes"] += 1
                if scored["known_error_triggered"]:
                    stat["known_errors"] += 1
                stat["latencies_ms"].append(duration_ms)

                cat = scored["failure_category"]
                stat["failure_categories"][cat] = stat["failure_categories"].get(cat, 0) + 1

                results_rows.append(
                    {
                        "repetition": rep,
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "group": group,
                        "measured_latency_ms": round(duration_ms, 4),
                        "is_success": scored["is_success"],
                        "known_error_triggered": scored["known_error_triggered"],
                        "failure_category": scored["failure_category"],
                    }
                )

    metrics_by_group = {}
    for g, stat in group_stats.items():
        total = stat["total"]
        latencies = stat["latencies_ms"]
        metrics_by_group[g] = {
            "success_rate": round(stat["successes"] / max(1, total), 4),
            "known_error_rate": round(stat["known_errors"] / max(1, total), 4),
            "latency_ms": {
                "mean": round(sum(latencies) / max(1, len(latencies)), 4) if latencies else 0.0,
                "min": round(min(latencies), 4) if latencies else 0.0,
                "max": round(max(latencies), 4) if latencies else 0.0,
            },
            "failure_categories": stat["failure_categories"],
            "declared_planned_token_budget": contract.get("budget", {}).get("declared_planned_tokens", 512),
        }

    return {
        "protocol": fixture.get("protocol", "experience-prepared-eval-v1"),
        "fixture_version": fixture.get("version", "1.0.0"),
        "fixture_split": split,
        "is_mock": isinstance(adapter, MockAlgorithmAdapter),
        "adapter_name": type(adapter).__name__,
        "repetitions": repetitions,
        "seed": seed,
        "total_adapter_calls": len(results_rows),
        "contract": contract,
        "metrics": metrics_by_group,
        "results": results_rows,
        "disclaimer": "synthetic pipeline check only; not model evidence or a production benefit estimate",
    }


def write_result(result: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + chr(10)
    output_path.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH, help="Path to synthetic tasks JSON fixture")
    parser.add_argument("--split", type=str, default="test", choices=["train", "dev", "test"], help="Evaluation split")
    parser.add_argument("--repetitions", type=int, default=1, help="Repetitions with seed shuffling")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", type=Path, help="Write JSON result to file; otherwise print summary")
    args = parser.parse_args()

    result = run_evaluation(
        adapter=MockAlgorithmAdapter(),
        fixture_path=args.fixture,
        split=args.split,
        repetitions=args.repetitions,
        seed=args.seed,
    )

    if args.output:
        write_result(result, args.output)
        print(f"Results successfully written to {args.output}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
