from contextlib import asynccontextmanager
import base64
import hashlib
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ego_bridge.model import Settings
from ego_bridge.server import build_app
from ego_bridge.gateway import PublicGateway
from test_reliability import FakeBrowser

TOKEN='a'*40
ISSUER='https://mcp.example/'
RESOURCE=ISSUER+'mcp'

@pytest.fixture
def oauth(tmp_path):
    @asynccontextmanager
    async def setup():
        settings=Settings(tmp_path,oauth_issuer=ISSUER,public_gateway_port=14103,allowed_hosts=('localhost','mcp.example'))
        app=build_app(settings,adapter=FakeBrowser(),clients={'alice':TOKEN,'bob':'b'*40})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',headers={'Host':'localhost'}) as private:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PublicGateway(app)),base_url=ISSUER) as public:
                    yield app,private,public
    return setup()

async def grant(private, public, scopes='research:read research:write', token=TOKEN):
    r=await public.post('/register',json={'client_name':'Test client','redirect_uris':['http://127.0.0.1:9999/callback'],'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':scopes})
    assert r.status_code==201,r.text
    client=r.json()
    verifier='x'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    r=await public.get('/authorize',params={'client_id':client['client_id'],'redirect_uri':'http://127.0.0.1:9999/callback','response_type':'code','code_challenge':challenge,'code_challenge_method':'S256','scope':scopes,'state':'state-123','resource':RESOURCE})
    assert r.status_code==302,r.text
    location=r.headers['location']
    page=await private.get(location)
    assert page.status_code==200,page.text
    assert page.headers['referrer-policy']=='same-origin'
    assert "form-action 'self' http://127.0.0.1:9999" in page.headers['content-security-policy']
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
    r=await private.post(location,data={'csrf':csrf,'token':token,'decision':'approve'},headers={'Origin':'http://127.0.0.1:8766'})
    assert r.status_code==303,r.text
    query=parse_qs(urlsplit(r.headers['location']).query)
    assert query['state']==['state-123']
    code=query['code'][0]
    form={'grant_type':'authorization_code','client_id':client['client_id'],'code':code,'code_verifier':verifier,'redirect_uri':'http://127.0.0.1:9999/callback','resource':RESOURCE}
    return client,form

async def test_public_surface_and_legacy_credentials(oauth):
    async with oauth as (app,private,public):
        for path in ['/ui/','/ui/api/exports','/api/list_queries','/artifacts/x/content','/%75i/','/mcp/../ui/']:
            assert (await public.get(path,headers={'Authorization':'Bearer '+TOKEN})).status_code==404
        r=await public.post('/mcp',json={},headers={'Authorization':'Bearer '+TOKEN})
        assert r.status_code==401 and RESOURCE.split('/mcp')[0] in r.headers['www-authenticate']
        assert (await private.post('/api/list_queries',json={},headers={'Authorization':'Bearer '+TOKEN})).status_code==200
        m=(await public.get('/.well-known/oauth-protected-resource/mcp')).json()
        assert m['resource']==RESOURCE
        assert m['authorization_servers']==[ISSUER]
        assert (await public.post('/register',json={'redirect_uris':['https://evil.example/callback']})).status_code==400

