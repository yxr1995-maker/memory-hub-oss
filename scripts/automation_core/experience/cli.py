"""Explicit local management CLI; never called implicitly by a read."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from . import AccessContext, ExperienceError, initialize, record_episode, recall_for_decision, read_evidence
from .contracts import require, validate_payload

WIKISKILL_ROOT=Path(__file__).resolve().parents[3]/'evaluation'/'wikiskill'

# Outcome wording is the only success/failure signal; anything ambiguous stays unclassified
# rather than guessing, so a weak label can never manufacture a candidate.
_SUCCESS_TOKENS=frozenset(('success','successful','succeed','succeeds','succeeded','pass','passes',
                           'passed','verified','ok','okay','fixed','resolved','complete','completed'))
_FAILURE_TOKENS=frozenset(('fail','fails','failed','failing','failure','error','errors','timeout',
                           'timedout','crash','crashed','blocked','regression','broken','abort','aborted'))
_NEGATED_SUCCESS=('未通过','未成功','未验证','未能通过','没有通过','不通过','没通过','未完成','没有完成')
_SUCCESS_CJK=('成功','通过','已验证','验证通过','完成','修复')
_FAILURE_CJK=('失败','超时','错误','异常','中断','崩溃','回归')


def _norm(value):
    return str(value).strip().lower() if value is not None else ''


def _classify_outcome(outcome):
    if type(outcome) is not dict:
        return None
    fields=[_norm(outcome.get('status')),_norm(outcome.get('verification')),
            _norm(outcome.get('business_result')),_norm(outcome.get('audience_feedback'))]
    joined=' '.join(f for f in fields if f)
    if not joined:
        return None
    if any(token in joined for token in _NEGATED_SUCCESS):
        return 'failure'
    if any(token in joined for token in _FAILURE_CJK):
        return 'failure'
    if any(token in joined for token in _SUCCESS_CJK):
        return 'success'
    words=set(re.findall(r'[a-z]+',joined))
    if words & _FAILURE_TOKENS:
        return 'failure'
    if words & _SUCCESS_TOKENS:
        return 'success'
    return None


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
                                     separators=(',',':')).encode()).hexdigest()[:16]


def _episodes_from_file(path):
    episodes=[]
    for number,line in enumerate(Path(path).read_text().splitlines(),1):
        if not line.strip():
            continue
        try:
            payload=json.loads(line)
        except ValueError as e:
            raise ExperienceError('PAYLOAD_INVALID',str(path)+':'+str(number)+': '+str(e)) from None
        validate_payload(payload)
        episodes.append((Path(path).name+'#%02d'%number,payload))
    return episodes


def _episodes_from_db(db_path,ctx):
    from .store import connect
    episodes=[]
    with connect(db_path) as c:
        rows=c.execute("""SELECT e.event_id,e.collection_id,v.payload_json FROM experience_events e
            JOIN experience_versions v ON v.event_id=e.event_id AND v.revision=e.revision
            WHERE e.status='active' AND v.review_required=0 ORDER BY e.event_id""").fetchall()
    for row in rows:
        if not ctx.may_access(row['collection_id']):
            continue
        episodes.append((row['event_id'],json.loads(row['payload_json'])))
    return episodes


def _pattern_source(payload):
    refs=payload['source_refs']
    return refs[0]['ref'] if len(refs)==1 else '+'.join(r['ref'] for r in refs)


def _source_trace_chars(payload):
    narrative=payload.get('narrative')
    reason=payload.get('explicit_reason')
    if type(narrative) is not str or (reason is not None and type(reason) is not str):
        return None
    return len(narrative)+len(reason or '')


def _write_once(path,content):
    if path.exists():
        raise ExperienceError('PATTERN_EXISTS','refusing to overwrite '+str(path))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content)


def _record_rejection(output_root,cid,reason,now,source,payload):
    record={'candidate_id':cid,'decision':'rejected','reason':reason,'recorded_at':now,
            'pattern_source':_pattern_source(payload),'episode_ref':source,
            'source_kind':payload['source_kind'],'outcome':payload['outcome']}
    _write_once(output_root/'results'/(cid+'.json'),json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    return record


def extract_pattern(db_path,ctx,*,file_path=None,output_root=WIKISKILL_ROOT,owner=False):
    require(ctx.role=='owner','candidate extraction is an explicit management entry','ACCESS_DENIED')
    output_root=Path(output_root)
    scorecard_path=output_root/'scorecard.json'
    require(scorecard_path.is_file(),'scorecard.json not found; refusing to write artifacts without it','SOURCE_MISSING')
    scorecard=json.loads(scorecard_path.read_text())
    artifacts=scorecard.get('artifacts',{})
    require(artifacts.get('candidate_required')==['pattern_source','proposal','diff','score'],
            'scorecard candidate contract changed','SCHEMA_MISMATCH')
    require(artifacts.get('result_required')==['candidate_id','decision','reason','recorded_at'],
            'scorecard result contract changed','SCHEMA_MISMATCH')
    episodes=_episodes_from_file(file_path) if file_path else _episodes_from_db(db_path,ctx)
    require(episodes,'no episodes supplied and no active versions available','SOURCE_MISSING')
    now=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    groups={}
    unclassified=[]
    skipped=0
    for source,payload in episodes:
        if not ctx.may_access(payload['collection_id']):
            skipped+=1
            continue
        role=_classify_outcome(payload['outcome'])
        if role is None:
            unclassified.append((source,payload))
            continue
        key=(payload['collection_id'],_fingerprint(payload['conditions']))
        groups.setdefault(key,{'collection_id':payload['collection_id'],'conditions':payload['conditions'],
                               'success':[],'failure':[]})[role].append((source,payload))
    written=[]
    rejected=[]
    tick=chr(96)
    for key,group in sorted(groups.items(),key=lambda kv:kv[0][1]):
        if not group['failure'] or not group['success']:
            present='success' if group['success'] else 'failure'
            reason='no_failure_counterexample' if present=='success' else 'no_success_counterexample'
            reason=reason+'; conditions='+json.dumps(group['conditions'],ensure_ascii=False,sort_keys=True)
            for source,payload in group[present]:
                cid='rejected-'+_fingerprint({'c':group['collection_id'],'s':source})
                rejected.append(_record_rejection(output_root,cid,reason,now,source,payload))
            continue
        success_source,success_payload=group['success'][0]
        failure_source,failure_payload=group['failure'][0]
        seed='|'.join([group['collection_id'],_fingerprint(group['conditions']),success_source,failure_source])
        cid='candidate-'+hashlib.sha256(seed.encode()).hexdigest()[:16]
        pattern_source=_pattern_source(success_payload)+'::'+_pattern_source(failure_payload)
        proposal=('在条件 '+json.dumps(group['conditions'],ensure_ascii=False,sort_keys=True)+' 下，'
                  +failure_payload['goal']+' 失败而 '+success_payload['goal']+' 成功；沿用成功侧的 '
                  +(success_payload.get('explicit_reason') or success_payload['narrative']))
        diff='\n'.join(['--- a/skill.md','+++ b/skill.md','@@',
                        '+'+success_payload['narrative'],
                        ('+'+str(success_payload.get('explicit_reason') or '')).rstrip(),
                        '-'+failure_payload['narrative']])
        trace_chars=len(success_payload['narrative'])+len(failure_payload['narrative'])
        source_counts=(_source_trace_chars(success_payload),_source_trace_chars(failure_payload))
        source_trace_chars=sum(source_counts) if None not in source_counts else None
        command_echo=bool(re.search(r'^\s*[$>]\s+',diff,re.M) or
                          re.search(r'(?:^|\n)\s*(?:bash|python3?|git|sed|rg|cat|find)\s+',diff,re.I))
        score={'retrieval_hit_at_5':None,'retrieval_mrr':None,'retrieval_ratio':None,
               'paired_host_gate':None,'compression_guard':'pending_threshold',
               'command_echo_guard':'fail' if command_echo else 'pass',
               'retained_trace_chars':trace_chars,'source_trace_chars':source_trace_chars,
               'R_val':None,'R_best':None}
        fence=tick*3
        frontmatter='\n'.join(['---',
                               'candidate_id: '+json.dumps(cid,ensure_ascii=False),
                               'pattern_source: '+json.dumps(pattern_source,ensure_ascii=False),
                               'collection_id: '+json.dumps(group['collection_id'],ensure_ascii=False),
                               'conditions: '+json.dumps(group['conditions'],ensure_ascii=False,sort_keys=True),
                               'score: '+json.dumps(score,ensure_ascii=False,sort_keys=True),
                               '---'])
        md=(frontmatter+'\n\n# '+success_payload['goal']+'\n\n## proposal\n\n'+proposal
           +'\n\n## diff\n\n'+fence+'diff\n'+diff+'\n'+fence+'\n\n## evidence\n\n'
           +'- success '+tick+success_source+tick+' source_kind='+success_payload['source_kind']
           +' source_refs='+json.dumps(success_payload['source_refs'],ensure_ascii=False)+'\n'
           +'- failure '+tick+failure_source+tick+' source_kind='+failure_payload['source_kind']
           +' source_refs='+json.dumps(failure_payload['source_refs'],ensure_ascii=False)+'\n')
        _write_once(output_root/'candidates'/(cid+'.md'),md)
        written.append({'candidate_id':cid,'path':str(output_root/'candidates'/(cid+'.md')),
                        'pattern_source':pattern_source,'success_episode':success_source,
                        'failure_episode':failure_source,'score':score})
    for source,payload in unclassified:
        cid='rejected-'+_fingerprint({'u':source})
        rejected.append(_record_rejection(output_root,cid,'outcome_status_unclassified',now,source,payload))
    return {'candidates':written,'rejected':rejected,'episodes_read':len(episodes),
            'episodes_skipped_out_of_scope':skipped,'recorded_at':now}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,default=Path(os.environ.get('MEMORY_HUB_DATA',str(Path.home()/'.memory-hub')))/'experience.sqlite3')
    p.add_argument('--collection',action='append',default=[])
    p.add_argument('--artifact-root',action='append',default=[])
    p.add_argument('--subject',default='local-cli')
    p.add_argument('--owner',action='store_true',help='Explicit trusted local management entry; never an MCP argument')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    sub.add_parser('rebuild-index',help='Rebuild derived keys from current versions; owner with all collections required')
    q=sub.add_parser('record');q.add_argument('--file',type=Path,required=True);q.add_argument('--key',required=True)
    q=sub.add_parser('recall');q.add_argument('--task',required=True);q.add_argument('--mode',choices=['evidence','explore'],default='evidence');q.add_argument('--max-chars',type=int,default=6000);q.add_argument('--conditions')
    q=sub.add_parser('read');q.add_argument('--id',required=True);q.add_argument('--revision',type=int,required=True);q.add_argument('--offset',type=int,default=0);q.add_argument('--max-chars',type=int,default=12000)
    q=sub.add_parser('revise');q.add_argument('--id',required=True);q.add_argument('--base-revision',type=int,required=True);q.add_argument('--patch',type=Path,required=True);q.add_argument('--reason',required=True);q.add_argument('--evidence-id',action='append',required=True)
    for command in ('revoke','purge'):
        q=sub.add_parser(command);q.add_argument('--id',required=True);q.add_argument('--base-revision',type=int,required=True);q.add_argument('--reason',required=True)
    q=sub.add_parser('export');q.add_argument('--id',required=True);q.add_argument('--destination',type=Path,required=True)
    for command in ('backup','restore'):
        q=sub.add_parser(command,help='Copy --db to a new destination; explicit owner entry only')
        q.add_argument('--destination',type=Path,required=True)
        if command=='restore':
            q.add_argument('--current-store',type=Path,help='Current governance database; required before a restored copy can be read')
    q=sub.add_parser('extract-pattern',help='Pair success and failure episodes into one candidate proposal; never drops an unpaired episode silently')
    q.add_argument('--file',type=Path,help='Episode JSONL input; omit to read current active versions from --db')
    q.add_argument('--output-root',type=Path,default=WIKISKILL_ROOT,help='Directory holding scorecard.json, candidates/ and results/')
    a=p.parse_args(argv)
    try:
        ctx=AccessContext(a.subject,tuple(a.collection),'owner' if a.owner else 'agent',tuple(a.artifact_root))
        if a.command=='init':
            if not a.owner:raise ExperienceError('ACCESS_DENIED','initialization requires explicit management entry')
            initialize(a.db);result={'initialized':True}
        elif a.command=='rebuild-index':
            from .store import rebuild_keys
            result=rebuild_keys(a.db,ctx)
        elif a.command=='record':result=record_episode(a.db,ctx,json.loads(a.file.read_text()),idempotency_key=a.key)
        elif a.command=='recall':result=recall_for_decision(a.db,ctx,task=a.task,mode=a.mode,max_chars=a.max_chars,conditions=json.loads(a.conditions) if getattr(a,'conditions',None) else None)
        elif a.command=='revise':
            from .revisions import revise_understanding
            result=revise_understanding(a.db,ctx,target_id=a.id,base_revision=a.base_revision,patch=json.loads(a.patch.read_text()),reason=a.reason,evidence_ids=a.evidence_id)
        elif a.command in ('revoke','purge'):
            from .revisions import revoke,purge
            result=(revoke if a.command=='revoke' else purge)(a.db,ctx,target_id=a.id,base_revision=a.base_revision,reason=a.reason)
        elif a.command=='export':
            from .revisions import export_episode
            result=export_episode(a.db,ctx,target_id=a.id,destination=a.destination)
        elif a.command in ('backup','restore'):
            from .recovery import backup_store,restore_store
            result=(backup_store(a.db,ctx,destination=a.destination) if a.command=='backup'
                    else restore_store(a.db,ctx,destination=a.destination,current_store=a.current_store))
        elif a.command=='extract-pattern':
            result=extract_pattern(a.db,ctx,file_path=a.file,output_root=a.output_root,owner=a.owner)
        else:result=read_evidence(a.db,ctx,evidence_id=a.id,revision=a.revision,offset=a.offset,max_chars=a.max_chars)
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
        return 0
    except (ExperienceError,OSError,ValueError) as e:
        print(json.dumps({'error':{'code':getattr(e,'code','PAYLOAD_INVALID'),'message':str(e)}},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
