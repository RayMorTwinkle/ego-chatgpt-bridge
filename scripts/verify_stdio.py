"""Checks a real stdio Agent connection through the HTTPS daemon, without chatting."""
import asyncio
import json
from pathlib import Path

from ego_bridge.cli import DEFAULT_RUNTIME
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]


async def main():
    params=StdioServerParameters(command=str(DEFAULT_RUNTIME/'.venv/bin/ego-bridge'),args=['--runtime',str(DEFAULT_RUNTIME),'stdio'])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            initialized = await session.initialize()
            tools=await session.list_tools()
            descriptions = {tool.name: tool.description for tool in tools.tools}
            assert 'wait_query' in descriptions
            assert '10 分鐘以上' in initialized.instructions
            assert '10 分鐘以上' in descriptions['create_query']
            result=await session.call_tool('diagnose',{})
            assert result.structuredContent['ok']
            target=DEFAULT_RUNTIME/'verification/stdio-verification.json'
            target.parent.mkdir(mode=0o700,exist_ok=True)
            target.write_text(json.dumps({'ok':True,'transport':'stdio proxy -> daemon','tool_count':len(tools.tools),'diagnose':result.structuredContent},ensure_ascii=False,indent=2)+'\n')
            print('Real stdio MCP handshake, tool schemas, and structured result verified.')


if __name__=='__main__':asyncio.run(main())
