"""Agent-facing client helpers; local or environment-configured LAN access."""
from __future__ import annotations

import hashlib
import os
import re
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .auth import load_clients
from .model import BridgeError


def connection(settings,owner=None,url=None,ca_file=None,token_env='EGO_BRIDGE_TOKEN'):
    endpoint=(url or os.environ.get('EGO_BRIDGE_URL') or settings.base_url).rstrip('/')
    parsed=urlsplit(endpoint)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError('INVALID_ENDPOINT','服务地址必须是无凭据的 HTTP／HTTPS 地址。')
    token=os.environ.get(token_env) or load_clients(settings.runtime)[owner or settings.default_owner]
    context=True
    if parsed.scheme=='https':
        certificate=ca_file or os.environ.get('EGO_BRIDGE_CA') or settings.runtime/'tls-cert.pem'
        context=ssl.create_default_context(cafile=str(certificate))
    return endpoint,httpx.Client(verify=context,headers={'Authorization':'Bearer '+token},timeout=90,trust_env=False)


def download_artifact(client,endpoint,artifact_id,output: Path):
    if not re.fullmatch(r'art_[0-9a-f]{32}',artifact_id):raise BridgeError('INVALID_ARTIFACT_ID','产物标识格式不正确。')
    response=client.post(endpoint+'/api/get_artifact',json={'artifact_id':artifact_id});response.raise_for_status();value=response.json()
    if not value.get('ok'):raise BridgeError(value['error']['error_code'],value['error']['message'])
    meta=value['artifact']
    if meta['status']!='AVAILABLE':raise BridgeError('ARTIFACT_NOT_READY','产物尚未可用。')
    output=output.expanduser().absolute()
    if output.exists():raise BridgeError('OUTPUT_EXISTS','目标已存在，拒绝覆盖。')
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.ego-download-',delete=False) as file:
            temporary=Path(file.name);digest=hashlib.sha256();size=0
            # Construct a same-server URL; never forward a token to a returned third-party URL.
            with client.stream('GET',endpoint+f'/artifacts/{artifact_id}/content') as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>meta['size_bytes']:raise BridgeError('DOWNLOAD_INTEGRITY_FAILED','下载内容超出元数据大小。')
                    digest.update(chunk);file.write(chunk)
            if size!=meta['size_bytes'] or digest.hexdigest()!=meta['sha256']:
                raise BridgeError('DOWNLOAD_INTEGRITY_FAILED','下载大小或 SHA-256 不匹配。')
            file.flush();os.fsync(file.fileno())
        # Publish atomically without replacing an existing file, even across racing clients.
        os.link(temporary,output)
        return {'ok':True,'artifact_id':artifact_id,'output':str(output),'filename':meta['filename'],'mime_type':meta['mime_type'],'size_bytes':size,'sha256':digest.hexdigest()}
    finally:
        if temporary:temporary.unlink(missing_ok=True)
