from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import math
import zipfile
from pathlib import Path

from .model import BridgeError, Settings


def backup_expiry(path: Path,retention_days: int) -> float:
    """A backup must not extend the life of its oldest included artifact."""
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo('backup-manifest.json').file_size>4*1024*1024:raise ValueError('Backup manifest too large')
        manifest=json.loads(archive.read('backup-manifest.json'))
        if not isinstance(manifest,dict):raise ValueError('Invalid backup manifest')
        limit=path.stat().st_mtime+retention_days*86400
        if manifest.get('expires_at') is not None:
            value=float(manifest['expires_at'])
            if not math.isfinite(value) or value<=0:raise ValueError('Invalid backup expiry')
            return min(limit,value)
        # Legacy archives predate expiry manifests. Read just their SQLite snapshot,
        # never extract arbitrary archive paths, and derive the original capture age.
        info=archive.getinfo('bridge.sqlite3')
        if info.file_size>128*1024*1024:raise ValueError('Legacy backup too large for expiry migration')
        with tempfile.TemporaryDirectory(prefix='ego-expiry-') as work:
            snapshot=Path(work)/'snapshot.sqlite3';snapshot.write_bytes(archive.read(info))
            try:
                with sqlite3.connect(snapshot) as db:
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
                    if exists:
                        columns={r[1] for r in db.execute('PRAGMA table_info(artifacts)')}
                        field='expires_at' if 'expires_at' in columns else f'updated_at+{int(retention_days)*86400}'
                        row=db.execute(f"SELECT min({field}) FROM artifacts WHERE status='AVAILABLE'").fetchone()
                        if row[0] is not None:limit=min(limit,float(row[0]))
            except sqlite3.DatabaseError as error:raise ValueError('Invalid backup database') from error
        return limit


def backup_with_artifacts(runtime: Path,destination: Path):
    from .retention import maintenance_lock
    with maintenance_lock(runtime,blocking=True):
        return _backup_with_artifacts(runtime,destination)


def _backup_with_artifacts(runtime: Path,destination: Path):
    settings=Settings.load(runtime);created_at=time.time()
    destination=destination.expanduser().resolve()
    temporary=destination.with_name(destination.name+'.part')
    try:
        with tempfile.TemporaryDirectory(prefix='ego-backup-') as work:
            snapshot=Path(work)/'bridge.sqlite3'
            with sqlite3.connect(f'file:{runtime/"bridge.sqlite3"}?mode=ro',uri=True) as source,sqlite3.connect(snapshot) as target:
                source.backup(target)
                present=target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
                rows=[]
                if present:
                    columns={r[1] for r in target.execute('PRAGMA table_info(artifacts)')}
                    if 'expires_at' not in columns:target.execute('ALTER TABLE artifacts ADD COLUMN expires_at REAL')
                    target.execute('UPDATE artifacts SET expires_at=updated_at+? WHERE expires_at IS NULL',(settings.artifact_retention_days*86400,))
                    target.execute("UPDATE artifacts SET status='EXPIRED' WHERE expires_at<=?",(created_at,))
                    rows=target.execute("SELECT id,size_bytes,sha256,expires_at FROM artifacts WHERE status='AVAILABLE'").fetchall()
            fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
            os.close(fd)
            manifest={'format_version':2,'created_at':created_at,'expires_at':min([created_at+settings.artifact_retention_days*86400]+[r[3] for r in rows]),'artifacts':[],'not_included':['.env','TLS keys','debug export archives']}
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,'bridge.sqlite3')
                for art,size,checksum,expires in rows:
                    if not re.fullmatch(r'art_[0-9a-f]{32}',art):raise BridgeError('BACKUP_INVALID_METADATA','产物标识不正确。')
                    path=runtime/'artifacts'/(art+'.blob')
                    if path.is_symlink():raise BridgeError('BACKUP_INVALID_FILE','产物路径不能是符号链接。')
                    received=0;digest=hashlib.sha256();name='artifacts/'+art+'.blob'
                    with path.open('rb') as source,archive.open(name,'w') as target:
                        while chunk:=source.read(1024*1024):
                            received+=len(chunk);digest.update(chunk);target.write(chunk)
                    if received!=size or digest.hexdigest()!=checksum:raise BridgeError('BACKUP_INTEGRITY_FAILED','备份期间产物缺失或发生变化，未发布备份。')
                    manifest['artifacts'].append({'artifact_id':art,'size_bytes':size,'sha256':checksum,'expires_at':expires})
                archive.writestr('backup-manifest.json',json.dumps(manifest,indent=2))
            os.replace(temporary,destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
