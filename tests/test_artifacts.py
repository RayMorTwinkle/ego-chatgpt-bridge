import base64
import hashlib
import zipfile
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ego_bridge.artifacts import Artifacts,filename,inspect_file
from ego_bridge.model import Settings,BridgeError
from ego_bridge.server import build_app
from ego_bridge.store import Store
from ego_bridge.service import Service
from ego_bridge.backup import backup_with_artifacts
from test_reliability import FakeBrowser

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII=')


class FileBrowser(FakeBrowser):
    def __init__(self,data=PNG):super().__init__();self.data=data;self.downloads=0
    async def call(self,action,**kw):
        if action=='capture_artifact':
            self.downloads+=1
            Path(kw['destination']).write_bytes(self.data)
            return {'saved':True}
        return await super().call(action,**kw)


async def seed(artifacts,store,browser,name='test.png'):
    op=store.enqueue('alice','r',dict(text='x',effort='extra_high',soft_timeout=1,hard_timeout=2),'p',3)
    rows=await artifacts.collect(op,[{'kind':'file_card','index':0,'name':name}],browser,{},'user')
    return op,rows[0]


async def test_artifact_verification_idempotency_and_private_manifest(tmp_path):
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path),store);b=FileBrowser()
    op,row=await seed(a,store,b)
    assert row['status']=='AVAILABLE' and row['mime_type']=='image/png'
    assert row['size_bytes']==len(PNG) and row['sha256']==hashlib.sha256(PNG).hexdigest()
    assert 'local_path' not in row and 'source_url' not in row and row['requires_authentication']
    result=await a.collect(op,[{'kind':'file_card','index':0,'name':'test.png'}],b,{},'user')
    assert b.downloads==1 and result[0]['artifact_id']==row['artifact_id']
    with pytest.raises(BridgeError):a.get('bob',row['artifact_id'])
    store.close()


@pytest.mark.parametrize('content,name,max_size',[(b'<html>login</html>','x.png',100),(b'','x.csv',100),(b'x'*101,'x.csv',100)])
async def test_invalid_or_partial_file_never_becomes_available(tmp_path,content,name,max_size):
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path,artifact_max_bytes=max_size),store)
    op,row=await seed(a,store,FileBrowser(content),name)
    assert row['status']=='FAILED' and row['download_url'] is None
    store.close()


async def test_corruption_and_symlink_are_not_served(tmp_path):
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path),store)
    op,row=await seed(a,store,FileBrowser())
    target=a.path(row['artifact_id']);target.write_bytes(PNG+b'changed')
    with pytest.raises(BridgeError,match='校验值'):a.available_path('alice',row['artifact_id'])
    target.unlink();(tmp_path/'outside').write_bytes(PNG);target.symlink_to(tmp_path/'outside')
    with pytest.raises(BridgeError):a.available_path('alice',row['artifact_id'])
    store.close()


async def test_rename_before_metadata_commit_reconciles_without_download(tmp_path):
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path),store);b=FileBrowser()
    op,row=await seed(a,store,b)
    store.db.execute("UPDATE artifacts SET status='DOWNLOADING',sha256=NULL WHERE id=?",(row['artifact_id'],))
    rows=await a.collect(op,[{'kind':'file_card','index':0,'name':'test.png'}],b,{},'user')
    assert rows[0]['status']=='AVAILABLE' and b.downloads==1
    store.close()


async def test_authenticated_http_bytes_and_native_mcp_image(tmp_path):
    app=build_app(Settings(tmp_path),adapter=FileBrowser(),clients={'alice':'a'*40,'bob':'b'*40})
    async with app.router.lifespan_context(app):
        s=app.state.service
        op,row=await seed(s.artifacts,s.store,FileBrowser())
        path='/artifacts/'+row['artifact_id']+'/content'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://localhost') as client:
            assert (await client.get(path)).status_code==401
            denied=await client.get(path,headers={'Authorization':'Bearer '+'b'*40});assert denied.status_code==404
            headers={'Authorization':'Bearer '+'a'*40}
            response=await client.get(path,headers=headers)
            assert response.content==PNG and response.headers['content-type']=='image/png'
            assert response.headers['x-content-type-options']=='nosniff' and 'attachment' in response.headers['content-disposition']
            partial=await client.get(path,headers={**headers,'Range':'bytes=0-7'})
            assert partial.status_code==206 and partial.content==PNG[:8]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'a'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (rd,wr,_):
                async with ClientSession(rd,wr) as m:
                    await m.initialize();r=await m.call_tool('read_artifact',{'artifact_id':row['artifact_id']})
                    assert not r.isError
                    image=next(c for c in r.content if c.type=='image')
                    assert base64.b64decode(image.data)==PNG
                    linked=await m.call_tool('read_artifact',{'artifact_id':row['artifact_id'],'inline_image':False})
                    link=next(c for c in linked.content if c.type=='resource_link')
                    resource=await m.read_resource(link.uri)
                    assert base64.b64decode(resource.contents[0].blob)==PNG
        # A prior successful read must not cache one owner's bytes for a different owner.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+'b'*40}) as http:
            async with streamable_http_client('https://localhost/mcp',http_client=http) as (rd,wr,_):
                async with ClientSession(rd,wr) as m:
                    await m.initialize()
                    from mcp.shared.exceptions import McpError
                    with pytest.raises(McpError):await m.read_resource(link.uri)


