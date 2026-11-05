"""Loopback-only public surface. Shares the backend, never exposes its UI/API routes."""
from __future__ import annotations
import asyncio
import ipaddress
import signal
import time
from contextlib import contextmanager

import uvicorn
from starlette.responses import JSONResponse
from .oauth import PUBLIC_PATHS


class PublicGateway:
    def __init__(self, app):
        self.app=app
        self.buckets={}

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':return
        try: local=ipaddress.ip_address(scope['client'][0]).is_loopback
        except (ValueError,TypeError,KeyError):local=False
        raw=scope.get('raw_path',scope['path'].encode()).split(b'?',1)[0]
        if not local or scope['path'] not in PUBLIC_PATHS or any(part in raw for part in (b'%',b'\\',b'//')):
            return await JSONResponse({'error':'not_found'},status_code=404)(scope,receive,send)
        # Public OAuth endpoints are deliberately unauthenticated, but bounded. Counters
        # are global because all FRP traffic has the same loopback transport peer.
        path=scope['path']
        if path in {'/register','/authorize','/token'}:
            now=time.monotonic();start,count=self.buckets.get(path,(now,0))
            if now-start>=60:start,count=now,0
            limit={'/register':10,'/authorize':60,'/token':180}[path]
            if count>=limit:
                return await JSONResponse({'error':'temporarily_unavailable'},status_code=429,headers={'Retry-After':'60','Cache-Control':'no-store'})(scope,receive,send)
            self.buckets[path]=(start,count+1)
        # This flag is an internal ASGI state value, never an HTTP header.
        scope=dict(scope,state={**scope.get('state',{}),'public_gateway':True})
        return await self.app(scope,receive,send)


async def serve_apps(app, settings, host=None, port=None):
    class Server(uvicorn.Server):
        @contextmanager
        def capture_signals(self): yield
    tls={'ssl_certfile':str(settings.runtime/'tls-cert.pem'),'ssl_keyfile':str(settings.runtime/'tls-key.pem')} if settings.tls_enabled else {}
    common=dict(access_log=False,log_level='warning',timeout_graceful_shutdown=10,proxy_headers=False)
    servers=[Server(uvicorn.Config(app,host=host or settings.host,port=port or settings.port,**tls,**common))]
    if settings.oauth_issuer:
        if not settings.public_gateway_port:raise ValueError('OAuth requires a separate loopback gateway port')
        servers.append(Server(uvicorn.Config(PublicGateway(app),host='127.0.0.1',port=settings.public_gateway_port,lifespan='off',**common)))
    loop=asyncio.get_running_loop()
    def stop():
        # Drain the public listener before the primary lifespan closes SQLite.
        servers[-1].should_exit=True
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop)
    # Start the secondary listener only after the main application has initialized.
    tasks=[asyncio.create_task(servers[0].serve())]
    try:
        while not servers[0].started:
            if tasks[0].done():return await tasks[0]
            await asyncio.sleep(.05)
        tasks.extend(asyncio.create_task(server.serve()) for server in servers[1:])
        await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:server.should_exit=True
        await asyncio.gather(*tasks,return_exceptions=True)
        for sig in (signal.SIGTERM,signal.SIGINT):loop.remove_signal_handler(sig)
