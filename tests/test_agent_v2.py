import asyncio
import base64
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ego_bridge import agent_view
from ego_bridge.model import Settings
from ego_bridge.server import build_app
from ego_bridge.service import Service
from ego_bridge.store import Store
from test_reliability import FakeBrowser
from test_artifacts import FileBrowser, PNG


@pytest.fixture
async def rig(tmp_path):
    db=Store(tmp_path);browser=FakeBrowser()
    service=Service(Settings(tmp_path,completion_confirm_seconds=0,poll_seconds=.01),db,browser)
    yield service,db,browser
    await service.stop();db.close()


async def ask(s, **args):
    value=await s.dispatch('alice','ask',{'text':'你好，请回答。',**args})
    assert value['ok'],value
    return value


async def finish(s,db,value):
    await s.execute(db.operation(value['message_id']))
    await s.observe(db.operation(value['message_id']))


async def test_automatic_dedup_identity_query_hash_and_restart(rig):
    s,db,b=rig
    a,z=await asyncio.gather(ask(s),ask(s))
    assert a['message_id']==z['message_id'] and len(db.candidates())==1
    other=await s.dispatch('bob','ask',{'text':'你好，请回答。'})
    assert other['query_id']!=a['query_id']
    attached={'attachments':[{'filename':'a.txt','content_base64':base64.b64encode(b'hello').decode()}]}
    f=await ask(s,**attached)
    renamed=await ask(s,attachments=[{'filename':'b.txt','content_base64':'aGVsbG8='}])
    assert f['message_id']==renamed['message_id']!=a['message_id']
    await finish(s,db,a)
    follow=await ask(s,query_id=a['query_id'])
    assert follow['round']==2 and follow['message_id']!=a['message_id']
    assert (await ask(s,query_id=a['query_id']))['message_id']==follow['message_id']
    fresh=Service(s.settings,db,b)
    assert (await ask(fresh))['message_id']==a['message_id']
    await fresh.stop()


async def test_dedup_age_active_and_explicit_key(rig):
    s,db,b=rig
    a=await ask(s)
    db.db.execute('UPDATE operations SET created_at=created_at-601 WHERE id=?',(a['message_id'],))
    assert (await ask(s))['message_id']==a['message_id']
    await finish(s,db,a)
    new=await ask(s)
    assert new['message_id']!=a['message_id']
    x=await ask(s,request_id='explicit')
    assert x['message_id']!=new['message_id']
    assert (await ask(s,request_id='explicit'))['message_id']==x['message_id']
    conflict=await s.dispatch('alice','ask',{'text':'different','request_id':'explicit'})
    assert conflict['problem']['code']=='IDEMPOTENCY_CONFLICT'


async def test_compact_result_markdown_thread_and_verbose(rig):
    s,db,b=rig
    a=await ask(s)
    assert a['status']=='running' and a['phase']=='排队中' and 'answer' not in a
    await s.execute(db.operation(a['message_id']))
    b.pages['p1'][0].update(assistant_text='这是本次测试的简短回答。',assistant_markdown='**这是本次测试的简短回答。**',sources=[{'url':'https://example.com','title':'example'}])
    await s.observe(db.operation(a['message_id']))
    args={'query_id':a['query_id']}
    done=await s.dispatch('alice','wait',{**args,'timeout_seconds':0})
    assert done['status']=='done' and done['answer'].startswith('**')
    assert done['sources']==['https://example.com'] and len(json.dumps(done,ensure_ascii=False))<600
    assert not {'execution_context','content_sha256','complete','extraction'} & done.keys()
    old=await s.dispatch('alice','get_query_result',{**args,'compact':False})
    assert await s.dispatch('alice','get',{**args,'verbose':True})==old
    db.db.execute('UPDATE queries SET chat_title=? WHERE id=?',('服务端标题',a['query_id']))
    second=await ask(s,query_id=a['query_id'],text='接着解释。')
    thread=await s.dispatch('alice','get_thread',args)
    assert thread['title']=='你好，请回答。' and thread['status']=='running'
    assert [m['round'] for m in thread['messages']]==[1,2]
    assert thread['messages'][0]['answer']==done['answer'] and not thread['messages'][0]['partial']
    assert thread['messages'][1]['prompt']=='接着解释。' and thread['chat_title']=='服务端标题'
    first,pending=thread['messages']
    assert first['raw_status']=='COMPLETED' and first['last_error'] is None
    assert first['generation_complete'] and first['delivery_complete'] and not first['delivery_expired']
    assert first['effort']=='extra_high' and first['progress']==dict(answer_characters=len('这是本次测试的简短回答。'),source_count=1)
    assert first['sources']==[{'title':'example','url':'https://example.com'}]
    assert first['external_images']==first['unsupported_outputs']==[]
    assert pending['raw_status']=='QUEUED' and not pending['generation_complete'] and not pending['delivery_complete']
    assert pending['auto_retry']==dict(attempts=0,max=3,next_at=None,kind=None)
    assert pending['progress']==dict(answer_characters=0,source_count=0)
    legacy_list=await s.dispatch('alice','list_queries',{})
    assert legacy_list['queries'][0]['rounds']==2
    listed=await s.dispatch('alice','list',{'status':'running'})
    assert set(listed['queries'][0])=={'query_id','title','status','rounds','updated_at'}
    assert (await s.dispatch('alice','list',{'status':'done'}))['queries']==[]
    print('V2_TEST_JSON '+json.dumps({'ask_input':{'text':'你好，请回答。'},'ask_output':a,'wait_input':{**args,'timeout_seconds':0},'wait_output':done},ensure_ascii=False))