async def test_pkce_code_replay_refresh_rotation_revocation_and_restart(oauth,tmp_path):
    async with oauth as (app,private,public):
        client,form=await grant(private,public)
        r=await public.post('/token',data={**form,'code_verifier':'wrong'})
        assert r.status_code==400
        assert (await public.post('/token',data={**form,'resource':'https://evil.example/mcp'})).status_code==400
        r=await public.post('/token',data=form)
        assert r.status_code==200,r.text
        tokens=r.json()
        assert tokens['expires_in']==3600
        assert (await public.post('/token',data=form)).status_code==400
        # MCP runs through the restricted surface using the delegated owner.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PublicGateway(app)),headers={'Authorization':'Bearer '+tokens['access_token']}) as http:
            async with streamable_http_client(RESOURCE,http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    assert tools.tools[0].meta['securitySchemes']
                    result=await session.call_tool('list',{})
                    assert result.structuredContent['queries']==[]
        db=app.state.oauth.db
        assert tokens['access_token'] not in '\n'.join(row[0] for row in db.execute('select value from oauth_objects'))
        refresh={'grant_type':'refresh_token','client_id':client['client_id'],'refresh_token':tokens['refresh_token'],'resource':RESOURCE}
        r=await public.post('/token',data=refresh);assert r.status_code==200,r.text
        rotated=r.json()
        assert rotated['refresh_token']!=tokens['refresh_token']
        assert await app.state.oauth.load_access_token(rotated['access_token'])
        # A replay of an already rotated refresh token invalidates the whole family.
        assert (await public.post('/token',data=refresh)).status_code==400
        assert await app.state.oauth.load_access_token(rotated['access_token']) is None
        _,form=await grant(private,public)
        fresh=(await public.post('/token',data=form)).json()
        r=await public.post('/revoke',data={'client_id':form['client_id'],'token':fresh['refresh_token'],'token_type_hint':'refresh_token'})
        assert r.status_code==200,r.text
        assert await app.state.oauth.load_access_token(fresh['access_token']) is None

async def test_consent_csrf_scope_owner_and_revoke(oauth):
    async with oauth as (app,private,public):
        client,form=await grant(private,public,scopes='research:read',token='b'*40)
        tokens=(await public.post('/token',data=form)).json()
        bearer={'Authorization':'Bearer '+tokens['access_token']}
        r=await private.post('/api/create_query',json={'text':'x','request_id':'x'},headers=bearer)
        assert r.status_code==403
        r=await private.post('/api/list_queries',json={},headers=bearer)
        assert r.status_code==200 and r.json()['queries']==[]
        assert (await private.get('/ui/api/me',headers=bearer)).json()['owner']=='bob' # consent establishes bob cookie
        info=(await private.post('/ui/api/mcp-info',json={})).json()
        assert info['connections']['easytier']['configuration']['mcpServers']['ego-research']['headers']['Authorization']=='Bearer '+'b'*40
        public_config=info['oauth_configuration']
        assert public_config['mcpServers']['ego-research-public']=={'url':RESOURCE}
        assert TOKEN not in json.dumps(public_config) and 'b'*40 not in json.dumps(public_config)
        grants=await private.get('/ui/api/oauth/grants')
        gid=grants.json()['grants'][0]['id']
        assert (await private.post('/ui/api/oauth/grants',json={'id':gid})).status_code==403
        assert (await private.post('/ui/api/oauth/grants',json={'id':gid},headers={'Origin':'http://127.0.0.1:8766'})).status_code==200
        assert await app.state.oauth.load_access_token(tokens['access_token']) is None

async def test_wrong_resource_redirect_and_csrf_cannot_create_grants(oauth):
    async with oauth as (app,private,public):
        c=(await public.post('/register',json={'redirect_uris':['http://127.0.0.1:9999/callback'],'token_endpoint_auth_method':'none'})).json()
        base={'client_id':c['client_id'],'response_type':'code','redirect_uri':'http://127.0.0.1:9999/callback','code_challenge':'x'*43,'code_challenge_method':'S256','resource':RESOURCE}
        assert (await public.get('/authorize',params={**base,'redirect_uri':'https://evil.example'})).status_code==400
        r=await public.get('/authorize',params={**base,'resource':'https://evil.example/mcp'})
        assert 'invalid_request' in r.headers.get('location',r.text)
        r=await public.get('/authorize',params=base);location=r.headers['location']
        await private.get(location)
        assert (await private.post(location,data={'csrf':'wrong','token':TOKEN,'decision':'approve'},headers={'Origin':'http://127.0.0.1:8766'})).status_code==403
        assert app.state.oauth.grants('alice')==[]
        assert (await public.post('/register',json={'redirect_uris':['https://chatgpt.com/connector/oauth/example'],'client_name':'ChatGPT'})).status_code==201

async def test_confidential_client_persistence_expiry_and_token_rotation(oauth):
    from ego_bridge.oauth import OAuthProvider, hashed
    async with oauth as (app,private,public):
        _,form=await grant(private,public)
        tokens=(await public.post('/token',data=form)).json()
        old=app.state.oauth
        reloaded=OAuthProvider(old.settings,old.store,old.clients)
        assert (await reloaded.load_access_token(tokens['access_token'])).subject=='alice'
        changed=OAuthProvider(old.settings,old.store,{'alice':'rotated-token','bob':'b'*40})
        assert await changed.load_access_token(tokens['access_token']) is None
        row=old.get('access',hashed(tokens['access_token']));row['expires_at']=0;old.put('access',hashed(tokens['access_token']),row)
        assert await old.load_access_token(tokens['access_token']) is None
        c=(await public.post('/register',json={'redirect_uris':['https://chatgpt.com/connector/oauth/callback'],'token_endpoint_auth_method':'client_secret_post'})).json()
        assert c.get('client_secret') and c.get('client_secret_expires_at') in (None,0)
        # A delegated credential cannot log into the WebUI as the long-term owner token.
        assert (await private.post('/ui/api/login',json={'token':tokens['access_token']})).status_code==401

async def test_public_registration_is_bounded_but_discovery_stays_available(oauth):
    async with oauth as (app,private,public):
        huge=await public.post('/register',content='x'*16385,headers={'Content-Type':'application/json'})
        assert huge.status_code==413
        for _ in range(10):
            response=await public.post('/register',json={'redirect_uris':['http://127.0.0.1:9999/callback'],'token_endpoint_auth_method':'none'})
        assert response.status_code==429 and response.headers['retry-after']=='60'
        assert (await public.get('/.well-known/oauth-protected-resource/mcp')).status_code==200

async def test_consent_rejects_owner_changed_in_another_tab(oauth):
    async with oauth as (app,private,public):
        client,_=await grant(private,public)
        response=await public.get('/authorize',params={'client_id':client['client_id'],'response_type':'code','redirect_uri':'http://127.0.0.1:9999/callback','code_challenge':'x'*43,'code_challenge_method':'S256','resource':RESOURCE})
        location=response.headers['location'];page=await private.get(location)
        assert 'alice' in page.text
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        assert (await private.post('/ui/api/login',json={'token':'b'*40})).status_code==200
        response=await private.post(location,data={'csrf':csrf,'decision':'approve'},headers={'Origin':'http://127.0.0.1:8766'})
        assert response.status_code==409
        assert app.state.oauth.grants('bob')==[]
