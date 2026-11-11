"""The default MCP catalog. REST and WebUI use the same service dispatch."""
import json
from typing import Annotated

from mcp import types
from mcp.server.fastmcp import Context
from pydantic import Field


INSTRUCTIONS = ('用 ask 新建研究或在原 query_id 追问。'
    '用 wait 等到完成，仍在运行时按 next 继续等待，研究可能超过 10 分钟。'
    '服务端自动核对会话和收集文件，needs_you 时按 what_to_do 处理。'
    '用 read_artifact 读取文件；回答质量和来源请自行核验。')


def done_text(value):
    # Text-only clients still need the ids to follow up and fetch files.
    lines=[value.get('answer',''),'','---',f"query_id: {value['query_id']} · 第 {value['round']} 轮 · 追问用 ask(query_id=...)"]
    lines+=[f"文件：{f['filename']}（{f['artifact_id']}，用 read_artifact 读取）" for f in value.get('files',[])]
    if value.get('sources'):lines.append('来源：'+' '.join(value['sources'][:20]))
    return '\n'.join(lines)


def envelope(value):
    text=(done_text(value) if value.get('status')=='done' and 'answer' in value
          else json.dumps(value,ensure_ascii=False,separators=(',',':')))
    return types.CallToolResult(content=[types.TextContent(type='text',text=text)],
        structuredContent=value,isError=not value.get('ok',False))


def register(mcp, dispatch, attachments_type):
    async def call(ctx, name, args):
        async def progress(elapsed, phase):
            await ctx.report_progress(progress=elapsed,message=phase)
        return envelope(await dispatch(ctx.request_context.request,name,args,progress=progress))

    @mcp.tool(structured_output=False,description='提交研究；带 query_id 可继续追问，wait_seconds 可顺便等待，省略 request_id 会自动去重。')
    async def ask(text: str, ctx: Context, query_id: str | None = None, effort: str = 'extra_high',
                  attachments: attachments_type | None = None, request_id: str | None = None,
                  wait_seconds: Annotated[float,Field(description='顺便等待的秒数，0..300。')] = 0) -> types.CallToolResult:
        return await call(ctx,'ask',dict(text=text,query_id=query_id,effort=effort,
            attachments=[file.model_dump() for file in attachments or []],request_id=request_id,wait_seconds=wait_seconds))

    @mcp.tool(structured_output=False,description='等待最终回答和文件；仍在运行时返回简短进度，继续按 next 等待。')
    async def wait(query_id: str, ctx: Context, message_id: str | None = None,
                   timeout_seconds: Annotated[float,Field(description='等待秒数，0..300，默认 50；客户端工具超时较长时可调大。')] = 50) -> types.CallToolResult:
        return await call(ctx,'wait',dict(query_id=query_id,message_id=message_id,timeout_seconds=timeout_seconds))

    @mcp.tool(structured_output=False,description='读取简短结果；verbose=true 可查看完整记录。')
    async def get(query_id: str, ctx: Context, message_id: str | None = None, verbose: bool = False) -> types.CallToolResult:
        return await call(ctx,'get',dict(query_id=query_id,message_id=message_id,verbose=verbose))

    @mcp.tool(name='list',structured_output=False,description='列出自己的会话，可按 running、done、needs_you、failed、cancelled 筛选。')
    async def list_queries(ctx: Context, status: str | None = None, limit: int = 20) -> types.CallToolResult:
        return await call(ctx,'list',dict(status=status,limit=limit))

    @mcp.tool(structured_output=False,description='取消排队任务，或尽力停止已开始的回答。')
    async def cancel(query_id: str, ctx: Context, message_id: str | None = None) -> types.CallToolResult:
        return await call(ctx,'cancel',dict(query_id=query_id,message_id=message_id))
