from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path

from .model import BridgeError, Settings


class EgoAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.script = Path(__file__).with_name("browser.mjs").read_text()
        directory = Path.home() / "apps/ego-chatgpt-bridge/space-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.space_lock = (directory / f"space-{settings.space_id}.lock").open("a+")
        os.chmod(self.space_lock.name, 0o600)
        try:
            fcntl.flock(self.space_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.space_lock.close()
            raise BridgeError("SPACE_BUSY", "另一个桥接进程正在操作该 Ego 空间。", action=True)

    def close(self):
        self.space_lock.close()

    async def call(self, action: str, **arguments) -> dict:
        s = self.settings
        if not s.expected_account or (not action.startswith('debug_') and not s.project_id):
            raise BridgeError('BROWSER_NOT_CONFIGURED','请在运行目录 config.json 配置真实空间、专案标识和预期账户显示名。',stage=action,action=True,safe=action!='submit')
        payload = dict(action=action, project_id=s.project_id, project_url=s.project_url,
                       space_id=s.space_id, space_name=s.space_name, expected_account=s.expected_account, **arguments)
        script = "const input=" + json.dumps(payload, ensure_ascii=False) + ";\n" + self.script
        async with self.lock:
            try:
                process = await asyncio.create_subprocess_exec(s.ego_cli, "nodejs", stdin=asyncio.subprocess.PIPE,
                                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError:
                raise BridgeError("EGO_UNAVAILABLE", "找不到已安装的 ego-browser CLI。", stage=action, action=True, safe=action != "submit")
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(script.encode()), s.browser_timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                process.kill()
                await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BridgeError("BROWSER_TIMEOUT", "Ego 调用超时；命令可能已经产生页面效果。", stage=action, action=True)
            # Ego currently forwards console.log to stderr. Accept either channel,
            # and parse only our tagged JSON; never forward raw streams to clients.
            output = stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")
            lines = [line[len("EGO_BRIDGE_JSON:"):] for line in output.splitlines() if line.startswith("EGO_BRIDGE_JSON:")]
            if not lines:
                # Raw stderr may include URLs or query text: never forward it to callers/logs.
                raise BridgeError("EGO_CONNECTION_FAILED", "Ego 未返回结构化操作回执。", stage=action, retryable=True,
                                  action=True, context={"exit_code": process.returncode})
            result = json.loads(lines[-1])
            if not result["ok"]:
                error = result["error"]
                raise BridgeError(error["error_code"], error["message"], stage=error["stage"], action=True,
                                  context=error.get("diagnostic_context"))
            return result["value"]
