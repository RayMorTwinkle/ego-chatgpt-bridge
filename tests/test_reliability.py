import asyncio
import base64
import copy
from pathlib import Path

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
    assert op['status']==('QUEUED' if code in {'EGO_UNAVAILABLE','PAGE_LOAD_FAILED'} else 'NEEDS_ATTENTION') and op['submitted_at'] is None
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
    settings = Settings(tmp_path, poll_seconds=0.01, completion_confirm_seconds=0)
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


async def test_inline_code_rendering_can_recover_without_resending_or_accepting_other_text(rig):
    service, store, browser = rig
    task = await create(service, text='Read `SKILL.md` now.')
    await execute(service, store, task)
    turn = browser.pages['p1'][0]
    turn['user_source_text'] = turn['user_text']
    turn['user_text'] = turn['user_text'].replace('`SKILL.md`', 'SKILL.md')
    turn['user_source_text'] = turn['user_source_text'].replace(' now.', ' later.')
    await service.observe(store.operation(task['message_id']))
    assert store.operation(task['message_id'])['status'] == 'OUTCOME_UNKNOWN'
    turn['user_source_text'] = turn['user_source_text'].replace(' later.', ' now.')
    recovered = await service.dispatch('alice', 'recover_session', {'query_id': task['query_id']})
    assert recovered['status'] == 'COMPLETED' and len(browser.sent) == 1


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


async def test_attention_and_unknown_submissions_do_not_starve_the_queue(rig):
    service, store, browser = rig
    for request_id, status in [('timed-out', 'NEEDS_ATTENTION'), ('unconfirmed', 'OUTCOME_UNKNOWN')]:
        task = await create(service, request_id=request_id)
        store.transition(task['message_id'], 'PREPARING')
        store.transition(task['message_id'], 'SUBMITTING')
        store.transition(task['message_id'], status)
    pending = await create(service, request_id='next')
    executed = asyncio.Event()
    original = browser.call

    async def signal_submission(action, **kwargs):
        result = await original(action, **kwargs)
        if action == 'submit':
            executed.set()
        return result

    browser.call = signal_submission
    await service.start()
    try:
        await asyncio.wait_for(executed.wait(), timeout=.5)
        assert len(browser.sent) == 1 and pending['message_id'] in browser.sent[0]
    finally:
        await service.stop()


async def test_attachment_bytes_are_staged_for_prepare_and_released_at_submit(rig):
    service, store, browser = rig
    data = b'attachment content\n'
    files = [{'filename': 'report.txt', 'content_base64': base64.b64encode(data).decode()}]
    queued = await create(service, attachments=files)
    op_id = queued['message_id']
    assert queued['attachments'][0]['size_bytes'] == len(data)
    assert 'content_base64' not in str(queued)
    assert store.uploads(op_id) == {'report.txt': data}
    assert (await create(service, attachments=files))['message_id'] == op_id
    changed = await service.dispatch('alice', 'create_query', {'request_id': 'r1', 'text': 'Question',
        'attachments': [{'filename': 'report.txt', 'content_base64': 'YQ=='}]})
    assert changed['error']['error_code'] == 'IDEMPOTENCY_CONFLICT'
    original = browser.call
    staged = []

    async def inspect_upload(action, **kwargs):
        if action == 'prepare':
            staged.append(Path(kwargs['attachment_paths'][0]))
            assert staged[0].name.endswith('-report.txt') and staged[0].read_bytes() == data
            assert kwargs['attachment_names'] == [staged[0].name]
        if action == 'submit':
            assert kwargs['attachment_names'] == [staged[0].name]
            assert not staged[0].exists() and not store.uploads(op_id)
        return await original(action, **kwargs)

    browser.call = inspect_upload
    await execute(service, store, queued)
    assert len(browser.sent) == 1


@pytest.mark.parametrize('filename,content,code', [
    ('../../secret', 'YQ==', 'INVALID_ATTACHMENT_NAME'),
    ('report.txt', '?', 'INVALID_ATTACHMENT_CONTENT'),
    ('report.txt', '', 'ATTACHMENT_SIZE_LIMIT'),
])
async def test_invalid_upload_does_not_enqueue(rig, filename, content, code):
    service, store, _ = rig
    result = await service.dispatch('alice', 'create_query', {'request_id': 'bad-upload', 'text': 'Question',
        'attachments': [{'filename': filename, 'content_base64': content}]})
    assert result['error']['error_code'] == code and not store.candidates()


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

