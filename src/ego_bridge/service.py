from __future__ import annotations

import asyncio
import contextlib
import json
import re
from urllib.parse import urlparse

from .model import ACTIVE, TERMINAL, BridgeError, Settings, now, provisional_conversation
from .store import Store
from .artifacts import Artifacts


def normalize(text: str) -> str:
    return " ".join(text.split())


def history(turns: list[dict]) -> list[dict]:
    return [{"user_id": t.get("user_id"), "user_text": normalize(t.get("user_text", "")),
             "assistant_id": t.get("assistant_id"), "assistant_text": normalize(t.get("assistant_text", ""))} for t in turns]


class Service:
    def __init__(self, settings: Settings, store: Store, adapter):
        self.settings, self.store, self.adapter = settings, store, adapter
        self.artifacts = Artifacts(settings, store)
        self.worker = None
        self.wake = asyncio.Event()
        self.control = asyncio.Lock()
        self.last_worker_error = None

    async def start(self):
        for op in self.store.active():
            if op["status"] == "PREPARING":
                self.store.transition(op["id"], "QUEUED")
            elif op["status"] == "SUBMITTING":
                self.store.transition(op["id"], "OUTCOME_UNKNOWN", error=BridgeError("SEND_OUTCOME_UNKNOWN", "服务在发送边界中断，先核对页面。", action=True).data)
        self.worker = asyncio.create_task(self.run(), name="ego-bridge-worker")

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if hasattr(self.adapter, "close"):
            self.adapter.close()

    def view(self, op: dict) -> dict:
        q = self.store.query(op["owner"], op["query_id"])
        return dict(query_id=q["id"], message_id=op["id"], request_id=op["request_id"], sequence=op["sequence"], status=op["status"],
                    conversation_id=q["conversation_id"], conversation_url=q["conversation_url"],
                    project_identifier=q["project_id"], space_identifier=q["space_id"],
                    runtime_space_identifier=self.settings.space_id,
                    requested_model="GPT-6", requested_effort=op["payload"]["effort"],
                    observed=op["observed"], last_error=op["error"], blocked_predecessor=self.blocked_predecessor(op),
                    created_at=op["created_at"], updated_at=op["updated_at"])

    async def dispatch(self, owner: str, tool: str, args: dict) -> dict:
        try:
            return await self._dispatch(owner, tool, args)
        except BridgeError as e:
            e.data.update(query_id=args.get("query_id"), request_id=args.get("request_id"))
            return {"ok": False, "error": e.data}
        except (TypeError, ValueError, KeyError) as e:
            return {"ok": False, "error": BridgeError("INVALID_ARGUMENT", "参数格式不正确。", context={"type": type(e).__name__}).data}

    async def _dispatch(self, owner: str, tool: str, args: dict) -> dict:
        if tool == 'get_artifact':
            return {'ok':True,'artifact':self.artifacts.public(self.artifacts.get(owner,args.get('artifact_id')))}
        if tool in {"create_query", "continue_query"}:
            text, request = args.get("text"), args.get("request_id")
            if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                raise BridgeError("INVALID_ARGUMENT", "text 必须为 1..100000 字符的非空字符串。")
            if not isinstance(request, str) or not 1 <= len(request) <= 200:
                raise BridgeError("INVALID_ARGUMENT", "必须提供 1..200 字符 request_id 幂等键。")
            if args.get("model", "GPT-6") != "GPT-6":
                raise BridgeError("MODEL_NOT_ALLOWED", "只允许默认 GPT-6，不使用 5.6 或 Pro。")
            effort = args.get("effort", "extra_high")
            if effort not in {"extra_high", "high"}:
                raise BridgeError("EFFORT_NOT_ALLOWED", "只允许 extra_high 或 high。")
            soft, hard = args.get("soft_timeout", self.settings.soft_timeout), args.get("hard_timeout", self.settings.hard_timeout)
            if not isinstance(soft, (int, float)) or not isinstance(hard, (int, float)) or not 1 <= soft <= hard <= 86400:
                raise BridgeError("INVALID_TIMEOUT", "需要 1 <= soft_timeout <= hard_timeout <= 86400。")
            qid = args.get("query_id") if tool == "continue_query" else None
            if tool == "continue_query" and not qid:
                raise BridgeError("INVALID_ARGUMENT", "continue_query 需要 query_id。")
            payload = dict(text=text, effort=effort, soft_timeout=soft, hard_timeout=hard)
            op = self.store.enqueue(owner, request, payload, self.settings.project_id, self.settings.space_id, qid)
            self.wake.set()
            return {"ok": True, **self.view(op)}
        if tool == "list_queries":
            limit, offset = args.get("limit", 20), args.get("offset", 0)
            if not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
                raise BridgeError("INVALID_ARGUMENT", "分页需要 limit=1..100、offset>=0。")
            rows = self.store.list_queries(owner, args.get("status"), limit, offset)
            return {"ok": True, "queries": rows, "next_offset": offset + limit if len(rows) == limit else None}
        if tool == "health_check":
            async with self.control:
                health = await self.adapter.call("health")
            return {"ok": True, "runtime": health, "active_operations": len(self.store.active()), "worker_error": self.last_worker_error}
        if tool == "diagnose" and not args.get("query_id"):
            return {"ok": True, "worker_running": bool(self.worker and not self.worker.done()), "last_worker_error": self.last_worker_error,
                    "active_count": len(self.store.active()), "model_policy": "GPT-6 / extra_high, high only", "max_active": self.settings.max_active}
        qid = args.get("query_id")
        q = self.store.query(owner, qid)
        op = self.store.operation(args["message_id"]) if args.get("message_id") else self.store.latest(owner, qid)
        if op["owner"] != owner or op["query_id"] != qid:
            raise BridgeError("OPERATION_NOT_FOUND", "该消息不属于指定会话。")
        if tool == "get_query_status":
            elapsed = now() - (op["submitted_at"] or op["created_at"])
            return {"ok": True, **self.view(op), "soft_timeout_exceeded": elapsed > op["payload"]["soft_timeout"] and op["status"] not in TERMINAL}
        if tool == "get_query_result":
            result = dict(op["result"] or {})
            result.pop("history", None)
            if op['result'] is not None:
                result['artifacts']=self.artifacts.list(owner,op['id'])
                if 'outputs' in result:
                    result['outputs']=self.outputs(result)
            generation_complete=bool(op['result'] and op['result'].get('completeness_signal')=='complete')
            delivery_complete=bool(generation_complete and result.get('delivery_complete',True) and all(a['status']=='AVAILABLE' for a in result.get('artifacts',[])))
            return {"ok": True, **self.view(op), "generation_complete":generation_complete,"delivery_complete":delivery_complete,
                    "complete": op["status"] == "COMPLETED" and delivery_complete, "result": result or None}
        if tool == 'list_artifacts':
            return {'ok':True,'query_id':qid,'message_id':op['id'],'artifacts':self.artifacts.list(owner,op['id'])}
        if tool == 'collect_artifacts':
            if op['status'] not in {'COMPLETED','NEEDS_ATTENTION'} or not op['result'] or op['result'].get('completeness_signal')!='complete':
                raise BridgeError('RESPONSE_NOT_COMPLETE','只有已确认生成完成的回答能单独收集产物。',stage='artifact')
            if provisional_conversation(q['conversation_id']):
                raise BridgeError('CONVERSATION_ID_PENDING','先用 recover_session 核验正式会话地址，再收集产物。',stage='artifact',action=True)
            async with self.control:
                response=await self.adapter.call('recover',**self.browser_args(op))
                self.store.patch(op['id'],page_label=response['page_label'])
                matches=[t for t in response['state']['turns'] if t.get('user_id')==op['result'].get('user_message_id') and normalize(t.get('user_text',''))==normalize(self.browser_args(op)['send_text'])]
                if len(matches)!=1 or matches[0].get('assistant_id')!=op['result'].get('assistant_message_id'):
                    raise BridgeError('ARTIFACT_ASSOCIATION_FAILED','产物消息身份与原回答不一致。',stage='artifact',action=True)
                self.store.bind_conversation(qid,q['conversation_url'],q['conversation_id'],self.settings.space_id)
                result=await self.package_result(self.store.operation(op['id']),matches[0],dict(op['result']))
                self.store.transition(op['id'],'COMPLETED' if result['delivery_complete'] else op['status'],result=result,
                                      error=None if result['delivery_complete'] else BridgeError('ARTIFACT_CAPTURE_FAILED','至少一个产物尚未拿到有效字节。',stage='artifact',action=True).data)
                if result['delivery_complete']:
                    try:
                        closed=await self.adapter.call('close',**self.browser_args(self.store.operation(op['id'])))
                        if closed.get('closed'):self.store.patch(op['id'],page_label=None)
                    except Exception:pass  # Byte delivery is durable; cleanup never resends.
            return {'ok':True,**self.view(self.store.operation(op['id'])),'delivery_complete':result['delivery_complete'],'artifacts':result['artifacts']}
        if tool == "list_messages":
            return {"ok": True, "messages": [{**self.view(self.store.operation(r[0])), "text": self.store.operation(r[0])["payload"]["text"]}
                for r in self.store.db.execute("SELECT id FROM operations WHERE query_id=? ORDER BY sequence LIMIT 1000", (qid,))]}
        if tool == "get_conversation_info":
            return {"ok": True, "conversation": q}
        if tool == "diagnose":
            return {"ok": True, **self.view(op), "recent_events": self.store.events(qid),
                    "recovery_metadata": {"page_label_hint": op["page_label"], "has_submission_intent": op["submitted_at"] is not None,
                                          "has_baseline": op["baseline"] is not None},
                    "blocked_predecessor": self.blocked_predecessor(op)}
        if tool == "recover_session":
            async with self.control:
                if op["status"] in {"OUTCOME_UNKNOWN", "NEEDS_ATTENTION", "WAITING_FOR_RESPONSE", "GENERATING", "SUBMITTING"}:
                    await self.observe(op, recover=True)
            return {"ok": True, **self.view(self.store.operation(op["id"]))}
        if tool == "retry_query":
            if op["status"] != "NEEDS_ATTENTION" or op["submitted_at"] is not None:
                raise BridgeError("UNSAFE_RETRY", "只允许尚未进入发送边界的人工修复任务重试；已发送或结果不确定不能重发。", action=True)
            self.store.transition(op["id"], "QUEUED")
            self.wake.set()
            return {"ok": True, **self.view(self.store.operation(op["id"]))}
        if tool == "cancel_query":
            async with self.control:
                op = self.store.operation(op["id"])
                if op["status"] == "QUEUED":
                    self.store.transition(op["id"], "CANCELLED")
                    return {"ok": True, "cancelled": True, "best_effort": False, **self.view(self.store.operation(op["id"]))}
                if op["status"] in {"WAITING_FOR_RESPONSE", "GENERATING"}:
                    response = await self.adapter.call("cancel", **self.browser_args(op))
                    if response["stopped"]:
                        self.store.transition(op["id"], op["status"], error=BridgeError("CANCEL_REQUESTED", "已请求停止，等待页面确认。", stage="cancel").data)
                    return {"ok": True, "cancelled": False, "stop_requested": response["stopped"], "best_effort": True, **self.view(self.store.operation(op["id"]))}
                return {"ok": True, "cancelled": op["status"] == "CANCELLED", "best_effort": True, **self.view(op)}
        if tool == "delete_query":
            self.artifacts.delete_query(owner, qid)
            return {"ok": True, "local_records_deleted": True, "remote_conversation_deleted": False}
        raise BridgeError("UNKNOWN_TOOL", "未知工具名称。")

    def blocked_predecessor(self, op):
        row = self.store.db.execute("SELECT id,status FROM operations WHERE query_id=? AND sequence<? AND status!='COMPLETED' ORDER BY sequence LIMIT 1", (op["query_id"], op["sequence"])).fetchone()
        return dict(row) if row else None

    def browser_args(self, op):
        q = self.store.query(op["owner"], op["query_id"])
        return dict(page_label=op["page_label"] if q['space_id']==self.settings.space_id else None, conversation_url=None if provisional_conversation(q['conversation_id']) else q["conversation_url"],
                    send_text=op["payload"]["text"] + f"\n\n[ego-bridge request:{op['id']}]", effort=(op["observed"] or {}).get("effort", op["payload"]["effort"]), baseline=op["baseline"],
                    target_user_message_id=(op['result'] or {}).get('user_message_id'),target_assistant_message_id=(op['result'] or {}).get('assistant_message_id'))

    async def run(self):
        while True:
            try:
                async with self.control:
                    # Polling only reads known pages. A failed observation never triggers a send.
                    for op in self.store.active():
                        if op["status"] in {"WAITING_FOR_RESPONSE", "GENERATING"}:
                            await self.observe(op)
                    occupied = self.store.db.execute("SELECT COUNT(*) FROM operations WHERE status IN ('PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING') OR (status IN ('NEEDS_ATTENTION','OUTCOME_UNKNOWN') AND submitted_at IS NOT NULL)").fetchone()[0]
                    for op in self.store.candidates()[:max(0, self.settings.max_active - occupied)]:
                        await self.execute(op)
                self.last_worker_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_worker_error = {"error_code": "WORKER_FAILED", "type": type(error).__name__}
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), self.settings.poll_seconds)
            except TimeoutError:
                pass

    async def execute(self, op):
        op_id = op["id"]
        try:
            self.store.transition(op_id, "PREPARING")
            args = self.browser_args(op)
            if op['kind']=='create':
                args['discardable_drafts']=[json.loads(r['payload'])['text']+f"\n\n[ego-bridge request:{r['id']}]" for r in self.store.db.execute(
                    "SELECT id,payload FROM operations WHERE owner=? AND submitted_at IS NULL AND status IN ('NEEDS_ATTENTION','CANCELLED')",(op['owner'],))]
            if op["sequence"] > 1:
                prev = self.store.db.execute("SELECT id FROM operations WHERE query_id=? AND sequence=?", (op["query_id"], op["sequence"] - 1)).fetchone()
                previous = self.store.operation(prev[0])
                args["expected_history"] = previous["result"]["history"]
                if not args["page_label"]:
                    args["page_label"] = previous["page_label"]
            prepared = await self.adapter.call("prepare", **args)
            self.store.db.execute('UPDATE queries SET space_id=?,updated_at=? WHERE id=?',(self.settings.space_id,now(),op['query_id']))
            self.store.patch(op_id, page_label=prepared["page_label"], baseline=prepared["baseline"], observed=prepared["observed"])
            # Durable intent MUST commit before crossing the non-idempotent click boundary.
            self.store.transition(op_id, "SUBMITTING")
            op = self.store.operation(op_id)
            await self.adapter.call("submit", **self.browser_args(op))
            self.store.transition(op_id, "WAITING_FOR_RESPONSE")
        except BridgeError as error:
            current = self.store.operation(op_id)
            label = error.data.get("diagnostic_context", {}).get("page_label")
            if label:
                self.store.patch(op_id, page_label=label)
            status = "OUTCOME_UNKNOWN" if current["status"] == "SUBMITTING" else "NEEDS_ATTENTION"
            error.data["safe_to_retry"] = status != "OUTCOME_UNKNOWN"
            self.store.transition(op_id, status, error=error.data)
        except Exception as error:
            current = self.store.operation(op_id)
            status = "OUTCOME_UNKNOWN" if current["status"] == "SUBMITTING" else "NEEDS_ATTENTION"
            self.store.transition(op_id, status, error=BridgeError("EXECUTION_ERROR", "执行器内部异常，保留任务现场。", stage=current["status"], action=True, context={"type": type(error).__name__}).data)

    async def observe(self, op, recover=False):
        try:
            if not op["baseline"] or op["submitted_at"] is None:
                # Preparation failures can retry but must never be treated as sent messages.
                return
            response = await self.adapter.call("recover" if recover else "observe", **self.browser_args(op))
            self.store.patch(op["id"], page_label=response["page_label"])
            s, baseline = response["state"], op["baseline"]
            turns, old = s["turns"], baseline["turns"]
            if history(turns[:len(old)]) != history(old):
                raise BridgeError("HISTORY_CHANGED", "已存在的消息被修改，停止自动关联。", action=True)
            new = turns[len(old):]
            expected = normalize(self.browser_args(op)["send_text"])
            if not new:
                if now() - op["submitted_at"] > 30 or recover:
                    raise BridgeError("SEND_OUTCOME_UNKNOWN", "页面未提供新消息证据；不能判断是否发送。", action=True)
                return
            if len(new) != 1 or normalize(new[0]["user_text"]) != expected:
                raise BridgeError("MESSAGE_ASSOCIATION_FAILED", "发现额外消息或不匹配内容，停止关联结果。", action=True)
            cid = s["identity"].get("conversation")
            if not cid:
                return
            if provisional_conversation(cid):
                if now()-op['submitted_at']>op['payload']['hard_timeout']:
                    raise BridgeError('CONVERSATION_ID_PENDING','网页仍只有临时对话地址；保留发送现场，不能重新发送。',action=True)
                self.store.transition(op['id'],'GENERATING',error=None)
                return
            # Normalize the verified route, ignoring the optional project-title suffix.
            url = f"https://chatgpt.com/g/{self.settings.project_id}/c/{cid}"
            # Legacy temporary routes can be promoted only here, after exact
            # baseline, nonce-bearing user text and turn-count reconciliation.
            self.store.bind_conversation(op["query_id"], url, cid, self.settings.space_id,promote_provisional=True)
            turn = new[0]
            state = turn["state"]
            label=s.get("model_label")
            if label is not None:
                effort=(op["observed"] or {}).get("effort",op["payload"]["effort"])
                effort_ok=bool(re.search(r"Extra High|極高|极高",label)) if effort=='extra_high' else bool(re.search(r"(^|\s)High($|\s)|推理強度\s*高$",label))
                if not effort_ok or re.search(r"5\.6|\bPro\b",label):
                    raise BridgeError("MODEL_SELECTION_CHANGED", "网页当前模型／思考选择发生变化，无法继续确认所请求配置。",stage="observe",action=True)
            result = dict(content=turn["assistant_text"], sources=turn.get("sources", []),
                          user_message_id=turn.get("user_id"), assistant_message_id=turn.get("assistant_id"),
                          history=history(turns), observed=op["observed"], completeness_signal=state if turn.get('render_ready',True) else 'pending_render',
                          warning="模型与思考等级仅通过网页选择器核验，未验证推理后端。")
            if state == "complete" and turn.get('render_ready',True) and (turn["assistant_text"] or turn.get('artifacts') or turn.get('unsupported_outputs')) and (turn.get("regenerate") or turn.get('artifacts') or turn.get('unsupported_outputs')):
                result=await self.package_result(op,turn,result)
                self.store.transition(op["id"], "COMPLETED" if result['delivery_complete'] else 'NEEDS_ATTENTION', result=result,
                                      error=None if result['delivery_complete'] else BridgeError('ARTIFACT_CAPTURE_FAILED','回答已生成，部分产物尚未得到可回传内容；无需重新发送问题。',stage='artifact',action=True).data)
                if not result['delivery_complete']:
                    return
                try:
                    closed = await self.adapter.call("close", **self.browser_args(self.store.operation(op["id"])))
                    if closed.get("closed"):
                        self.store.patch(op["id"], page_label=None)
                except Exception:
                    # Completion is already durable. Cleanup failure must not undo it or resend.
                    pass
            elif state in {"interrupted", "cancelled", "stopped", "error", "failed"}:
                cancelled = op["error"] and op["error"].get("error_code") == "CANCEL_REQUESTED"
                self.store.transition(op["id"], "CANCELLED" if cancelled else "NEEDS_ATTENTION", result=result,
                                      error=None if cancelled else BridgeError("RESPONSE_INTERRUPTED", "网页回答被中断或出错。", action=True).data)
            elif now() - op["submitted_at"] > op["payload"]["hard_timeout"]:
                self.store.transition(op["id"], "NEEDS_ATTENTION", result=result, error=BridgeError("GENERATION_TIMEOUT", "超过硬超时；网页可能仍在执行，可对账恢复。", action=True).data)
            else:
                target = "GENERATING" if state not in {"pending", "queued", "waiting"} else "WAITING_FOR_RESPONSE"
                # Do not regress GENERATING to WAITING on transient UI states.
                if op["status"] == "GENERATING" and target == "WAITING_FOR_RESPONSE":
                    target = "GENERATING"
                cancel_error=op["error"] if op["error"] and op["error"].get("error_code")=="CANCEL_REQUESTED" else None
                self.store.transition(op["id"], target, result=result, error=cancel_error)
        except BridgeError as error:
            unknown = error.data["error_code"] in {"SEND_OUTCOME_UNKNOWN", "MESSAGE_ASSOCIATION_FAILED", "HISTORY_CHANGED"}
            self.store.transition(op["id"], "OUTCOME_UNKNOWN" if unknown else "NEEDS_ATTENTION", error=error.data)

    async def package_result(self,op,turn,result):
        result.update(delivery_complete=False,delivery_phase='COLLECTING_ARTIFACTS')
        self.store.transition(op['id'],op['status'],result=result,error=op['error'])
        manifest=await self.artifacts.collect(op,turn.get('artifacts',[]),self.adapter,self.browser_args(op),turn['user_id'])
        result['artifacts']=manifest
        result['unsupported_outputs']=turn.get('unsupported_outputs',[])
        result['conversation_view_url']=self.store.query(op['owner'],op['query_id'])['conversation_url']
        result['delivery_complete']=all(a['status']=='AVAILABLE' for a in manifest) and not result['unsupported_outputs']
        result['artifact_discovery']='rendered_ui_only'
        result['delivery_phase']='AVAILABLE' if result['delivery_complete'] else 'PARTIAL'
        result['outputs']=self.outputs(result)
        return result

    @staticmethod
    def outputs(result):
        return ([{'type':'text','text':result['content']}] if result['content'] else [])+[
            {'type':'link',**link} for link in result.get('sources',[])]+[{'type':'artifact',**a} for a in result.get('artifacts',[])]+[
            {'type':'preview',**item,'view_url':result.get('conversation_view_url'),'requires_chatgpt_login':True} for item in result.get('unsupported_outputs',[])]
