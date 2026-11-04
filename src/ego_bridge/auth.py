"""Private .env credentials and one-year, token-bound browser sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from dotenv import dotenv_values, set_key

from .model import BridgeError

SESSION_SECONDS = 365 * 24 * 60 * 60
COOKIE_NAME = "ego_bridge_session"


def env_values(runtime: Path) -> dict:
    return dict(dotenv_values(runtime / ".env", interpolate=False))


def load_clients(runtime: Path) -> dict[str,str]:
    values = env_values(runtime)
    if values.get("EGO_BRIDGE_TOKEN"):
        clients = json.loads(values.get("EGO_BRIDGE_CLIENT_TOKENS_JSON") or "{}")
        clients[values.get("EGO_BRIDGE_OWNER") or "default"] = values["EGO_BRIDGE_TOKEN"]
        return clients
    # Read-only compatibility for explicit migration from the original release.
    old = runtime / "clients.json"
    if old.exists():
        return json.loads(old.read_text())
    raise BridgeError("TOKEN_NOT_CONFIGURED", "先运行 init，在运行目录的 .env 配置 token。", action=True)


def save_clients(runtime: Path, clients: dict, owner: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", owner):
        raise ValueError("Invalid owner name")
    path = runtime / ".env"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    set_key(str(path), "EGO_BRIDGE_OWNER", owner)
    set_key(str(path), "EGO_BRIDGE_TOKEN", clients[owner])
    set_key(str(path), "EGO_BRIDGE_CLIENT_TOKENS_JSON", json.dumps({k:v for k,v in clients.items() if k!=owner}, separators=(",", ":")))
    if not env_values(runtime).get("WEB_SESSION_SECRET"):
        set_key(str(path), "WEB_SESSION_SECRET", secrets.token_urlsafe(48))
    path.chmod(0o600)


def token_owner(clients: dict, token: str) -> str | None:
    for owner, expected in clients.items():
        if token and hmac.compare_digest(token.encode(), expected.encode()):
            return owner
    return None


class BrowserSessions:
    def __init__(self, clients: dict, secret: str, admin_owner: str):
        self.clients, self.secret, self.admin_owner = clients, secret.encode(), admin_owner

    def issue(self, owner: str) -> str:
        data = {"owner":owner,"expires":int(time.time())+SESSION_SECONDS,
                "token_hash":hashlib.sha256(self.clients[owner].encode()).hexdigest()}
        encoded = base64.urlsafe_b64encode(json.dumps(data,separators=(",",":")).encode()).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).hexdigest()
        return encoded.decode()+"."+signature

    def owner(self, cookie: str | None) -> str | None:
        try:
            encoded, signature = cookie.rsplit(".",1)
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,expected):return None
            data=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
            owner=data["owner"]
            if data["expires"]<time.time() or owner not in self.clients:return None
            current=hashlib.sha256(self.clients[owner].encode()).hexdigest()
            return owner if hmac.compare_digest(data["token_hash"],current) else None
        except (ValueError,TypeError,KeyError,AttributeError):
            return None
