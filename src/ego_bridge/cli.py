from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path

import httpx

from .model import TERMINAL, Settings, BridgeError
from .auth import load_clients, save_clients, env_values

DEFAULT_RUNTIME = Path(os.environ.get("EGO_BRIDGE_RUNTIME", str(Path.home()/"apps/ego-chatgpt-bridge")))


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def initialize(runtime: Path, *, renew_tls: bool = False):
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    if not (runtime / "config.json").exists():
        private_json(runtime / "config.json", {"host": "127.0.0.1", "allowed_hosts": ["localhost", "127.0.0.1"], "allowed_client_cidrs": ["127.0.0.0/8"]})
    hosts = set(json.loads((runtime / "config.json").read_text()).get("allowed_hosts", ["localhost", "127.0.0.1"]))
    if not (runtime / ".env").exists():
        old=runtime/"clients.json"
        clients=json.loads(old.read_text()) if old.exists() else {"default":secrets.token_urlsafe(32)}
        owner=Settings.load(runtime).default_owner
        if owner not in clients:owner=next(iter(clients))
        save_clients(runtime,clients,owner)
    if renew_tls or (Settings.load(runtime).tls_enabled and not (runtime / "tls-cert.pem").exists()):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from datetime import datetime, timedelta, timezone
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ego Bridge LAN")])
        sans = []
        for host in hosts:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                sans.append(x509.DNSName(host))
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
        (runtime / "tls-cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (runtime / "tls-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(runtime / "tls-key.pem", 0o600)
    print(f"Runtime initialized: {runtime}; tokens are in .env (0600), not printed.")


async def stdio_proxy(settings, owner):
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    token = load_clients(settings.runtime)[owner]
    ssl_context = ssl.create_default_context(cafile=str(settings.runtime / "tls-cert.pem")) if settings.tls_enabled else True
    async with httpx.AsyncClient(verify=ssl_context, headers={"Authorization": f"Bearer {token}"}, timeout=90,trust_env=False) as http:
        async with streamable_http_client(settings.base_url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as remote:
                await remote.initialize()
                proxy = Server("ego-bridge-stdio-proxy")
                @proxy.list_tools()
                async def tools():
                    return (await remote.list_tools()).tools
                @proxy.call_tool()
                async def call(name: str, arguments: dict):
                    return await remote.call_tool(name, arguments)
                @proxy.list_resources()
                async def resources():return (await remote.list_resources()).resources
                @proxy.list_resource_templates()
                async def templates():return (await remote.list_resource_templates()).resourceTemplates
                @proxy.read_resource()
                async def resource(uri):
                    from mcp.server.lowlevel.helper_types import ReadResourceContents
                    import base64
                    result=await remote.read_resource(uri)
                    return [ReadResourceContents(content=c.text if isinstance(c,types.TextResourceContents) else base64.b64decode(c.blob),mime_type=c.mimeType) for c in result.contents]
                async with stdio_server() as (r, w):
                    await proxy.run(r, w, proxy.create_initialization_options())


def _main():
    parser = argparse.ArgumentParser(description="Ego Browser research bridge")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("renew-tls")
    auto=sub.add_parser('autostart',help='macOS 登录自启与进程/HTTP 保活')
    auto.add_argument('action',choices=['install','uninstall','status','restart'])
    supervisor=sub.add_parser('supervise',help='前台守护服务，供 launchd 使用')
    supervisor.add_argument('--interval',type=float,default=10)
    supervisor.add_argument('--timeout',type=float,default=3)
    supervisor.add_argument('--failures',type=int,default=3)
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    call = sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("--json", default="{}")
    call.add_argument("--file", type=Path)
    call.add_argument("--owner")
    call.add_argument("--url")
    call.add_argument('--ca-file',type=Path)
    call.add_argument('--token-env',default='EGO_BRIDGE_TOKEN')
    call.add_argument("--wait", action="store_true")
    call.add_argument("--wait-timeout", type=float, default=7200)
    download=sub.add_parser('download',help='下载原始产物并校验 SHA-256，不覆盖已有文件')
    download.add_argument('artifact_id')
    download.add_argument('--output',type=Path,required=True)
    download.add_argument('--owner')
    download.add_argument('--url')
    download.add_argument('--ca-file',type=Path)
    download.add_argument('--token-env',default='EGO_BRIDGE_TOKEN')
    token = sub.add_parser("add-client")
    token.add_argument("owner")
    backup=sub.add_parser("backup")
    backup.add_argument("destination",type=Path)
    backup.add_argument("--include-artifacts",action='store_true')
    stdio = sub.add_parser("stdio")
    stdio.add_argument("--owner")
    args = parser.parse_args()
    runtime = args.runtime.expanduser().resolve()
    if args.command == "init":
        return initialize(runtime)
    if args.command == "renew-tls":
        return initialize(runtime, renew_tls=True)
    settings = Settings.load(runtime)
    if args.command=='autostart':
        from .autostart import manage_autostart
        return manage_autostart(runtime,args.action)
    if args.command=='supervise':
        from .supervisor import run_supervisor
        return run_supervisor(runtime,interval=args.interval,timeout=args.timeout,failures=args.failures)
    if args.command == "add-client":
        clients = load_clients(runtime)
        if args.owner in clients:
            parser.error("Client already exists; refusing to overwrite its token")
        clients[args.owner] = secrets.token_urlsafe(32)
        save_clients(runtime, clients,env_values(runtime).get('EGO_BRIDGE_OWNER') or settings.default_owner)
        print("Client added to private .env; restart service to load it.")
        return
    if args.command == "serve":
        from .server import build_app
        app = build_app(settings)
        private_json(runtime / "process.json", {"pid": os.getpid(), "port": args.port or settings.port})
        try:
            from .gateway import serve_apps
            asyncio.run(serve_apps(app,settings,args.host,args.port))
        finally:
            (runtime / "process.json").unlink(missing_ok=True)
        return
    if args.command == "stdio":
        return asyncio.run(stdio_proxy(settings, args.owner or settings.default_owner))
    if args.command == "backup":
        if args.include_artifacts:
            from .backup import backup_with_artifacts
            backup_with_artifacts(runtime,args.destination)
            print(f"Database and verified artifact bytes backed up: {args.destination}")
            return
        import sqlite3
        with sqlite3.connect(f"file:{runtime / 'bridge.sqlite3'}?mode=ro", uri=True) as source:
            with sqlite3.connect(args.destination) as target:
                source.backup(target)
        os.chmod(args.destination, 0o600)
        print(f"Backup saved: {args.destination}")
        return
    from .client import connection,download_artifact
    if args.command=='download':
        try:
            endpoint,client=connection(settings,args.owner,args.url,args.ca_file,args.token_env)
            with client:data=download_artifact(client,endpoint,args.artifact_id,args.output)
            print(json.dumps(data,ensure_ascii=False,indent=2));return
        except (BridgeError,httpx.HTTPError,OSError) as error:
            print(json.dumps({'ok':False,'error':error.data if isinstance(error,BridgeError) else {'error_code':'DOWNLOAD_FAILED','type':type(error).__name__}},ensure_ascii=False));sys.exit(1)
    params = json.loads(args.file.read_text() if args.file else args.json)
    endpoint,client=connection(settings,args.owner,args.url,args.ca_file,args.token_env)
    with client:
        response = client.post(f"{endpoint}/api/{args.tool}", json=params)
        response.raise_for_status()
        data = response.json()
        deadline = time.monotonic() + args.wait_timeout
        while args.wait and data.get("ok") and data.get("query_id") and data.get("status") not in TERMINAL:
            if time.monotonic() >= deadline:
                print(json.dumps({"client_wait_timeout": True, "background_task_continues": True, **data}, ensure_ascii=False, indent=2))
                sys.exit(3)
            data = client.post(f"{endpoint}/api/wait_query", json={"query_id": data["query_id"], "message_id": data["message_id"],
                "cursor":data.get('cursor'),"timeout_seconds":min(25,max(0,deadline-time.monotonic()))}).json()
        if args.wait and data.get('ok') and data.get('query_id'):
            data=client.post(f"{endpoint}/api/get_query_result",json={"query_id":data['query_id'],"message_id":data['message_id'],"include_outputs":False,"content_format":"markdown"}).json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if not data.get("ok"):
            sys.exit(1)
        if args.wait and args.tool in {'create_query','continue_query','get_query_status','get_query_result'} and data.get('status')!='COMPLETED':
            sys.exit(2)


def main():
    try:return _main()
    except (BridgeError,httpx.HTTPError,OSError,ValueError) as error:
        detail=error.data if isinstance(error,BridgeError) else {'error_code':'CLIENT_ERROR','type':type(error).__name__,
            'message':'检查服务状态、HTTPS 地址、公有 CA 证书、token 配置和参数。'}
        print(json.dumps({'ok':False,'error':detail},ensure_ascii=False));sys.exit(1)


if __name__ == "__main__":
    main()