async def test_hydrating_user_bubble_is_pending_then_streams_without_resend(rig):
    s,db,fake=rig
    fake.complete=False
    a=await create(s,text='Research across multiple\nparagraphs with sources')
    await execute(s,db,a)
    turn=fake.pages['p1'][0]
    original=turn['user_text'];turn.update(user_text='',user_id=None,assistant_text='',render_ready=False)
    await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='WAITING_FOR_RESPONSE'
    turn.update(user_text=original,user_id='u1',assistant_text='First research section',render_ready=True)
    await s.observe(db.operation(a['message_id']))
    partial=await s.dispatch('alice','get_query_result',{'query_id':a['query_id']})
    assert partial['status']=='GENERATING' and not partial['complete']
    assert partial['result']['content']=='First research section'
    assert partial['result_is_partial'] and partial['next_action']['tool']=='wait_query'
    turn.update(assistant_text='First research section\nSecond research section',state='complete',regenerate=True)
    await s.observe(db.operation(a['message_id']))
    final=await s.dispatch('alice','get_query_result',{'query_id':a['query_id']})
    assert final['complete'] and not final['result_is_partial'] and len(fake.sent)==1


async def test_association_failure_explains_specific_mismatch_and_server_managed_browser(rig):
    s,db,fake=rig
    a=await create(s)
    await execute(s,db,a)
    fake.pages['p1'][0]['user_text']='Different message'
    await s.observe(db.operation(a['message_id']))
    r=await s.dispatch('alice','diagnose',{'query_id':a['query_id']})
    context=r['last_error']['diagnostic_context']
    assert context['reason']=='user_text_mismatch'
    assert context['expected_new_turn_count']==1 and context['observed_new_turn_count']==1
    assert context['expected_user_text_sha256']!=context['observed_user_text_sha256']
    assert context['request_marker_present'] is False
    assert r['execution_context']['browser_location']=='bridge_server'
    assert r['next_action']['tool']=='recover_session'
    assert 'Different message' not in str(context)
    assert len(fake.sent)==1


async def test_health_failure_is_advisory_and_does_not_block_independently_verified_send(rig):
    s,db,fake=rig;original=fake.call
    async def health_timeout(action,**kw):
        if action=='health':raise BridgeError('BROWSER_TIMEOUT','injected browser page timeout',stage='health')
        return await original(action,**kw)
    fake.call=health_timeout
    result=await s.dispatch('alice','health_check',{})
    assert not result['ok'] and result['health_check_is_advisory'] and result['service_available']
    assert result['execution_context']['client_local_browser_cli_required'] is False
    task=await create(s);await execute(s,db,task)
    assert len(fake.sent)==1 and db.operation(task['message_id'])['status']=='WAITING_FOR_RESPONSE'

async def test_completion_requires_stable_entire_turn_and_resets_on_more_output(rig,monkeypatch):
    s,db,fake=rig
    s.settings.completion_confirm_seconds=4
    clock=[1000.0];monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    a=await create(s);await execute(s,db,a)
    await s.observe(db.operation(a['message_id']))
    r=await s.dispatch('alice','get_query_result',{'query_id':a['query_id']})
    assert not r['complete'] and not r['generation_complete']
    turn=fake.pages['p1'][0];turn.update(state='thinking',assistant_text='First section')
    clock[0]+=5;await s.observe(db.operation(a['message_id']))
    turn.update(state='complete',assistant_text='First section and final section')
    clock[0]+=5;await s.observe(db.operation(a['message_id']))
    assert db.operation(a['message_id'])['status']=='GENERATING'
    clock[0]+=5;await s.observe(db.operation(a['message_id']))
    r=await s.dispatch('alice','get_query_result',{'query_id':a['query_id']})
    assert r['complete'] and r['extraction_stable'] and r['semantic_validation']=='not_performed'
    assert len(fake.sent)==1

