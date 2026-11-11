from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .model import ACTIVE, TERMINAL, TRANSITIONS, BridgeError, digest, identifier, now, provisional_conversation
from .uploads import MAX_PENDING_BYTES


class Store:
    def __init__(self, directory: Path, *, lock: bool = True):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.lock_file = None
        if lock:
            self.lock_file = (directory / "service.lock").open("a+")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock_file.close()
                raise BridgeError("RUNTIME_BUSY", "另一个服务进程已持有此运行目录。", action=True)
        self.db = sqlite3.connect(directory / "bridge.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BridgeError("DATABASE_CORRUPT", "数据库校验失败；请恢复备份，不要自动重建。", action=True)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS queries(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_url TEXT, conversation_id TEXT,
            project_id TEXT NOT NULL, space_id INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS operations(
            id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES queries(id), owner TEXT NOT NULL,
            request_id TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
            sequence INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
            page_label TEXT, baseline TEXT, observed TEXT, result TEXT, error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, submitted_at REAL,
            UNIQUE(owner,request_id), UNIQUE(query_id,sequence));
          CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
            previous TEXT, status TEXT NOT NULL, at REAL NOT NULL, detail TEXT);
          CREATE TABLE IF NOT EXISTS operation_uploads(
            operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL, content BLOB NOT NULL, PRIMARY KEY(operation_id,filename));
          CREATE INDEX IF NOT EXISTS operation_status ON operations(status,created_at);
        """)
        for table, column in (("queries", "chat_title"), ("operations", "auto_retry")):
            if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        os.chmod(directory / "bridge.sqlite3", 0o600)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()
        if self.lock_file:
            self.lock_file.close()

    def query(self, owner: str, query_id: str) -> dict:
        row = self.db.execute("SELECT * FROM queries WHERE id=? AND owner=?", (query_id, owner)).fetchone()
        if not row:
            raise BridgeError("QUERY_NOT_FOUND", "找不到该调用方的 Query ID。")
        return dict(row)

    def operation(self, operation_id: str) -> dict:
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            raise BridgeError("OPERATION_NOT_FOUND", "找不到消息操作。")
        result = dict(row)
        for key in ("payload", "baseline", "observed", "result", "error", "auto_retry"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def latest(self, owner: str, query_id: str) -> dict:
        self.query(owner, query_id)
        row = self.db.execute("SELECT id FROM operations WHERE query_id=? ORDER BY sequence DESC LIMIT 1", (query_id,)).fetchone()
        return self.operation(row[0])

    def enqueue(self, owner: str, request_id: str, payload: dict, project_id: str, space_id: int,
                query_id: str | None = None, *, uploads=()) -> dict:
        kind = "continue" if query_id else "create"
        fingerprint = digest(dict(kind=kind, query_id=query_id, payload=payload))
        with self.transaction():
            old = self.db.execute("SELECT id,fingerprint FROM operations WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise BridgeError("IDEMPOTENCY_CONFLICT", "相同幂等键对应不同请求。")
                return self.operation(old["id"])
            queued = self.db.execute("SELECT COUNT(*) FROM operations WHERE owner=? AND status='QUEUED'", (owner,)).fetchone()[0]
            if queued >= 100:
                raise BridgeError("QUEUE_FULL", "该调用方已有 100 条待处理消息。", safe=True, retryable=True)
            if uploads:
                pending = self.db.execute('SELECT COALESCE(SUM(length(content)),0) FROM operation_uploads').fetchone()[0]
                if pending + sum(len(data) for _, data in uploads) > MAX_PENDING_BYTES:
                    raise BridgeError('UPLOAD_QUEUE_FULL', '待处理附件暂存已满，请等候或清理失败任务后重试。', safe=True, retryable=True)
            t = now()
            if query_id:
                q = self.query(owner, query_id)
                last = self.latest(owner, query_id)
                if last["status"] not in {"COMPLETED", "QUEUED", *ACTIVE}:
                    raise BridgeError("QUERY_NOT_APPENDABLE", "先处理该会话的不确定或失败状态。", action=True)
                sequence = last["sequence"] + 1
            else:
                query_id = identifier("q")
                sequence = 1
                self.db.execute("INSERT INTO queries(id,owner,conversation_url,conversation_id,project_id,space_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (query_id, owner, None, None, project_id, space_id, t, t))
            op_id = identifier("msg")
            self.db.execute("""INSERT INTO operations(id,query_id,owner,request_id,kind,fingerprint,sequence,status,payload,created_at,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (op_id, query_id, owner, request_id, kind, fingerprint, sequence, "QUEUED", json.dumps(payload, ensure_ascii=False), t, t))
            self.db.executemany('INSERT INTO operation_uploads VALUES(?,?,?)', ((op_id, name, data) for name, data in uploads))
            self.db.execute("INSERT INTO events(operation_id,status,at) VALUES(?,?,?)", (op_id, "QUEUED", t))
            self.db.execute("UPDATE queries SET updated_at=? WHERE id=?", (t, query_id))
        return self.operation(op_id)

    def transition(self, op_id: str, status: str, *, error: dict | None = None, result: dict | None = None):
        with self.transaction():
            op = self.operation(op_id)
            if error:
                error = {**error, "query_id": op["query_id"], "request_id": op["request_id"], "message_id": op_id}
            if status != op["status"] and status not in TRANSITIONS.get(op["status"], set()):
                raise BridgeError("INVALID_STATE_TRANSITION", f"不允许 {op['status']} → {status}")
            t = now()
            self.db.execute("UPDATE operations SET status=?,error=?,result=COALESCE(?,result),updated_at=?,submitted_at=CASE WHEN ?='SUBMITTING' THEN ? ELSE submitted_at END WHERE id=?",
                            (status, json.dumps(error) if error else None, json.dumps(result, ensure_ascii=False) if result else None, t, status, t, op_id))
            if status in {'SUBMITTING', 'CANCELLED', 'FAILED'}:
                self.db.execute('DELETE FROM operation_uploads WHERE operation_id=?', (op_id,))
            if status != op["status"]:
                self.db.execute("INSERT INTO events(operation_id,previous,status,at,detail) VALUES(?,?,?,?,?)", (op_id, op["status"], status, t, json.dumps(error) if error else None))
            self.db.execute("UPDATE queries SET updated_at=? WHERE id=?", (t, op["query_id"]))

    def patch(self, op_id: str, **fields):
        allowed = {"page_label", "baseline", "observed", "submitted_at", "auto_retry"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported operation fields")
        for key in {"baseline", "observed", "auto_retry"} & fields.keys():
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = now()
        self.db.execute(f"UPDATE operations SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), op_id))

    def bind_conversation(self, query_id: str, url: str, conversation_id: str, space_id: int | None = None, *, promote_provisional=False):
        row = self.db.execute("SELECT conversation_url,conversation_id FROM queries WHERE id=?", (query_id,)).fetchone()
        allowed_promotion=promote_provisional and provisional_conversation(row[1]) and not provisional_conversation(conversation_id)
        if row[0] and row[0] != url and not allowed_promotion:
            raise BridgeError("CONVERSATION_MISMATCH", "会话地址与持久化映射不一致。", action=True)
        self.db.execute("UPDATE queries SET conversation_url=?,conversation_id=?,space_id=COALESCE(?,space_id),updated_at=? WHERE id=?", (url, conversation_id, space_id, now(), query_id))

    def candidates(self) -> list[dict]:
        rows = self.db.execute("""SELECT o.id FROM operations o WHERE o.status='QUEUED'
           AND NOT EXISTS(SELECT 1 FROM operations p WHERE p.query_id=o.query_id AND p.sequence<o.sequence AND p.status!='COMPLETED')
           ORDER BY o.created_at""").fetchall()
        return [self.operation(r[0]) for r in rows]

    def active(self) -> list[dict]:
        rows = self.db.execute("SELECT id FROM operations WHERE status IN ('PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING') ORDER BY created_at").fetchall()
        return [self.operation(r[0]) for r in rows]

    def uploads(self, op_id: str) -> dict[str, bytes]:
        return dict(self.db.execute('SELECT filename,content FROM operation_uploads WHERE operation_id=?', (op_id,)))

    def list_queries(self, owner: str, status: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        rows = self.db.execute("""SELECT q.*,o.status,o.id AS message_id FROM queries q JOIN operations o ON o.query_id=q.id
           WHERE q.owner=? AND o.sequence=(SELECT MAX(p.sequence) FROM operations p WHERE p.query_id=q.id)
           AND (? IS NULL OR o.status=?) ORDER BY q.created_at DESC LIMIT ? OFFSET ?""", (owner, status, status, limit, offset)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            first=self.db.execute('SELECT payload FROM operations WHERE query_id=? ORDER BY sequence LIMIT 1',(item['id'],)).fetchone()
            item['title']=json.loads(first[0])['text'][:160] if first else item['id']
            result.append(item)
        return result

    def events(self, query_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT e.* FROM events e JOIN operations o ON o.id=e.operation_id WHERE o.query_id=? ORDER BY e.id DESC LIMIT 50", (query_id,))]

    def backup(self, destination: Path):
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
        os.chmod(destination, 0o600)

    def delete(self, owner: str, query_id: str):
        self.query(owner, query_id)
        ops = [self.operation(r[0]) for r in self.db.execute("SELECT id FROM operations WHERE query_id=?", (query_id,))]
        if any(o["status"] not in TERMINAL for o in ops):
            raise BridgeError("QUERY_BUSY", "任务未结束，不能清理。")
        with self.transaction():
            self.db.execute("DELETE FROM events WHERE operation_id IN (SELECT id FROM operations WHERE query_id=?)", (query_id,))
            self.db.execute("DELETE FROM operations WHERE query_id=?", (query_id,))
            self.db.execute("DELETE FROM queries WHERE id=?", (query_id,))
