import json
from pathlib import Path
import sqlite3
import pytest
from scripts.automation_core import learning_loop as ll


def event(id='one'):
    return {'id':id,'session_id':'s','project':'p','role':'user','text':'我长期偏好报告使用编号列表。','source':'hermes-native','created_at':'2026-10-18T00:00:00Z'}


def run(tmp_path,path,**kw):
    return ll.run_once(tmp_path/'wiki',tmp_path/'data',tmp_path/'hub',[path],**kw)


def test_import_restart_partial_and_idempotency(tmp_path):
    p=tmp_path/'input.jsonl'; p.write_text(json.dumps(event())+'\n'+json.dumps(event('two')))
    r=run(tmp_path,p,import_only=True)
    assert r['imported']==1
    assert run(tmp_path,p,import_only=True)['imported']==0
    with p.open('a') as f:f.write('\n')
    assert run(tmp_path,p,import_only=True)['imported']==1
    p.rename(tmp_path/'old'); p.write_text(json.dumps(event())+'\n')
    assert run(tmp_path,p,import_only=True)['duplicates']==1


def test_quarantine_and_redaction(tmp_path):
    p=tmp_path/'input.jsonl'; bad=event();bad.pop('session_id')
    safe=event('safe');safe.update(role='assistant',text='API_KEY="test-secret"')
    p.write_text('invalid\n'+json.dumps(bad)+'\n'+json.dumps(safe)+'\n')
    r=run(tmp_path,p,import_only=True)
    assert r['quarantined']==2 and r['imported']==1
    with ll.connect(tmp_path/'data') as c:
        row=c.execute('select role,text from queue').fetchone()
        assert row['role']=='assistant' and 'test-secret' not in row['text']


def test_transaction_failure_rolls_back_cursor(tmp_path,monkeypatch):
    p=tmp_path/'input.jsonl';p.write_text(json.dumps(event())+'\n')
    def fail(*a):raise RuntimeError('fixture')
    monkeypatch.setattr(ll,'record_observation',fail)
    assert run(tmp_path,p,import_only=True)['result']=='failed'
    with ll.connect(tmp_path/'data') as c:
        assert c.execute('select count(*) from learning_cursors').fetchone()[0]==0


def test_loop_concurrency_returns_busy(tmp_path):
    import fcntl
    data=tmp_path/'data';data.mkdir()
    with (data/'learning-loop.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        r=run(tmp_path,tmp_path/'absent',import_only=True)
        assert r['result']=='busy' and Path(r['receipt_path']).is_file()


def test_oversize_complete_line_is_quarantined_without_stalling(tmp_path):
    p=tmp_path/'input.jsonl';p.write_text(json.dumps(event('huge') | {'text':'x'*70000})+'\n'+json.dumps(event('good'))+'\n')
    r=run(tmp_path,p,import_only=True)
    assert r['quarantined']==1 and r['imported']==1


def test_existing_worker_publish_index_and_recall(tmp_path,monkeypatch):
    import subprocess
    from scripts.automation_core import memory_worker as worker
    from scripts.automation_core.codex_integration import configure
    from scripts.automation_core.query_planner import SearchRequest
    from scripts.automation_core.service import MemoryService
    wiki=tmp_path/'wiki';wiki.mkdir()
    (wiki/'SCHEMA.md').write_text('# Schema\n')
    (wiki/'index.md').write_text('# Index\n')
    (wiki/'log.md').write_text('# Log\n')
    for args in [['init'],['config','user.name','Fixture'],['config','user.email','fixture@example.invalid'],['add','.'],['commit','-m','baseline']]:
        subprocess.run(['git',*args],cwd=wiki,check=True,capture_output=True)
    configure(tmp_path/'data',publish=True)
    p=tmp_path/'input.jsonl';p.write_text(json.dumps(event())+'\n')
    def propose(rows):
        return [{'title':'编号列表偏好','kind':'preference','quote':rows[0]['text'],'queue_ids':[rows[0]['id']]}]
    monkeypatch.setattr(worker,'propose',propose)
    r=run(tmp_path,p)
    assert r['result']=='published' and r['learning_completed']
    result=MemoryService(wiki,tmp_path/'data',tmp_path/'hub').search(SearchRequest('编号列表',expand=False,fuse=False))
    assert any(x.path.startswith('decisions/memory-') for x in result.results)


@pytest.mark.parametrize('state',['failed','busy','disabled','pending_index'])
def test_worker_status_receipt(tmp_path,monkeypatch,state):
    p=tmp_path/'input.jsonl';p.write_text(json.dumps(event())+'\n')
    monkeypatch.setattr(ll,'worker_run',lambda *a,**kw:{'result':state,'published':0})
    r=run(tmp_path,p)
    assert r['result']==state
    assert Path(r['receipt_path']).is_file()