async def test_thread_preserves_media_references_and_observed_effort(rig):
    s,db,b=rig;a=await ask(s);await finish(s,db,a)
    op=db.operation(a['message_id']);result=dict(op['result'])
    result.update(external_images=[{'title':'diagram','url':'https://example.com/image.png','delivery':'reference_only'}],
        unsupported_outputs=[{'kind':'video','export_status':'UNSUPPORTED'}])
    db.transition(op['id'],op['status'],result=result)
    db.patch(op['id'],observed={**op['observed'],'effort':'high'})
    message=(await s.dispatch('alice','get_thread',{'query_id':a['query_id']}))['messages'][0]
    assert message['external_images']==result['external_images'] and message['unsupported_outputs']==result['unsupported_outputs']
    assert message['effort']=='high' and len(b.sent)==1


async def test_wait_pins_round_and_ask_can_wait(rig,monkeypatch):
    monkeypatch.setattr(agent_view,'POLL_SECONDS',.005)
    s,db,b=rig
    async def complete_later():
        await asyncio.sleep(.02)
        await finish(s,db,{'message_id':db.candidates()[0]['id']})
    task=asyncio.create_task(complete_later())
    done=await ask(s,wait_seconds=1)
    await task
    assert done['status']=='done' and done['answer']=='answer'
    next_round=await ask(s,query_id=done['query_id'],text='next')
    async def append_later():
        await asyncio.sleep(.02)
        third=await ask(s,query_id=done['query_id'],text='third')
        await finish(s,db,next_round)
        return third
    task=asyncio.create_task(append_later())
    waited=await s.dispatch('alice','wait',{'query_id':done['query_id'],'timeout_seconds':1})
    third=await task
    assert waited['message_id']==next_round['message_id']!=third['message_id'] and waited['status']=='done'


@pytest.mark.parametrize('timeout',[301,-1,float('nan'),float('inf'),'120',True])
async def test_wait_bounds_and_ask_does_not_submit_invalid_timeout(rig,timeout):
    s,db,b=rig
    r=await s.dispatch('alice','ask',{'text':'x','wait_seconds':timeout})
    assert not r['ok'] and r['problem']['code']=='INVALID_TIMEOUT' and not db.candidates()
    a=await ask(s)
    r=await s.dispatch('alice','wait',{'query_id':a['query_id'],'timeout_seconds':timeout})
    assert r['problem']['code']=='INVALID_TIMEOUT'


async def test_timeout_300_accepted_and_no_running_body_progress(rig,monkeypatch):
    s,db,b=rig
    a=await ask(s);await finish(s,db,a)
    assert (await s.dispatch('alice','wait',{'query_id':a['query_id'],'timeout_seconds':300}))['status']=='done'
    running=await ask(s,text='another');events=[]
    monkeypatch.setattr(agent_view,'PROGRESS_SECONDS',.01);monkeypatch.setattr(agent_view,'POLL_SECONDS',.005)
    async def progress(elapsed,phase):events.append((elapsed,phase))
    value=await s.dispatch('alice','wait',{'query_id':running['query_id'],'timeout_seconds':.045},progress=progress)
    assert value['status']=='running' and 'answer' not in value and len(events)>=2
    assert all(phase=='排队中' for _,phase in events) and events==sorted(events)


