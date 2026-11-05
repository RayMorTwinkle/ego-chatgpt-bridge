"""Bounded local storage, explicit expiry receipts, and download-aware cleanup."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import time
import zipfile

from starlette.responses import FileResponse

from .model import BridgeError

DAY=86400
LOG_NAMES=('service.log','supervisor.log','backup.log')


@contextmanager
def maintenance_lock(runtime, *, blocking=False):
    fd=os.open(runtime/'maintenance.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def private_json(path, data):
    temporary=path.with_name(path.name+'.part')
    with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as out:
        json.dump(data,out,ensure_ascii=False)
    os.replace(temporary,path)


class LeasedFileResponse(FileResponse):
    """A cleanup sweep cannot unlink a file while Starlette streams/ranges it."""
    def __init__(self,*args,leases: Counter,lease_key: str,**kwargs):
        super().__init__(*args,**kwargs)
        self.leases,self.lease_key=leases,lease_key
        leases[lease_key]+=1

    async def __call__(self,scope,receive,send):
        try:return await super().__call__(scope,receive,send)
        finally:
            self.leases[self.lease_key]-=1
            if self.leases[self.lease_key]<=0:del self.leases[self.lease_key]


def rotate_logs(settings, *, at=None):
    """Copy/truncate preserves launchd/child open descriptors; never rename a live log."""
    at=time.time() if at is None else at
    runtime=settings.runtime;result={'rotated':0,'removed':0,'errors':[]}
    fd=os.open(runtime/'logrotate.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(result,skipped='busy')
        state_file=runtime/'logrotate.json'
        try:state=json.loads(state_file.read_text()) if state_file.is_file() else {}
        except (ValueError,OSError):state={}
        today=datetime.datetime.fromtimestamp(at,datetime.timezone.utc).date().isoformat()
        for name in LOG_NAMES:
            path=runtime/name
            try:
                if path.is_symlink():continue
                if path.is_file():
                    path.chmod(0o600);stat=path.stat()
                    previous=state.get(name,datetime.datetime.fromtimestamp(stat.st_mtime,datetime.timezone.utc).date().isoformat())
                    if stat.st_size and (stat.st_size>=settings.log_max_bytes or previous<today):
                        stamp=datetime.datetime.fromtimestamp(at,datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
                        archive=runtime/f'{name}.{stamp}-{time.time_ns()}.gz'
                        import gzip
                        temporary=archive.with_name(archive.name+'.part')
                        # Copy a bounded snapshot. Keep bytes appended during compression by
                        # moving that tail to the front before truncating the existing inode.
                        with path.open('r+b',buffering=0) as live:
                            size=os.fstat(live.fileno()).st_size
                            with os.fdopen(os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as out:
                                with gzip.GzipFile(fileobj=out,mode='wb',mtime=int(at)) as zipped:
                                    left=size
                                    while left:
                                        chunk=live.read(min(left,1024*1024))
                                        if not chunk:raise OSError('Log changed during rotation')
                                        zipped.write(chunk);left-=len(chunk)
                                out.flush();os.fsync(out.fileno())
                            os.replace(temporary,archive)
                            # stdout/stderr producers use append mode. This small final
                            # copy/truncate window has the same limitation as logrotate's
                            # copytruncate; do not claim lossless forensic logging.
                            tail=live.read();live.seek(0);live.write(tail);live.truncate(len(tail))
                        result['rotated']+=1
                    state[name]=today
                for stale in runtime.glob(name+'.*.gz.part'):
                    if not stale.is_symlink() and stale.is_file() and stale.stat().st_mtime<=at-3600:
                        stale.unlink();result['removed']+=1
                archives=sorted((p for p in runtime.glob(name+'.*.gz') if p.is_file() and not p.is_symlink()),key=lambda p:p.stat().st_mtime,reverse=True)
                for index,archive in enumerate(archives):
                    if index>=settings.log_backup_count or archive.stat().st_mtime<=at-settings.log_retention_days*DAY:
                        archive.unlink();result['removed']+=1
            except OSError as error:
                result['errors'].append({'log':name,'type':type(error).__name__})
        private_json(state_file,state)
    return result


class Retention:
    def __init__(self,service,exports):
        self.service,self.exports=service,exports
        self.settings=service.settings;self.task=None;self.last=None
        previous=self.settings.runtime/'retention.json'
        try:self.last=json.loads(previous.read_text())
        except (ValueError,OSError):pass

    def policy(self):
        s=self.settings
        return dict(artifact_days=s.artifact_retention_days,export_days=s.export_retention_days,
            log_days=s.log_retention_days,log_max_bytes=s.log_max_bytes,log_backup_count=s.log_backup_count,
            interval_seconds=s.maintenance_interval_seconds,conversation_text='preserved',backup_copies='expire no later than included artifacts')

    def sweep(self, *, at=None):
        at=time.time() if at is None else at
        s=self.settings;db=self.service.store.db
        report=dict(at=at,policy=self.policy(),artifacts_expired=0,exports_expired=0,backups_removed=0,temporary_files_removed=0,bytes_removed=0,errors=[])
        try:
            with maintenance_lock(s.runtime):
                rows=db.execute("""SELECT a.id FROM artifacts a JOIN operations o ON o.id=a.operation_id
                    WHERE a.expires_at<=? AND NOT EXISTS(SELECT 1 FROM operations busy WHERE busy.query_id=o.query_id
                    AND busy.status IN ('QUEUED','PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING'))""",(at,)).fetchall()
                for row in rows:
                    key=row['id']
                    if self.service.artifacts.downloads[key]:continue
                    try:
                        paths=[self.service.artifacts.path(key),self.service.artifacts.path(key,temporary=True)]
                        size=sum(p.lstat().st_size for p in paths if p.is_file() and not p.is_symlink())
                        previous=db.execute('SELECT status FROM artifacts WHERE id=?',(key,)).fetchone()[0]
                        self.service.artifacts.expire(key,at=at)
                        report['bytes_removed']+=size
                        if previous!='EXPIRED':report['artifacts_expired']+=1
                    except (OSError,BridgeError) as error:report['errors'].append({'area':'artifact','type':type(error).__name__})
                for row in db.execute("SELECT id,status FROM debug_exports WHERE expires_at<=? AND status NOT IN ('QUEUED','RUNNING')",(at,)).fetchall():
                    key=row['id']
                    if self.exports.downloads[key]:continue
                    try:
                        folder=self.exports.directory(key)
                        size=sum(p.lstat().st_size for p in folder.rglob('*') if p.is_file() and not p.is_symlink()) if folder.is_dir() and not folder.is_symlink() else 0
                        # Tombstone first: a crash can leave excess bytes, never a false ready ZIP.
                        db.execute("UPDATE debug_exports SET status='EXPIRED',error=?,updated_at=? WHERE id=?",(json.dumps({'error_code':'EXPORT_EXPIRED','message':'归档已超过保留期，下载已停用。'}),at,key))
                        if folder.is_symlink():folder.unlink()
                        elif folder.exists():shutil.rmtree(folder)
                        report['bytes_removed']+=size
                        if row['status']!='EXPIRED':report['exports_expired']+=1
                    except OSError as error:report['errors'].append({'area':'export','type':type(error).__name__})
                # Stale staging bytes from interrupted captures, only when there are no
                # browser/export operations or file readers that might still own them.
                busy=db.execute("SELECT 1 FROM operations WHERE status IN ('QUEUED','PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING') LIMIT 1").fetchone()
                if not busy and not self.service.artifacts.downloads:
                    known={r[0] for r in db.execute('SELECT id FROM artifacts')}
                    for path in self.service.artifacts.root.iterdir():
                        if not re.fullmatch(r'art_[0-9a-f]{32}\.(part|blob)',path.name) or path.stem in known:continue
                        if path.lstat().st_mtime<=at-s.artifact_retention_days*DAY:
                            size=path.lstat().st_size;path.unlink();report['bytes_removed']+=size;report['temporary_files_removed']+=1
                export_busy=db.execute("SELECT 1 FROM debug_exports WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
                if not export_busy and not self.exports.downloads:
                    known={r[0] for r in db.execute('SELECT id FROM debug_exports')}
                    for folder in self.exports.root.iterdir():
                        if folder.name in known or not re.fullmatch(r'export_[0-9a-f]{32}',folder.name):continue
                        if folder.lstat().st_mtime<=at-s.export_retention_days*DAY:
                            if folder.is_symlink():folder.unlink()
                            elif folder.is_dir():shutil.rmtree(folder)
                            report['temporary_files_removed']+=1
                # QA evidence is app-owned scratch data, not durable conversation text.
                for name in ('verification','evidence'):
                    scratch=s.runtime/name
                    if not scratch.is_dir() or scratch.is_symlink():continue
                    for path in scratch.rglob('*'):
                        if path.is_file() and not path.is_symlink() and path.stat().st_mtime<=at-s.artifact_retention_days*DAY:
                            size=path.stat().st_size;path.unlink();report['temporary_files_removed']+=1;report['bytes_removed']+=size
                from .backup import backup_expiry
                backups=s.runtime/'backups'
                for path in ([] if backups.is_symlink() else backups.rglob('*.zip.part')):
                    if not path.is_symlink() and path.is_file() and path.stat().st_mtime<=at-3600:
                        size=path.stat().st_size;path.unlink();report['temporary_files_removed']+=1;report['bytes_removed']+=size
                for path in ([] if backups.is_symlink() else backups.rglob('*.zip')):
                    if path.is_symlink() or not path.is_file():continue
                    try:
                        expiry=backup_expiry(path,s.artifact_retention_days)
                        if expiry<=at:
                            size=path.stat().st_size;path.unlink();report['backups_removed']+=1;report['bytes_removed']+=size
                    except (OSError,ValueError,KeyError,RuntimeError,zipfile.BadZipFile) as error:
                        report['errors'].append({'area':'backup','type':type(error).__name__})
        except BlockingIOError:report['skipped']='backup_in_progress'
        report['logs']=rotate_logs(s,at=at)
        self.last=report;private_json(s.runtime/'retention.json',report)
        return report

    async def run(self):
        while True:
            try:
                async with self.service.control:self.sweep()
            except asyncio.CancelledError:raise
            except Exception as error:
                self.last={'at':time.time(),'error':type(error).__name__}
            await asyncio.sleep(self.settings.maintenance_interval_seconds)

    def start(self):self.task=asyncio.create_task(self.run(),name='ego-bridge-retention')

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