async def test_wait_cursor_suppresses_unchanged_content_and_preserves_owner_scope(rig, monkeypatch):
    s,db,fake=rig
    fake.complete=False
    a=await create(s, soft_timeout=1);await execute(s,db,a);await s.observe(db.operation(a['message_id']))
    args={'query_id':a['query_id'],'timeout_seconds':0,'include_content':True}
    first=await s.dispatch('alice','wait_query',args)
    assert first['changed'] and first['result']['content']=='partial'
    later = first['progress']['last_observed_at'] + 2
    monkeypatch.setattr('ego_bridge.service.now', lambda: later)
    await s.observe(db.operation(a['message_id']))
    again=await s.dispatch('alice','wait_query',{**args,'cursor':first['cursor']})
    assert again['timed_out'] and not again['changed'] and 'result' not in again
    assert again['progress']['last_observed_at'] == later
    assert again['progress']['answer_characters'] == len('partial')
    assert again['progress']['source_count'] == 0
    assert again['soft_timeout_exceeded']
    assert not (await s.dispatch('bob','wait_query',args))['ok']
    fake.pages['p1'][0]['assistant_text']='revised'
    await s.observe(db.operation(a['message_id']))
    changed=await s.dispatch('alice','wait_query',{**args,'cursor':first['cursor']})
    assert changed['changed'] and changed['result']['content']=='revised'
    assert 'outputs' not in changed['result']

async def test_stop_signal_prevents_packaging_and_request_context_is_explicit(rig):
    s,db,fake=rig
    a=await create(s,context_mode='request_only_best_effort');await execute(s,db,a)
    assert '不要把项目记忆' in fake.sent[0]
    fake.pages['p1'][0]['generation_active']=True
    await s.observe(db.operation(a['message_id']))
    result=await s.dispatch('alice','get_query_result',{'query_id':a['query_id']})
    assert result['generation_done'] and not result['generation_complete'] and not result['complete']
    assert not result['result']['context_policy']['memory_isolation_verified']

async def test_wait_releases_event_loop_and_returns_markdown_without_duplicate_outputs(rig):
    import asyncio
    s,db,fake=rig;fake.complete=False
    a=await create(s);await execute(s,db,a);await s.observe(db.operation(a['message_id']))
    before=await s.dispatch('alice','wait_query',{'query_id':a['query_id'],'timeout_seconds':0})
    async def change():
        await asyncio.sleep(.02)
        fake.pages['p1'][0].update(assistant_text='New body',assistant_markdown='## New body')
        await s.observe(db.operation(a['message_id']))
    task=asyncio.create_task(change())
    result=await s.dispatch('alice','wait_query',{'query_id':a['query_id'],'cursor':before['cursor'],'timeout_seconds':1,'include_content':True,'content_format':'markdown'})
    await task
    assert result['changed'] and result['result']['content']=='## New body'
    assert 'markdown' not in result['result'] and 'outputs' not in result['result']

async def test_health_timeout_is_page_scoped_but_login_failure_still_needs_operator(rig):
    s,db,fake=rig
    async def fail(action,**kw):raise BridgeError('EGO_PAGE_EVALUATION_TIMED_OUT','timeout',stage='health',action=True,context={'page_label':'p5'})
    fake.call=fail
    r=await s.dispatch('alice','health_check',{})
    assert not r['error']['user_action_required'] and r['error']['safe_to_retry']
    assert r['health']['probe']['page_label']=='p5' and not r['health']['submission_blocked_by_probe']
    async def login(action,**kw):raise BridgeError('LOGIN_REQUIRED','login',stage='health',action=True)
    fake.call=login
    r=await s.dispatch('alice','health_check',{})
    assert r['error']['user_action_required']


