from __future__ import annotations

import hmac
import base64
import ipaddress
import json
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp import types
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Mount, Route

from .adapter import EgoAdapter
from .model import Settings, BridgeError
from .service import Service
from .store import Store
from .auth import load_clients


class TokenAuth:
    def __init__(self, app, clients, allowed_client_cidrs):
        self.app, self.clients = app, clients
        self.networks = [ipaddress.ip_network(cidr) for cidr in allowed_client_cidrs]

    def owner(self, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        for name, expected in self.clients.items():
            if token and hmac.compare_digest(token.encode(), expected.encode()):
                return name
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            peer = ipaddress.ip_address(scope["client"][0])
            peer = getattr(peer, "ipv4_mapped", None) or peer
            permitted = any(peer in network for network in self.networks)
        except (ValueError, TypeError, KeyError):
            permitted = False
        if not permitted:
            return await JSONResponse({"ok": False, "error": {"error_code": "SOURCE_NETWORK_DENIED"}}, status_code=403)(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        ui = scope.get('path') in {'/','/ui'} or scope.get('path','').startswith('/ui/')
        owner = self.owner(headers.get("authorization", "")) if not ui else None
        if not ui and not owner:
            return await JSONResponse({"ok": False, "error": {"error_code": "UNAUTHORIZED", "message": "需要有效的调用方 Bearer token。"}}, status_code=401)(scope, receive, send)
        # Native Agent clients do not send Origin. Browser-origin access is denied.
        if not ui and "origin" in headers:
            return await JSONResponse({"ok": False, "error": {"error_code": "ORIGIN_NOT_ALLOWED"}}, status_code=403)(scope, receive, send)
        try:
            if int(headers.get("content-length", "0")) > 1024 * 1024:
                return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"error": "INVALID_CONTENT_LENGTH"}, status_code=400)(scope, receive, send)
        scope.setdefault("state", {})["owner"] = owner
        if scope.get("method") == "POST":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 1024 * 1024:
                    return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)
                if not message.get("more_body"):
                    break
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            return await self.app(scope, replay_receive, send)
        await self.app(scope, receive, send)


