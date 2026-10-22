#!/usr/bin/env python3
"""Lightweight retrieval fidelity probe.

Counts over a bounded sample of the last learning receipts:
- ask/search related observations? Or alternatively, log a "metric" entry.
But simpler: append a metric line to a local log and compute moving average.

Records: how often a question becomes "ungrounded" for a choice decision vs a
normal ask. This is a pragmatic guardrail for the weak-pool regression we saw.
"""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class RetrievalMetric:
    ts: str
    asks_total: int = 0
    asks_answered: int = 0
    decisions_total: int = 0
    decisions_ungrounded: int = 0
    recall_misses: int = 0

    def ratio_ask_answered(self) -> float:
        return (self.asks_answered / self.asks_total) if self.asks_total else 1.0

    def ratio_dec_ungrounded(self) -> float:
        return (self.decisions_ungrounded / self.decisions_total) if self.decisions_total else 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

def log_metric(base: Path, m: RetrievalMetric) -> Path:
    out = base / "metrics" / "retrieval-fidelity.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(m.to_json() + "\n", encoding="utf-8")
    return out

if __name__ == "__main__":
    home = Path.home()
    m = RetrievalMetric(ts=datetime.now(timezone.utc).isoformat())
    log_metric(home / ".memory-hub", m)
    print(m.to_json())
