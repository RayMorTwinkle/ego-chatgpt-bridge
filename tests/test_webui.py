import asyncio
import base64
import json
import time
import zipfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ego_bridge.auth import BrowserSessions,COOKIE_NAME,SESSION_SECONDS
from ego_bridge.model import Settings
from ego_bridge.server import build_app
from test_artifacts import FileBrowser,PNG


class ExportBrowser(FileBrowser):
    def __init__(self):super().__init__();self.debug_actions=[]
    async def call(self,action,**kw):
        if action.startswith('debug_'):
            self.debug_actions.append(action)
            if action=='debug_observe':return {'page_label':'debug','state':{'turns':[{'state':'complete','user_id':'u','user_text':'Question','assistant_id':'a','assistant_text':'Reply','sources':[], 'artifacts':[{'kind':'file_card','index':0,'name':'real.png'}]}]}}
            if action=='debug_capture':
                from pathlib import Path
                Path(kw['destination']).write_bytes(PNG);return {'saved':True}
            if action=='debug_close':return {'closed':True}
        return await super().call(action,**kw)


def test_cookie_is_signed_expires_and_token_rotation_revokes_it():
    s=BrowserSessions({'alice':'x'*40},'session-secret','alice');cookie=s.issue('alice')
    assert s.owner(cookie)=='alice' and s.owner(cookie+'tampered') is None
    data=json.loads(base64.urlsafe_b64decode(cookie.split('.')[0]+'='*(-len(cookie.split('.')[0])%4)))
    assert SESSION_SECONDS-2<=data['expires']-time.time()<=SESSION_SECONDS
    assert 'x'*40 not in cookie
    assert BrowserSessions({'alice':'new-token'},'session-secret','alice').owner(cookie) is None


async def test_web_login_cookie_mcp_info_csrf_and_logout(tmp_path):
    app=build_app(Settings(tmp_path,default_owner='alice',tls_enabled=True),adapter=ExportBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
            assert (await c.get('/ui/')).status_code==200
            assert (await c.get('/ui/api/me')).status_code==401
            bad=await c.post('/ui/api/login',json={'token':'wrong'});assert bad.status_code==401
            r=await c.post('/ui/api/login',json={'token':'a'*40},headers={'Origin':'https://localhost'})
            cookie=r.headers['set-cookie'];assert 'Max-Age=31536000' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
            assert (await c.get('/ui/api/me')).json()['owner']=='alice'
            assert (await c.post('/ui/api/mcp-info',json={},headers={'Origin':'https://evil.example'})).status_code==403
            info=(await c.post('/ui/api/mcp-info',json={})).json();assert info['authorization']=='Bearer '+'a'*40
            assert info['configuration']['mcpServers']['ego-research']['headers']['Authorization']==info['authorization']
            await c.post('/ui/api/logout',json={});assert (await c.get('/ui/api/me')).status_code==401


async def test_debug_export_is_cookie_only_not_mcp_and_can_read_nonproject_chat(tmp_path):
    browser=ExportBrowser();app=build_app(Settings(tmp_path,default_owner='alice',poll_seconds=.01),adapter=browser,clients={'alice':'a'*40,'bob':'b'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
            payload={'conversation_url':'https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee','request_id':'export-1'}
            assert (await c.post('/ui/api/exports',json=payload,headers={'Authorization':'Bearer '+'a'*40})).status_code==401
            await c.post('/ui/api/login',json={'token':'b'*40});assert (await c.post('/ui/api/exports',json=payload)).status_code==403
            await c.post('/ui/api/login',json={'token':'a'*40})
            r=(await c.post('/ui/api/exports',json=payload)).json();job=r['export']['id']
            same=(await c.post('/ui/api/exports',json=payload)).json();assert same['export']['id']==job
            for _ in range(100):
                row=(await c.get('/ui/api/exports/'+job)).json()['export']
                if row['status'] not in {'QUEUED','RUNNING'}:break
                await asyncio.sleep(.01)
            assert row['status']=='COMPLETED',row
            archive=await c.get(row['download_url']);assert archive.headers['content-type']=='application/zip'
            import io
            with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
                manifest=json.loads(z.read('manifest.json'));assert manifest['conversation_url']==payload['conversation_url']
                assert manifest['read_only'] and z.read(manifest['artifacts'][0]['archive_path'])==PNG
                assert 'Question' in z.read('transcript.md').decode()
            assert app.state.service.store.list_queries('alice')==[] and not browser.sent
            assert set(browser.debug_actions)=={'debug_observe','debug_capture','debug_close'}
            assert (await c.post('/ui/api/exports',json={'conversation_url':'http://127.0.0.1/secrets','request_id':'bad'})).status_code==400
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (r,w,_):
                async with ClientSession(r,w) as m:
                    await m.initialize();names={t.name for t in (await m.list_tools()).tools}
                    assert not any('export' in n or 'debug' in n for n in names)


async def test_web_login_page_is_also_easytier_restricted(tmp_path):
    app=build_app(Settings(tmp_path,allowed_client_cidrs=('10.126.126.0/24',)),adapter=ExportBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('10.241.1.2',1234)),base_url='https://localhost') as c:
            assert (await c.get('/ui/')).status_code==403
            assert (await c.post('/ui/api/login',json={'token':'a'*40})).status_code==403


async def test_export_does_not_claim_complete_for_unhydrated_historical_reply(tmp_path):
    browser=ExportBrowser();original=browser.call
    async def read(action,**kw):
        result=await original(action,**kw)
        if action=='debug_observe':result['state']['turns'][0]['render_ready']=False
        return result
    browser.call=read;app=build_app(Settings(tmp_path,default_owner='alice'),adapter=browser,clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as c:
            await c.post('/ui/api/login',json={'token':'a'*40})
            job=(await c.post('/ui/api/exports',json={'conversation_url':'https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee','request_id':'render'})).json()['export']['id']
            for _ in range(100):
                item=(await c.get('/ui/api/exports/'+job)).json()['export']
                if item['status'] not in {'QUEUED','RUNNING'}:break
                await asyncio.sleep(.01)
            assert item['status']=='PARTIAL' and not item['manifest']['complete_for_loaded_content']
            assert any('渲染' in text for text in item['manifest']['warnings'])


async def test_http_needs_no_ca_and_uses_distinct_one_year_cookie(tmp_path):
    from ego_bridge.auth import HTTP_COOKIE_NAME
    (tmp_path/'.env').write_text("WEB_SESSION_SECRET='test-secret'\n")
    app=build_app(Settings(tmp_path,default_owner='alice'),adapter=ExportBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
            c.cookies.set(COOKIE_NAME,BrowserSessions({'alice':'a'*40},'test-secret','alice').issue('alice'))
            assert (await c.get('/ui/api/me')).status_code==401
            login=await c.post('/ui/api/login',json={'token':'a'*40});cookie=login.headers['set-cookie']
            assert HTTP_COOKIE_NAME in cookie and 'HttpOnly' in cookie and 'Max-Age=31536000' in cookie
            assert '; Secure' not in cookie
            assert (await c.get('/ui/api/me')).json()['owner']=='alice'
            info=(await c.post('/ui/api/mcp-info',json={})).json()
            assert info['mcp_url'].startswith('http://') and not info['tls_enabled']
            assert info['certificate_path'] is None and info['certificate_download_url'] is None
            assert (await c.get('/ui/api/certificate')).status_code==404
