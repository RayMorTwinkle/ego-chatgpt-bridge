"""Opt-in live MCP acceptance: one small chat, no duplicate submissions."""
import asyncio,json,time,uuid,sys
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ego_bridge.auth import load_clients
from ego_bridge.model import Settings
from ego_bridge.cli import private_json

async def main():
    runtime=Path.home()/'apps/ego-chatgpt-bridge';settings=Settings.load(runtime)
    path=runtime/'verification/feedback-acceptance.json';report=json.loads(path.read_text()) if '--verify-existing' in sys.argv else {'samples':[]}
    async with httpx.AsyncClient(trust_env=False,timeout=120,headers={'Authorization':'Bearer '+load_clients(runtime)[settings.default_owner]}) as http:
        async with streamable_http_client(settings.base_url+'/mcp',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize()
                async def call(tool,args):
                    r=await client.call_tool(tool,args);v=r.structuredContent
                    assert v and v.get('ok'),(tool,(v or {}).get('error'))
                    return v
                health=await call('health_check',{});report['health']=health
                print(json.dumps({'health_ok':health['health']['probe']['ok'],'page':health['runtime']['page_label'],'worker_running':health['health']['worker']['running']}),flush=True)
                op={'query_id':report['query_id'],'message_id':report['message_id']} if '--verify-existing' in sys.argv else await call('retry_query',{'query_id':json.loads(path.read_text())['query_id']}) if '--resume-unsent' in sys.argv else await call('create_query',{'text':'桥接回归测试（验证纯文本 URL 输入修复），不联网。请先写一句开场白，再给一个 Markdown 表格：列名 Name、Source，一行 Python、[Official](https://www.python.org/)。最后单独一行 END_OF_BRIDGE_TEST。不要添加其他内容。','request_id':'feedback-'+uuid.uuid4().hex,'context_mode':'request_only_best_effort','hard_timeout':600})
                report['query_id']=op['query_id'];report['message_id']=op['message_id'];private_json(path,report)
                cursor=None;deadline=time.monotonic()+660
                while time.monotonic()<deadline:
                    r=await call('wait_query',{'query_id':op['query_id'],'message_id':op['message_id'],'cursor':cursor,'timeout_seconds':25})
                    cursor=r['cursor'];report['samples'].append({k:r.get(k) for k in ('status','generation_done','extraction_stable','generation_complete','complete','changed','cursor')})
                    assert 'result' not in r
                    print(json.dumps({k:v for k,v in report['samples'][-1].items() if k!='cursor'}),flush=True);private_json(path,report)
                    if r['complete']:break
                    assert r['status'] not in {'NEEDS_ATTENTION','OUTCOME_UNKNOWN','CANCELLED'},r.get('last_error')
                else:raise RuntimeError('Timed out; original operation preserved')
                r=await call('get_query_result',{'query_id':op['query_id']});body=r['result']
                assert r['extraction_stable'] and r['semantic_validation']=='not_performed'
                assert body['completion_confirmation']['samples']>=2
                assert body['content_format']=='markdown' and '| Name | Source |' in body['content']
                assert '[Official](https://www.python.org/' in body['content'] and 'END_OF_BRIDGE_TEST' in body['content'].replace('\\_','_')
                assert any(link['url'].startswith('https://www.python.org/') for link in body['sources'])
                assert 'outputs' not in body and 'markdown' not in body
                assert body['extraction']['table_count']==1 and body['extraction']['source_status']=='sources_present'
                unchanged=await call('wait_query',{'query_id':op['query_id'],'cursor':cursor,'timeout_seconds':0,'include_content':True})
                assert not unchanged['changed'] and 'result' not in unchanged
                report['result']=r;report['completed_at']=time.time();report['unchanged_suppressed']=True;private_json(path,report)
                print('PASS: dedicated health page, real MCP cursor wait, stable completion, Markdown table/link, context warning',flush=True)

if __name__=='__main__':asyncio.run(main())
