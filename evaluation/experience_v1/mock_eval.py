"""Deterministic mock-only A/B/C evaluation adapter and result recorder.

This entry point never calls a model or reads Memory Hub stores. Supply an
adapter callable to run the same scoring/recording path with another backend.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

FIXTURE_PATH = Path(__file__).with_name("mock_fixtures.json")
GROUPS = ("A", "B", "C")


class MockAdapter:
    """Return fixed synthetic answers; no network or model client is used."""

    def __call__(self, group: str, case: dict) -> str:
        return case["answers"][group]


def _score(answer: str, expected_terms: list[str]) -> dict:
    normalized = answer.casefold()
    matched = [term for term in expected_terms if term.casefold() in normalized]
    return {"matched_terms": matched, "score": len(matched) / max(1, len(expected_terms))}


def run_evaluation(adapter: Callable[[str, dict], str], fixture_path: Path = FIXTURE_PATH) -> dict:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    rows = []
    totals = {group: [] for group in GROUPS}
    for case in fixture["cases"]:
        for group in GROUPS:
            answer = adapter(group, case)
            scored = _score(answer, case["expected_terms"])
            totals[group].append(scored["score"])
            rows.append({"case_id": case["case_id"], "group": group, "answer": answer, **scored})
    return {
        "protocol": fixture["protocol"],
        "fixture_type": fixture["fixture_type"],
        "is_mock": isinstance(adapter, MockAdapter),
        "adapter": type(adapter).__name__,
        "adapter_calls": len(rows),
        "scores": {group: sum(values) / len(values) if values else 0.0 for group, values in totals.items()},
        "results": rows,
        "interpretation": "synthetic pipeline check only; not model evidence or a production benefit estimate",
    }


def write_result(result: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON result here; otherwise print it")
    args = parser.parse_args()
    result = run_evaluation(MockAdapter())
    if args.output:
        write_result(result, args.output)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
