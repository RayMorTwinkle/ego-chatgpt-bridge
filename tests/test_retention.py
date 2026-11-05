"""Storage deletion tests use isolated runtimes and real files/archives, never production clocks."""
import asyncio
from collections import Counter
import gzip
import json
import os
from pathlib import Path
import sqlite3
import time
import zipfile

import httpx
import pytest

from ego_bridge.backup import backup_with_artifacts,backup_expiry
from ego_bridge.exports import ExportJobs
from ego_bridge.model import Settings,BridgeError,identifier
from ego_bridge.retention import Retention,rotate_logs,LeasedFileResponse,maintenance_lock
from ego_bridge.server import build_app
from ego_bridge.service import Service
from ego_bridge.store import Store
from test_artifacts import FileBrowser,PNG,seed

DAY=86400


def environment(tmp_path,**options):
    settings=Settings(tmp_path,**options);store=Store(tmp_path)
    service=Service(settings,store,FileBrowser());exports=ExportJobs(service)
    retention=Retention(service,exports);service.retention=retention
    return store,service,exports,retention


def finished(store,op):
    store.db.execute("UPDATE operations SET status='COMPLETED',result=? WHERE id=?",(json.dumps({'content':'preserved text','completeness_signal':'complete','delivery_complete':True,'artifacts':[]}),op['id']))


