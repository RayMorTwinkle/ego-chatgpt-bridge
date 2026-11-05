"""Private, content-verified artifacts. Remote callers never supply filesystem paths or URLs to fetch."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import zipfile
from pathlib import Path

from .model import TERMINAL, BridgeError, Settings, digest, identifier, now


def filename(value: str) -> str:
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:180].strip() or "artifact.bin"


def inspect_file(path: Path, name: str, max_bytes: int) -> dict:
    if path.is_symlink() or not path.is_file():
        raise BridgeError("ARTIFACT_MISSING", "浏览器没有提供实际文件字节。", stage="artifact")
    size = path.stat().st_size
    if size == 0:
        raise BridgeError("ARTIFACT_EMPTY", "浏览器取得了 0 字节，未收到实际文件内容。", stage="artifact", context={'size_bytes':0})
    if size > max_bytes:
        raise BridgeError("ARTIFACT_TOO_LARGE", "文件超过本地接收上限。", stage="artifact",context={'size_bytes':size,'limit_bytes':max_bytes})
    with path.open("rb") as stream:
        prefix = stream.read(512)
        stream.seek(0)
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    suffix = Path(name).suffix.lower()
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    magic = {".png": b"\x89PNG\r\n\x1a\n", ".pdf": b"%PDF-", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
    if suffix in magic and not prefix.startswith(magic[suffix]):
        raise BridgeError("ARTIFACT_TYPE_MISMATCH", "下载内容的文件签名不匹配。", stage="artifact")
    if suffix in {".docx", ".xlsx", ".pptx", ".zip"}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                expected = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}.get(suffix)
                if expected and expected not in names:
                    raise ValueError("missing document")
        except (zipfile.BadZipFile, ValueError):
            raise BridgeError("ARTIFACT_TYPE_MISMATCH", "下载内容不是有效的目标文档容器。", stage="artifact")
    if suffix in {".csv", ".txt", ".md"} and prefix.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise BridgeError("ARTIFACT_TYPE_MISMATCH", "文件下载返回了 HTML 页面。", stage="artifact")
    kind = "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "document" if suffix in {".pdf", ".docx", ".xlsx", ".pptx", ".csv"} else "file"
    return dict(size_bytes=size, sha256=checksum, mime_type=mime, kind=kind)


class Artifacts:
    def __init__(self, settings: Settings, store):
        self.settings, self.store = settings, store
        self.root = settings.runtime / "artifacts"
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        store.db.executescript("""
          CREATE TABLE IF NOT EXISTS artifacts(
            id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
            source_key TEXT NOT NULL, candidate TEXT NOT NULL, filename TEXT NOT NULL, status TEXT NOT NULL,
            mime_type TEXT, kind TEXT, size_bytes INTEGER, sha256 TEXT, error TEXT, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, UNIQUE(operation_id,source_key));
        """)

    def get(self, owner: str, artifact_id: str) -> dict:
        row = self.store.db.execute("""SELECT a.* FROM artifacts a JOIN operations o ON o.id=a.operation_id
                  WHERE a.id=? AND o.owner=?""", (artifact_id, owner)).fetchone()
        if not row:
            raise BridgeError("ARTIFACT_NOT_FOUND", "找不到该调用方的产物。", stage="artifact")
        return dict(row)

    def path(self, artifact_id: str, *, temporary=False) -> Path:
        if not re.fullmatch(r"art_[0-9a-f]{32}", artifact_id):
            raise BridgeError("ARTIFACT_NOT_FOUND", "无效产物标识。", stage="artifact")
        return self.root / (artifact_id + (".part" if temporary else ".blob"))

    def public(self, row: dict) -> dict:
        result = {k: row[k] for k in ("id", "operation_id", "filename", "status", "mime_type", "kind", "size_bytes", "sha256")}
        result["artifact_id"] = result.pop("id")
        result["download_url"] = self.settings.base_url + f"/artifacts/{row['id']}/content" if row["status"] == "AVAILABLE" else None
        result["requires_authentication"] = True
        result['mcp_resource_uri']=f"ego-artifact://{row['id']}" if row['status']=='AVAILABLE' else None
        result["error"] = json.loads(row["error"]) if row["error"] else None
        return result

    def list(self, owner: str, operation_id: str) -> list[dict]:
        op = self.store.operation(operation_id)
        if op["owner"] != owner:
            raise BridgeError("ARTIFACT_NOT_FOUND", "找不到该调用方的产物。", stage="artifact")
        return [self.public(dict(r)) for r in self.store.db.execute("SELECT * FROM artifacts WHERE operation_id=? ORDER BY created_at,id", (operation_id,))]

    def available_path(self, owner: str, artifact_id: str) -> tuple[dict, Path]:
        row = self.get(owner, artifact_id)
        path = self.path(artifact_id)
        if row["status"] != "AVAILABLE":
            raise BridgeError("ARTIFACT_NOT_READY", "产物尚不可下载。", stage="artifact")
        try:
            info = inspect_file(path, row["filename"], self.settings.artifact_max_bytes)
            if info["sha256"] != row["sha256"] or info["size_bytes"] != row["size_bytes"]:
                raise BridgeError("ARTIFACT_INTEGRITY_FAILED", "本地产物内容与保存的校验值不一致。", stage="artifact")
        except BridgeError as error:
            self.store.db.execute("UPDATE artifacts SET status='FAILED',error=?,updated_at=? WHERE id=?",(json.dumps(error.data),now(),artifact_id))
            raise
        return row, path

    async def collect(self, op: dict, candidates: list[dict], adapter, browser_args: dict, user_message_id: str) -> list[dict]:
        for candidate in candidates:
            key = digest({k: candidate.get(k) for k in ("kind", "name", "index")})
            row = self.store.db.execute("SELECT * FROM artifacts WHERE operation_id=? AND source_key=?", (op["id"], key)).fetchone()
            if row:
                row = dict(row)
            else:
                art_id = identifier("art")
                self.store.db.execute("INSERT INTO artifacts(id,operation_id,source_key,candidate,filename,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                      (art_id, op["id"], key, json.dumps(candidate), filename(candidate["name"]), "PENDING", now(), now()))
                row = self.get(op["owner"], art_id)
            destination = self.path(row["id"])
            temporary = self.path(row["id"], temporary=True)
            capture_receipt = None
            try:
                if row["status"] == "AVAILABLE":
                    self.available_path(op["owner"], row["id"])
                    continue
                if row['status']=='FAILED':
                    destination.unlink(missing_ok=True)
                # An interrupted rename/DB update can be reconciled without downloading again.
                if not destination.exists():
                    temporary.unlink(missing_ok=True)
                    self.store.db.execute("UPDATE artifacts SET status='DOWNLOADING',updated_at=? WHERE id=?", (now(), row["id"]))
                    capture_receipt=await adapter.call("capture_artifact", **browser_args, user_message_id=user_message_id, candidate=candidate, destination=str(temporary.resolve()))
                    if candidate['kind']=='inline_image':
                        extension={'image/png':'.png','image/jpeg':'.jpg','image/gif':'.gif','image/webp':'.webp'}.get(capture_receipt.get('mime_type'))
                        if extension:
                            row['filename']=Path(row['filename']).stem+extension
                            self.store.db.execute('UPDATE artifacts SET filename=? WHERE id=?',(row['filename'],row['id']))
                    inspect_file(temporary, row["filename"], self.settings.artifact_max_bytes)
                    temporary.chmod(0o600)
                    with temporary.open("rb") as f:
                        os.fsync(f.fileno())
                    os.replace(temporary, destination)
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
                info = inspect_file(destination, row["filename"], self.settings.artifact_max_bytes)
                self.store.db.execute("UPDATE artifacts SET status='AVAILABLE',mime_type=?,kind=?,size_bytes=?,sha256=?,error=NULL,updated_at=? WHERE id=?",
                                      (info["mime_type"], info["kind"], info["size_bytes"], info["sha256"], now(), row["id"]))
            except BridgeError as error:
                if capture_receipt:
                    error.data['diagnostic_context']['capture_receipt']={k:v for k,v in capture_receipt.items() if k in {'transport','size_bytes','mime_type','provider_size_bytes'}}
                temporary.unlink(missing_ok=True)
                self.store.db.execute("UPDATE artifacts SET status='FAILED',error=?,updated_at=? WHERE id=?", (json.dumps(error.data), now(), row["id"]))
            except OSError as error:
                temporary.unlink(missing_ok=True)
                self.store.db.execute("UPDATE artifacts SET status='FAILED',error=?,updated_at=? WHERE id=?", (json.dumps(dict(error_code="ARTIFACT_STORAGE_FAILED",message=str(error))), now(), row["id"]))
        return self.list(op["owner"], op["id"])

    def delete_query(self, owner, query_id):
        self.store.query(owner, query_id)
        rows = list(self.store.db.execute("SELECT a.id FROM artifacts a JOIN operations o ON o.id=a.operation_id WHERE o.query_id=?", (query_id,)))
        self.store.delete(owner, query_id)  # Verifies all operations are terminal before deleting metadata.
        for row in rows:
            self.path(row[0]).unlink(missing_ok=True)
            self.path(row[0], temporary=True).unlink(missing_ok=True)
