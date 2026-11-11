"""Small Agent results and bounded, read-only recovery of existing submissions."""
from __future__ import annotations

import asyncio
import json
import math
from uuid import uuid4

from .model import BridgeError, TERMINAL, now
from .uploads import decode_uploads


PROGRESS_SECONDS = 10
POLL_SECONDS = .25

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
        self.tasks = {}
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS agent_recovery(
            operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
            attempts INTEGER NOT NULL, last_attempt REAL NOT NULL, last_error TEXT)''')

    def operation(self, owner, args):
        qid=args.get('query_id'); self.store.query(owner,qid)
        op=self.store.operation(args['message_id']) if args.get('message_id') else self.store.latest(owner,qid)
        if op['owner']!=owner or op['query_id']!=qid:
            raise BridgeError('OPERATION_NOT_FOUND','该消息不属于指定会话。')
        return op

    def recovery_tool(self, op):
        result=op.get('result') or {}
        if op['status']=='OUTCOME_UNKNOWN' and op['submitted_at'] is not None and op['baseline']:
            return 'recover_session'
        if (op['status'] in {'COMPLETED','NEEDS_ATTENTION'} and result.get('completeness_signal')=='complete'
                and not result.get('delivery_complete',True) and not result.get('unsupported_outputs')):
            return 'collect_artifacts'
        return None

    def attempts(self, op):
        row=self.store.db.execute('SELECT attempts,last_attempt FROM agent_recovery WHERE operation_id=?',(op['id'],)).fetchone()
        return tuple(row) if row else (0,0)

    def automatic(self, op):
        return bool(self.recovery_tool(op) and (self.attempts(op)[0]<3 or op['id'] in self.tasks))

    def tick(self):
        for row in self.store.db.execute("SELECT id FROM operations WHERE status IN ('OUTCOME_UNKNOWN','NEEDS_ATTENTION','COMPLETED')").fetchall():
            if len(self.tasks)>=self.service.settings.max_active:break
            op=self.store.operation(row[0]); _,last=self.attempts(op)
            if self.automatic(op) and op['id'] not in self.tasks and now()-last>=10:
                self.tasks[op['id']]=asyncio.create_task(self.repair(op))

    async def repair(self, op):
        try:
            # Both legacy operations verify the original message and never submit.
            count,_=self.attempts(op)
            self.store.db.execute('INSERT OR REPLACE INTO agent_recovery(operation_id,attempts,last_attempt) VALUES(?,?,?)',(op['id'],count+1,now()))
            value=await self.service.dispatch(op['owner'],self.recovery_tool(op),dict(query_id=op['query_id'],message_id=op['id']))
            if value.get('error'):
                self.store.db.execute('UPDATE agent_recovery SET last_error=? WHERE operation_id=?',(json.dumps(value['error'],ensure_ascii=False),op['id']))
            if (value.get('error') or {}).get('error_code')=='RECOVERY_BUSY':
                self.store.db.execute('UPDATE agent_recovery SET attempts=? WHERE operation_id=?',(count,op['id']))
        except Exception as error:
            detail=BridgeError('AUTO_RECOVERY_FAILED','自动处理遇到执行器异常。',context={'type':type(error).__name__}).data
            self.store.db.execute('UPDATE agent_recovery SET last_error=? WHERE operation_id=?',(json.dumps(detail,ensure_ascii=False),op['id']))
        finally:
            self.tasks.pop(op['id'],None)

    async def stop(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def snapshot(self, owner, op, *, partial=False):
        value=await self.service._dispatch(owner,'get_query_result',dict(query_id=op['query_id'],message_id=op['id'],
            compact=False,include_outputs=False,content_format='markdown'))
        result=value.get('result') or {}; state=op['status']
        automatic=self.automatic(op)
        status=('done' if value['complete'] else 'cancelled' if state=='CANCELLED' else 'failed' if state=='FAILED'
                else 'running' if state not in TERMINAL or automatic else 'needs_you')
        output=dict(ok=True,query_id=op['query_id'],message_id=op['id'],round=op['sequence'],status=status,
            elapsed_seconds=round(max(0,(now() if status=='running' else op['updated_at'])-(op['submitted_at'] or op['created_at'])),1),
            answer_characters=len(result.get('content') or ''))
        if status=='running':
            output['phase']='收集文件中' if self.recovery_tool(op)=='collect_artifacts' or result.get('delivery_phase')=='COLLECTING_ARTIFACTS' else PHASES.get(state,'核对中')
            output['next']={'tool':'wait','arguments':{'query_id':op['query_id'],'message_id':op['id'],'timeout_seconds':120}}
        if status=='done' or partial:output['answer']=result.get('content') or ''
        if partial:output['partial']=bool(value['result_is_partial'])
        files=[{k:a.get(k) for k in ('artifact_id','filename','mime_type','size_bytes','download_url')}
               for a in result.get('artifacts',[]) if a['status']=='AVAILABLE']
        if files:output['files']=files
        sources=list(dict.fromkeys(s if isinstance(s,str) else s['url'] for s in result.get('sources',[]) if isinstance(s,str) or s.get('url')))
        if sources:output['sources']=sources
        if status in {'needs_you','failed'}:
            error=value.get('last_error') or {}
            recovery=self.store.db.execute('SELECT last_error FROM agent_recovery WHERE operation_id=?',(op['id'],)).fetchone()
            if recovery and recovery[0]:error=json.loads(recovery[0])
            code=error.get('error_code','INCOMPLETE_RESULT')
            action=('请在服务端完成登录或验证后联系维护者恢复原任务，服务端已停止自动处理。' if code in {'LOGIN_REQUIRED','VERIFICATION_REQUIRED'}
                    else '请联系维护者处理原任务，服务端已停止自动处理，请保留原任务编号。')
            output['problem']={'code':code,'message':error.get('message','结果尚未完整交付。'),'what_to_do':action}
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
                item=self.store.operation(row[0]);value=await self.snapshot(owner,item,partial=True)
                messages.append({k:value[k] for k in ('message_id','round','status','answer')} | dict(
                    phase=value.get('phase'),prompt=item['payload']['text'],attachments=item['payload'].get('attachments',[]),
                    partial=value['partial'],files=value.get('files',[]),sources=value.get('sources',[]),problem=value.get('problem'),
                    created_at=item['created_at'],updated_at=item['updated_at']))
            return dict(ok=True,query_id=q['id'],title=messages[0]['prompt'][:160],conversation_url=q['conversation_url'],status=messages[-1]['status'],messages=messages)
        if tool=='cancel':
            await self.service._dispatch(owner,'cancel_query',dict(query_id=op['query_id'],message_id=op['id']))
            return await self.snapshot(owner,self.store.operation(op['id']))
        if tool=='get' and args.get('verbose',False):
            return await self.service._dispatch(owner,'get_query_result',dict(query_id=op['query_id'],message_id=op['id'],compact=False))
        if tool=='wait':
            timeout=seconds(args.get('timeout_seconds',120));loop=asyncio.get_running_loop()
            start=loop.time();deadline=start+timeout;last=start
            while True:
                self.tick()
                value=await self.snapshot(owner,self.store.operation(op['id']))
                if value['status']!='running' or loop.time()>=deadline:return value
                if progress and loop.time()-last>=PROGRESS_SECONDS:
                    await progress(loop.time()-start,value.get('phase','处理中'));last=loop.time()
                await asyncio.sleep(min(POLL_SECONDS,max(0,deadline-loop.time())))
        self.tick()
        return await self.snapshot(owner,op)