async def test_expiry_preserves_text_and_metadata_then_explicit_recollect_restores_bytes(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    browser=FileBrowser();op,row=await seed(service.artifacts,store,browser);finished(store,op)
    aid=row['artifact_id'];path=service.artifacts.path(aid)
    store.db.execute('UPDATE artifacts SET expires_at=? WHERE id=?',(time.time()-1,aid))
    assert service.artifacts.public(service.artifacts.get('alice',aid))['download_url'] is None
    with pytest.raises(BridgeError) as e:service.artifacts.available_path('alice',aid)
    assert e.value.data['error_code']=='ARTIFACT_EXPIRED'
    report=retention.sweep()
    assert report['artifacts_expired']==1 and not report['errors'] and not path.exists()
    assert store.operation(op['id'])['result']['content']=='preserved text'
    assert service.artifacts.get('alice',aid)['sha256']==row['sha256']
    result=await service.dispatch('alice','get_query_result',{'query_id':op['query_id']})
    assert result['generation_complete'] and not result['delivery_complete'] and result['delivery_expired']
    assert retention.sweep()['artifacts_expired']==0
    collected=await service.artifacts.collect(op,[{'kind':'file_card','index':0,'name':'test.png'}],browser,{},'user')
    assert browser.downloads==2 and not browser.sent
    assert collected[0]['status']=='AVAILABLE' and collected[0]['expires_at']>time.time()+13*DAY
    assert path.read_bytes()==PNG
    store.close()


async def test_followup_and_multiple_download_leases_delay_physical_cleanup(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    op,row=await seed(service.artifacts,store,FileBrowser());finished(store,op)
    aid=row['artifact_id'];path=service.artifacts.path(aid)
    follow=store.enqueue('alice','follow',op['payload'],'p',3,op['query_id'])
    store.db.execute('UPDATE artifacts SET expires_at=? WHERE id=?',(time.time()-1,aid))
    assert retention.sweep()['artifacts_expired']==0 and path.exists()
    finished(store,follow)
    service.artifacts.downloads[aid]=2
    assert retention.sweep()['artifacts_expired']==0
    service.artifacts.downloads[aid]-=1
    assert retention.sweep()['artifacts_expired']==0 and path.exists()
    del service.artifacts.downloads[aid]
    assert retention.sweep()['artifacts_expired']==1 and not path.exists()
    store.close()


async def test_range_file_response_holds_lease_until_stream_finishes(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    op,row=await seed(service.artifacts,store,FileBrowser());finished(store,op)
    aid=row['artifact_id'];path=service.artifacts.path(aid)
    after=row['expires_at']+1
    response=LeasedFileResponse(path,leases=service.artifacts.downloads,lease_key=aid)
    chunks=[]
    async def send(message):
        retention.sweep(at=after)
        assert path.exists()
        if message['type']=='http.response.body':chunks.append(message.get('body',b''))
    async def receive():return {'type':'http.request','body':b''}
    await response({'type':'http','asgi':{'spec_version':'2.4'},'method':'GET','headers':[(b'range',b'bytes=0-7')]},receive,send)
    assert b''.join(chunks)==PNG[:8] and not service.artifacts.downloads
    assert retention.sweep(at=after)['artifacts_expired']==1
    store.close()


def export_record(store,exports,status='COMPLETED'):
    job=identifier('export');at=time.time()-15*DAY
    store.db.execute('INSERT INTO debug_exports(id,owner,request_id,url,status,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?)',(job,'alice',job,'https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',status,at,at,at+14*DAY))
    folder=exports.directory(job);folder.mkdir();(folder/'conversation.zip').write_bytes(b'zip-placeholder');(folder/'work.png').write_bytes(PNG)
    return job,folder


def test_exports_and_unreferenced_staging_cleanup_are_bounded(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    old,folder=export_record(store,exports)
    running,active=export_record(store,exports,'RUNNING')
    exports.downloads[old]=1
    assert retention.sweep()['exports_expired']==0 and folder.exists() and active.exists()
    del exports.downloads[old]
    report=retention.sweep();assert report['exports_expired']==1
    assert not folder.exists() and active.exists() and exports.get('alice',old)['status']=='EXPIRED'
    assert exports.get('alice',old)['download_url'] is None
    orphan=service.artifacts.root/('art_'+'f'*32+'.part');orphan.write_bytes(b'partial');os.utime(orphan,(0,0))
    unrelated=service.artifacts.root/'notes.txt';unrelated.write_text('not managed');os.utime(unrelated,(0,0))
    outside=tmp_path/'outside';outside.write_text('preserve')
    link=service.artifacts.root/('art_'+'e'*32+'.blob');link.symlink_to(outside);os.utime(link,(0,0),follow_symlinks=False)
    retention.sweep();assert not orphan.exists() and not link.exists() and outside.read_text()=='preserve' and unrelated.exists()
    store.close()


async def test_backup_expires_with_oldest_artifact_and_cannot_restore_expired_bytes(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    op,row=await seed(service.artifacts,store,FileBrowser());finished(store,op)
    expiry=time.time()+60;store.db.execute('UPDATE artifacts SET expires_at=? WHERE id=?',(expiry,row['artifact_id']))
    folder=tmp_path/'backups';folder.mkdir();backup=folder/'verified.zip'
    backup_with_artifacts(tmp_path,backup)
    assert backup_expiry(backup,14)==expiry
    with zipfile.ZipFile(backup) as z:
        manifest=json.loads(z.read('backup-manifest.json'))
        assert manifest['expires_at']==expiry and z.read('artifacts/'+row['artifact_id']+'.blob')==PNG
    report=retention.sweep(at=expiry+1)
    assert report['backups_removed']==1 and not backup.exists()
    # A new snapshot records the tombstone and contains no expired blob.
    backup_with_artifacts(tmp_path,backup)
    with zipfile.ZipFile(backup) as z:
        assert not any(n.startswith('artifacts/') for n in z.namelist())
        assert json.loads(z.read('backup-manifest.json'))['artifacts']==[]
    store.close()


async def test_legacy_backup_uses_capture_age_and_backup_lock_defers_cleanup(tmp_path):
    store,service,exports,retention=environment(tmp_path)
    op,row=await seed(service.artifacts,store,FileBrowser());finished(store,op)
    old=time.time()-15*DAY
    store.db.execute('UPDATE artifacts SET updated_at=?,expires_at=? WHERE id=?',(old,old+14*DAY,row['artifact_id']))
    snapshot=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(snapshot) as target:store.db.backup(target)
    with sqlite3.connect(snapshot) as target:target.execute('ALTER TABLE artifacts DROP COLUMN expires_at')
    folder=tmp_path/'backups';folder.mkdir();archive=folder/'legacy.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.write(snapshot,'bridge.sqlite3');z.writestr('backup-manifest.json',json.dumps({'format_version':1,'artifacts':[]}));z.writestr('artifacts/'+row['artifact_id']+'.blob',PNG)
    assert backup_expiry(archive,14)==old+14*DAY
    with maintenance_lock(tmp_path):
        result=retention.sweep();assert result['skipped']=='backup_in_progress' and archive.exists()
    assert retention.sweep()['backups_removed']==1
    store.close()


def test_logs_rotate_with_writer_open_and_prune_by_count_and_age(tmp_path):
    settings=Settings(tmp_path,log_max_bytes=64,log_backup_count=2)
    path=tmp_path/'service.log'
    with path.open('ab',buffering=0) as producer:
        inode=path.stat().st_ino
        producer.write(b'a'*80)
        assert rotate_logs(settings)['rotated']==1
        assert path.stat().st_ino==inode and path.read_bytes()==b''
        archive=next(tmp_path.glob('service.log.*.gz'));assert gzip.decompress(archive.read_bytes())==b'a'*80
        producer.write(b'b'*80);rotate_logs(settings)
        producer.write(b'c'*80);rotate_logs(settings)
        assert len(list(tmp_path.glob('service.log.*.gz')))==2
        producer.write(b'current');assert path.read_bytes()==b'current'
    for archive in tmp_path.glob('service.log.*.gz'):os.utime(archive,(0,0))
    assert rotate_logs(settings)['removed']==2
    assert path.stat().st_mode&0o777==0o600
    (tmp_path/'logrotate.json').write_text(json.dumps({'service.log':'2000-01-01'}))
    assert rotate_logs(settings)['rotated']==1  # daily rotation even for a small log


async def test_expired_downloads_return_gone_over_http(tmp_path):
    app=build_app(Settings(tmp_path,default_owner='alice'),adapter=FileBrowser(),clients={'alice':'a'*40})
    async with app.router.lifespan_context(app):
        service=app.state.service;op,row=await seed(service.artifacts,service.store,FileBrowser());finished(service.store,op)
        service.store.db.execute('UPDATE artifacts SET expires_at=0 WHERE id=?',(row['artifact_id'],))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
            response=await c.get('/artifacts/'+row['artifact_id']+'/content',headers={'Authorization':'Bearer '+'a'*40})
            assert response.status_code==410 and response.json()['error']['error_code']=='ARTIFACT_EXPIRED'
            await c.post('/ui/api/login',json={'token':'a'*40})
            assert (await c.get('/ui/api/artifacts/'+row['artifact_id'])).status_code==410
