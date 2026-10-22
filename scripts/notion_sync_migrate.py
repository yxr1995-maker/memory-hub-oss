#!/usr/bin/env python3
"""对显式指定的 notion-sync 库执行 migration 或回滚。必须传 --db，避免误碰真实库。

用法：
  python3 scripts/notion_sync_migrate.py --db /path/notion-sync.sqlite3 migrate
  python3 scripts/notion_sync_migrate.py --db /path/notion-sync.sqlite3 rollback
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.automation_core.notion_sync import migrations  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("action", choices=("migrate", "rollback"))
    a = ap.parse_args(argv)
    fn = migrations.migrate if a.action == "migrate" else migrations.rollback
    print(f"{a.action}: user_version={fn(Path(a.db))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
