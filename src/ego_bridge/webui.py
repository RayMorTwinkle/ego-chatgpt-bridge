from __future__ import annotations

import json
import hashlib
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse,FileResponse,RedirectResponse
from starlette.routing import Route,Mount
from starlette.staticfiles import StaticFiles

from .auth import BrowserSessions,COOKIE_NAME,HTTP_COOKIE_NAME,SESSION_SECONDS,env_values,token_owner
from .exports import ExportJobs
from .model import BridgeError
from .retention import LeasedFileResponse


def ui_routes(service,clients,sessions=None):
    settings=service.settings;values=env_values(settings.runtime)
    cookie_name=COOKIE_NAME if settings.tls_enabled else HTTP_COOKIE_NAME
    sessions=sessions or BrowserSessions(clients,values.get('WEB_SESSION_SECRET') or secrets.token_urlsafe(48),values.get('EGO_BRIDGE_OWNER') or settings.default_owner)
    jobs=ExportJobs(service)
    static=Path(__file__).with_name('web')
    allowed={'create_query','continue_query','get_query_status','get_query_result','list_queries','health_check','diagnose','recover_session','retry_query','cancel_query','list_messages','get_conversation_info','delete_query','list_artifacts','collect_artifacts','get_artifact'}

    def response(data,status=200):return JSONResponse(data,status_code=status,headers={'Cache-Control':'no-store'})

    def owner(request,*,admin=False):
        current=sessions.owner(request.cookies.get(cookie_name))
        if not current:raise BridgeError('LOGIN_REQUIRED','请使用 MCP token 登录。')
        if admin and current!=sessions.admin_owner:raise BridgeError('ADMIN_REQUIRED','此调试功能仅供主 token 的 WebUI 会话使用。')
        return current

    def origin(request):
        supplied=request.headers.get('origin')
        expected=f'{request.url.scheme}://{request.headers.get("host","")}'
        if supplied and supplied!=expected:raise BridgeError('ORIGIN_NOT_ALLOWED','请求来源不匹配。')

    async def guarded(request,callback):
        try:
            if request.method not in {'GET','HEAD'}:origin(request)
            return await callback(request)
        except BridgeError as error:
            code=error.data['error_code'];status=401 if code=='LOGIN_REQUIRED' else 403 if code in {'ADMIN_REQUIRED','ORIGIN_NOT_ALLOWED'} else 404 if code in {'EXPORT_NOT_FOUND','ARTIFACT_NOT_FOUND'} else 410 if code in {'EXPORT_EXPIRED','ARTIFACT_EXPIRED'} else 400
            return response({'ok':False,'error':error.data},status)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError):
            return response({'ok':False,'error':{'error_code':'INVALID_ARGUMENT','message':'请求格式不正确。'}},400)

    def route(path,callback,methods=['GET']):
        async def handle(request):return await guarded(request,callback)
        return Route(path,handle,methods=methods)

    async def root(request):return RedirectResponse('/ui/')
    async def index(request):return FileResponse(static/'index.html',headers={'Cache-Control':'no-store',
        'Content-Security-Policy':"default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
    async def login(request):
        data=await request.json();current=token_owner(clients,str(data.get('token','')))
        if not current:return response({'ok':False,'error':{'error_code':'INVALID_TOKEN','message':'Token 不正确。'}},401)
        result=response({'ok':True,'owner':current,'admin':current==sessions.admin_owner})
        result.set_cookie(cookie_name,sessions.issue(current),max_age=SESSION_SECONDS,httponly=True,secure=settings.tls_enabled,samesite='strict',path='/ui')
        return result
    async def logout(request):
        result=response({'ok':True});result.delete_cookie(cookie_name,path='/ui',secure=settings.tls_enabled,httponly=True,samesite='strict');return result
    async def me(request):
        current=owner(request)
        return response({'ok':True,'owner':current,'admin':current==sessions.admin_owner,'project_name':settings.project_name,
                         'retention':service.retention.policy() if hasattr(service,'retention') else None,'base_url':settings.base_url,'model':'GPT-6','preferred_effort':'extra_high','runtime_path':str(settings.runtime) if current==sessions.admin_owner else None})
    async def tool(request):
        current=owner(request);name=request.path_params['name']
        if name not in allowed:raise BridgeError('UNKNOWN_TOOL','此入口不提供该操作。')
        data=await request.json()
        if not isinstance(data,dict):raise ValueError()
        return response(await service.dispatch(current,name,data))
    async def mcp_info(request):
        current=owner(request);header='Bearer '+clients[current]
        private_configuration={'mcpServers':{'ego-research':{'url':settings.base_url+'/mcp','headers':{'Authorization':header}}}}
        public_url=settings.oauth_issuer.rstrip('/')+'/mcp' if settings.oauth_issuer else None
        public_configuration={'mcpServers':{'ego-research-public':{'url':public_url}}} if public_url else None
        return response({'ok':True,'mcp_url':settings.base_url+'/mcp','api_base_url':settings.base_url,
            'authorization':header,'tls_enabled':settings.tls_enabled,
            'oauth_mcp_url':public_url,'oauth_configuration':public_configuration,
            'connections':{'easytier':{'url':settings.base_url+'/mcp','authentication':'bearer','recommended':True,'configuration':private_configuration},
                           'public':{'url':public_url,'authentication':'oauth','recommended':False,'configuration':public_configuration}},
            'certificate_path':str(settings.runtime/'tls-cert.pem') if settings.tls_enabled else None,
            'certificate_download_url':'/ui/api/certificate' if settings.tls_enabled else None,
            'certificate_sha256':hashlib.sha256((settings.runtime/'tls-cert.pem').read_bytes()).hexdigest() if settings.tls_enabled and (settings.runtime/'tls-cert.pem').exists() else None,
            'token_file':str(settings.runtime/'.env') if current==sessions.admin_owner else None,
            'configuration':private_configuration})
    async def certificate(request):
        owner(request)
        if not settings.tls_enabled:return response({'ok':False,'error':{'error_code':'TLS_DISABLED','message':'当前使用 EasyTier 内 HTTP，无需 CA 证书。'}},404)
        return FileResponse(settings.runtime/'tls-cert.pem',filename='ego-bridge-ca.pem',media_type='application/x-pem-file',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    async def artifact(request):
        current=owner(request);row,path=service.artifacts.available_path(current,request.path_params['artifact_id'])
        image=row['mime_type'] in {'image/png','image/jpeg','image/gif','image/webp'} and request.query_params.get('download')!='1'
        return LeasedFileResponse(path,leases=service.artifacts.downloads,lease_key=row['id'],filename=row['filename'],media_type=row['mime_type'],content_disposition_type='inline' if image else 'attachment',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox"})
    async def exports(request):
        current=owner(request,admin=True)
        if request.method=='GET':return response({'ok':True,'exports':jobs.list(current)})
        data=await request.json();return response({'ok':True,'export':jobs.create(current,data.get('conversation_url'),data.get('request_id'))})
    async def export_item(request):
        current=owner(request,admin=True);job=request.path_params['export_id']
        if request.method=='DELETE':jobs.delete(current,job);return response({'ok':True})
        return response({'ok':True,'export':jobs.get(current,job)})
    async def export_download(request):
        current=owner(request,admin=True);job=request.path_params['export_id'];row=jobs.get(current,job)
        if row['status']=='EXPIRED':raise BridgeError('EXPORT_EXPIRED','归档已超过保留期，请重新发起只读导出。')
        if not row['download_url']:raise BridgeError('EXPORT_NOT_READY','ZIP 尚未就绪。')
        return LeasedFileResponse(jobs.directory(job)/'conversation.zip',leases=jobs.downloads,lease_key=job,filename=job+'.zip',media_type='application/zip',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

    return [Route('/',root),Route('/ui',root),Route('/ui/',index),Mount('/ui/assets',StaticFiles(directory=static)),
            route('/ui/api/login',login,['POST']),route('/ui/api/logout',logout,['POST']),route('/ui/api/me',me),
            route('/ui/api/tool/{name}',tool,['POST']),route('/ui/api/mcp-info',mcp_info,['POST']),route('/ui/api/certificate',certificate),
            route('/ui/api/artifacts/{artifact_id}',artifact,['GET','HEAD']),
            route('/ui/api/exports',exports,['GET','POST']),route('/ui/api/exports/{export_id}',export_item,['GET','DELETE']),
            route('/ui/api/exports/{export_id}/download',export_download)],jobs
