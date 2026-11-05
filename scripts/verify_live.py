"""Explicit live integration test: sends test messages only inside the configured project."""
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

import httpx
from ego_bridge.cli import DEFAULT_RUNTIME
from ego_bridge.auth import load_clients
from ego_bridge.model import Settings
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=DEFAULT_RUNTIME


async def main():
    token=load_clients(RUNTIME)[Settings.load(RUNTIME).default_owner]
    settings=Settings.load(RUNTIME)
    context=ssl.create_default_context(cafile=str(RUNTIME/'tls-cert.pem')) if settings.tls_enabled else True
    config=json.loads((RUNTIME/'config.json').read_text())
    hosts=config['allowed_hosts']
    lan=next((h for h in hosts if h.startswith(('192.168.','10.','172.'))),'127.0.0.1')
    url=settings.base_url
    report={'started_at':time.time(),'endpoint':url,'mode':'real_browser_and_network','checks':[],'tasks':[]}
    target=RUNTIME/'verification/live-integration.json'
    target.parent.mkdir(mode=0o700,exist_ok=True)
    def save():
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    async with httpx.AsyncClient(verify=context,headers={'Authorization':f'Bearer {token}'},timeout=90) as http:
        async def api(name,args):
            r=await http.post(url+'/api/'+name,json=args)
            r.raise_for_status()
            value=r.json()
            if not value.get('ok'):
                raise RuntimeError(value)
            return value
        async with httpx.AsyncClient(verify=context) as anonymous:
            assert (await anonymous.post(url+'/api/list_queries',json={})).status_code==401
        report['checks'].append('Configured LAN transport connected; anonymous access rejected')
        async with streamable_http_client(url+'/mcp',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                assert len(tools.tools)==17
                result=await session.call_tool('list_queries',{})
                assert result.structuredContent['ok']
                report['checks'].append('Real MCP SDK initialize, 17 tool schemas, tools/call with structured output')
        stamp=str(time.time_ns())
        requests=[dict(text=f'EGO_LIVE_{stamp}_{n}: 这是桥接服务集成测试。请只回复 LIVE_OK_{n}。',request_id=f'live-{stamp}-{n}') for n in (1,2)]
        tasks=await asyncio.gather(*(api('create_query',r) for r in requests))
        report['tasks']=[dict(query_id=t['query_id'],message_id=t['message_id']) for t in tasks]
        duplicate=await api('create_query',requests[0])
        assert duplicate['message_id']==tasks[0]['message_id']
        report['checks'].append('Duplicate idempotency key reused same query and message')
        save()
        active_overlap=False
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            states=await asyncio.gather(*(api('get_query_result',dict(query_id=t['query_id'])) for t in tasks))
            statuses=[s['status'] for s in states]
            if all(s in {'SUBMITTING','WAITING_FOR_RESPONSE','GENERATING'} for s in statuses):
                active_overlap=True
            if any(s in {'NEEDS_ATTENTION','FAILED','OUTCOME_UNKNOWN'} for s in statuses):
                report['failure']=states;save();raise RuntimeError(states)
            if all(s=='COMPLETED' for s in statuses):
                break
            await asyncio.sleep(1)
        else:
            report['failure']='Live test polling timeout; background tasks retained';save();raise TimeoutError()
        for i,s in enumerate(states,1):
            assert s['complete'] and s['result']['content'].strip()==f'LIVE_OK_{i}'
        report['results']=states
        report['scheduler_active_overlap_observed']=active_overlap
        report['checks'].append('Two separate project conversations completed through asynchronous scheduler')
        qid=tasks[0]['query_id']
        follow=await api('continue_query',dict(query_id=qid,text='请原样回复你上一次回复的标记，并加上 :FOLLOWUP。',request_id=f'live-{stamp}-follow'))
        while time.monotonic()<deadline:
            result=await api('get_query_result',dict(query_id=qid,message_id=follow['message_id']))
            if result['status']=='COMPLETED':
                break
            if result['status'] in {'NEEDS_ATTENTION','FAILED','OUTCOME_UNKNOWN'}:
                report['failure']=result;save();raise RuntimeError(result)
            await asyncio.sleep(1)
        assert result['result']['content'].strip()=='LIVE_OK_1:FOLLOWUP'
        assert result['conversation_id']==states[0]['conversation_id']
        report['followup']=result
        report['checks'].append('Follow-up retained exact conversation ID and previous answer context')
        report['finished_at']=time.time()
        save()
        print(json.dumps({'ok':True,'endpoint':url,'checks':report['checks'],'scheduler_active_overlap_observed':active_overlap,'report':str(target)},ensure_ascii=False,indent=2))


if __name__=='__main__':
    asyncio.run(main())
