"""Real HTTPS OAuth/MCP/file roundtrip, then revoke the temporary grant. No secrets printed."""
import argparse,asyncio,base64,hashlib,json,re,secrets,time
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ego_bridge.model import Settings
from ego_bridge.auth import load_clients

async def main(runtime):
    settings=Settings.load(runtime);issuer=settings.oauth_issuer.rstrip('/');resource=issuer+'/mcp'
    if not issuer:raise RuntimeError('OAuth is not configured')
    checks={};tokens=None;client=None
    async with httpx.AsyncClient(timeout=30,trust_env=False) as public,httpx.AsyncClient(timeout=30,trust_env=False) as private:
        m=(await public.get(issuer+'/.well-known/oauth-protected-resource/mcp'));m.raise_for_status();assert m.json()['resource']==resource;checks['trusted_https_discovery']=True;print('Verified HTTPS discovery',flush=True)
        for path in ('/ui/','/ui/api/exports','/api/list_queries'):
            assert (await public.get(issuer+path)).status_code==404
        checks['ui_api_export_denied']=True
        owner_token=load_clients(runtime)[settings.default_owner]
        assert (await public.post(resource,json={},headers={'Authorization':'Bearer '+owner_token})).status_code==401
        checks['public_management_token_denied']=True
        response=await public.post(issuer+'/register',json={'client_name':'OAuth network acceptance','redirect_uris':['http://127.0.0.1:49999/callback'],'token_endpoint_auth_method':'none','scope':'research:read'})
        response.raise_for_status();client=response.json()
        verifier=secrets.token_urlsafe(64);challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        response=await public.get(issuer+'/authorize',params={'client_id':client['client_id'],'response_type':'code','redirect_uri':client['redirect_uris'][0],'state':'acceptance-'+secrets.token_hex(8),'code_challenge':challenge,'code_challenge_method':'S256','resource':resource,'scope':'research:read'})
        assert response.status_code==302
        location=response.headers['location'];assert location.startswith(settings.base_url+'/ui/oauth/consent?')
        page=await private.get(location);page.raise_for_status();csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        response=await private.post(location,data={'csrf':csrf,'token':owner_token,'decision':'approve'},headers={'Origin':settings.base_url})
        assert response.status_code==303
        code=parse_qs(urlsplit(response.headers['location']).query)['code'][0]
        response=await public.post(issuer+'/token',data={'client_id':client['client_id'],'grant_type':'authorization_code','code':code,'code_verifier':verifier,'redirect_uri':client['redirect_uris'][0],'resource':resource})
        response.raise_for_status();tokens=response.json();checks['dcr_pkce_private_consent']=True;print('Verified DCR, PKCE and private consent',flush=True)
        try:
            async with httpx.AsyncClient(timeout=60,trust_env=False,headers={'Authorization':'Bearer '+tokens['access_token']}) as http:
                async with streamable_http_client(resource,http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize();tools=await session.list_tools();assert {'create_query','wait_query','get_query_result','read_artifact'}<={t.name for t in tools.tools};checks['mcp_tool_count']=len(tools.tools)
                        result=await session.call_tool('list_queries',{'limit':100});assert not result.isError;queries=result.structuredContent['queries'];checks['owner_queries_returned']=len(queries)
                        if queries:
                            waited=await session.call_tool('wait_query',{'query_id':queries[0]['id'],'timeout_seconds':0})
                            assert not waited.isError and 'cursor' in waited.structuredContent and 'result' not in waited.structuredContent
                            checks['public_oauth_compact_wait']=True
                        rejected=await http.post(resource,json={'jsonrpc':'2.0','id':77,'method':'tools/call','params':{'name':'create_query','arguments':{'text':'never sent','request_id':'scope-probe'}}})
                        assert rejected.status_code==403;checks['readonly_write_denied']=True
                        gate=asyncio.Semaphore(4)
                        async def query_artifacts(query):
                            async with gate:
                                result=await session.call_tool('list_artifacts',{'query_id':query['id']})
                                found=result.structuredContent.get('artifacts',[])
                                if not found:
                                    history=await session.call_tool('list_messages',{'query_id':query['id']})
                                    for message in history.structuredContent.get('messages',[])[:-1]:
                                        previous=await session.call_tool('list_artifacts',{'query_id':query['id'],'message_id':message['message_id']})
                                        found.extend(previous.structuredContent.get('artifacts',[]))
                                return [a for a in found if a['status']=='AVAILABLE']
                        artifacts=[a for group in await asyncio.gather(*(query_artifacts(query) for query in queries)) for a in group]
                        files=[];image=False
                        for art in artifacts:
                            aid=art['artifact_id'];uri=art['mcp_resource_uri'];result=await session.read_resource(uri)
                            raw=b''.join(base64.b64decode(c.blob) if hasattr(c,'blob') else c.text.encode() for c in result.contents)
                            assert len(raw)==art['size_bytes'] and hashlib.sha256(raw).hexdigest()==art['sha256']
                            files.append({'mime_type':art['mime_type'],'size_bytes':len(raw),'sha256_verified':True})
                            if art['mime_type'].startswith('image/'):
                                preview=await session.call_tool('read_artifact',{'artifact_id':aid});image |= any(c.type=='image' for c in preview.content)
                        print('Verified artifact bytes:',len(files),'native image:',image,flush=True)
                        assert files and image
                        checks.update(files=files,native_image_content=True)
            refreshed=await public.post(issuer+'/token',data={'client_id':client['client_id'],'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],'resource':resource})
            refreshed.raise_for_status();assert refreshed.json()['refresh_token']!=tokens['refresh_token'];tokens=refreshed.json();checks['refresh_rotation']=True;print('Verified refresh rotation',flush=True)
        finally:
            if tokens:
                response=await public.post(issuer+'/revoke',data={'client_id':client['client_id'],'token':tokens['refresh_token'],'token_type_hint':'refresh_token'})
                assert response.status_code==200
                assert (await public.post(resource,json={},headers={'Authorization':'Bearer '+tokens['access_token']})).status_code==401
                checks['revocation_enforced']=True
        checks['completed_at']=time.time()
        directory=runtime/'verification';directory.mkdir(mode=0o700,exist_ok=True)
        path=directory/'oauth-network-acceptance.json';path.write_text(json.dumps(checks,indent=2));path.chmod(0o600)
        print(json.dumps(checks,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,default=Path.home()/'apps/ego-chatgpt-bridge');args=p.parse_args()
    asyncio.run(main(args.runtime.expanduser().resolve()))
