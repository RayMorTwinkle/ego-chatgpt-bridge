import copy

import pytest

from ego_bridge.model import BridgeError, Settings
from ego_bridge.service import Service
from ego_bridge.store import Store


class FakeBrowser:
    """Fault injection only; these are not claims about the real browser."""
    def __init__(self):
        self.pages = {}
        self.sent = []
        self.fail_prepare = False
        self.crash_after_send = False
        self.complete = True
        self.regenerate = True

    async def call(self, action, **kw):
        if action == "health":
            return {"healthy": True, "simulated": True}
        label = kw.get("page_label")
        if action == "prepare":
            if self.fail_prepare:
                raise BridgeError("LOGIN_REQUIRED", "Login expired", action=True)
            label = label or f"p{len(self.pages)+1}"
            self.pages.setdefault(label, [])
            return {"page_label": label, "baseline": {"turns": copy.deepcopy(self.pages[label])},
                    "observed": {"model": "GPT-6", "effort": kw["effort"], "verification": "ui_selection", "backend_verified": False}}
        if action == "submit":
            self.sent.append(kw["send_text"])
            self.pages[label].append({"state": "complete" if self.complete else "thinking", "user_id": f"u{len(self.sent)}",
                                      "user_text": kw["send_text"], "assistant_id": f"a{len(self.sent)}",
                                      "assistant_text": "answer" if self.complete else "partial", "sources": [], "regenerate": self.regenerate})
            if self.crash_after_send:
                raise BridgeError("BROWSER_TIMEOUT", "Crashed after remote accepted")
            return {"clicked": True}
        if action == "cancel":
            return {"stopped": False, "state": {}}
        if action == "close":
            return {"closed": False}
        if action in {"observe", "recover"}:
            return {"page_label": label, "state": {"turns": copy.deepcopy(self.pages[label]), "identity": {"conversation": f"conversation-{label}"}}}
        raise AssertionError(action)


@pytest.mark.parametrize('code',[
 'EGO_UNAVAILABLE','SPACE_MISSING','SPACE_IDENTITY_MISMATCH','SPACE_USER_CONTROL','LOGIN_REQUIRED',
 'ACCOUNT_MISMATCH','PROJECT_MISMATCH','UNEXPECTED_DIALOG','UI_CHANGED','MODEL_UNAVAILABLE',
 'EFFORT_UNAVAILABLE','MODEL_MISMATCH','CONVERSATION_MISMATCH','PAGE_LOAD_FAILED'
])
async def test_pre_send_failures_preserve_structured_reason_and_do_not_send(rig,code):
    s,db,fake=rig
    original=fake.call
    async def injected(action,**kw):
        if action=='prepare':
            raise BridgeError(code,'injected failure',stage='prepare',action=True)
        return await original(action,**kw)
    fake.call=injected
    a=await create(s)
    await execute(s,db,a)
    op=db.operation(a['message_id'])
    assert op['status']=='NEEDS_ATTENTION' and op['submitted_at'] is None
    assert op['error']['error_code']==code and op['error']['stage']=='prepare'
    assert op['error']['query_id']==a['query_id'] and op['error']['request_id']=='r1'
    assert not fake.sent


async def test_duplicate_send_evidence_is_ambiguous_not_success(rig):
    s,db,fake=rig
    a=await create(s)
    await execute(s,db,a)
    fake.pages['p1'].append(copy.deepcopy(fake.pages['p1'][0]))
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='OUTCOME_UNKNOWN'
    assert len(fake.sent)==1


async def test_unknown_completion_hard_times_out_not_success(rig):
    s,db,fake=rig
    a=await create(s,soft_timeout=1,hard_timeout=2)
    await execute(s,db,a)
    fake.pages['p1'][0].update(state='unrecognized',assistant_text='looks final')
    db.patch(a['message_id'],submitted_at=.1)
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='NEEDS_ATTENTION'
    assert db.operation(a['message_id'])['error']['error_code']=='GENERATION_TIMEOUT'


async def test_clearing_local_records_does_not_claim_remote_delete(rig):
    s,db,fake=rig
    a=await create(s)
    await s.dispatch('alice','cancel_query',{'query_id':a['query_id']})
    r=await s.dispatch('alice','delete_query',{'query_id':a['query_id']})
    assert r['local_records_deleted'] and not r['remote_conversation_deleted']
    assert (await s.dispatch('alice','list_queries',{}))['queries']==[]


async def test_invalid_state_transition_rejected(rig):
    s,db,fake=rig
    a=await create(s)
    with pytest.raises(BridgeError):
        db.transition(a['message_id'],'COMPLETED')
    assert db.operation(a['message_id'])['status']=='QUEUED'


async def test_changed_effort_selection_is_not_reported_as_success(rig):
    s,db,fake=rig
    a=await create(s)
    await execute(s,db,a)
    original=fake.call
    async def changed(action,**kw):
        result=await original(action,**kw)
        if action=='observe':result['state']['model_label']='推理強度\n中'
        return result
    fake.call=changed
    await s.observe(db.operation(a['message_id']))
    op=db.operation(a['message_id'])
    assert op['status']=='NEEDS_ATTENTION' and op['error']['error_code']=='MODEL_SELECTION_CHANGED'