async def test_answer_load_error_keeps_partial_and_requires_new_confirmation(rig, monkeypatch):
    s, db, fake = rig
    s.settings.completion_confirm_seconds = 4
    clock = [1000.0]
    monkeypatch.setattr('ego_bridge.service.now', lambda: clock[0])
    task = await create(s)
    await execute(s, db, task)
    mid = task['message_id']
    args = {'query_id': task['query_id']}
    turn = fake.pages['p1'][0]
    turn.update(state='complete', assistant_text='The rendered answer')
    await s.observe(db.operation(mid))
    turn.update(assistant_text='', render_error='ANSWER_LOAD_FAILED', render_ready=False,
                artifacts=[{'kind': 'file_card', 'name': 'report.csv', 'index': 0}])
    clock[0] += 5
    await s.observe(db.operation(mid))
    clock[0] += 5
    await s.observe(db.operation(mid))
    result = await s.dispatch('alice', 'get_query_result', args)
    assert result['generation_done'] and not result['complete']
    assert result['result_is_partial'] and result['result']['content'] == 'The rendered answer'
    assert result['result']['render_error'] == 'ANSWER_LOAD_FAILED'
    assert result['result']['artifacts'] == []
    turn.update(assistant_text='The rendered answer', render_error=None, render_ready=True, artifacts=[])
    clock[0] += 5
    await s.observe(db.operation(mid))
    assert not (await s.dispatch('alice', 'get_query_result', args))['complete']
    clock[0] += 5
    await s.observe(db.operation(mid))
    assert (await s.dispatch('alice', 'get_query_result', args))['complete']
    assert len(fake.sent) == 1


async def test_historical_load_error_result_is_not_served_as_complete(rig):
    s, db, fake = rig
    task = await create(s)
    await execute(s, db, task)
    db.transition(task['message_id'], 'COMPLETED', result={
        'content': '無法載入這則回覆', 'markdown': '無法載入這則回覆',
        'completeness_signal': 'complete', 'extraction_stable': True,
    })
    result = await s.dispatch('alice', 'get_query_result', {'query_id': task['query_id']})
    assert not result['complete'] and not result['generation_complete']
    assert result['result']['content'] == ''
    assert result['last_error']['error_code'] == 'ANSWER_LOAD_FAILED'


@pytest.mark.parametrize('code',[
    'BROWSER_OPERATION_FAILED','BROWSER_TIMEOUT','ATTACHMENT_UPLOAD_FAILED','EGO_CONNECTION_FAILED',
    'ATTACHMENT_UPLOAD_INCOMPLETE','DRAFT_ATTACHMENT_MISMATCH','HISTORY_NOT_CONFIRMED','TAB_MISSING',
])
async def test_pre_send_retry_backoff_is_bounded_and_errors_are_durable(rig,monkeypatch,code):
    s,db,fake=rig;clock=[1000.0]
    monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    calls=[]
    async def fail(action,**kw):
        calls.append(action)
        raise BridgeError(code,'temporary failure',stage='prepare',action=True,context={'page_label':'p1'})
    fake.call=fail
    task=await create(s)
    for attempt,delay in enumerate((15,60,180),1):
        await execute(s,db,task)
        op=db.operation(task['message_id'])
        assert op['status']=='QUEUED' and op['submitted_at'] is None
        assert op['auto_retry']['attempts']==attempt and op['auto_retry']['next_at']==clock[0]+delay
        assert not op['error']['user_action_required']
        before=len(calls)
        await execute(s,db,task)
        assert len(calls)==before  # A poll before the deadline must not run prepare.
        clock[0]+=delay
    await execute(s,db,task)
    op=db.operation(task['message_id'])
    assert op['status']=='NEEDS_ATTENTION' and op['auto_retry']['next_at'] is None
    assert len(op['auto_retry']['history'])==4 and len(calls)==4 and not fake.sent
    assert all(h['error']['error_code']==code for h in op['auto_retry']['history'])
    assert (await s.dispatch('alice','get_query_status',{'query_id':task['query_id']}))['auto_retry']['max']==3


async def test_pre_send_retry_survives_reopen_and_reuses_upload_bytes(rig,monkeypatch):
    s,db,fake=rig;clock=[1000.0]
    monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    original=fake.call
    async def fail(action,**kw):
        if action=='prepare':raise BridgeError('ATTACHMENT_UPLOAD_FAILED','upload failed',stage='prepare')
        return await original(action,**kw)
    fake.call=fail
    task=await create(s,attachments=[{'filename':'report.txt','content_base64':'YQ=='}])
    await execute(s,db,task)
    db.close();s.store=Store(s.settings.runtime);s.artifacts.store=s.store
    try:
        assert s.store.uploads(task['message_id'])=={'report.txt':b'a'}
        await s.execute(s.store.operation(task['message_id']))
        assert not fake.sent
        fake.call=original;clock[0]+=15
        await s.execute(s.store.operation(task['message_id']))
        assert len(fake.sent)==1 and s.store.operation(task['message_id'])['auto_retry']['next_at'] is None
    finally:s.store.close()


