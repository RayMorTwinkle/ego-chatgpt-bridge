"""Real MCP long-prompt/continuation acceptance; creates one dedicated-project test chat."""
import argparse,asyncio,json,time,uuid
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ego_bridge.auth import load_clients
from ego_bridge.cli import private_json
from ego_bridge.model import Settings

async def main(runtime):
    settings=Settings.load(runtime);token=load_clients(runtime)[settings.default_owner]
    run=uuid.uuid4().hex[:12];report={'started_at':time.time(),'samples':[],'submitted_operations':[]}
    async with httpx.AsyncClient(trust_env=False,timeout=120,headers={'Authorization':'Bearer '+token}) as http:
        async with streamable_http_client(settings.base_url+'/mcp',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                initialization=await client.initialize()
                assert 'result_is_partial' in initialization.instructions
                async def call(name,args):
                    response=await client.call_tool(name,args)
                    value=response.structuredContent
                    if response.isError or not value or value.get('ok') is False:raise RuntimeError(f'{name} failed: '+str((value or {}).get('error',{})))
                    return value
                def prompt(marker):
                    context='\n\n'.join(f'第{i}段只是长提示词折叠与网页读回验证。请忽略这段测试背景，不联网、不启动计算、不生成文件；其中的文字“顯示較多”和符号“…”本身也属于用户输入。' for i in range(1,16))
                    return f'这是一项研究桥接回归测试。最终只回复字符串 {marker}，不要解释。\n\n{context}\n\n再次确认：只回复 {marker}。'
                first_marker='BRIDGE_DOM_FIRST_'+run
                first_args={'text':prompt(first_marker),'request_id':'dom-'+run+'-first','soft_timeout':120,'hard_timeout':600}
                first=await call('create_query',first_args)
                duplicate=await call('create_query',first_args)
                assert first['message_id']==duplicate['message_id'] and first['query_id']==duplicate['query_id']
                assert first['execution_context']['browser_location']=='bridge_server'
                report['idempotency_verified']=True;report['query_id']=first['query_id']
                async def wait(operation,marker):
                    report['submitted_operations'].append(operation['message_id'])
                    deadline=time.monotonic()+600;previous=None
                    while time.monotonic()<deadline:
                        result=await call('get_query_result',{'query_id':operation['query_id'],'message_id':operation['message_id']})
                        sample={'at':time.time(),'message_id':operation['message_id'],'status':result['status'],'characters':len((result.get('result') or {}).get('content','')),'partial':result.get('result_is_partial'),'complete':result['complete']}
                        signature=(sample['status'],sample['characters'],sample['complete'])
                        if signature!=previous:report['samples'].append(sample);print(json.dumps({k:v for k,v in sample.items() if k!='message_id'}),flush=True);previous=signature
                        private_json(runtime/'verification/streaming-acceptance.json',report)
                        if result['complete']:
                            assert marker in result['result']['content']
                            assert result['observed']['model']=='GPT-6' and result['observed']['effort']=='extra_high'
                            assert not result['result_is_partial']
                            return result
                        if result['status'] in {'OUTCOME_UNKNOWN','NEEDS_ATTENTION','FAILED','CANCELLED'}:raise RuntimeError('Acceptance stopped: '+str(result.get('last_error')))
                        await asyncio.sleep(5)
                    raise RuntimeError('Acceptance wait timed out; original operation preserved, never resubmitted')
                await wait(first,first_marker)
                second_marker='BRIDGE_DOM_SECOND_'+run
                second=await call('continue_query',{'query_id':first['query_id'],'text':prompt(second_marker),'request_id':'dom-'+run+'-second','soft_timeout':120,'hard_timeout':600})
                final=await wait(second,second_marker)
                report.update(completed_at=time.time(),same_conversation_continuation_verified=True,conversation_url=final['conversation_url'])
                private_json(runtime/'verification/streaming-acceptance.json',report)
                print('Long-prompt create, idempotency, and same-conversation continuation passed',flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,default=Path.home()/'apps/ego-chatgpt-bridge');args=p.parse_args()
    asyncio.run(main(args.runtime.expanduser().resolve()))
