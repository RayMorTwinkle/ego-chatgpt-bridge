from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .model import BridgeError


def backup_with_artifacts(runtime: Path,destination: Path):
    destination=destination.expanduser().resolve()
    temporary=destination.with_name(destination.name+'.part')
    try:
        with tempfile.TemporaryDirectory(prefix='ego-backup-') as work:
            snapshot=Path(work)/'bridge.sqlite3'
            with sqlite3.connect(f'file:{runtime/"bridge.sqlite3"}?mode=ro',uri=True) as source,sqlite3.connect(snapshot) as target:
                source.backup(target)
                present=target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
                rows=target.execute("SELECT id,size_bytes,sha256 FROM artifacts WHERE status='AVAILABLE'").fetchall() if present else []
            fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
            os.close(fd)
            manifest={'format_version':1,'artifacts':[],'not_included':['.env','TLS keys','debug export archives']}
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,'bridge.sqlite3')
                for art,size,checksum in rows:
                    if not re.fullmatch(r'art_[0-9a-f]{32}',art):raise BridgeError('BACKUP_INVALID_METADATA','产物标识不正确。')
                    path=runtime/'artifacts'/(art+'.blob')
                    if path.is_symlink():raise BridgeError('BACKUP_INVALID_FILE','产物路径不能是符号链接。')
                    received=0;digest=hashlib.sha256();name='artifacts/'+art+'.blob'
                    with path.open('rb') as source,archive.open(name,'w') as target:
                        while chunk:=source.read(1024*1024):
                            received+=len(chunk);digest.update(chunk);target.write(chunk)
                    if received!=size or digest.hexdigest()!=checksum:raise BridgeError('BACKUP_INTEGRITY_FAILED','备份期间产物缺失或发生变化，未发布备份。')
                    manifest['artifacts'].append({'artifact_id':art,'size_bytes':size,'sha256':checksum})
                archive.writestr('backup-manifest.json',json.dumps(manifest,indent=2))
            os.replace(temporary,destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
