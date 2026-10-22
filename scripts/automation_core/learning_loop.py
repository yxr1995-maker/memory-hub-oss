"""Crash-safe observation import into the existing evidence-gated queue."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from .codex_memory import connect,clean_text
from .hermes_capture import atomic_json
from .memory_worker import run_once as worker_run


def now():return datetime.now(timezone.utc).isoformat()


def tables(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS learning_cursors(path TEXT PRIMARY KEY,identity TEXT,offset INTEGER,prefix_hash TEXT);
        CREATE TABLE IF NOT EXISTS learning_observations(event_key TEXT PRIMARY KEY,queue_id TEXT,path TEXT,offset INTEGER,content_hash TEXT,disposition TEXT,reason TEXT,run_id TEXT);
        CREATE TABLE IF NOT EXISTS learning_runs(id TEXT PRIMARY KEY,receipt TEXT NOT NULL);''')


def record_observation(c,row,raw,path,offset,run_id):
    digest=hashlib.sha256(raw).hexdigest()
    key=hashlib.sha256((str(path)+':'+str(offset)+':'+digest).encode()).hexdigest()
    required=('session_id','project','role','text','source','created_at')
    reason=''
    if not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not row[k].strip() for k in required):reason='missing_provenance'
    elif not isinstance(row.get('turn_id') or row.get('id'),str):reason='missing_turn_id'
    elif row['role'] not in ('user','assistant','tool'):reason='unsupported_role'
    elif row['session_id'] in ('unknown','default') or (row.get('turn_id') or row.get('id'))=='unknown':reason='unknown_provenance'
    else:
        try:datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
        except ValueError:reason='invalid_timestamp'
    queue_id=None
    if reason:disposition='quarantined'
    else:
        text=clean_text(row['text'])
        if not text:disposition='filtered';reason='empty_or_system_injection'
        else:
            turn=row.get('turn_id') or row['id']
            queue_id=hashlib.sha256(json.dumps([row['session_id'],turn,row['role'],text]).encode()).hexdigest()
            count=c.execute('insert or ignore into queue(id,session_id,turn_id,project,text,role,source,created_at) values(?,?,?,?,?,?,?,?)',
                (queue_id,row['session_id'],turn,row['project'],text,row['role'],row['source_uri'] if isinstance(row.get('source_uri'),str) else row['source'],row['created_at'])).rowcount
            disposition='imported' if count else 'duplicates'
    c.execute('insert or ignore into learning_observations values(?,?,?,?,?,?,?,?)',
        (key,queue_id,str(path),offset,digest,disposition,reason,run_id))
    return disposition


def import_file(c,path,run_id,max_bytes=2*1024*1024,max_rows=1000):
    counts={k:0 for k in ('imported','duplicates','filtered','quarantined')}
    if not path.exists():return dict(counts,source_status='absent')
    if path.is_symlink() or not path.is_file():raise ValueError('invalid_source_file')
    with path.open('rb') as f:
        stat=os.fstat(f.fileno());identity=f'{stat.st_dev}:{stat.st_ino}'
        old=c.execute('select * from learning_cursors where path=?',(str(path),)).fetchone()
        offset=old['offset'] if old and old['identity']==identity and old['offset']<=stat.st_size else 0
        if offset:
            f.seek(0); prefix=hashlib.sha256(f.read(min(offset,128))).hexdigest()
            if prefix!=old['prefix_hash']:offset=0
        f.seek(offset); consumed=0; count=0
        while count<max_rows and consumed<max_bytes:
            pos=f.tell();raw=f.readline(min(max_bytes-consumed,65536)+1)
            if not raw:break
            if not raw.endswith(b'\n'):
                if len(raw)>65536:
                    digest=hashlib.sha256(raw);total=len(raw);complete=False
                    while total<max_bytes-consumed:
                        chunk=f.readline(min(65536,max_bytes-consumed-total))
                        if not chunk:break
                        digest.update(chunk);total+=len(chunk)
                        if chunk.endswith(b'\n'):complete=True;break
                    if complete:
                        key=hashlib.sha256((str(path)+':'+str(pos)+':'+digest.hexdigest()).encode()).hexdigest()
                        c.execute('insert or ignore into learning_observations values(?,?,?,?,?,?,?,?)',
                            (key,None,str(path),pos,digest.hexdigest(),'quarantined','oversized_line',run_id))
                        counts['quarantined']+=1;consumed+=total;count+=1;offset=f.tell();continue
                # A partial final line is retried without advancing the cursor.
                break
            consumed+=len(raw);count+=1
            try:row=json.loads(raw)
            except (ValueError,UnicodeError):row=None
            counts[record_observation(c,row,raw,path,pos,run_id)]+=1
            offset=f.tell()
        f.seek(0);prefix=hashlib.sha256(f.read(min(offset,128))).hexdigest()
        c.execute('insert or replace into learning_cursors values(?,?,?,?)',(str(path),identity,offset,prefix))
    return dict(counts,source_status='read',offset=offset)


def run_once(wiki:Path,data:Path,hub:Path,source_files=None,import_only=False):
    data.mkdir(parents=True,exist_ok=True);run_id='learn-'+uuid4().hex
    receipt={'run_id':run_id,'started_at':now(),'result':'running','imported':0,'duplicates':0,'filtered':0,'quarantined':0,'sources':[]}
    path=data/'learning-receipts'/f'{run_id}.json';receipt['receipt_path']=str(path)
    with (data/'learning-loop.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            receipt.update(result='busy',finished_at=now());atomic_json(path,receipt);return receipt
        try:
            files=source_files if source_files is not None else [hub/'staging/observations-hermes-native.jsonl',hub/'staging/observations-hermes.jsonl',Path.home()/'Documents/memory-hub/staging/observations-hermes.jsonl']
            with connect(data) as c:
                tables(c)
                for file in list(dict.fromkeys(Path(p).absolute() for p in files))[:8]:
                    c.execute('BEGIN IMMEDIATE')
                    detail=import_file(c,file,run_id)
                    c.commit()
                    receipt['sources'].append({'path':str(file),**detail})
                    for k in ('imported','duplicates','filtered','quarantined'):receipt[k]+=detail[k]
            if import_only:receipt['result']='imported'
            else:
                os.environ.setdefault('OPENCODEX_URL','http://127.0.0.1:3425/v1')
                os.environ.setdefault('CLAUDE_MEM_MODEL','group/route')
                worker=worker_run(wiki,data);receipt['worker']=worker;receipt['result']=worker.get('result','failed')
                receipt['learning_completed']=False
                if worker.get('published',0)>0 and worker.get('index')=='ready':
                    with connect(data) as c:
                        published=c.execute("select id,turn_id from queue where status='done' and id in (select queue_id from learning_observations where run_id=?)",(run_id,)).fetchall()
                    import sqlite3
                    verified=[]
                    if (data/'index.db').is_file():
                        with sqlite3.connect((data/'index.db').resolve().as_uri()+'?mode=ro',uri=True) as index:
                            for row in published:
                                if index.execute('select 1 from pages where instr(content,?)>0 limit 1',(row['turn_id'],)).fetchone():
                                    verified.append(row['id'])
                    receipt['verified_queue_ids']=verified
                    receipt['learning_completed']=bool(verified)
        except Exception as exc:
            receipt.update(result='failed',error=type(exc).__name__)
        finally:
            receipt['finished_at']=now()
            with connect(data) as c:
                tables(c);receipt['queue_counts']=dict(c.execute('select status,count(*) from queue group by status').fetchall())
                c.execute('insert or replace into learning_runs values(?,?)',(run_id,json.dumps(receipt,ensure_ascii=False)))
            atomic_json(path,receipt);atomic_json(data/'learning-loop-state.json',receipt)
        return receipt


def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--import-only',action='store_true')
    for name,default in [('wiki',Path.home()/'llm-wiki'),('data',Path.home()/'.memory-hub'),('hub',Path.home()/'memory-hub')]:p.add_argument('--'+name,type=Path,default=default)
    p.add_argument('--source-file',type=Path,action='append');a=p.parse_args()
    r=run_once(a.wiki,a.data,a.hub,a.source_file,a.import_only);print(json.dumps(r,ensure_ascii=False))
    return 1 if r['result'] in ('failed','pending_index','disabled') else 0