async def test_unknown_automatically_reconciles_once_after_backoff_without_resending(rig,monkeypatch):
    s,db,fake=rig;clock=[1000.0]
    monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    fake.crash_after_send=True
    task=await create(s);await execute(s,db,task)
    assert db.operation(task['message_id'])['status']=='OUTCOME_UNKNOWN'
    await s.automatic_recovery()
    assert db.operation(task['message_id'])['status']=='OUTCOME_UNKNOWN'
    clock[0]+=15;await s.automatic_recovery()
    op=db.operation(task['message_id'])
    assert op['status']=='COMPLETED' and len(fake.sent)==1
    assert op['auto_retry']['counters']['recover']==1 and op['auto_retry']['next_at'] is None


async def test_failed_automatic_reconciliation_is_not_repeated(rig,monkeypatch):
    s,db,fake=rig;clock=[1000.0];calls=[]
    monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    fake.crash_after_send=True
    task=await create(s);await execute(s,db,task)
    fake.pages['p1'][0]['user_text']='wrong message'
    original=fake.call
    async def track(action,**kw):
        calls.append(action);return await original(action,**kw)
    fake.call=track
    clock[0]+=15;await s.automatic_recovery()
    for _ in range(3):
        clock[0]+=180;await s.automatic_recovery()
    op=db.operation(task['message_id'])
    assert op['status']=='OUTCOME_UNKNOWN' and op['auto_retry']['next_at'] is None
    assert calls==['recover'] and len(fake.sent)==1
    await s.execute(op)
    assert len(fake.sent)==1


@pytest.mark.parametrize('title,expected',[(None,None),('ChatGPT',None),('New chat',None),('Research findings - ChatGPT','Research findings'),('研究结果','研究结果')])
async def test_chat_title_is_optional_and_returned_in_all_query_views(rig,title,expected):
    s,db,fake=rig;original=fake.call
    async def titled(action,**kw):
        result=await original(action,**kw)
        if action in {'observe','recover'}:result['state']['title']=title
        return result
    fake.call=titled
    task=await create(s);await execute(s,db,task);await s.observe(db.operation(task['message_id']))
    for tool in ('get_query_status','get_query_result'):
        result=await s.dispatch('alice',tool,{'query_id':task['query_id'],'compact':True})
        assert result['chat_title']==expected and result['status']=='COMPLETED'
    assert (await s.dispatch('alice','list_queries',{}))['queries'][0]['chat_title']==expected


def test_old_query_schema_migrates_without_losing_rows(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path/'bridge.sqlite3') as db:
        db.execute('CREATE TABLE queries(id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation_url TEXT,conversation_id TEXT,project_id TEXT NOT NULL,space_id INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)')
        db.execute("INSERT INTO queries VALUES('old','alice',NULL,NULL,'project',2,1,1)")
    store=Store(tmp_path)
    assert store.query('alice','old')['chat_title'] is None
    store.close()
    store=Store(tmp_path)
    assert store.query('alice','old')['space_id']==2
    store.close()


async def test_worker_automatically_runs_due_retry_without_manual_dispatch(rig,monkeypatch):
    s,db,fake=rig;clock=[1000.0];calls=[]
    monkeypatch.setattr('ego_bridge.service.now',lambda:clock[0])
    failed=asyncio.Event();sent=asyncio.Event();original=fake.call
    async def temporary(action,**kw):
        if action=='prepare':
            calls.append(action)
            if len(calls)==1:
                failed.set()
                raise BridgeError('BROWSER_OPERATION_FAILED','temporary model locator failure',stage='prepare')
        result=await original(action,**kw)
        if action=='submit':sent.set()
        return result
    fake.call=temporary
    task=await create(s)
    await s.start()
    try:
        await asyncio.wait_for(failed.wait(),1)
        assert db.operation(task['message_id'])['status']=='QUEUED'
        clock[0]+=14;s.wake.set();await asyncio.sleep(.02)
        assert len(calls)==1 and not fake.sent
        clock[0]+=1;s.wake.set()
        await asyncio.wait_for(sent.wait(),1)
        assert len(calls)==2 and len(fake.sent)==1
    finally:await s.stop()
