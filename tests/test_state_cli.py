"""CLI-level tests for kv / timeline / state-sync."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
CLI = HUB / "scripts" / "automation_cli.py"


def _run(args, data, *, check=True):
    env = {**os.environ, "MEMORY_HUB_DATA": str(data)}
    env.pop("WIKI_PATH", None)
    proc = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, timeout=30)
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


def test_kv_cli_roundtrip(tmp_path):
    data = tmp_path / "data"
    _run(["kv", "set", "gateway_port", "7788", "--evidence", "notes/gatewayx.md"], data)
    out = _run(["kv", "get", "gateway_port", "--json"], data).stdout
    entry = json.loads(out)
    assert entry["value"] == "7788" and entry["evidence_path"] == "notes/gatewayx.md"
    assert entry["access_count"] == 1
    listed = _run(["kv", "list", "--json"], data).stdout
    assert [e["key"] for e in json.loads(listed)] == ["gateway_port"]
    assert _run(["kv", "rm", "gateway_port"], data).stdout.strip() == "ok"
    assert _run(["kv", "get", "gateway_port"], data, check=False).returncode == 1


def test_timeline_cli_roundtrip(tmp_path):
    data = tmp_path / "data"
    _run(["timeline", "add", "bob", "works_at", "Acme", "--exclusive"], data)
    _run(["timeline", "add", "bob", "works_at", "Hermes", "--exclusive"], data)
    active = _run(["timeline", "active", "bob", "--predicate", "works_at"], data).stdout
    assert "Hermes" in active and "Acme" not in active
    history = _run(["timeline", "history", "bob", "works_at", "Acme"], data).stdout
    assert "→" in history  # closed span shown
    at = _run(["timeline", "at", "bob"], data).stdout
    assert "Hermes" in at


def test_state_sync_export_import_merge(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _run(["kv", "set", "model", "group/route"], a)
    _run(["timeline", "add", "bob", "uses", "gatewayx"], a)
    dump = tmp_path / "state.jsonl"
    out = _run(["state-sync", "export", str(dump)], a).stdout
    assert "2 rows" in out
    # Newer local value on B must win after importing A's older file.
    _run(["kv", "set", "model", "gpt-5"], b)
    res = _run(["state-sync", "import", str(dump)], b).stdout
    assert "timeline_new=1" in res
    got = json.loads(_run(["kv", "get", "model", "--json"], b).stdout)
    assert got["value"] == "gpt-5"  # local newer value survives
    # And on a fresh store, everything imports.
    c = tmp_path / "c"
    _run(["state-sync", "import", str(dump)], c)
    assert json.loads(_run(["kv", "get", "model", "--json"], c).stdout)["value"] == "group/route"
    assert "gatewayx" in _run(["timeline", "active", "bob"], c).stdout


def test_provider_shaped_flags_are_accepted(tmp_path):
    """The Hermes provider passes --json/--top; these must parse (regression:
    a provider/tool contract mismatch failed at runtime, not at review time)."""
    data = tmp_path / "data"
    out = subprocess.run(
        [sys.executable, str(CLI), "decide", "--help"], capture_output=True, text=True)
    assert "--json" in out.stdout and "--top" in out.stdout
    # timeline --json must emit a JSON document
    env = {**os.environ, "MEMORY_HUB_DATA": str(data)}
    proc = subprocess.run([sys.executable, str(CLI), "timeline", "active", "--json"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0 and json.loads(proc.stdout) == {"edges": []}


def test_decay_marks_cold_not_deleted(tmp_path):
    data = tmp_path / "data"
    _run(["kv", "set", "stale", "v"], data)
    import sqlite3
    con = sqlite3.connect(data / "state.db")
    con.execute("UPDATE entity_kv SET updated='2020-01-01T00:00:00Z'")
    con.commit(); con.close()
    env = {**os.environ, "MEMORY_HUB_DATA": str(data)}
    code = (
        "from pathlib import Path; import os;"
        "from scripts.automation_core.state_store import StateStore;"
        "s = StateStore(Path(os.environ['MEMORY_HUB_DATA']));"
        "print(s.apply_decay(max_inactive_days=1));"
        "print('cold', s.cold_keys());"  # marks before any access
        "assert s.get_kv('user','default','stale').value == 'v';"  # still readable
        "print('revived', [k for k in s.cold_keys()])"  # access revives the key
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          cwd=str(HUB), timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "cold" in proc.stdout and "'stale'" in proc.stdout
    assert "revived []" in proc.stdout
