from importlib.metadata import version

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


async def test_real_sdk_handshake_tools_and_owner_context(tmp_path):
    app=build_app(Settings(tmp_path),adapter=FakeBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    initialization=await session.initialize()
                    assert 'result_is_partial' in initialization.instructions and '服務端' in initialization.instructions
                    assert initialization.serverInfo.version == version("ego-chatgpt-bridge")
                    catalog={t.name:t.description for t in (await session.list_tools()).tools}
                    assert {'create_query','continue_query','get_query_status','get_query_result','list_queries','health_check','diagnose','wait_query'}<=catalog.keys()
                    result=await session.call_tool('list_queries',{})
                    assert not result.isError
                    assert result.structuredContent['ok']
                    assert result.structuredContent['queries']==[]
                    created=(await session.call_tool('create_query',{'text':'test','request_id':'sdk-wait'})).structuredContent
                    assert created['accepted'] and created['next_action']['tool']=='wait_query'
                    for phrase in ('10 分鐘以上','wait_query','請勿重送','complete=true'):
                        assert all(phrase in channel for channel in (
                            initialization.instructions, catalog['create_query'], created['waiting_guidance'],
                        ))
                    waited=await session.call_tool('wait_query',{'query_id':created['query_id'],'timeout_seconds':0})
                    assert not waited.isError and waited.structuredContent['changed']
                    assert 'result' not in waited.structuredContent
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
