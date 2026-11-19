import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {readChatPage,domCapabilities} from '../src/ego_bridge/page-state.mjs';

// The DOM adapter contract gate. Fixtures are sanitized snapshots of real
// ChatGPT pages; when ChatGPT redesigns, refresh them with
// scripts/capture_dom_snapshot.mjs (service stopped) plus the sanitizer noted
// in docs/operations.md, and this file will pinpoint which capabilities broke.
const PAGE_LEVEL=['editor','send_button','draft','attachment_group','model_label','dialogs','login_detection','page_url'];
const TURN_LEVEL=['turns','turn_state','user_message_id','assistant_message_id','user_text','assistant_text','answer_roots','completion_signals','artifact_fields'];
const REQUIRED={project:PAGE_LEVEL,conversation:[...PAGE_LEVEL,...TURN_LEVEL]};

const fixtureDir=fileURLToPath(new URL('../tests/fixtures/dom/',import.meta.url));
const fixtures=fs.existsSync(fixtureDir)
  ? fs.readdirSync(fixtureDir).filter(name=>name.endsWith('.json')).map(name=>JSON.parse(fs.readFileSync(path.join(fixtureDir,name),'utf8')))
  : [];

test('committed canary fixtures exist and satisfy the full DOM contract',()=>{
  assert.ok(fixtures.length>=2,'expected at least a project-page and a conversation fixture');
  const kinds=new Set(fixtures.map(f=>f.kind));
  assert.ok(kinds.has('project')&&kinds.has('conversation'),'need both fixture kinds');
  for(const fixture of fixtures){
    const capabilities=domCapabilities(fixture.state);
    const required=REQUIRED[fixture.kind];
    const failed=required.filter(name=>!capabilities[name]);
    assert.deepEqual(failed,[],`${fixture.kind} fixture lost capabilities: ${failed.join(', ')}`);
  }
});

test('damage simulation: a ChatGPT redesign flips exactly the affected capabilities',()=>{
  const conversation=fixtures.find(f=>f.kind==='conversation');
  assert.ok(conversation,'conversation fixture required for damage tests');
  const clone=()=>JSON.parse(JSON.stringify(conversation.state));
  // reader_diagnostics disappears -> answer evidence gone
  let state=clone();for(const turn of state.turns)delete turn.reader_diagnostics;
  assert.equal(domCapabilities(state).answer_roots,false);
  // turn state attribute renamed -> state machine input lost
  state=clone();for(const turn of state.turns)delete turn.state;
  assert.equal(domCapabilities(state).turn_state,false);
  // With no provable complete turns, assistant id capability becomes vacuous-true.
  state=clone();for(const turn of state.turns){turn.state='complete';turn.assistant_id=null;}
  assert.equal(domCapabilities(state).assistant_message_id,false);
  // message id attribute renamed -> message association lost
  state=clone();for(const turn of state.turns)turn.user_id=null;
  assert.equal(domCapabilities(state).user_message_id,false);
  // completion signals lost -> completion confirmation impossible
  state=clone();for(const turn of state.turns)delete turn.render_ready;
  assert.equal(domCapabilities(state).completion_signals,false);
  // model switcher selector gone -> effort/model verification impossible
  state=clone();state.model_label=null;
  assert.equal(domCapabilities(state).model_label,false);
  // composer gone -> cannot send anything
  state=clone();state.editor_present=false;
  assert.equal(domCapabilities(state).editor,false);
});

test('live runtime snapshots pass the same contract when EGO_DOM_SNAPSHOTS is set',()=>{
  const directory=process.env.EGO_DOM_SNAPSHOTS;
  if(!directory||!fs.existsSync(directory))return; // opt-in: local maintenance runs
  const files=fs.readdirSync(directory).filter(name=>name.endsWith('.json'));
  assert.ok(files.length>0,'snapshot directory exists but is empty');
  for(const name of files){
    const snapshot=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));
    const capabilities=domCapabilities(snapshot.state);
    const failed=(REQUIRED[snapshot.kind]||[]).filter(key=>!capabilities[key]);
    assert.deepEqual(failed,[],`${name} lost capabilities: ${failed.join(', ')}`);
  }
});

test('domCapabilities reports honest vacuous values on an empty state',()=>{
  const capabilities=domCapabilities({turns:[],editor_present:true,draft:'',buttons:[{label:'傳送'}],
    draft_attachments:[],draft_uploading:false,model_label:'GPT-6',dialogs:[],alerts:[],login:false,url:'https://chatgpt.com/g/x/project'});
  assert.equal(capabilities.turns,false); // no turns to prove turn-level capabilities
  assert.equal(capabilities.assistant_message_id,true); // vacuous: no complete turns
  assert.equal(capabilities.editor,true);
});
