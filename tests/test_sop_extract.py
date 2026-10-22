"""Procedural memory: SOP extraction contracts."""
from scripts.automation_core.sop_extract import (
    SopCandidate,
    ToolStep,
    classify_success,
    find_sops,
    load_tool_steps,
    _normalize_tool,
)


def _s(turn, tool, ok=True, project="p1"):
    return ToolStep(turn_id=turn, project=project, tool=tool, ok=ok)


def test_classify_success_reads_real_markers():
    assert classify_success("tests/test_x.py::test_a PASSED\nexit_code=0\n1 passed") is True
    assert classify_success("Traceback (most recent call last):\n  KeyError: x") is False
    assert classify_success("exit_code=127\ncommand not found") is False
    assert classify_success("some prose with nothing conclusive") is None


def test_tool_names_normalize_to_coarse_classes():
    assert _normalize_tool("terminal") == "shell"
    assert _normalize_tool("execute_command") == "shell"
    assert _normalize_tool("web_search") == "search"
    assert _normalize_tool("read_file") == "read"
    assert _normalize_tool("patch") == "write"
    assert _normalize_tool("") == "unknown"


def test_repeated_successful_chain_becomes_a_sop():
    steps = []
    for i in range(3):
        steps += [_s(f"r{i}1", "shell"), _s(f"r{i}2", "read"), _s(f"r{i}3", "shell")]
    sops = find_sops(steps, min_occurrences=3, window=3)
    assert sops, "three identical successful chains must be found"
    top = sops[0]
    assert top.successes == 3 and top.success_rate == 1.0


def test_single_occurrence_is_not_a_procedure():
    steps = [_s("a1", "shell"), _s("a2", "read"), _s("a3", "shell")]
    assert find_sops(steps, min_occurrences=3, window=3) == []


def test_failing_chain_is_not_a_procedure():
    steps = []
    for i in range(4):
        steps += [_s(f"f{i}1", "shell"), _s(f"f{i}2", "shell", ok=False)]
    assert find_sops(steps, min_occurrences=2, window=2) == []


def test_all_unknown_tool_chain_is_skipped():
    steps = [_s(f"u{i}", "unknown") for i in range(6)]
    assert find_sops(steps, min_occurrences=2, window=3) == []


def test_candidate_serializes_with_evidence():
    steps = []
    for i in range(3):
        steps += [_s(f"s{i}1", "shell"), _s(f"s{i}2", "write")]
    cand = find_sops(steps, min_occurrences=3, window=2)[0]
    d = cand.to_dict()
    assert d["occurrences"] == 3 and d["signature"] == ["shell", "write"]
    assert len(d["example_turns"]) >= 1
    assert 0 < d["success_rate"] <= 1.0


def test_load_tool_steps_is_safe_without_db(tmp_path):
    assert load_tool_steps(tmp_path / "missing.db") == []


def test_load_tool_steps_ignores_provenance_uri_as_tool(tmp_path):
    """Real-data bug: queue.source holds a file:// provenance URI for native
    rows. Using it as tool identity normalized every chain to a unique garbage
    token, so the top SOPs were session paths, not procedures."""
    import sqlite3
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE queue (id TEXT, turn_id TEXT, project TEXT, source TEXT, text TEXT, role TEXT)")
    for i in range(4):
        con.execute("INSERT INTO queue VALUES (?,?,?,?,?,'tool')",
                    (str(i), f"t{i}", "p",
                     f"file:///tmp/sessions/session-{i}.jsonl#messages.id={i}",
                     "1 passed\nexit_code=0"))
    con.commit(); con.close()
    steps = load_tool_steps(db)
    assert all(not s.tool.startswith("file") for s in steps)
    assert all("/" not in s.tool for s in steps)


def test_load_tool_steps_reads_queue(tmp_path):
    import sqlite3
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE queue (id TEXT, turn_id TEXT, project TEXT, source TEXT, text TEXT, role TEXT)")
    con.execute("INSERT INTO queue VALUES ('1','t1','p','shell','1 passed\nexit_code=0','tool')")
    con.execute("INSERT INTO queue VALUES ('2','t2','p','shell','Traceback (most recent call last)','tool')")
    con.commit(); con.close()
    steps = load_tool_steps(db)
    # Rows come back in insertion order, so the pass precedes the traceback.
    assert [s.ok for s in steps] == [True, False]
    # Rows come back in insertion order, so the pass precedes the traceback.