"""OAuth 2.1 provider. SDK handles protocol/PKCE; opaque credentials stay hashed at rest."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time
from urllib.parse import urlsplit, parse_qs

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
    RefreshToken, AuthorizeError, TokenError, RegistrationError, construct_redirect_uri)
from mcp.server.auth.routes import create_auth_routes, build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .auth import BrowserSessions, COOKIE_NAME, HTTP_COOKIE_NAME, SESSION_SECONDS, env_values, token_owner

SCOPES = ['research:read', 'research:write']
WRITE_TOOLS = {'create_query','continue_query','recover_session','retry_query','cancel_query','collect_artifacts','delete_query','health_check'}
PUBLIC_PATHS = {'/mcp','/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp',
    '/.well-known/oauth-authorization-server','/authorize','/token','/register','/revoke'}

def hashed(value): return hashlib.sha256(value.encode()).hexdigest()


class OAuthProvider:
    def __init__(self, settings, store, clients):
        self.settings, self.store, self.clients = settings, store, clients
        self.issuer = str(AnyHttpUrl(settings.oauth_issuer))
        parsed = urlsplit(self.issuer)
        if parsed.scheme != 'https' or parsed.path != '/' or parsed.query or parsed.fragment or parsed.username or parsed.port not in (None,443):
            raise ValueError('oauth_issuer must be a public HTTPS origin without a path or credentials')
        self.resource = self.issuer.rstrip('/') + '/mcp'
        self.db = store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS oauth_objects(kind TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(kind,key))')
        values = env_values(settings.runtime)
        self.cookie = COOKIE_NAME if settings.tls_enabled else HTTP_COOKIE_NAME
        self.sessions = BrowserSessions(clients, values.get('WEB_SESSION_SECRET') or secrets.token_urlsafe(48), values.get('EGO_BRIDGE_OWNER') or settings.default_owner)

    def get(self, kind, key):
        row = self.db.execute('SELECT value FROM oauth_objects WHERE kind=? AND key=?',(kind,key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, value):
        self.db.execute('INSERT OR REPLACE INTO oauth_objects VALUES(?,?,?)',(kind,key,json.dumps(value)))

    def delete(self, kind, key):
        self.db.execute('DELETE FROM oauth_objects WHERE kind=? AND key=?',(kind,key))

    def active_grant(self, key):
        grant = self.get('grant',key)
        if not grant or grant.get('revoked') or grant['expires_at'] <= time.time(): return None
        current = self.clients.get(grant['owner'])
        return grant if current and hmac.compare_digest(grant['fingerprint'],hashed(current)) else None

    async def get_client(self, client_id):
        row = self.get('client',client_id)
        return OAuthClientInformationFull.model_validate(row) if row else None

    async def register_client(self, client_info):
        # No fetching arbitrary client metadata URLs: DCR is sufficient for ChatGPT and Codex.
        for uri in client_info.redirect_uris:
            p = urlsplit(str(uri))
            allowed = (p.scheme == 'https' and p.hostname in self.settings.oauth_redirect_hosts) or (p.scheme == 'http' and p.hostname in {'127.0.0.1','localhost','::1'})
            if not allowed or p.username or p.fragment:
                raise RegistrationError('invalid_redirect_uri','Use a configured HTTPS callback host or a loopback callback.')
        if client_info.token_endpoint_auth_method not in {'none','client_secret_post','client_secret_basic'}:
            raise RegistrationError('invalid_client_metadata','Unsupported token authentication method')
        count = self.db.execute("SELECT count(*) FROM oauth_objects WHERE kind='client'").fetchone()[0]
        if count >= 1000: raise RegistrationError('invalid_client_metadata','Registration capacity reached; contact the administrator')
        self.put('client',client_info.client_id,client_info.model_dump(mode='json'))

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource and params.resource != self.resource:
            raise AuthorizeError('invalid_request','Incorrect resource audience')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',params.code_challenge):
            raise AuthorizeError('invalid_request','A valid S256 PKCE challenge is required')
        scopes = params.scopes if params.scopes is not None else SCOPES
        if not scopes or not set(scopes) <= set(SCOPES): raise AuthorizeError('invalid_scope')
        for row in self.db.execute("SELECT key,value FROM oauth_objects WHERE kind='pending'").fetchall():
            if json.loads(row['value'])['expires_at']<=time.time():self.delete('pending',row['key'])
        if self.db.execute("SELECT count(*) FROM oauth_objects WHERE kind='pending'").fetchone()[0]>=1000:
            raise AuthorizeError('temporarily_unavailable','Pending authorization capacity reached')
        key = secrets.token_urlsafe(32)
        self.put('pending',hashed(key),dict(client_id=client.client_id,params=params.model_dump(mode='json'),scopes=scopes,expires_at=time.time()+600))
        return self.settings.base_url + '/ui/oauth/consent?request=' + key

    def pending(self, key):
        row = self.get('pending',hashed(key))
        return row if row and row['expires_at'] > time.time() else None

    async def load_authorization_code(self, client, authorization_code):
        row = self.get('code',hashed(authorization_code))
        if not row or row['client_id'] != client.client_id or row['expires_at'] <= time.time() or not self.active_grant(row['grant']): return None
        return AuthorizationCode(code=authorization_code,**{k:v for k,v in row.items() if k!='grant'})

    def issue_tokens(self, client_id, grant_id, scopes):
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        grant = self.active_grant(grant_id)
        if not grant: raise TokenError('invalid_grant')
        common = dict(client_id=client_id,scopes=scopes,resource=self.resource,subject=grant['owner'],grant=grant_id)
        self.put('access',hashed(access),dict(**common,expires_at=int(time.time())+3600))
        self.put('refresh',hashed(refresh),dict(**common,expires_at=min(int(time.time())+30*86400,grant['expires_at']),used=False))
        return OAuthToken(access_token=access,refresh_token=refresh,expires_in=3600,scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.transaction():
            row = self.get('code',hashed(authorization_code.code))
            if not row or row['client_id'] != client.client_id or row['expires_at']<=time.time(): raise TokenError('invalid_grant')
            self.delete('code',hashed(authorization_code.code))
            return self.issue_tokens(client.client_id,row['grant'],row['scopes'])

    async def load_refresh_token(self, client, refresh_token):
        row = self.get('refresh',hashed(refresh_token))
        if not row or row['client_id'] != client.client_id: return None
        if row.get('used'):
            self.revoke_grant(row['grant'])
            return None
        if row['expires_at']<=time.time() or not self.active_grant(row['grant']): return None
        return RefreshToken(token=refresh_token,**{k:v for k,v in row.items() if k not in {'grant','used'}})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        replay = False
        with self.store.transaction():
            row = self.get('refresh',hashed(refresh_token.token))
            if not row or row['client_id'] != client.client_id or row['expires_at']<=time.time(): raise TokenError('invalid_grant')
            if row.get('used'):
                self.revoke_grant(row['grant']); replay = True
            else:
                if not set(scopes)<=set(row['scopes']): raise TokenError('invalid_scope')
                row['used']=True; self.put('refresh',hashed(refresh_token.token),row)
                result = self.issue_tokens(client.client_id,row['grant'],scopes)
        if replay: raise TokenError('invalid_grant')
        return result

    async def load_access_token(self, token):
        row = self.get('access',hashed(token))
        if not row or row['expires_at']<=time.time() or row['resource']!=self.resource or not self.active_grant(row['grant']): return None
        return AccessToken(token=token,**{k:v for k,v in row.items() if k!='grant'})

    def revoke_grant(self, key):
        grant = self.get('grant',key)
        if grant: grant['revoked']=True; self.put('grant',key,grant)

    async def revoke_token(self, token):
        row = self.get('access' if isinstance(token,AccessToken) else 'refresh',hashed(token.token))
        if row: self.revoke_grant(row['grant'])

    def grants(self, owner):
        rows = self.db.execute("SELECT key,value FROM oauth_objects WHERE kind='grant'").fetchall()
        return [dict(id=r[0],client_name=g['client_name'],scopes=g['scopes'],created_at=g['created_at'],expires_at=g['expires_at'],revoked=bool(g.get('revoked')))
                for r in rows if (g:=json.loads(r[1]))['owner']==owner]

    def challenge(self):
        return 'Bearer resource_metadata="'+self.issuer.rstrip('/')+'/.well-known/oauth-protected-resource/mcp"'

    def routes(self):
        routes = create_auth_routes(self,AnyHttpUrl(self.issuer),
            client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=SCOPES,default_scopes=SCOPES),
            revocation_options=RevocationOptions(enabled=True))
        async def as_metadata(request):
            metadata=build_metadata(AnyHttpUrl(self.issuer),None,ClientRegistrationOptions(enabled=True,valid_scopes=SCOPES,default_scopes=SCOPES),RevocationOptions(enabled=True))
            data=metadata.model_dump(mode='json',exclude_none=True)
            data['token_endpoint_auth_methods_supported']=['none','client_secret_post','client_secret_basic']
            return JSONResponse(data,headers={'Access-Control-Allow-Origin':'*','Cache-Control':'no-store'})
        routes[0]=Route('/.well-known/oauth-authorization-server',as_metadata,methods=['GET','OPTIONS'])
        # SDK 1.x RevocationRequest incorrectly requires a client_secret form field even
        # for public clients (and a body client_id for HTTP Basic). Reuse its authenticator,
        # then implement RFC7009's non-disclosing revocation response without that model.
        from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
        authenticator=ClientAuthenticator(self)
        async def revoke(request):
            try: client=await authenticator.authenticate_request(request)
            except AuthenticationError:
                return JSONResponse({'error':'invalid_client'},status_code=401)
            form=await request.form();raw=form.get('token')
            if not isinstance(raw,str) or not raw:return JSONResponse({'error':'invalid_request'},status_code=400)
            if form.get('token_type_hint') not in (None,'access_token','refresh_token'):
                return JSONResponse({'error':'unsupported_token_type'},status_code=400)
            token=await self.load_refresh_token(client,raw) if form.get('token_type_hint')=='refresh_token' else await self.load_access_token(raw)
            if token is None:
                token=await self.load_access_token(raw) if form.get('token_type_hint')=='refresh_token' else await self.load_refresh_token(client,raw)
            if token and token.client_id==client.client_id:await self.revoke_token(token)
            return Response(status_code=200,headers={'Cache-Control':'no-store','Pragma':'no-cache'})
        routes=[Route('/revoke',revoke,methods=['POST']) if r.path=='/revoke' else r for r in routes]
        # The SDK validates the PKCE exchange; explicitly bind token requests to our resource.
        for route in routes:
            original = route.app
            async def guarded(scope,receive,send,original=original):
                if scope['path']=='/token' and scope['method']=='POST':
                    body=bytearray()
                    while True:
                        msg=await receive()
                        if msg['type']=='http.disconnect':return
                        body.extend(msg.get('body',b''))
                        if len(body)>65536:return await JSONResponse({'error':'invalid_request'},status_code=413)(scope,receive,send)
                        if not msg.get('more_body'):break
                    form=parse_qs(body.decode(errors='replace'),keep_blank_values=True)
                    if 'resource' in form and form['resource'] != [self.resource]:
                        return await JSONResponse({'error':'invalid_target'},status_code=400)(scope,receive,send)
                    delivered=False
                    async def replay():
                        nonlocal delivered
                        if not delivered:
                            delivered=True;return {'type':'http.request','body':bytes(body),'more_body':False}
                        return await receive()
                    return await original(scope,replay,send)
                return await original(scope,receive,send)
            route.app=guarded

        async def metadata(request):
            return JSONResponse(dict(resource=self.resource,authorization_servers=[self.issuer],scopes_supported=SCOPES,bearer_methods_supported=['header']),headers={'Access-Control-Allow-Origin':'*','Cache-Control':'no-store'})
        routes += [Route(p,metadata,methods=['GET','OPTIONS']) for p in ('/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp')]
        routes += [Route('/ui/oauth/consent',self.consent,methods=['GET','POST']),Route('/ui/api/oauth/grants',self.manage,methods=['GET','POST'])]
        return routes

    async def manage(self, request):
        owner=self.sessions.owner(request.cookies.get(self.cookie))
        if not owner:return JSONResponse({'ok':False,'error':{'error_code':'LOGIN_REQUIRED'}},status_code=401)
        if request.method=='POST':
            if request.headers.get('origin') != self.settings.base_url:return JSONResponse({'error':'origin'},status_code=403)
            data=await request.json();grant=self.get('grant',str(data.get('id','')))
            if not grant or grant['owner']!=owner:return JSONResponse({'error':'not_found'},status_code=404)
            self.revoke_grant(data['id'])
        return JSONResponse({'ok':True,'grants':self.grants(owner)},headers={'Cache-Control':'no-store'})

    async def consent(self, request: Request):
        key=request.query_params.get('request','');pending=self.pending(key)
        callback=urlsplit(pending['params']['redirect_uri']) if pending else None
        callback_origin=f'{callback.scheme}://{callback.netloc}' if callback else ''
        headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':f"default-src 'none'; style-src 'self'; form-action 'self' {callback_origin}; frame-ancestors 'none'; base-uri 'none'"}
        if not pending:return HTMLResponse('授权请求已过期或已使用，请重新连接。',status_code=400,headers=headers)
        owner=self.sessions.owner(request.cookies.get(self.cookie))
        if request.method=='GET':
            csrf=secrets.token_urlsafe(32);pending['csrf']=hashed(csrf);pending['display_owner']=owner;self.put('pending',hashed(key),pending)
            client=await self.get_client(pending['client_id']);esc=html.escape
            login='' if owner else '<label>MCP Token（只提交给此 EasyTier 工作台）<input name="token" type="password" required autocomplete="current-password"></label>'
            body=f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>授权 MCP 连接</title><link rel="stylesheet" href="/ui/assets/style.css"><body><main class="login-card"><h1>授权 MCP 连接</h1><p>应用：{esc(client.client_name or client.client_id)}</p><p>当前身份：{esc(owner or '请登录')}</p><p>回调地址：{esc(pending['params']['redirect_uri'])}</p><p>权限：{esc(', '.join(pending['scopes']))}</p><p>允许读取研究及文件；写权限允许创建、追问、停止和清理任务。请确认是你刚发起的连接。可随时在工作台撤销。</p><form method="post"><input type="hidden" name="csrf" value="{csrf}">{login}<button name="decision" value="approve" class="primary">允许连接</button><button name="decision" value="deny" formnovalidate>拒绝</button></form></main></body></html>'''
            result=HTMLResponse(body,headers=headers)
            result.set_cookie('ego_oauth_csrf',csrf,max_age=600,httponly=True,secure=self.settings.tls_enabled,samesite='strict',path='/ui/oauth')
            return result
        if request.headers.get('origin')!=self.settings.base_url:return HTMLResponse('来源不匹配',status_code=403,headers=headers)
        form=parse_qs((await request.body()).decode(errors='replace'),keep_blank_values=True)
        csrf=form.get('csrf',[''])[0]
        if not csrf or not hmac.compare_digest(hashed(csrf),pending.get('csrf','')) or not hmac.compare_digest(csrf,request.cookies.get('ego_oauth_csrf','')):
            return HTMLResponse('CSRF 验证失败',status_code=403,headers=headers)
        if form.get('decision')==['deny']:
            self.delete('pending',hashed(key))
            return RedirectResponse(construct_redirect_uri(pending['params']['redirect_uri'],error='access_denied',state=pending['params'].get('state')),status_code=303,headers=headers)
        displayed=pending.get('display_owner')
        if displayed and owner!=displayed:return HTMLResponse('登录身份已改变，请刷新后确认。',status_code=409,headers=headers)
        owner=owner if displayed else token_owner(self.clients,form.get('token',[''])[0])
        if not owner:return HTMLResponse('Token 不正确，请返回重试。',status_code=401,headers=headers)
        if form.get('decision')!=['approve']:return HTMLResponse('无效请求',status_code=400,headers=headers)
        code=secrets.token_urlsafe(32);grant=secrets.token_urlsafe(24);params=pending['params'];now=int(time.time())
        with self.store.transaction():
            # The request is single-use even if approval arrives concurrently.
            if not self.pending(key):return HTMLResponse('请求已使用',status_code=400,headers=headers)
            client=await self.get_client(pending['client_id'])
            self.put('grant',grant,dict(owner=owner,fingerprint=hashed(self.clients[owner]),client_id=client.client_id,client_name=client.client_name or client.client_id,scopes=pending['scopes'],created_at=now,expires_at=now+SESSION_SECONDS,revoked=False))
            self.put('code',hashed(code),dict(client_id=client.client_id,scopes=pending['scopes'],expires_at=time.time()+120,code_challenge=params['code_challenge'],redirect_uri=params['redirect_uri'],redirect_uri_provided_explicitly=params['redirect_uri_provided_explicitly'],resource=self.resource,subject=owner,grant=grant))
            self.delete('pending',hashed(key))
        result=RedirectResponse(construct_redirect_uri(params['redirect_uri'],code=code,state=params.get('state')),status_code=303,headers=headers)
        result.set_cookie(self.cookie,self.sessions.issue(owner),max_age=SESSION_SECONDS,httponly=True,secure=self.settings.tls_enabled,samesite='strict',path='/ui')
        result.delete_cookie('ego_oauth_csrf',path='/ui/oauth')
        return result