def test_filename_never_supplies_a_storage_path():
    assert filename('../../secret\r\n.png')=='secret.png'
    assert filename('C:\\temp\\file.csv')=='file.csv'


async def test_image_only_answer_and_download_retry_never_resend_prompt(tmp_path):
    settings=Settings(tmp_path,completion_confirm_seconds=0);store=Store(tmp_path);browser=FileBrowser(data=b'');service=Service(settings,store,browser)
    task=await service.dispatch('alice','create_query',{'text':'Create image','request_id':'image'})
    op=store.operation(task['message_id']);await service.execute(op)
    browser.pages['p1'][0].update(assistant_text='',regenerate=False,artifacts=[{'kind':'file_card','index':0,'name':'test.png'}])
    await service.observe(store.operation(op['id']))
    r=await service.dispatch('alice','get_query_result',{'query_id':task['query_id']})
    assert r['generation_complete'] and not r['delivery_complete'] and not r['complete']
    assert r['status']=='GENERATING' and r['auto_retry']['attempts']==1
    assert r['result']['artifacts'][0]['error']['error_code']=='ARTIFACT_EMPTY'
    browser.data=PNG
    await service.dispatch('alice','recover_session',{'query_id':task['query_id']})
    r=await service.dispatch('alice','get_query_result',{'query_id':task['query_id']})
    assert r['complete'] and r['delivery_complete'] and r['result']['content']==''
    assert r['result']['outputs'][0]['type']=='artifact' and len(browser.sent)==1
    store.close()


async def test_unexported_media_is_reported_without_blocking_delivery(tmp_path):
    settings=Settings(tmp_path,completion_confirm_seconds=0);store=Store(tmp_path);browser=FileBrowser();s=Service(settings,store,browser)
    task=await s.dispatch('alice','create_query',{'text':'Create video','request_id':'video'})
    await s.execute(store.operation(task['message_id']))
    browser.pages['p1'][0].update(assistant_text='',regenerate=False,unsupported_outputs=[{'kind':'video','export_status':'UNSUPPORTED'}])
    await s.observe(store.operation(task['message_id']))
    result=await s.dispatch('alice','get_query_result',{'query_id':task['query_id']})
    assert result['generation_complete'] and result['delivery_complete'] and result['complete']
    assert result['status']=='COMPLETED' and result['result']['delivery_notes']
    assert result['result']['outputs'][0]['type']=='preview'
    store.close()


