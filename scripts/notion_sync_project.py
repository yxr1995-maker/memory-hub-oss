#!/usr/bin/env python3
"""手动触发一次 Notion 投影。必须显式传 --db；凭证只从 env 文件读，不打印。

  python3 scripts/notion_sync_project.py --db /path/notion-sync.sqlite3 --once
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.automation_core.notion_sync import notion_client as nc  # noqa: E402
from scripts.automation_core.notion_sync import projector as pj  # noqa: E402


def main(argv=None, client_factory=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--once", action="store_true", required=True,
                    help="只处理一批到期事件后退出")
    ap.add_argument("--env-file", default=str(nc.DEFAULT_ENV_FILE))
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args(argv)
    try:
        client = (client_factory or nc.NotionRestClient.from_env_file)(a.env_file)
    except nc.NotionConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    try:
        r = pj.project_once(Path(a.db), client, limit=a.limit)
    except RuntimeError as e:
        print(f"投影失败：{e}", file=sys.stderr)
        return 1
    print(f"done={len(r.done)} retried={len(r.retried)} dead={len(r.dead)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
