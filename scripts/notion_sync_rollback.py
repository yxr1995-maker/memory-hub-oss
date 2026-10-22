#!/usr/bin/env python3
"""回滚 notion-sync migration：删除 outbox/sync_map/store_meta，user_version 置 0。必须传 --db。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.notion_sync_migrate import main  # noqa: E402

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    raise SystemExit(main(["--db", ap.parse_args().db, "rollback"]))
