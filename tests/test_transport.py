from importlib.metadata import version
import json
import logging

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ego_bridge.model import Settings
from ego_bridge.server import build_app
from test_reliability import FakeBrowser


async def test_auth_host_origin_and_body_boundaries(tmp_path):
    app=build_app(Settings(tmp_path),adapter=FakeBrowser(),clients={'alice':'a'*40,'bob':'b'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as client:
            r=await client.post('/api/list_queries',json={})
            assert r.status_code==401
            headers={'Authorization':'Bearer '+'a'*40}
            r=await client.post('/api/list_queries',json={},headers={**headers,'Origin':'https://evil.example'})
            assert r.status_code==403
            r=await client.post('/api/list_queries',json={},headers={**headers,'Host':'evil.example'})
            assert r.status_code==400
            r=await client.post('/api/list_queries',content='x'*1048577,headers=headers)
            assert r.status_code==413
            r=await client.post('/api/create_query',json={'text':'x','request_id':'r'},headers=headers)
            qid=r.json()['query_id']
            r=await client.post('/api/get_query_status',json={'query_id':qid},headers={'Authorization':'Bearer '+'b'*40})
            assert r.json()['error']['error_code']=='QUERY_NOT_FOUND'


async def test_mcp_accepts_attachment_body_above_sdk_default_limit(tmp_path):
    app=build_app(Settings(tmp_path),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool('create_query',{'text':'','request_id':'large-body-validation',
                        'attachments':[{'filename':'large.bin','content_base64':'eA=='*(1024*1024+1)}]})
                    assert result.structuredContent['error']['error_code']=='INVALID_ARGUMENT'


async def test_real_sdk_handshake_tools_and_owner_context(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ego_bridge.submission")
    app=build_app(Settings(tmp_path),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    initialization=await session.initialize()
                    assert 'result_is_partial' in initialization.instructions and '服務端' in initialization.instructions
                    assert initialization.serverInfo.version == version("ego-chatgpt-bridge")
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    catalog = {name: tool.description for name, tool in tools.items()}
                    assert {'create_query','continue_query','get_query_status','get_query_result','list_queries','health_check','diagnose','wait_query'}<=catalog.keys()
                    for name, parameter in [('create_query', 'soft_timeout'), ('continue_query', 'hard_timeout'), ('wait_query', 'timeout_seconds')]:
                        assert '秒' in tools[name].inputSchema['properties'][parameter]['description']
                    result=await session.call_tool('list_queries',{})
                    assert not result.isError
                    assert result.structuredContent['ok']
                    assert result.structuredContent['queries']==[]
                    submission = {'text': 'private research text', 'request_id': 'sdk-wait',
                                  'attachments': [{'filename': 'sdk.txt', 'content_base64': 'aGVsbG8='}]}
                    created=(await session.call_tool('create_query',submission)).structuredContent
                    assert created['attachments'][0]['filename'] == 'sdk.txt'
                    assert 'content_base64' not in str(created)
                    duplicate=(await session.call_tool('create_query',submission)).structuredContent
                    trace = created['submission_trace']
                    assert trace['http_received_at'] <= trace['dispatch_started_at'] <= trace['operation_created_at'] <= trace['response_ready_at']
                    assert duplicate['message_id'] == created['message_id']
                    assert duplicate['submission_trace']['operation_created_at'] == trace['operation_created_at']
                    assert duplicate['submission_trace']['trace_id'] != trace['trace_id']
                    matching = []
                    for record in caplog.records:
                        if record.name != 'ego_bridge.submission':
                            continue
                        event, body = record.getMessage().split(' ', 1)
                        assert submission['text'] not in body and submission['request_id'] not in body
                        entry = json.loads(body)
                        if entry['trace_id'] == trace['trace_id']:
                            matching.append((event, entry))
                    assert [event for event, _ in matching] == ['submission_received', 'submission_result']
                    assert matching[1][1]['query_id'] == created['query_id']
                    assert created['accepted'] and created['next_action']['tool']=='wait_query'
                    for phrase in ('10 分鐘以上','wait_query','請勿重送','complete=true'):
                        assert all(phrase in channel for channel in (
                            initialization.instructions, catalog['create_query'], created['waiting_guidance'],
                        ))
                    waited=await session.call_tool('wait_query',{'query_id':created['query_id'],'timeout_seconds':0})
                    assert not waited.isError and waited.structuredContent['changed']
                    assert 'result' not in waited.structuredContent
                    assert 'progress' in waited.structuredContent
                    next_action=waited.structuredContent.get('next_action')
                    if next_action:assert next_action['arguments']['cursor']==waited.structuredContent['cursor']
                    body=await session.call_tool('get_query_result',{'query_id':created['query_id'],'include_content':False})
                    assert not body.isError and 'result' not in body.structuredContent
                    denied=await session.call_tool('wait_query',{'query_id':'missing','timeout_seconds':0})
                    assert denied.isError and not denied.structuredContent['ok']


async def test_easytier_source_filter_does_not_trust_forwarded_headers(tmp_path):
    app=build_app(Settings(tmp_path,allowed_client_cidrs=('10.126.126.0/24',)),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        for source,allowed in [('10.126.126.22',True),('198.51.100.12',False),('127.0.0.1',False),('10.126.127.22',False)]:
            transport=httpx.ASGITransport(app=app,client=(source,1234))
            async with httpx.AsyncClient(transport=transport,base_url='https://localhost') as client:
                result=await client.post('/api/list_queries',json={},headers={'Authorization':'Bearer '+'a'*40,'X-Forwarded-For':'10.126.126.22'})
                assert result.status_code==(200 if allowed else 403)
                if not allowed:assert result.json()['error']['error_code']=='SOURCE_NETWORK_DENIED'