async def test_cancel_request_survives_intermediate_poll(rig):
    s,db,fake=rig
    fake.complete=False
    a=await create(s)
    await execute(s,db,a)
    original=fake.call
    async def stopped(action,**kw):
        if action=='cancel':return {'stopped':True}
        return await original(action,**kw)
    fake.call=stopped
    await s.dispatch('alice','cancel_query',{'query_id':a['query_id']})
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['error']['error_code']=='CANCEL_REQUESTED'
    fake.pages['p1'][0]['state']='interrupted'
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='CANCELLED'


@pytest.mark.parametrize('code',['EGO_CONNECTION_FAILED','TAB_MISSING','LOGIN_REQUIRED','PAGE_LOAD_FAILED','CONVERSATION_MISMATCH','UNEXPECTED_DIALOG','UI_CHANGED','MODEL_UNAVAILABLE'])
async def test_observation_failure_can_recover_without_resend(rig,code):
    s,db,fake=rig
    a=await create(s)
    await execute(s,db,a)
    original=fake.call
    async def disconnected(action,**kw):
        if action=='observe':raise BridgeError(code,'injected observation failure',stage='observe',action=True)
        return await original(action,**kw)
    fake.call=disconnected
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='NEEDS_ATTENTION'
    fake.call=original
    r=await s.dispatch('alice','recover_session',{'query_id':a['query_id']})
    assert r['status']=='COMPLETED' and len(fake.sent)==1


@pytest.fixture
def rig(tmp_path):
    settings = Settings(tmp_path, poll_seconds=0.01)
    store = Store(tmp_path)
    fake = FakeBrowser()
    service = Service(settings, store, fake)
    yield service, store, fake
    store.close()


async def create(service, text="Question", request_id="r1", owner="alice", **kw):
    r = await service.dispatch(owner, "create_query", {"text": text, "request_id": request_id, **kw})
    assert r["ok"], r
    return r


async def execute(service, store, response):
    await service.execute(store.operation(response["message_id"]))


