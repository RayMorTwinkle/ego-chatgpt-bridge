from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "NEEDS_ATTENTION", "OUTCOME_UNKNOWN", "CANCELLED"}
ACTIVE = {"PREPARING", "SUBMITTING", "WAITING_FOR_RESPONSE", "GENERATING"}
TRANSITIONS = {
    "QUEUED": {"PREPARING", "CANCELLED", "NEEDS_ATTENTION"},
    "PREPARING": {"SUBMITTING", "FAILED", "NEEDS_ATTENTION", "QUEUED"},
    "SUBMITTING": {"WAITING_FOR_RESPONSE", "GENERATING", "COMPLETED", "OUTCOME_UNKNOWN", "NEEDS_ATTENTION"},
    "WAITING_FOR_RESPONSE": {"GENERATING", "COMPLETED", "NEEDS_ATTENTION", "OUTCOME_UNKNOWN", "CANCELLED"},
    "GENERATING": {"COMPLETED", "NEEDS_ATTENTION", "OUTCOME_UNKNOWN", "CANCELLED"},
    "NEEDS_ATTENTION": {"WAITING_FOR_RESPONSE", "GENERATING", "COMPLETED", "QUEUED", "OUTCOME_UNKNOWN"},
    "OUTCOME_UNKNOWN": {"WAITING_FOR_RESPONSE", "GENERATING", "COMPLETED", "NEEDS_ATTENTION"},
}


def now() -> float:
    return time.time()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, stage: str = "validation", safe: bool = False,
                 retryable: bool = False, action: bool = False, context: dict | None = None):
        super().__init__(message)
        self.data = dict(error_code=code, message=message, stage=stage, retryable=retryable,
                         safe_to_retry=safe, user_action_required=action,
                         diagnostic_context=context or {}, recovery_suggestion=self.suggestion(code))

    @staticmethod
    def suggestion(code: str) -> str:
        if code in {"LOGIN_REQUIRED", "VERIFICATION_REQUIRED"}:
            return "在 Ego 专用空间人工完成登录或验证，再调用 recover_session。"
        if code in {"SEND_OUTCOME_UNKNOWN", "BROWSER_TIMEOUT"}:
            return "保留现场，核对指定对话与消息；调用 recover_session 对账，不要重发。"
        return "查看 diagnose 返回的阶段和状态；修复后使用对应恢复操作。"


@dataclass
class Settings:
    runtime: Path
    project_id: str = ""
    project_name: str = "Ego Research Workspace"
    space_id: int = 0
    space_name: str = "ego-research-runtime"
    ego_cli: str = "ego-browser"
    expected_account: str = ""
    default_owner: str = "default"
    max_active: int = 2
    poll_seconds: float = 4
    soft_timeout: float = 600
    hard_timeout: float = 3600
    browser_timeout: float = 55
    host: str = "127.0.0.1"
    port: int = 8766
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_client_cidrs: tuple[str, ...] = ("127.0.0.0/8",)
    artifact_max_bytes: int = 50 * 1024 * 1024

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"https://host.invalid:{self.port}".replace("host.invalid", f"[{host}]" if ":" in host else host)

    @property
    def project_url(self) -> str:
        return f"https://chatgpt.com/g/{self.project_id}/project"

    @classmethod
    def load(cls, runtime: Path) -> Settings:
        path = runtime / "config.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        for key in ("allowed_hosts", "allowed_client_cidrs"):
            if key in data:
                data[key] = tuple(data[key])
        result = cls(runtime=runtime, **data)
        if not 1 <= result.max_active <= 4:
            raise ValueError("max_active must be 1..4")
        return result
