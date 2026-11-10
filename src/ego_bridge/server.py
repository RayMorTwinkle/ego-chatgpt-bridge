from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated, Any
from uuid import uuid4

from mcp.server.fastmcp import Context, FastMCP
from mcp import types
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .adapter import EgoAdapter
from .model import Settings, BridgeError, now
from .service import LONG_RESEARCH_GUIDANCE, Service
from .store import Store
from .auth import load_clients
from .oauth import PUBLIC_PATHS
from .uploads import MAX_FILES, MAX_REQUEST_BYTES


SUBMISSION_TOOLS = {"create_query", "continue_query"}
ATTACHMENT_ROUTES = {"/mcp"} | {prefix + tool for prefix in ("/api/", "/ui/api/tool/") for tool in SUBMISSION_TOOLS}
submission_log = logging.getLogger("ego_bridge.submission")

SoftTimeout = Annotated[float, Field(description=(
    "秒，預設 600。提交前從入列起算，進入提交後從提交時間起算。"
    "超過時回傳 soft_timeout_exceeded=true，任務繼續執行。"
))]
HardTimeout = Annotated[float, Field(description=(
    "秒，預設 3600，從進入提交時計算。觀察到逾時且尚未完成時，橋接器停止自動觀察並標記 NEEDS_ATTENTION。"
    "網頁可能仍在生成，請使用原 ID 呼叫 recover_session 對帳，再用 get_query_result 取回結果。"
))]
WaitTimeout = Annotated[float, Field(description=(
    "本次等待的秒數，範圍 0–30，預設 25。0 會立即取快照。"
    "timed_out=true 只表示本次等候未見新變化，任務會繼續，請用原 ID 和 cursor 再等。"
))]


class Attachment(BaseModel):
    filename: Annotated[str, Field(description='原始檔名，僅檔名。')]
    content_base64: Annotated[str, Field(description='原始檔案位元組的標準 Base64 編碼。')]


Attachments = Annotated[list[Attachment], Field(max_length=MAX_FILES, description='本輪附件。原始大小合計最多 5 MiB。')]