def build_app(settings: Settings, *, adapter=None, clients=None, store=None):
    clients = clients or load_clients(settings.runtime)
    store = store or Store(settings.runtime)
    service = Service(settings, store, adapter or EgoAdapter(settings))
    from .webui import ui_routes
    web_routes,exports = ui_routes(service,clients)
    security = TransportSecuritySettings(allowed_hosts=[item for h in settings.allowed_hosts for item in (h, f"{h}:*")], allowed_origins=[])
    mcp = FastMCP("Ego ChatGPT Bridge", stateless_http=True, json_response=True, transport_security=security)

    async def call(ctx: Context, name: str, args: dict):
        # Use request state, not a global user/session variable: sessions must not leak ownership.
        owner = ctx.request_context.request.state.owner
        return await service.dispatch(owner, name, args)

    @mcp.tool()
    async def create_query(text: str, request_id: str, ctx: Context, effort: str = "extra_high", model: str = "GPT-6", soft_timeout: float = 600, hard_timeout: float = 3600) -> dict[str, Any]:
        """在固定项目新建异步查询。request_id 是调用方作用域的幂等键。只允许 GPT-6 和 extra_high/high。"""
        return await call(ctx, "create_query", dict(text=text, request_id=request_id, effort=effort, model=model, soft_timeout=soft_timeout, hard_timeout=hard_timeout))

    @mcp.tool()
    async def continue_query(query_id: str, text: str, request_id: str, ctx: Context, effort: str = "extra_high", soft_timeout: float = 600, hard_timeout: float = 3600) -> dict[str, Any]:
        """在 Query ID 对应的同一网页会话追加消息；同会话按接收顺序执行。"""
        return await call(ctx, "continue_query", dict(query_id=query_id, text=text, request_id=request_id, effort=effort, soft_timeout=soft_timeout, hard_timeout=hard_timeout))

    @mcp.tool()
    async def get_query_status(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """读取持久化状态，不触发重发。可指定消息操作 ID。"""
        return await call(ctx, "get_query_status", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def get_query_result(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """获取回答、来源和完整性；未完成的内容不会标记 complete=true。"""
        return await call(ctx, "get_query_result", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def list_queries(ctx: Context, status: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """分页列出当前 token 所属调用方的查询。"""
        return await call(ctx, "list_queries", dict(status=status, limit=limit, offset=offset))

    @mcp.tool()
    async def health_check(ctx: Context) -> dict[str, Any]:
        """实际检查 Ego、专用空间、账户显示名称、项目及模型选择器。"""
        return await call(ctx, "health_check", {})

    @mcp.tool()
    async def diagnose(ctx: Context, query_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        """返回任务状态、错误、转换记录或服务诊断，不包含普通日志中的敏感正文。"""
        return await call(ctx, "diagnose", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def recover_session(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """对账已提交的消息；可以重开精确会话地址，但永远不会重新发送。"""
        return await call(ctx, "recover_session", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def retry_query(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """仅重试没有进入发送边界的 NEEDS_ATTENTION 消息。发送结果不确定时拒绝重试。"""
        return await call(ctx, "retry_query", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def cancel_query(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """队列中可取消；已发送消息只尽力请求网页停止，不保证撤销生成。"""
        return await call(ctx, "cancel_query", dict(query_id=query_id, message_id=message_id))

    @mcp.tool()
    async def list_messages(query_id: str, ctx: Context) -> dict[str, Any]:
        """按顺序列出该调用方的会话消息操作。"""
        return await call(ctx, "list_messages", dict(query_id=query_id))

    @mcp.tool()
    async def get_conversation_info(query_id: str, ctx: Context) -> dict[str, Any]:
        """读取持久化项目、空间和实际会话地址。"""
        return await call(ctx, "get_conversation_info", dict(query_id=query_id))

    @mcp.tool()
    async def list_artifacts(query_id: str,ctx: Context,message_id: str | None=None) -> dict[str,Any]:
        """列出回答的文件、图片产物，含状态、MIME、字节数、SHA-256 和认证下载地址。"""
        return await call(ctx,'list_artifacts',dict(query_id=query_id,message_id=message_id))

    @mcp.tool()
    async def collect_artifacts(query_id: str,ctx: Context,message_id: str | None=None) -> dict[str,Any]:
        """从已生成回答重新收集产物，只下载，不重新提问或重新生成。"""
        return await call(ctx,'collect_artifacts',dict(query_id=query_id,message_id=message_id))

    @mcp.tool()
    async def get_artifact(artifact_id: str,ctx: Context) -> dict[str,Any]:
        """读取当前调用方的产物元数据与认证下载地址。"""
        return await call(ctx,'get_artifact',dict(artifact_id=artifact_id))

    @mcp.tool(structured_output=False)
    async def read_artifact(artifact_id: str,ctx: Context,inline_image: bool=True) -> types.CallToolResult:
        """不超过 4 MiB 的 PNG/JPEG/GIF/WebP 可返回原生 MCP ImageContent；其他文件返回需认证 ResourceLink。"""
        try:
            row,path=service.artifacts.available_path(ctx.request_context.request.state.owner,artifact_id)
            meta=service.artifacts.public(row)
            contents=[types.TextContent(type='text',text=json.dumps(meta,ensure_ascii=False))]
            if inline_image and row['mime_type'] in {'image/png','image/jpeg','image/gif','image/webp'} and row['size_bytes']<=4*1024*1024:
                contents.append(types.ImageContent(type='image',data=base64.b64encode(path.read_bytes()).decode(),mimeType=row['mime_type']))
            else:
                contents.append(types.ResourceLink(type='resource_link',name=row['filename'],uri=meta['mcp_resource_uri'],mimeType=row['mime_type'],size=row['size_bytes']))
            return types.CallToolResult(content=contents)
        except BridgeError as error:
            return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text=json.dumps(error.data,ensure_ascii=False))])

    @mcp.resource('ego-artifact://{artifact_id}',mime_type='application/octet-stream',description='原始产物字节；身份沿用当前 MCP 连接，实际文件类型见产物元数据。')
    async def artifact_bytes(artifact_id: str,ctx: Context) -> bytes:
        row,path=service.artifacts.available_path(ctx.request_context.request.state.owner,artifact_id)
        return path.read_bytes()

    @mcp.tool()
    async def delete_query(query_id: str, ctx: Context) -> dict[str, Any]:
        """明确清理已终止任务的本地正文和记录；不删除 ChatGPT 远端会话。"""
        return await call(ctx, "delete_query", dict(query_id=query_id))

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            await service.start()
            try:
                yield
            finally:
                await exports.stop()
                await service.stop()
                store.close()

    async def api(request: Request):
        try:
            args = await request.json()
            if not isinstance(args, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return JSONResponse({"ok": False, "error": {"error_code": "INVALID_JSON"}}, status_code=400)
        return JSONResponse(await service.dispatch(request.state.owner, request.path_params["tool"], args))

    async def download_artifact(request: Request):
        try:
            row,path=service.artifacts.available_path(request.state.owner,request.path_params['artifact_id'])
            return FileResponse(path,filename=row['filename'],media_type=row['mime_type'],headers={
                'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff',
                'Content-Security-Policy':"default-src 'none'; sandbox",'ETag':'"'+row['sha256']+'"'})
        except BridgeError as error:
            return JSONResponse({'ok':False,'error':error.data},status_code=404 if error.data['error_code']=='ARTIFACT_NOT_FOUND' else 409)

    app = Starlette(routes=[*web_routes,Route("/api/{tool}", api, methods=["POST"]),Route('/artifacts/{artifact_id}/content',download_artifact,methods=['GET','HEAD']), Mount("/", app=mcp_app)], lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(TokenAuth, clients=clients, allowed_client_cidrs=settings.allowed_client_cidrs)
    return app
