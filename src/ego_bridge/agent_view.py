"""Small Agent results backed by execution-layer state."""
from __future__ import annotations

import asyncio
import json
import math
from uuid import uuid4

from .model import BridgeError, TERMINAL, now
from .uploads import decode_uploads


PROGRESS_SECONDS = 10
POLL_SECONDS = 1.0

TOOLS = {'ask','wait','get','list','cancel','get_thread'}
PHASES = {'QUEUED':'排队中','PREPARING':'发送中','SUBMITTING':'发送中',
          'WAITING_FOR_RESPONSE':'思考中','GENERATING':'生成中','OUTCOME_UNKNOWN':'核对中'}


def seconds(value):
    if type(value) not in {int,float} or not math.isfinite(value) or not 0<=value<=300:
        raise BridgeError('INVALID_TIMEOUT','等待时间必须为 0..300 秒。')
    return value


class AgentView:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def operation(self, owner, args):
        qid=args.get('query_id'); self.store.query(owner,qid)
        op=self.store.operation(args['message_id']) if args.get('message_id') else self.store.latest(owner,qid)
        if op['owner']!=owner or op['query_id']!=qid:
            raise BridgeError('OPERATION_NOT_FOUND','该消息不属于指定会话。')
        return op

    async def snapshot(self, owner, op, *, partial=False, thread=False):
        value=await self.service._dispatch(owner,'get_query_result',dict(query_id=op['query_id'],message_id=op['id'],
            compact=False,include_outputs=False,content_format='markdown'))
        result=value.get('result') or {}; state=op['status']
        retry=value.get('auto_retry') or {}
        recovering=state=='OUTCOME_UNKNOWN' and retry.get('kind')=='recover' and retry.get('next_at') is not None
        status=('done' if value['complete'] else 'cancelled' if state=='CANCELLED' else 'failed' if state=='FAILED'
                else 'running' if state not in TERMINAL or recovering else 'needs_you')
        output=dict(ok=True,query_id=op['query_id'],message_id=op['id'],round=op['sequence'],status=status,
            elapsed_seconds=round(max(0,(now() if status=='running' else op['updated_at'])-(op['submitted_at'] or op['created_at'])),1),
            answer_characters=len(result.get('content') or ''))
        if status=='running':
            output['phase']=('收集文件中' if state=='GENERATING' and result.get('completeness_signal')=='complete' and not value['delivery_complete']
                else '等待自动重试' if state=='QUEUED' and retry.get('next_at') is not None else PHASES.get(state,'核对中'))
            output['next']={'tool':'wait','arguments':{'query_id':op['query_id'],'message_id':op['id'],'timeout_seconds':50}}
        if status=='done' or partial:output['answer']=result.get('content') or ''
        if partial:output['partial']=bool(value['result_is_partial'])
        files=[{k:a.get(k) for k in ('artifact_id','filename','mime_type','size_bytes','download_url')}
               for a in result.get('artifacts',[]) if a['status']=='AVAILABLE']
        if files:output['files']=files
        sources=list(dict.fromkeys(s if isinstance(s,str) else s['url'] for s in result.get('sources',[]) if isinstance(s,str) or s.get('url')))
        if sources:output['sources']=sources
        if status in {'needs_you','failed'}:
            error=value.get('last_error') or {}
            code=error.get('error_code','INCOMPLETE_RESULT');attempts=retry.get('attempts',0)
            action=(f'回答已生成，文件补收 {attempts} 次失败；可在 WebUI 重新收集文件。' if code=='ARTIFACT_CAPTURE_FAILED'
                    else f'服务端已自动重试 {attempts} 次仍失败；修好浏览器/登录问题后可在 WebUI 点重试，或调用旧接口 retry_query。'
                    if error.get('safe_to_retry') and attempts>=retry.get('max',3) and retry.get('next_at') is None
                    else '需要在服务端完成登录或验证。' if code in {'LOGIN_REQUIRED','VERIFICATION_REQUIRED'}
                    else '请把 query_id 交给维护者，服务端不会重发。')
            output['problem']={'code':code,'message':error.get('message','结果尚未完整交付。'),'what_to_do':action}
        if thread:
            output.update(raw_status=state,last_error=op['error'],
                generation_complete=value['generation_complete'],delivery_complete=value['delivery_complete'],
                delivery_expired=value['delivery_expired'],effort=(op.get('observed') or {}).get('effort',op['payload']['effort']),
                auto_retry={k:retry.get(k) for k in ('attempts','max','next_at','kind')},
                progress={k:value['progress'][k] for k in ('answer_characters','source_count')},
                sources=[{'title':s.get('title'),'url':s['url']} if isinstance(s,dict) else {'title':None,'url':s}
                         for s in result.get('sources',[]) if isinstance(s,str) or s.get('url')],
                external_images=result.get('external_images',[]),unsupported_outputs=result.get('unsupported_outputs',[]))
        return output

    async def dispatch(self, owner, tool, args, progress=None):
        if tool=='ask':
            timeout=seconds(args.get('wait_seconds',0))
            text=args.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>100000:
                raise BridgeError('INVALID_ARGUMENT','text 必须为 1..100000 字符的非空字符串。')
            if args.get('effort','extra_high') not in {'extra_high','high'}:
                raise BridgeError('EFFORT_NOT_ALLOWED','只允许 extra_high 或 high。')
            request=args.get('request_id'); qid=args.get('query_id')
            if qid is not None:self.store.query(owner,qid)
            if request is None:
                # No awaits between lookup and enqueue: concurrent requests on this
                # service's event loop cannot both miss the same durable operation.
                attachments,_=decode_uploads(args.get('attachments'))
                hashes=sorted(a['sha256'] for a in attachments)
                rows=self.store.db.execute('''SELECT id FROM operations WHERE owner=? AND
                    ((? IS NULL AND kind='create') OR (? IS NOT NULL AND kind='continue' AND query_id=?)) ORDER BY created_at DESC''',(owner,qid,qid,qid)).fetchall()
                for row in rows:
                    op=self.store.operation(row[0])
                    if (op['payload']['text']==args.get('text') and sorted(a['sha256'] for a in op['payload'].get('attachments',[]))==hashes
                            and (op['status'] not in TERMINAL or op['status']=='OUTCOME_UNKNOWN' or now()-op['created_at']<=600)):
                        return await self.dispatch(owner,'wait',dict(query_id=op['query_id'],message_id=op['id'],timeout_seconds=timeout),progress)
                request='auto_'+uuid4().hex
            value=await self.service._dispatch(owner,'continue_query' if qid else 'create_query',{**args,'request_id':request})
            return await self.dispatch(owner,'wait',dict(query_id=value['query_id'],message_id=value['message_id'],timeout_seconds=timeout),progress)
        if tool=='list':
            status=args.get('status');limit=args.get('limit',20)
            if status is not None and status not in {'running','done','needs_you','failed','cancelled'}:
                raise BridgeError('INVALID_ARGUMENT','status 必须为 running、done、needs_you、failed 或 cancelled。')
            if type(limit) is not int or not 1<=limit<=100:raise BridgeError('INVALID_ARGUMENT','limit 必须为 1..100。')
            queries=[]
            # Filter after status mapping, before applying the requested limit.
            for row in self.store.db.execute('SELECT id FROM queries WHERE owner=? ORDER BY updated_at DESC',(owner,)).fetchall():
                op=self.store.latest(owner,row[0]); value=await self.snapshot(owner,op)
                if status is None or value['status']==status:
                    first=self.store.db.execute('SELECT payload FROM operations WHERE query_id=? ORDER BY sequence LIMIT 1',(row[0],)).fetchone()
                    queries.append(dict(query_id=row[0],title=json.loads(first[0])['text'][:160],status=value['status'],rounds=op['sequence'],updated_at=self.store.query(owner,row[0])['updated_at']))
                    if len(queries)==limit:break
            return {'ok':True,'queries':queries}
        op=self.operation(owner,args)
        if tool=='get_thread':
            q=self.store.query(owner,op['query_id']);messages=[]
            for row in self.store.db.execute('SELECT id FROM operations WHERE query_id=? ORDER BY sequence',(q['id'],)).fetchall():
                item=self.store.operation(row[0]);value=await self.snapshot(owner,item,partial=True,thread=True)
                messages.append({k:value[k] for k in ('message_id','round','status','answer','raw_status','last_error','generation_complete','delivery_complete',
                    'delivery_expired','effort','auto_retry','progress','external_images','unsupported_outputs')} | dict(
                    phase=value.get('phase'),prompt=item['payload']['text'],attachments=item['payload'].get('attachments',[]),
                    partial=value['partial'],files=value.get('files',[]),sources=value.get('sources',[]),problem=value.get('problem'),
                    created_at=item['created_at'],updated_at=item['updated_at']))
            return dict(ok=True,query_id=q['id'],title=messages[0]['prompt'][:160],chat_title=q['chat_title'],conversation_url=q['conversation_url'],status=messages[-1]['status'],messages=messages)
        if tool=='cancel':
            await self.service._dispatch(owner,'cancel_query',dict(query_id=op['query_id'],message_id=op['id']))
            return await self.snapshot(owner,self.store.operation(op['id']))
        if tool=='get' and args.get('verbose',False):
            return await self.service._dispatch(owner,'get_query_result',dict(query_id=op['query_id'],message_id=op['id'],compact=False))
        if tool=='wait':
            timeout=seconds(args.get('timeout_seconds',50));loop=asyncio.get_running_loop()
            start=loop.time();deadline=start+timeout;last=start
            while True:
                value=await self.snapshot(owner,self.store.operation(op['id']))
                if value['status']!='running' or loop.time()>=deadline:return value
                if progress and loop.time()-last>=PROGRESS_SECONDS:
                    await progress(loop.time()-start,value.get('phase','处理中'));last=loop.time()
                await asyncio.sleep(min(POLL_SECONDS,max(0,deadline-loop.time())))
        return await self.snapshot(owner,op)