class TokenAuth:
    def __init__(self, app, clients, allowed_client_cidrs, oauth=None):
        self.oauth = oauth
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
        http_received_at = now()
        path = scope["path"]
        try:
            peer = ipaddress.ip_address(scope["client"][0])
            peer = getattr(peer, "ipv4_mapped", None) or peer
            permitted = any(peer in network for network in self.networks)
        except (ValueError, TypeError, KeyError):
            permitted = False
        public = scope.get("state", {}).get("public_gateway", False)
        if public and self.oauth:
            permitted = True
        if not permitted:
            return await JSONResponse({"ok": False, "error": {"error_code": "SOURCE_NETWORK_DENIED"}}, status_code=403)(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        ui = scope.get('path') in {'/','/ui'} or scope.get('path','').startswith('/ui/')
        oauth_route = self.oauth and path in PUBLIC_PATHS - {'/mcp'}
        token = headers.get('authorization','').removeprefix('Bearer ') if headers.get('authorization','').startswith('Bearer ') else ''
        owner = self.owner(headers.get("authorization", "")) if not ui and not public and not oauth_route else None
        scopes = None
        if not owner and not ui and not oauth_route and self.oauth:
            delegated = await self.oauth.load_access_token(token) if token else None
            if delegated:
                owner,scopes = delegated.subject,delegated.scopes
        if not ui and not oauth_route and not owner:
            return await JSONResponse({"ok": False, "error": {"error_code": "UNAUTHORIZED", "message": "需要有效的调用方 Bearer token。"}}, status_code=401, headers={"WWW-Authenticate":self.oauth.challenge()} if self.oauth else {})(scope, receive, send)
        # Native Agent clients do not send Origin. Browser-origin access is denied.
        if not ui and not oauth_route and "origin" in headers:
            return await JSONResponse({"ok": False, "error": {"error_code": "ORIGIN_NOT_ALLOWED"}}, status_code=403)(scope, receive, send)
        max_body=16384 if oauth_route else MAX_REQUEST_BYTES if path in ATTACHMENT_ROUTES else 1024*1024
        try:
            if int(headers.get("content-length", "0")) > max_body:
                return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"error": "INVALID_CONTENT_LENGTH"}, status_code=400)(scope, receive, send)
        scope.setdefault("state", {})["owner"] = owner
        scope["state"]["oauth_scopes"] = scopes
        if scopes is not None and scope['method'] in {'GET','HEAD'} and 'research:read' not in scopes:
            return await JSONResponse({'error':'insufficient_scope'},status_code=403)(scope,receive,send)
        if scope.get("method") == "POST":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > max_body:
                    return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)
                if not message.get("more_body"):
                    break
            name = path.removeprefix("/api/") if path.startswith("/api/") else None
            if path == "/mcp":
                try:
                    data = json.loads(body)
                    if data.get("method") == "tools/call":
                        candidate = data.get("params", {}).get("name")
                        name = candidate if isinstance(candidate, str) else None
                except (ValueError, TypeError, AttributeError):
                    pass
            if scopes is not None:
                from .oauth import WRITE_TOOLS
                required = 'research:write' if name in WRITE_TOOLS else 'research:read'
                if required not in scopes:
                    return await JSONResponse({'error':'insufficient_scope'},status_code=403,headers={'WWW-Authenticate':self.oauth.challenge()+', error="insufficient_scope", scope="'+required+'"'})(scope,receive,send)
            if name in SUBMISSION_TOOLS:
                trace = {"trace_id": uuid4().hex, "http_received_at": http_received_at}
                scope["state"]["submission_trace"] = trace
                route = "mcp" if path == "/mcp" else "api"
                submission_log.info("submission_received %s", json.dumps({**trace, "transport": route, "tool": name}))
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
    from .oauth import OAuthProvider
    oauth = OAuthProvider(settings,store,clients) if settings.oauth_issuer else None
    from .webui import ui_routes
    web_routes,exports = ui_routes(service,clients,sessions=oauth.sessions if oauth else None)
    oauth_routes=oauth.routes() if oauth else []
    from .retention import Retention, LeasedFileResponse
    retention=Retention(service,exports)
    service.retention=retention
    security = TransportSecuritySettings(allowed_hosts=[item for h in settings.allowed_hosts for item in (h, f"{h}:*")], allowed_origins=[])
    class BridgeMCP(FastMCP):
        async def list_tools(self):
            from .oauth import WRITE_TOOLS
            tools=await super().list_tools()
            for tool in tools:
                write=tool.name in WRITE_TOOLS
                tool.annotations=types.ToolAnnotations(readOnlyHint=not write,destructiveHint=tool.name=='delete_query',openWorldHint=write)
                if oauth:
                    schemes=[{'type':'oauth2','scopes':['research:read','research:write'] if write else ['research:read']}]
                    tool.meta={**(tool.meta or {}),'securitySchemes':schemes}
                    tool.model_extra['securitySchemes']=schemes
            return tools
    mcp = BridgeMCP(
        "Ego ChatGPT Bridge",
        instructions=(
            LONG_RESEARCH_GUIDANCE
            + "在橋接服務端的 Ego Browser 固定專案執行 GPT-6 研究。"
            "get_query_result 回傳累計快照，應替換舊快照。result_is_partial=true 表示中間結果。"
            "專案指令及記憶可能參與。health_check 是選用診斷。"
            "OUTCOME_UNKNOWN 請用 recover_session 或 diagnose，避免重送。"
            "空間 ID 屬於服務端瀏覽器。透過 read_artifact 或 resources/read 取回產物。"
        ),
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    mcp._mcp_server.version = version("ego-chatgpt-bridge")

    async def dispatch_http(request: Request, name: str, args: dict):
        # Use request state, not a global user/session variable: sessions must not leak ownership.
        owner = request.state.owner
        received = getattr(request.state, "submission_trace", None)
        if received is None:
            return await service.dispatch(owner, name, args)

        trace = {**received, "dispatch_started_at": now()}
        result = await service.dispatch(owner, name, args)
        trace["response_ready_at"] = now()
        if result.get("accepted"):
            trace["operation_created_at"] = result["created_at"]
            outcome = {**trace, "query_id": result["query_id"], "message_id": result["message_id"]}
        else:
            outcome = {**trace, "error_code": (result.get("error") or {}).get("error_code")}
        submission_log.info("submission_result %s", json.dumps(outcome))
        return {**result, "submission_trace": trace}

    async def call(ctx: Context, name: str, args: dict):
        return await dispatch_http(ctx.request_context.request, name, args)

    @mcp.tool(description=LONG_RESEARCH_GUIDANCE + (
        "在固定專案新建非同步查詢。request_id 是呼叫方的冪等鍵。"
        "支援 GPT-6 的 extra_high 與 high。context_mode=request_only_best_effort 可附加範圍約束。可用 attachments 附加檔案。"
    ))
    async def create_query(text: str, request_id: str, ctx: Context, effort: str = "extra_high", model: str = "GPT-6", soft_timeout: SoftTimeout = 600, hard_timeout: HardTimeout = 3600, context_mode: str = "project", attachments: Attachments | None = None) -> dict[str, Any]:
        return await call(ctx, "create_query", dict(text=text, request_id=request_id, effort=effort, model=model, soft_timeout=soft_timeout, hard_timeout=hard_timeout, context_mode=context_mode, attachments=[file.model_dump() for file in attachments or []]))

    @mcp.tool()
    async def continue_query(query_id: str, text: str, request_id: str, ctx: Context, effort: str = "extra_high", soft_timeout: SoftTimeout = 600, hard_timeout: HardTimeout = 3600, context_mode: str = "project", attachments: Attachments | None = None) -> dict[str, Any]:
        """在 Query ID 对应的同一网页会话追加消息；同会话按接收顺序执行。可用 attachments 附加文件。"""
        return await call(ctx, "continue_query", dict(query_id=query_id, text=text, request_id=request_id, effort=effort, soft_timeout=soft_timeout, hard_timeout=hard_timeout, context_mode=context_mode, attachments=[file.model_dump() for file in attachments or []]))

    @mcp.tool()
    async def get_query_status(query_id: str, ctx: Context, message_id: str | None = None) -> dict[str, Any]:
        """读取持久化状态，不触发重发。可指定消息操作 ID。"""
        return await call(ctx, "get_query_status", dict(query_id=query_id, message_id=message_id))

    def result_envelope(value):
        # Structured consumers get one full payload. Text-only consumers receive
        # the full payload too; use include_content=False / wait_query to keep it small.
        return types.CallToolResult(content=[types.TextContent(type='text',text=json.dumps(value,ensure_ascii=False))],
            structuredContent=value,isError=not value.get('ok',False))

    @mcp.tool(structured_output=False)
    async def get_query_result(query_id: str, ctx: Context, message_id: str | None = None,
            include_content: bool = True, include_outputs: bool = False, compact: bool = True,
            content_format: str = "markdown") -> types.CallToolResult:
        """取累计快照（替换旧快照，勿拼接）。默认保留 Markdown 表格/链接且不重复 outputs。complete 仅表示生成/交付完成，语义需调用方验收；中间结果 result_is_partial=true。轮询优先 wait_query。选择 structuredContent 或 JSON content 其中一个渠道，勿重复读取。"""
        return result_envelope(await call(ctx, "get_query_result", dict(query_id=query_id,message_id=message_id,
            include_content=include_content,include_outputs=include_outputs,compact=compact,content_format=content_format)))

    @mcp.tool(structured_output=False)
    async def wait_query(query_id: str, ctx: Context, message_id: str | None = None, cursor: str | None = None,
            timeout_seconds: WaitTimeout = 25, include_content: bool = False, content_format: str = "markdown") -> types.CallToolResult:
        """等待狀態或內容變化，單次最長 30 秒。帶回上次 cursor。即使本次沒有新變化，progress 仍提供最近觀測時間、正文長度及來源數。依 next_action 繼續原任務，僅 complete=true 後取得最終結果。include_content=true 時回傳累計快照。"""
        return result_envelope(await call(ctx,'wait_query',dict(query_id=query_id,message_id=message_id,cursor=cursor,
            timeout_seconds=timeout_seconds,include_content=include_content,content_format=content_format)))

    @mcp.tool()
    async def list_queries(ctx: Context, status: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """分页列出当前 token 所属调用方的查询。"""
        return await call(ctx, "list_queries", dict(status=status, limit=limit, offset=offset))

    @mcp.tool()
    async def health_check(ctx: Context) -> dict[str, Any]:
        """可选的服务端浏览器诊断，检查空间、账户、专案及模型选择器；会操作网页控件，可能比普通状态查询慢。不是 create_query 的准入门槛，失败不代表服务整体不可用；每个发送操作仍会独立核验。不要高频调用。"""
        return await call(ctx, "health_check", {})

    @mcp.tool()
    async def diagnose(ctx: Context, query_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
        """返回任务状态、具体消息关联差异、转换记录和服务端执行环境。空间编号仅在桥接服务器有效，不要用客户端本机 ego-browser 查找。持续恢复失败时把此证据交给维护者；诊断不返回完整提示词或浏览器凭据。"""
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
            retention.start()
            try:
                yield
            finally:
                await retention.stop()
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
        return JSONResponse(await dispatch_http(request, request.path_params["tool"], args))

    async def download_artifact(request: Request):
        try:
            row,path=service.artifacts.available_path(request.state.owner,request.path_params['artifact_id'])
            return LeasedFileResponse(path,leases=service.artifacts.downloads,lease_key=row['id'],filename=row['filename'],media_type=row['mime_type'],headers={
                'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff',
                'Content-Security-Policy':"default-src 'none'; sandbox",'ETag':'"'+row['sha256']+'"'})
        except BridgeError as error:
            return JSONResponse({'ok':False,'error':error.data},status_code=404 if error.data['error_code']=='ARTIFACT_NOT_FOUND' else 410 if error.data['error_code']=='ARTIFACT_EXPIRED' else 409)

    app = Starlette(routes=[*oauth_routes,*web_routes,Route("/api/{tool}", api, methods=["POST"]),Route('/artifacts/{artifact_id}/content',download_artifact,methods=['GET','HEAD']), Mount("/", app=mcp_app)], lifespan=lifespan)
    app.state.service = service
    app.state.oauth = oauth
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(TokenAuth, clients=clients, allowed_client_cidrs=settings.allowed_client_cidrs, oauth=oauth)
    return app