async def test_backup_contains_verified_blobs_and_aborts_on_corruption(tmp_path):
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path),store);op,row=await seed(a,store,FileBrowser())
    target=tmp_path/'backup.zip';backup_with_artifacts(tmp_path,target)
    with zipfile.ZipFile(target) as archive:
        assert archive.read('artifacts/'+row['artifact_id']+'.blob')==PNG
        assert 'bridge.sqlite3' in archive.namelist()
        assert '.env' not in archive.namelist()
    a.path(row['artifact_id']).write_bytes(PNG+b'changed')
    with pytest.raises(BridgeError):backup_with_artifacts(tmp_path,tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists()
    store.close()

@pytest.mark.parametrize('reference_present',[True,False])
async def test_external_web_images_do_not_block_real_file_delivery_or_get_fake_bytes(tmp_path,reference_present):
    import json
    settings=Settings(tmp_path,completion_confirm_seconds=0);store=Store(tmp_path);browser=FileBrowser();service=Service(settings,store,browser)
    op,row=await seed(service.artifacts,store,browser)
    reference={'index':0,'title':'Official diagram','url':'https://public.example.org/diagram.png','delivery':'reference_only'}
    store.db.execute("UPDATE artifacts SET status='FAILED',error=?,candidate=? WHERE id=?",(json.dumps({'error_code':'EXTERNAL_IMAGE_NOT_COPIED'}),json.dumps({'kind':'inline_image','index':0,'name':'image-1.png'}),row['artifact_id']))
    service.artifacts.path(row['artifact_id']).unlink()
    references=[reference] if reference_present else []
    turn={'user_id':'user','artifacts':[{'kind':'file_card','index':0,'name':'real.png'}],'external_images':references,'unsupported_outputs':[]}
    result=await service.package_result(op,turn,{'content':'Research text','sources':[],'artifacts':[]})
    assert result['delivery_complete'] and len(result['artifacts'])==1
    assert result['artifacts'][0]['filename']=='real.png' and result['artifacts'][0]['status']=='AVAILABLE'
    old=service.artifacts.public(service.artifacts.get('alice',row['artifact_id']))
    assert old['status']=='REFERENCE_ONLY' and old['download_url'] is None
    assert old['reference_url'] is None  # Never guess a legacy URL from a transient DOM index.
    assert result['external_images']==references
    assert any(o['type']=='link' and o.get('delivery')=='reference_only' for o in result['outputs'])==reference_present
    assert not browser.sent
    store.close()


async def test_automatic_artifact_retries_back_off_and_stop_after_three(tmp_path,monkeypatch):
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    settings=Settings(tmp_path,completion_confirm_seconds=0);store=Store(tmp_path)
    browser=FileBrowser(data=b'');service=Service(settings,store,browser)
    task=await service.dispatch('alice','create_query',{'text':'Create file','request_id':'file'})
    mid=task['message_id'];await service.execute(store.operation(mid))
    browser.pages['p1'][0]['artifacts']=[{'kind':'file_card','index':0,'name':'test.png'}]
    for attempt,delay in enumerate((15,60,180),1):
        await service.observe(store.operation(mid))
        op=store.operation(mid)
        assert op['status']=='GENERATING' and op['auto_retry']['attempts']==attempt
        assert op['auto_retry']['next_at']==clock[0]+delay
        await service.observe(op)
        assert store.operation(mid)['auto_retry']['attempts']==attempt
        clock[0]+=delay
    await service.observe(store.operation(mid))
    assert store.operation(mid)['status']=='NEEDS_ATTENTION'
    assert len(store.operation(mid)['auto_retry']['history'])==4 and len(browser.sent)==1
    store.close()


async def test_automatic_artifact_retry_can_finish_without_operator(tmp_path,monkeypatch):
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    settings=Settings(tmp_path,completion_confirm_seconds=0);store=Store(tmp_path)
    browser=FileBrowser(data=b'');service=Service(settings,store,browser)
    task=await service.dispatch('alice','create_query',{'text':'Create file','request_id':'file'})
    mid=task['message_id'];await service.execute(store.operation(mid))
    browser.pages['p1'][0]['artifacts']=[{'kind':'file_card','index':0,'name':'test.png'}]
    await service.observe(store.operation(mid))
    browser.data=PNG;clock[0]+=15
    await service.observe(store.operation(mid))
    assert store.operation(mid)['status']=='COMPLETED' and len(browser.sent)==1
    assert store.operation(mid)['auto_retry']['next_at'] is None
    store.close()


@pytest.mark.parametrize('code',['ARTIFACT_URL_MISSING','ARTIFACT_URL_MALFORMED','ARTIFACT_PROVIDER_ERROR','ARTIFACT_POD_EXPIRED','ARTIFACT_REFERENCE_NOT_FOUND','ARTIFACT_HTTP_TRANSIENT'])
async def test_capture_error_context_is_stored(tmp_path,code):
    import json
    context=dict(http_status=404,provider_status='error',provider_error_code='ace_pod_expired',
                 download_url_kind='missing',url_host=None,metadata_keys=['status','error_code'],attempt=3,elapsed_ms=4200)
    class BrokenBrowser:
        async def call(self,action,**kw):
            assert kw['assistant_message_id']=='assistant'
            raise BridgeError(code,'下载失败',stage='artifact',context=context)
    store=Store(tmp_path);a=Artifacts(Settings(tmp_path),store)
    op=store.enqueue('alice','r',dict(text='x',effort='extra_high',soft_timeout=1,hard_timeout=2),'p',3)
    rows=await a.collect(op,[{'kind':'file_card','index':0,'name':'SKILL.md'}],BrokenBrowser(),{},'user','assistant')
    assert rows[0]['status']=='FAILED' and rows[0]['error']['error_code']==code
    assert rows[0]['error']['diagnostic_context']==context
    saved=json.loads(store.db.execute('SELECT error FROM artifacts').fetchone()[0])
    assert saved==rows[0]['error']
    store.close()
