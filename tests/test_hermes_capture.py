import json
import sqlite3
import time
from pathlib import Path
import pytest
from scripts.automation_core import hermes_capture as hc


def source(tmp_path):
    db = tmp_path/'state.db'
    with sqlite3.connect(db) as c:
        c.executescript('CREATE TABLE sessions(id TEXT,profile_name TEXT,cwd TEXT,chat_type TEXT,parent_session_id TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INT,_compressed_summary INT,tool_name TEXT);')
        c.executemany('INSERT INTO sessions VALUES(?,?,?,?,?)', [('s','default',None,'dm',None),('other','private',None,'dm',None),('g','default',None,'group',None),('sub','default',None,None,'s')])
    return db


def add(db, id, session='s', role='user', content='明确的原始消息内容', tool_name=None):
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)',(id,session,role,content,time.time(),1,0,tool_name))


def test_profile_boundaries_and_real_ids(tmp_path):
    db=source(tmp_path)
    for i,s in enumerate(['s','other','g','sub'],1): add(db,i,s)
    before=db.read_bytes()
    r=hc.run_once(db,tmp_path/'hub',tmp_path/'data')
    rows=[json.loads(x) for x in (tmp_path/'hub/staging/observations-hermes-native.jsonl').read_text().splitlines()]
    assert len(rows)==1 and rows[0]['id']=='hermes-message-1'
    assert rows[0]['session_id']=='s' and rows[0]['project']=='hermes-default'
    assert r['excluded']==3 and db.read_bytes()==before
    assert hc.run_once(db,tmp_path/'hub',tmp_path/'data')['read']==0


def test_tool_evidence_and_redaction(tmp_path):
    db=source(tmp_path)
    add(db,1,role='tool',content=json.dumps({'output':'tests/test_a.py::test_x PASSED','exit_code':0}))
    add(db,2,role='assistant',content='API_KEY="very-secret-value"')
    hc.run_once(db,tmp_path/'hub',tmp_path/'data')
    rows=[json.loads(x) for x in (tmp_path/'hub/staging/observations-hermes-native.jsonl').read_text().splitlines()]
    assert rows[0]['text'].endswith('exit_code=0')
    assert rows[1]['role']=='assistant' and 'very-secret-value' not in rows[1]['text']


def test_append_failure_keeps_cursor(tmp_path,monkeypatch):
    db=source(tmp_path); add(db,1)
    def fail(*a): raise OSError('fixture')
    monkeypatch.setattr(hc,'append_rows',fail)
    assert hc.run_once(db,tmp_path/'hub',tmp_path/'data')['result']=='failed'
    assert not (tmp_path/'data/hermes-capture-state.json').exists()


def test_rotation_restarts_stable_ids(tmp_path):
    db=source(tmp_path); add(db,1)
    hc.run_once(db,tmp_path/'hub',tmp_path/'data')
    db.rename(tmp_path/'old.db'); db=source(tmp_path); add(db,1)
    assert hc.run_once(db,tmp_path/'hub',tmp_path/'data')['read']==1
    rows=[json.loads(x) for x in (tmp_path/'hub/staging/observations-hermes-native.jsonl').read_text().splitlines()]
    assert rows[0]['id']==rows[1]['id']
