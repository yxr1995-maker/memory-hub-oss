"""Scheduled runner checks with subprocess fault injection, no production I/O."""
import importlib.util
import json
from pathlib import Path
import subprocess


def load_runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('schedule_tick', Path(__file__).resolve().parents[1]/'scripts/learning_tick.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'DATA', tmp_path)
    return module


def test_scheduler_records_timeout(tmp_path, monkeypatch):
    m = load_runner(tmp_path, monkeypatch)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(m.subprocess, 'run', timeout)
    assert m.main() == 124
    record = json.loads((tmp_path/'learning-schedule-state.json').read_text())
    assert record['stages'][0]['result'] == 'timeout'
    assert record['exit_code'] == 124


def test_scheduler_stops_on_capture_failure(tmp_path, monkeypatch):
    m = load_runner(tmp_path, monkeypatch)
    calls = []
    def failed(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, '{"result":"failed"}', '')
    monkeypatch.setattr(m.subprocess, 'run', failed)
    assert m.main() == 2
    assert len(calls) == 1


def test_scheduler_records_both_stages(tmp_path, monkeypatch):
    m = load_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(m.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(command, 0, '{"result":"noop"}', ''))
    assert m.main() == 0
    record = json.loads((tmp_path/'learning-schedule-state.json').read_text())
    assert [s['stage'] for s in record['stages']] == ['capture', 'learning']