async def test_auto_recover_never_resends_and_bounded_budget(rig,monkeypatch):
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    s,db,b=rig;b.crash_after_send=True
    a=await ask(s);await s.execute(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['auto_retry']['kind']=='recover'
    await s.automatic_recovery()
    value=await s.dispatch('alice','get',{'query_id':a['query_id']})
    assert value['status']=='running' and value['phase']=='核对中'
    clock[0]+=15;await s.automatic_recovery()
    assert (await s.dispatch('alice','wait',{'query_id':a['query_id'],'timeout_seconds':0}))['status']=='done'
    assert len(b.sent)==1
    bad=await ask(s,text='broken');await s.execute(db.operation(bad['message_id']))
    b.pages['p2'].append(dict(b.pages['p2'][0]))
    await s.automatic_recovery();clock[0]+=15;await s.automatic_recovery()
    value=await s.dispatch('alice','get',{'query_id':bad['query_id']})
    assert value['status']=='needs_you' and '服务端不会重发' in value['problem']['what_to_do']
    retry=db.operation(bad['message_id'])['auto_retry']
    assert retry['attempts']==retry['max']==1 and retry['next_at'] is None
    fresh=Service(s.settings,db,b);await fresh.automatic_recovery()
    assert db.operation(bad['message_id'])['auto_retry']==retry and len(b.sent)==2
    assert not db.db.execute("SELECT name FROM sqlite_master WHERE name='agent_recovery'").fetchall()
    await fresh.stop()


async def test_auto_collect_files_and_no_resend(tmp_path,monkeypatch):
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    db=Store(tmp_path);b=FileBrowser(b'invalid');s=Service(Settings(tmp_path,completion_confirm_seconds=0),db,b)
    try:
        a=await ask(s);await s.execute(db.operation(a['message_id']))
        b.pages['p1'][0]['artifacts']=[{'kind':'file_card','index':0,'name':'test.png'}]
        await s.observe(db.operation(a['message_id']))
        value=await s.dispatch('alice','get',{'query_id':a['query_id']})
        assert value['status']=='running' and value['phase']=='收集文件中' and 'answer' not in value
        thread=await s.dispatch('alice','get_thread',{'query_id':a['query_id']})
        message=thread['messages'][0]
        assert message['generation_complete'] and not message['delivery_complete'] and not message['delivery_expired']
        assert message['raw_status']=='GENERATING' and message['auto_retry']==dict(attempts=1,max=3,next_at=1015.0,kind='artifact')
        b.data=PNG
        clock[0]+=15;await s.observe(db.operation(a['message_id']))
        done=await s.dispatch('alice','get',{'query_id':a['query_id']})
        assert done['status']=='done' and done['files'][0]['size_bytes']==len(PNG)
        assert set(done['files'][0])=={'artifact_id','filename','mime_type','size_bytes','download_url'}
        assert len(b.sent)==1
    finally:
        await s.stop();db.close()


async def test_cancel_and_owner_message_isolation(rig):
    s,db,b=rig;a=await ask(s);z=await ask(s,text='z')
    for tool in ['get','wait','get_thread','cancel']:
        denied=await s.dispatch('bob',tool,{'query_id':a['query_id'],'timeout_seconds':0})
        assert not denied['ok'] and denied['problem']['code']=='QUERY_NOT_FOUND'
        wrong=await s.dispatch('alice',tool,{'query_id':a['query_id'],'message_id':z['message_id']})
        assert wrong['problem']['code']=='OPERATION_NOT_FOUND'
    cancelled=await s.dispatch('alice','cancel',{'query_id':a['query_id']})
    assert cancelled['status']=='cancelled' and not b.sent
    await s.dispatch('alice','delete_query',{'query_id':a['query_id']})


@pytest.mark.parametrize('legacy',[False,True])
async def test_default_and_legacy_mcp_catalog_and_single_text_channel(tmp_path,legacy):
    app=build_app(Settings(tmp_path,mcp_legacy_tools=legacy,poll_seconds=.01,completion_confirm_seconds=0),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    init=await session.initialize();assert init.instructions.count('。')<=5
                    catalog={t.name:t for t in (await session.list_tools()).tools}
                    expected={'ask','wait','get','list','read_artifact','cancel','diagnose'}
                    assert expected<=catalog.keys() if legacy else expected==catalog.keys()
                    assert ('create_query' in catalog)==legacy
                    assert catalog['ask'].annotations.readOnlyHint is False
                    a=await session.call_tool('ask',{'text':'SDK test'})
                    assert len(a.content)==1 and json.loads(a.content[0].text)==a.structuredContent
                    done=await session.call_tool('wait',{'query_id':a.structuredContent['query_id'],'timeout_seconds':1})
                    assert done.structuredContent['status']=='done' and done.content[0].text.startswith('answer\n\n---\nquery_id: '+done.structuredContent['query_id'])
                    denied=await session.call_tool('get',{'query_id':'missing'})
                    assert denied.isError and denied.structuredContent['problem']['code']=='QUERY_NOT_FOUND'
            # Existing REST names remain available regardless of MCP registration.
            headers={'Authorization':'Bearer '+'a'*40}
            r=await http.post('https://localhost/api/list_queries',json={},headers=headers)
            assert r.json()['ok'] and r.json()['queries'][0]['rounds']==1
            qid=a.structuredContent['query_id']
            await http.post('https://localhost/ui/api/login',json={'token':'a'*40})
            thread=(await http.post('https://localhost/ui/api/tool/get_thread',json={'query_id':qid})).json()
            assert thread['messages'][0]['answer']=='answer'


async def test_progress_token_receives_mcp_sse_notifications(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_view,'PROGRESS_SECONDS',.01);monkeypatch.setattr(agent_view,'POLL_SECONDS',.005)
    browser=FakeBrowser();browser.complete=False
    app=build_app(Settings(tmp_path,poll_seconds=.01),adapter=browser,clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize();events=[]
                    async def progress(value,total,message):events.append((value,total,message))
                    result=await session.call_tool('ask',{'text':'long research','wait_seconds':.06},progress_callback=progress)
                    assert len(events)>=2 and all(event[1] is None for event in events)
                    assert result.structuredContent['status']=='running' and 'answer' not in result.structuredContent
                    assert all('partial' not in str(event) for event in events)


async def test_worker_recovers_without_agent_polling(rig,monkeypatch):
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    s,db,b=rig;b.crash_after_send=True
    a=await ask(s)
    await s.start()
    try:
        async with asyncio.timeout(1):
            while db.operation(a['message_id'])['status']!='COMPLETED':
                clock[0]+=15;await asyncio.sleep(.01)
        assert len(b.sent)==1 and db.operation(a['message_id'])['auto_retry']['attempts']==1
    finally:await s.stop()


async def test_failure_states_and_recovery_refuses_unsent_operations(rig):
    s,db,b=rig
    a=await ask(s);b.fail_prepare=True
    await s.execute(db.operation(a['message_id']))
    value=await s.dispatch('alice','get',{'query_id':a['query_id']})
    assert value['status']=='needs_you' and value['problem']['code']=='LOGIN_REQUIRED'
    assert not b.sent
    db.db.execute("UPDATE operations SET status='FAILED' WHERE id=?",(a['message_id'],))
    value=await s.dispatch('alice','get',{'query_id':a['query_id']})
    assert value['status']=='failed'
    db.db.execute("UPDATE operations SET status='OUTCOME_UNKNOWN' WHERE id=?",(a['message_id'],))
    assert (await s.dispatch('alice','get',{'query_id':a['query_id']}))['status']=='needs_you'


@pytest.mark.parametrize('state,retry,status,phase',[
    ('QUEUED',None,'running','排队中'),
    ('QUEUED',{'kind':'prepare','next_at':0},'running','等待自动重试'),
    ('PREPARING',None,'running','发送中'),
    ('SUBMITTING',None,'running','发送中'),
    ('WAITING_FOR_RESPONSE',None,'running','思考中'),
    ('GENERATING',None,'running','生成中'),
    ('OUTCOME_UNKNOWN',{'kind':'recover','next_at':0},'running','核对中'),
    ('OUTCOME_UNKNOWN',{'kind':'recover','next_at':None},'needs_you',None),
    ('OUTCOME_UNKNOWN',{'kind':'artifact','next_at':1000},'needs_you',None),
    ('NEEDS_ATTENTION',{'kind':'artifact','next_at':1000},'needs_you',None),
    ('CANCELLED',None,'cancelled',None),
    ('FAILED',None,'failed',None),
])
async def test_execution_state_mapping_has_no_recovery_side_effects(rig,state,retry,status,phase):
    s,db,b=rig;a=await ask(s)
    db.db.execute('UPDATE operations SET status=? WHERE id=?',(state,a['message_id']))
    db.patch(a['message_id'],auto_retry=retry)
    args={'query_id':a['query_id'],'timeout_seconds':0}
    for tool in ('get','wait'):
        value=await s.dispatch('alice',tool,args)
        assert value['status']==status and value.get('phase')==phase
    assert db.operation(a['message_id'])['auto_retry']==retry and not b.sent


@pytest.mark.parametrize('code,safe,attempts,action',[
    ('EGO_UNAVAILABLE',True,3,'服务端已自动重试 3 次仍失败；修好浏览器/登录问题后可在 WebUI 点重试，或调用旧接口 retry_query'),
    ('ARTIFACT_CAPTURE_FAILED',False,3,'回答已生成，文件补收 3 次失败；可在 WebUI 重新收集文件'),
    ('LOGIN_REQUIRED',True,0,'需要在服务端完成登录或验证'),
    ('VERIFICATION_REQUIRED',False,0,'需要在服务端完成登录或验证'),
    ('UNKNOWN',False,0,'请把 query_id 交给维护者，服务端不会重发'),
    ('EGO_UNAVAILABLE',True,2,'请把 query_id 交给维护者，服务端不会重发'),
])
async def test_problem_actions_and_thread_raw_error(rig,code,safe,attempts,action):
    s,db,b=rig;a=await ask(s)
    error={'error_code':code,'message':'injected','safe_to_retry':safe}
    db.transition(a['message_id'],'NEEDS_ATTENTION',error=error)
    db.patch(a['message_id'],auto_retry=dict(kind='artifact' if code=='ARTIFACT_CAPTURE_FAILED' else 'prepare',
        attempts=attempts,max=3,next_at=None,history=[{'error':error}]))
    value=await s.dispatch('alice','get',{'query_id':a['query_id']})
    assert action in value['problem']['what_to_do']
    thread=await s.dispatch('alice','get_thread',{'query_id':a['query_id']})
    message=thread['messages'][0]
    assert message['last_error']==db.operation(a['message_id'])['error'] and message['last_error']['safe_to_retry']==safe
    assert message['raw_status']=='NEEDS_ATTENTION' and message['auto_retry']==dict(
        kind='artifact' if code=='ARTIFACT_CAPTURE_FAILED' else 'prepare',attempts=attempts,max=3,next_at=None)
    assert not b.sent


async def test_invalid_query_never_creates_and_explicit_message_reads_old_round(rig):
    s,db,b=rig
    value=await s.dispatch('alice','ask',{'text':'x','query_id':''})
    assert not value['ok'] and not db.candidates()
    first=await ask(s);await finish(s,db,first)
    await ask(s,query_id=first['query_id'],text='next')
    historical=await s.dispatch('alice','get',{'query_id':first['query_id'],'message_id':first['message_id']})
    assert historical['round']==1 and historical['status']=='done'
    # A completed round reports execution duration, rather than its age.
    db.db.execute('UPDATE operations SET created_at=created_at-600,submitted_at=submitted_at-600,updated_at=updated_at-600 WHERE id=?',(first['message_id'],))
    old=await s.dispatch('alice','get',{'query_id':first['query_id'],'message_id':first['message_id']})
    assert old['elapsed_seconds']<1


async def test_mcp_ask_attachments_and_rest_new_names(tmp_path):
    app=build_app(Settings(tmp_path,poll_seconds=.01,completion_confirm_seconds=0),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    args={'text':'read this','attachments':[{'filename':'notes.txt','content_base64':'aGVsbG8='}]}
                    a=(await session.call_tool('ask',args)).structuredContent
                    qid=a['query_id']
                    thread=(await http.post('https://localhost/api/get_thread',json={'query_id':qid})).json()
                    attachment=thread['messages'][0]['attachments'][0]
                    assert attachment['filename']=='notes.txt' and attachment['size_bytes']==5
                    assert 'content_base64' not in str(thread)
                    duplicate=(await http.post('https://localhost/api/ask',json=args)).json()
                    assert duplicate['message_id']==a['message_id'] and 'submission_trace' not in duplicate
                    status=(await http.post('https://localhost/api/get',json={'query_id':qid})).json()
                    assert status['status'] in {'running','done'}


@pytest.mark.parametrize('kind',['mermaid','svg'])
async def test_preview_only_answer_completes_compact_tools(rig,kind):
    from ego_bridge.agent_mcp import envelope
    s,db,b=rig
    value=await ask(s)
    await s.execute(db.operation(value['message_id']))
    b.pages['p1'][0].update(assistant_text='',regenerate=False,unsupported_outputs=[{'kind':kind,'export_status':'UNSUPPORTED'}])
    await s.observe(db.operation(value['message_id']))
    op=db.operation(value['message_id'])
    assert op['status']=='COMPLETED' and op['result']['delivery_complete']
    assert op['result']['artifacts']==[] and op['result']['outputs'][0]['type']=='preview'
    for tool,args in [('get',{}),('wait',{'timeout_seconds':0}),('ask',{'text':'你好，请回答。'})]:
        result=await s.dispatch('alice',tool,({} if tool=='ask' else {'query_id':value['query_id']})|args)
        assert result['status']=='done' and result['delivery_notes']
        assert '无法导出' in envelope(result).content[0].text
