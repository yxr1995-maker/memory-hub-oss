#!/usr/bin/env python3
"""Read-only triage report for the candidate review backlog."""
from __future__ import annotations
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def report(data: Path, *, limit: int = 20) -> dict:
    result: dict = {}
    db = data / 'codex-memory.db'
    if not db.is_file():
        return {'queue': {}, 'error': 'missing_db'}
    with sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True) as con:
        result['queue'] = dict(con.execute('select status,count(*) from queue group by status').fetchall())
        result['oldest_candidates'] = [
            {'id': r[0], 'project': r[1], 'created_at': r[2]}
            for r in con.execute(
                "select id,project,created_at from queue where status='candidate' order by created_at limit ?",
                (limit,))]
        result['candidate_projects'] = dict(con.execute(
            "select project,count(*) from queue where status='candidate' group by project order by count(*) desc limit 10").fetchall())
    reasons = Counter()
    quotes = Counter()
    files = data / 'codex-candidates'
    scanned = 0
    if files.is_dir():
        for path in sorted(files.glob('*.json'))[:20000]:
            try:
                item = json.loads(path.read_text())
            except (OSError, ValueError):
                reasons['unreadable'] += 1
                continue
            scanned += 1
            reasons[item.get('reason', 'unknown')] += 1
            for proposal in item.get('proposals', []):
                if isinstance(proposal, dict) and proposal.get('quote'):
                    quotes[proposal['quote'][:120]] += 1
    result['candidate_files_scanned'] = scanned
    result['reasons'] = dict(reasons)
    result['top_distinct_quotes'] = [
        {'quote': q, 'count': c} for q, c in quotes.most_common(limit)]
    result['distinct_quotes'] = len(quotes)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path.home() / '.memory-hub')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(report(args.data, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
