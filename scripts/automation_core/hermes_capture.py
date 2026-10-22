"""Bounded, read-only native Hermes capture with crash-safe output ordering."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from .codex_memory import clean_text, project_identity


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.capture-', dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def append_rows(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    # A crash may leave a partial final line. Truncate only that incomplete tail.
    with path.open('a+b') as f:
        f.seek(0,2); size=f.tell()
        if size:
            f.seek(size-1)
            if f.read(1)!=b'\n':
                pos=size
                while pos>0:
                    start=max(0,pos-65536); f.seek(start); block=f.read(pos-start)
                    split=block.rfind(b'\n')
                    if split>=0: f.truncate(start+split+1); break
                    pos=start
                else: f.truncate(0)
        f.seek(0,2)
        for row in rows: f.write((json.dumps(row,ensure_ascii=False)+'\n').encode())
        f.flush(); os.fsync(f.fileno())


def run_once(source_db:Path, hub:Path, data:Path, since_hours=24,max_rows=200):
    data.mkdir(parents=True,exist_ok=True)
    result={'result':'noop','read':0,'importable':0,'filtered':0,'excluded':0}
    with (data/'hermes-capture.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return dict(result,result='busy')
        state_path=data/'hermes-capture-state.json'
        try:
            if not source_db.is_file(): return dict(result,result='unavailable')
            stat=source_db.stat(); identity=f'{stat.st_dev}:{stat.st_ino}'
            state=json.loads(state_path.read_text()) if state_path.exists() else {}
            last=int(state.get('last_id',0)) if state.get('identity')==identity else 0
            cutoff=time.time()-float(since_hours)*3600
            with sqlite3.connect(source_db.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as con:
                con.row_factory=sqlite3.Row
                highest=con.execute('select max(id) from messages').fetchone()[0] or 0
                if highest<last: last=0
                rows=con.execute('''SELECT m.id,m.session_id,m.role,m.content,m.timestamp,m.tool_name,
                    m.active,m._compressed_summary,s.profile_name,s.cwd,s.chat_type,s.parent_session_id
                    FROM messages m JOIN sessions s ON s.id=m.session_id
                    WHERE m.id>? AND m.timestamp>=? ORDER BY m.id LIMIT ?''',
                    (last,cutoff if last==0 else 0,max(1,min(int(max_rows),1000)))).fetchall()
            out=[];tool_names={}
            for row in rows:
                result['read']+=1; last=row['id']
                if row['profile_name'] not in ('default',None) or row['chat_type']=='group' or row['parent_session_id'] or row['active']!=1 or row['_compressed_summary'] or row['role'] not in ('user','assistant','tool'):
                    result['excluded']+=1; continue
                content=row['content'] or ''
                if row['role']=='tool':
                    # Tool identity lives in messages.tool_name, not in the
                    # content JSON (which only carries output/exit_code).
                    # Procedural memory needs it: a pass from pytest and a pass
                    # from bash are different steps.
                    name=row['tool_name']
                    if isinstance(name,str) and name:
                        tool_names[f'hermes-message-{row["id"]}']=name
                    try:
                        tool=json.loads(content)
                        if isinstance(tool,dict) and isinstance(tool.get('output'),str) and type(tool.get('exit_code')) is int:
                            content=tool['output']+'\nexit_code='+str(tool['exit_code'])
                    except (ValueError,TypeError): pass
                text=clean_text(content)
                if not text: result['filtered']+=1; continue
                timestamp=float(row['timestamp'])
                if not math.isfinite(timestamp): result['filtered']+=1; continue
                out.append({'id':f'hermes-message-{row["id"]}','turn_id':f'hermes-message-{row["id"]}',
                    'session_id':row['session_id'],'project':project_identity(row['cwd']) if row['cwd'] else 'hermes-default',
                    'role':row['role'],'text':text,'source':'hermes-native',
                    'source_uri':source_db.resolve().as_uri()+f'#messages.id={row["id"]}',
                    'created_at':datetime.fromtimestamp(timestamp,timezone.utc).isoformat()})
            if out: append_rows(hub/'staging/observations-hermes-native.jsonl',out)
            if tool_names: atomic_json(hub/'staging'/'tool-names.json',tool_names)
            result.update(result='captured' if out else 'noop',importable=len(out),last_id=last,identity=identity)
            atomic_json(state_path,result)
            return result
        except Exception as exc:
            return dict(result,result='failed',error=type(exc).__name__)


def main():
    p=argparse.ArgumentParser(); p.add_argument('--once',action='store_true')
    p.add_argument('--source-db',type=Path,default=Path.home()/'.hermes/state.db')
    p.add_argument('--hub',type=Path,default=Path.home()/'memory-hub')
    p.add_argument('--data',type=Path,default=Path.home()/'.memory-hub')
    p.add_argument('--since-hours',type=float,default=24);p.add_argument('--max-rows',type=int,default=200)
    a=p.parse_args(); r=run_once(a.source_db,a.hub,a.data,a.since_hours,a.max_rows)
    print(json.dumps(r,ensure_ascii=False)); return 1 if r['result'] in ('failed','unavailable') else 0