async def test_idempotency_and_content_conflict_and_owner_scope(rig):
    s, db, fake = rig
    a = await create(s)
    b = await create(s)
    assert a["query_id"] == b["query_id"]
    conflict = await s.dispatch("alice", "create_query", {"text": "other", "request_id": "r1"})
    assert conflict["error"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    other = await create(s, owner="bob")
    assert other["query_id"] != a["query_id"]
    assert len(db.candidates()) == 2


async def test_crash_after_acceptance_never_resends(rig):
    s, db, fake = rig
    fake.crash_after_send = True
    a = await create(s)
    await execute(s, db, a)
    assert db.operation(a["message_id"])["status"] == "OUTCOME_UNKNOWN"
    refusal = await s.dispatch("alice", "retry_query", {"query_id": a["query_id"]})
    assert refusal["error"]["error_code"] == "UNSAFE_RETRY"
    recovered = await s.dispatch("alice", "recover_session", {"query_id": a["query_id"]})
    assert recovered["status"] == "COMPLETED"
    assert len(fake.sent) == 1


async def test_restart_at_submission_boundary_does_not_enqueue(rig):
    s, db, fake = rig
    a = await create(s)
    db.transition(a["message_id"], "PREPARING")
    db.transition(a["message_id"], "SUBMITTING")
    await s.start()
    await s.stop()
    assert db.operation(a["message_id"])["status"] == "OUTCOME_UNKNOWN"
    assert fake.sent == []
    assert not db.candidates()


async def test_partial_and_unknown_completion_never_complete(rig):
    s, db, fake = rig
    fake.complete = False
    a = await create(s)
    await execute(s, db, a)
    await s.observe(db.operation(a["message_id"]))
    r = await s.dispatch("alice", "get_query_result", {"query_id": a["query_id"]})
    assert r["status"] == "GENERATING" and not r["complete"]
    assert r["result"]["content"] == "partial"
    fake.pages['p1'][0]['state'] = 'complete'
    fake.pages['p1'][0]['regenerate'] = False
    await s.observe(db.operation(a["message_id"]))
    assert db.operation(a["message_id"])["status"] != "COMPLETED"


async def test_same_conversation_order_and_parallel_candidates(rig):
    s, db, fake = rig
    a = await create(s)
    b = await s.dispatch("alice", "continue_query", {"query_id": a["query_id"], "text": "Followup", "request_id": "r2"})
    c = await create(s, request_id="r3")
    assert {o['id'] for o in db.candidates()} == {a['message_id'], c['message_id']}
    await execute(s, db, a)
    await s.observe(db.operation(a['message_id']))
    assert b['message_id'] in {o['id'] for o in db.candidates()}
    await execute(s, db, b)
    await s.observe(db.operation(b['message_id']))
    assert len(fake.pages['p1']) == 2
    assert len(fake.sent) == 2


async def test_cross_owner_cannot_inspect_or_continue(rig):
    s, db, fake = rig
    a = await create(s)
    for tool in ['get_query_result','diagnose','continue_query','recover_session','delete_query']:
        r = await s.dispatch('bob', tool, dict(query_id=a['query_id'],message_id=a['message_id'],text='x',request_id='x'))
        assert not r['ok'] and r['error']['error_code']=='QUERY_NOT_FOUND'
    assert (await s.dispatch('bob','list_queries',{}))['queries']==[]


async def test_manual_message_and_history_changes_stop_association(rig):
    s, db, fake = rig
    a = await create(s)
    await execute(s, db, a)
    fake.pages['p1'][0]['user_text']='manual user text'
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='OUTCOME_UNKNOWN'
    assert db.operation(a['message_id'])['error']['error_code']=='MESSAGE_ASSOCIATION_FAILED'


async def test_hard_timeout_can_recover_without_send(rig):
    s, db, fake = rig
    fake.complete=False
    a=await create(s,soft_timeout=1,hard_timeout=2)
    await execute(s,db,a)
    db.patch(a['message_id'],submitted_at=0.1)
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='NEEDS_ATTENTION'
    fake.pages['p1'][0].update(state='complete',assistant_text='late answer',regenerate=True)
    recovered=await s.dispatch('alice','recover_session',{'query_id':a['query_id']})
    assert recovered['status']=='COMPLETED' and len(fake.sent)==1


async def test_cancel_queue_and_best_effort_active(rig):
    s,db,fake=rig
    a=await create(s)
    r=await s.dispatch('alice','cancel_query',{'query_id':a['query_id']})
    assert r['cancelled'] and not r['best_effort'] and not fake.sent
    b=await create(s,request_id='r2')
    await execute(s,db,b)
    r=await s.dispatch('alice','cancel_query',{'query_id':b['query_id']})
    assert not r['cancelled'] and r['best_effort']
    assert r['status']=='WAITING_FOR_RESPONSE'


async def test_preparation_failure_safe_retry(rig):
    s,db,fake=rig
    fake.fail_prepare=True
    a=await create(s)
    await execute(s,db,a)
    assert db.operation(a['message_id'])['submitted_at'] is None
    fake.fail_prepare=False
    r=await s.dispatch('alice','retry_query',{'query_id':a['query_id']})
    assert r['status']=='QUEUED'
    await execute(s,db,a)
    assert len(fake.sent)==1


async def test_temporary_route_waits_for_server_identity_and_legacy_route_can_recover(rig):
    s,db,fake=rig
    task=await create(s);await execute(s,db,task)
    original=fake.call;cid='local-chatgpt%3Aaaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    async def observed(action,**kw):
        result=await original(action,**kw)
        if action in {'observe','recover'}:result['state']['identity']['conversation']=cid
        return result
    fake.call=observed
    await s.observe(db.operation(task['message_id']))
    assert db.query('alice',task['query_id'])['conversation_id'] is None
    assert db.operation(task['message_id'])['status']=='GENERATING' and len(fake.sent)==1
    # A previous version could persist the temporary route. Promote it only
    # after the exact nonce-bearing turn and unchanged history are reconciled.
    db.bind_conversation(task['query_id'],'https://chatgpt.com/g/project/c/'+cid,cid)
    cid='aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'
    result=await s.dispatch('alice','recover_session',{'query_id':task['query_id']})
    assert result['status']=='COMPLETED' and result['conversation_id']==cid and len(fake.sent)==1
    with pytest.raises(BridgeError):db.bind_conversation(task['query_id'],'https://chatgpt.com/c/different','different',promote_provisional=True)


async def test_completed_marker_does_not_complete_unhydrated_reply(rig):
    s,db,fake=rig;task=await create(s);await execute(s,db,task)
    fake.pages['p1'][0]['render_ready']=False
    await s.observe(db.operation(task['message_id']))
    result=await s.dispatch('alice','get_query_result',{'query_id':task['query_id']})
    assert not result['generation_complete'] and result['status']=='GENERATING'
    fake.pages['p1'][0]['render_ready']=True
    await s.observe(db.operation(task['message_id']))
    assert db.operation(task['message_id'])['status']=='COMPLETED' and len(fake.sent)==1


def test_single_writer_and_persistent_reopen(tmp_path):
    a=Store(tmp_path)
    with pytest.raises(BridgeError,match='另一个服务进程'):
        Store(tmp_path)
    op=a.enqueue('alice','r',dict(text='x',effort='high',soft_timeout=1,hard_timeout=2),'project',2)
    a.close()
    b=Store(tmp_path)
    assert b.operation(op['id'])['status']=='QUEUED'
    b.backup(tmp_path/'backup.sqlite3')
    b.close()


@pytest.mark.parametrize('bad',[
    {'model':'GPT-5.6 Sol'}, {'model':'Pro'}, {'effort':'medium'}, {'effort':'pro'}, {'soft_timeout':5,'hard_timeout':2}, {'text':''}
])
async def test_policy_rejections_before_browser_actions(rig,bad):
    s,db,fake=rig
    r=await s.dispatch('alice','create_query',dict({'text':'x','request_id':'r'},**bad))
    assert not r['ok'] and not fake.sent and not db.candidates()
