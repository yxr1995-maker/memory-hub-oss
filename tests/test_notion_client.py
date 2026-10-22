"""真实 Notion 客户端：只用 mock HTTP（注入 opener），不联网、不读真实 env 文件。"""
import io
import json
import logging
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.automation_core.notion_sync import migrations as m  # noqa: E402
from scripts.automation_core.notion_sync import notion_client as nc  # noqa: E402
from scripts.automation_core.notion_sync import projector as pj  # noqa: E402
import scripts.notion_sync_project as entry  # noqa: E402

TOKEN = "ntn_FAKEtoken1234567890abcdef"
DB_ID = "db-0000"


class Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(r)


def http_error(code, body=None):
    body = body if body is not None else {"message": f"echo {TOKEN}"}
    return urllib.error.HTTPError("https://api.notion.com/v1/pages", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


def client(opener):
    return nc.NotionRestClient(TOKEN, DB_ID, opener=opener)


PROPS = {"object_type": "page", "object_id": "notes/a.md", "status": "active",
         "decision": "approve", "version": "v1", "outbox_id": 7}


def test_create_page_request_body_and_headers():
    op = Opener([{"id": "page-1"}])
    assert client(op).create_page(PROPS) == "page-1"
    req = op.requests[0]
    assert req.full_url == "https://api.notion.com/v1/pages"
    assert req.get_method() == "POST"
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["authorization"] == "Bearer " + TOKEN
    assert h["notion-version"] == nc.NOTION_VERSION
    assert h["content-type"] == "application/json"
    body = json.loads(req.data)
    assert body["parent"] == {"database_id": DB_ID}
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "notes/a.md"
    assert body["properties"]["status"]["rich_text"][0]["text"]["content"] == "active"
    assert body["properties"]["outbox_id"]["rich_text"][0]["text"]["content"] == "7"


def test_update_and_read_page():
    op = Opener([{"id": "page-1"}, {"id": "page-1", "properties": {}}])
    c = client(op)
    c.update_page("page-1", PROPS)
    assert op.requests[0].get_method() == "PATCH"
    assert op.requests[0].full_url.endswith("/v1/pages/page-1")
    assert "parent" not in json.loads(op.requests[0].data)
    assert c.read_page("page-1")["id"] == "page-1"
    assert op.requests[1].get_method() == "GET" and op.requests[1].data is None


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_retryable_errors(code):
    with pytest.raises(nc.RetryableNotionError):
        client(Opener([http_error(code)])).create_page(PROPS)


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_errors(code):
    with pytest.raises(pj.PermanentProjectionError):
        client(Opener([http_error(code)])).create_page(PROPS)


def test_network_error_retryable():
    with pytest.raises(nc.RetryableNotionError):
        client(Opener([urllib.error.URLError("down")])).create_page(PROPS)


def test_token_never_in_errors_logs_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    c = client(Opener([http_error(401), http_error(500),
                       urllib.error.URLError(f"bad {TOKEN}")]))
    msgs = []
    for _ in range(3):
        with pytest.raises(Exception) as ei:
            c.create_page(PROPS)
        msgs.append(str(ei.value))
    assert all(TOKEN not in s for s in msgs) and all("***" in s for s in msgs)
    assert TOKEN not in caplog.text and caplog.text
    assert TOKEN not in repr(c) and TOKEN not in str(c)


def test_projector_records_redacted_error(tmp_path):
    db = tmp_path / "ns.sqlite3"
    m.migrate(db)
    with sqlite3.connect(db) as conn:
        m.enqueue(conn, "k1", "page", "notes/a.md", "review.approved", '{"status":"active"}')
        m.enqueue(conn, "k2", "page", "notes/b.md", "review.approved", '{"status":"active"}')
    r = pj.project_once(db, client(Opener([http_error(503), http_error(403)])))
    assert len(r.retried) == 1 and len(r.dead) == 1
    with sqlite3.connect(db) as conn:
        errs = [e for (e,) in conn.execute("SELECT last_error FROM outbox ORDER BY id")]
    assert all(TOKEN not in e and "HTTP" in e for e in errs)


def test_env_file_parsing_and_missing(tmp_path):
    with pytest.raises(nc.NotionConfigError, match="不存在"):
        nc.load_env_config(tmp_path / "missing.env")
    p = tmp_path / "x.env"
    p.write_text(f"# c\nexport NOTION_TOKEN='{TOKEN}'\n", encoding="utf-8")
    with pytest.raises(nc.NotionConfigError) as ei:
        nc.load_env_config(p)
    assert "NOTION_DATABASE_ID" in str(ei.value) and TOKEN not in str(ei.value)
    p.write_text(f'NOTION_TOKEN="{TOKEN}"\nNOTION_DATABASE_ID={DB_ID}\n', encoding="utf-8")
    cfg = nc.load_env_config(p)
    assert cfg["token"] == TOKEN and cfg["database_id"] == DB_ID
    c = nc.NotionRestClient.from_env_file(p, opener=Opener([]))
    assert c.database_id == DB_ID and TOKEN not in repr(c)


def test_entry_requires_db_and_once(tmp_path, capsys):
    with pytest.raises(SystemExit):
        entry.main(["--once"])
    with pytest.raises(SystemExit):
        entry.main(["--db", str(tmp_path / "x")])
    rc = entry.main(["--db", str(tmp_path / "x"), "--once", "--env-file", str(tmp_path / "no.env")])
    err = capsys.readouterr().err
    assert rc == 2 and "不存在" in err


def test_entry_runs_once_with_injected_client(tmp_path, capsys):
    db = tmp_path / "ns.sqlite3"
    m.migrate(db)
    with sqlite3.connect(db) as conn:
        m.enqueue(conn, "k1", "page", "notes/a.md", "review.approved", '{"status":"active"}')
    op = Opener([{"id": "page-9"}])
    rc = entry.main(["--db", str(db), "--once"], client_factory=lambda _p: client(op))
    assert rc == 0 and "done=1" in capsys.readouterr().out
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT notion_page_id FROM sync_map").fetchall() == [("page-9",)]
