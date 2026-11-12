import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {readChatPage} from '../src/ego_bridge/page-state.mjs';

function fixture(content,state='in_progress'){
  const {window}=parseHTML(`<html><head><title>Fixture</title></head><body><div data-talvt-turn-state="${state}">${content}</div><form><div contenteditable="true"></div><button aria-label="選取 ChatGPT 模型">極高</button></form></body></html>`);
  window.HTMLElement.prototype.getClientRects=function(){for(let e=this;e;e=e.parentElement)if(e.hidden||e.style?.display==='none')return [];return [{}];};
  window.HTMLElement.prototype.getBoundingClientRect=function(){return {width:this.tagName==='SVG'?20:128,height:this.tagName==='SVG'?20:128};};
  window.SVGElement.prototype.getClientRects=window.HTMLElement.prototype.getClientRects;
  window.SVGElement.prototype.getBoundingClientRect=()=>({width:20,height:20});
  globalThis.document=window.document;
  globalThis.location={href:'https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/c/bbbbbbbb-cccc-dddd-eeee-ffffffffffff'};
  globalThis.getComputedStyle=e=>({visibility:e.style.visibility||'visible'});
  return window.document;
}
const prompt='Compare a long research prompt.\n\n[ego-bridge request:msg_fixture]';
const user=()=>`<div data-chatgpt-search-message-ids="u1"><div data-user-message-bubble="true"><div data-search-result-target><div class="whitespace-pre-wrap">${prompt}</div></div><span aria-hidden="true">…</span><button data-thread-find-skip data-markdown-copy="exclude" aria-expanded="false">顯示較多</button></div><button aria-label="複製訊息"></button></div>`;
const thought='<div data-markdown-text-style="assistant-message" data-markdown-text-tone="tertiary" style="display:none">Searching sources</div>';
const body=()=>`<div data-chatgpt-selection-message-id="a1"><div data-markdown-text-style="assistant-message"><h2>Research answer</h2><p>Verified result <a href="https://example.org/official">Official source</a></p><div data-markdown-text-style="assistant-message"><pre><code>print("hello")\nprint("world")</code></pre><button data-markdown-copy="exclude">複製代碼</button></div></div></div>`;

test('user inline code has an exact source representation while literal code blocks keep their text',()=>{
  fixture('<div data-chatgpt-search-message-ids="u1"><div data-search-result-target>Read <code>SKILL.md</code> now.<pre><code>literal `code`</code></pre></div></div>');
  const turn=readChatPage().turns[0];
  assert.equal(turn.user_text,'Read SKILL.md now.\nliteral `code`');
  assert.equal(turn.user_source_text,'Read `SKILL.md` now.\nliteral `code`');
});

test('collapsed prompt excludes ellipsis and interactive chrome without stripping literal prompt words',()=>{
  fixture(user()+thought+body());
  assert.equal(readChatPage().turns[0].user_text,prompt);
  document.querySelector('button[aria-expanded]').textContent='顯示較少';
  assert.equal(readChatPage().turns[0].user_text,prompt);
});

test('research reader selects answer instead of hidden or visible reasoning; nested markdown is not duplicated',()=>{
  fixture(user()+thought+body()+'<div data-markdown-text-style="assistant-message" data-markdown-text-tone="tertiary">Checking more sources</div>');
  const t=readChatPage().turns[0];
  assert.match(t.assistant_text,/Research answer/);
  assert.doesNotMatch(t.assistant_text,/Searching sources|Checking more sources|複製代碼/);
  assert.equal(t.assistant_text.match(/print\("hello"\)/g).length,1);
  assert.equal(t.assistant_id,'a1');
  assert.equal(t.sources.length,1);
});

test('reasoning-only progress has no deliverable answer and is not render-ready',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message" data-markdown-text-tone="tertiary">Still searching</div>');
  const t=readChatPage().turns[0];
  assert.equal(t.state,'in_progress');assert.equal(t.assistant_text,'');assert.equal(t.render_ready,false);
});

test('streamed answer grows across observations without a submit or DOM mutation',()=>{
  fixture(user()+thought+body());
  const before=document.body.innerHTML;
  const first=readChatPage().turns[0];assert.match(first.assistant_text,/Research answer/);
  assert.equal(document.body.innerHTML,before);
  const p=document.createElement('p');p.textContent='New streamed section';
  document.querySelector('[data-chatgpt-selection-message-id] > div').append(p);
  const next=readChatPage().turns[0];assert.match(next.assistant_text,/New streamed section/);
  assert.ok(next.assistant_text.length>first.assistant_text.length);
});

test('UI control words typed by the user remain part of exact message evidence',()=>{
  fixture(user()+body());
  const literal='The phrase 顯示較多 and literal … are user content.\n\n[ego-bridge request:msg_fixture]';
  document.querySelector('[data-search-result-target] .whitespace-pre-wrap').textContent=literal;
  assert.equal(readChatPage().turns[0].user_text,literal);
});

test('public web images remain references; generated images retain their actual DOM index',()=>{
  fixture(user()+body(),'complete');
  document.querySelector('[data-chatgpt-selection-message-id] > div').insertAdjacentHTML('beforeend','<img src="https://public.example.org/diagram.png" alt="Official diagram"><img src="https://chatgpt.com/backend-api/files/native-image" alt="Generated output">');
  for(const image of document.querySelectorAll('img')){Object.defineProperty(image,'complete',{value:true});Object.defineProperty(image,'naturalWidth',{value:128});}
  const turn=readChatPage().turns[0];
  assert.equal(turn.external_images.length,1);
  assert.equal(turn.external_images[0].url,'https://public.example.org/diagram.png');
  assert.equal(turn.external_images[0].delivery,'reference_only');
  assert.deepEqual(turn.artifacts,[{kind:'inline_image',index:1,name:'image-2.png'}]);
  assert.equal(turn.render_ready,true);
});


test('search progress thumbnails outside the final answer are not deliverable artifacts',()=>{
  fixture(user()+body()+'<aside><img src="https://public.example.org/search-thumbnail.png" alt="Search preview"></aside>');
  const turn=readChatPage().turns[0];
  assert.deepEqual(turn.artifacts,[]);assert.deepEqual(turn.external_images,[]);
});

test('Markdown preserves tables, source positions, and excludes reasoning',()=>{
  fixture(user()+thought+`<div data-chatgpt-selection-message-id="a1"><div data-markdown-text-style="assistant-message"><h2>Options</h2><table><tr><th>Name</th><th>Source</th></tr><tr><td>A</td><td><a href="https://example.org/a">Official</a></td></tr></table><p>After table</p></div></div>`,'complete');
  const t=readChatPage().turns[0];
  assert.match(t.assistant_markdown,/\| Name \| Source \|/);
  assert.match(t.assistant_markdown,/\[Official\]\(https:\/\/example.org\/a\)/);
  assert.equal(t.extraction.answer_anchor_count,1);
  assert.equal(t.extraction.source_status,'sources_present');
  assert.equal(t.extraction.blocks.length,1);
  assert.doesNotMatch(t.assistant_markdown,/Searching sources/);
});

test('source absence and stop-generation signal are explicit',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message"><p>Introduction</p></div>','complete');
  document.querySelector('form').insertAdjacentHTML('beforeend','<button aria-label="停止串流"></button>');
  const t=readChatPage().turns[0];
  assert.equal(t.extraction.source_status,'no_sources_present');
  assert.equal(t.generation_active,true);
});

test('rich-link editor chrome does not add whitespace to exact submitted prompt',()=>{
  fixture(user());const editor=document.querySelector('form [contenteditable]');
  editor.innerHTML='<p>Read [Official](<span data-rich-text-generated-autolink><span aria-hidden="true" contenteditable="false">icon</span>https://example.org/</span>) now.</p><p>Second paragraph</p>';
  Object.defineProperty(editor,'innerText',{value:'Read [Official](\nhttps://example.org/) now.\nSecond paragraph'});
  assert.equal(readChatPage().draft,'Read [Official](https://example.org/) now.\n\nSecond paragraph');
});

test('KaTeX emits one TeX expression for each inline and display formula',()=>{
  const math=String.raw`<span class="katex"><span class="katex-mathml"><math><semantics><mrow>βj</mrow><annotation encoding="application/x-tex">\beta_j</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">βj</span></span>`;
  fixture(user()+`<div data-markdown-text-style="assistant-message"><p>Estimate ${math} here.</p><span class="katex-display">${math}</span></div>`);
  const md=readChatPage().turns[0].assistant_markdown;
  assert.ok(md.includes(String.raw`Estimate $\beta_j$ here.`));
  assert.ok(md.includes('$$\n'+String.raw`\beta_j`+'\n$$'));
  assert.doesNotMatch(md,/βj/);
});

test('Markdown code keeps its language, blank lines, literal backticks and code-only text',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message"><pre><button>Copy</button><code class="language-python">def f():\n\n\n    return "```"\n</code></pre></div>');
  const md=readChatPage().turns[0].assistant_markdown;
  assert.equal(md,'````python\ndef f():\n\n\n    return "```"\n````');
});

test('known answer-load placeholder cannot make an attached file turn render-ready',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message">無法載入這則回覆</div><button aria-label="開啟 report.csv 預覽">report.csv</button>','complete');
  const turn=readChatPage().turns[0];
  assert.equal(turn.render_error,'ANSWER_LOAD_FAILED');
  assert.equal(turn.render_ready,false);
  assert.equal(turn.assistant_text,'');
});

test('the known output popover is nonblocking while actual dialogs remain guarded',()=>{
  fixture(user()+body());
  document.body.insertAdjacentHTML('beforeend','<div role="dialog" data-slot="popover-content"><div data-summary-panel-variant="dynamic-isle">輸出內容</div></div><div role="dialog" aria-modal="true" data-slot="popover-content"><div data-summary-panel-variant="dynamic-isle">Confirm</div></div>');
  assert.deepEqual(readChatPage().dialogs,['Confirm']);
});

test('hydrated response math and native code widgets preserve their rendered source',()=>{
  fixture(user()+String.raw`<div data-markdown-text-style="assistant-message">
    <p>Estimate <span data-d-component="math">(\beta_j)</span>.</p>
    <div data-d-block data-d-component="math"><p>[Y_{ij}=\mu+\beta_j]</p></div>
    <div data-client-defined-widget="code_block"><span hidden data-code-block-pending="true"></span>
      <div data-code-block="true" data-markdown-copy="code-block">
        <div data-markdown-copy="exclude"><svg></svg><div>Python</div><button>Copy</button></div>
        <div data-code-block-source="true"><code><span>x = 1


print(x)</span></code></div>
      </div>
    </div>
  </div>`,'complete');
  const md=readChatPage().turns[0].assistant_markdown;
  assert.ok(md.includes(String.raw`Estimate $\beta_j$.`));
  assert.ok(md.includes('$$\n'+String.raw`Y_{ij}=\mu+\beta_j`+'\n$$'));
  assert.ok(md.includes('```python\nx = 1\n\n\nprint(x)\n```'));
  assert.doesNotMatch(md,/Copy/);
});

test('code widget must hydrate before the turn is ready for completion',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message"><p>Code follows</p><div data-client-defined-widget="code_block"><span data-code-block-pending="true" hidden></span></div></div>','complete');
  assert.equal(readChatPage().turns[0].render_ready,false);
  document.querySelector('[data-client-defined-widget="code_block"]').insertAdjacentHTML('beforeend','<div data-code-block="true"><div data-code-block-source="true"><code>x = 1</code></div></div>');
  assert.equal(readChatPage().turns[0].render_ready,true);
});

test('reply action row is completion evidence when regenerate is inside the menu',()=>{
  fixture(user()+body()+'<div class="turn-action-controls"><button aria-label="複製"></button><button aria-label="更多操作"></button></div>','complete');
  const turn=readChatPage().turns[0];
  assert.equal(turn.regenerate,false);
  assert.equal(turn.final_actions,true);
  document.querySelector('.turn-action-controls').hidden=true;
  assert.equal(readChatPage().turns[0].final_actions,false);
});

test('rendered Mermaid in a code widget is an unsupported output, not pending code',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message"><p>Diagram follows</p><div data-client-defined-widget="code_block"><div data-chatgpt-mermaid-preview><div data-mermaid-render-status="ready"><svg role="img" aria-label="Mermaid 圖表"><text>Node</text></svg></div></div></div></div>','complete');
  const turn=readChatPage().turns[0];
  assert.equal(turn.render_ready,true);
  assert.equal(turn.reader_diagnostics.pending_code,false);
  assert.deepEqual(turn.unsupported_outputs,[{kind:'mermaid',title:'Mermaid 圖表',export_status:'UNSUPPORTED'}]);
  assert.match(turn.assistant_markdown,/Mermaid 圖表/);
  assert.doesNotMatch(turn.assistant_markdown,/Node/);
});

test('live CodeMirror widgets preserve language and every editor line',()=>{
  fixture(user()+'<div data-markdown-text-style="assistant-message"><div data-client-defined-widget="code_block"><div data-code-block="true"><div data-markdown-copy="exclude"><button>Run</button></div><div class="cm-content" data-language="python"><div class="cm-line"><span>s</span> = 4</div><div class="cm-line"><br></div><div class="cm-line"><br></div><div class="cm-line">print(s)</div></div></div></div></div>','complete');
  const turn=readChatPage().turns[0];
  assert.equal(turn.render_ready,true);
  assert.equal(turn.assistant_markdown,'```python\ns = 4\n\n\nprint(s)\n```');
});

test('adjacent and empty DOM blocks share one blank line without changing code or math',()=>{
  fixture(user()+String.raw`<div data-markdown-text-style="assistant-message">
    <h2>Heading</h2>
    <section><p>First paragraph.</p><p><br></p><p>Second paragraph.</p></section>
    <pre><code class="language-python">x = 1


print(x)</code></pre>
    <div data-d-block data-d-component="math"><p>[x=1


y=2]</p></div>
    <p>End.</p>
  </div>`,'complete');
  const md=readChatPage().turns[0].assistant_markdown;
  assert.equal(md,'## Heading\n\nFirst paragraph.\n\nSecond paragraph.\n\n```python\nx = 1\n\n\nprint(x)\n```\n\n$$\nx=1\n\n\ny=2\n$$\n\nEnd.');
});

test('reused space ID falls back to the unique exact name without claiming the wrong space',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function runtimeSpace()'),source.indexOf('\nasync function state('));
  const receipt={};const claimed=[];
  const run=new Function('input','receipt','listTaskSpaces','claimTaskSpace','takeOverTaskSpace','fail',`${fn};return runtimeSpace();`);
  const task=await run({space_id:2,space_name:'bridge',force_space_takeover:true},receipt,
    async()=>[{id:2,name:'other',ownership:'user'},{id:9,name:'bridge',ownership:'user'}],
    async id=>{claimed.push(id);return {spaceId:id,name:'bridge'};},async()=>assert.fail('wrong takeover'),code=>{throw Error(code);});
  assert.equal(task.spaceId,9);assert.deepEqual(claimed,[9]);assert.equal(receipt.runtime_space.reason,'named_space_id_changed');
});

for(const [model,copy,regenerate,remove,preview] of [
  ['選取 ChatGPT 模型','複製','重新產生回覆','移除','開啟 report.csv 預覽'],
  ['选择 ChatGPT 模型','复制','重新生成回复','删除','打开 report.csv 预览'],
  ['Select ChatGPT model','Copy','Regenerate response','Remove','Open report.csv preview'],
])test(`reader handles model, completion, attachment and file labels: ${model}`,()=>{
  const doc=fixture(user()+body()+`<button aria-label="${regenerate}"></button><button aria-label="${preview}"></button><div class="turn-action-controls"><button aria-label="${copy}"></button><button aria-label="More actions"></button></div>`,'complete');
  doc.querySelector('form button').setAttribute('aria-label',model);
  doc.querySelector('form').insertAdjacentHTML('beforeend',`<div data-composer-attachments><button aria-label="${remove} report.csv"></button></div>`);
  const s=readChatPage();assert.equal(s.model_label,'極高');assert.deepEqual(s.draft_attachments,['report.csv']);
  assert.equal(s.turns[0].regenerate,true);assert.equal(s.turns[0].final_actions,true);
  assert.equal(s.turns[0].artifacts[0].name,'report.csv');
});

test('control locator prioritizes test IDs, handles hidden duplicates and all locale fallbacks',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
  const definitions=source.slice(source.indexOf('const CONTROLS'),source.indexOf('async function control('));
  const {CONTROLS,findControl}=new Function(`${definitions};return {CONTROLS,findControl};`)();
  fixture('');
  document.querySelector('form').insertAdjacentHTML('beforeend','<button data-testid="model-switcher-dropdown-button">High</button>');
  let selector=findControl({selectors:CONTROLS.model,kind:'model',mark:true});
  assert.equal(document.querySelector(selector).getAttribute('data-testid'),'model-switcher-dropdown-button');
  for(const [kind,labels] of Object.entries({model:['選取 ChatGPT 模型','选择 ChatGPT 模型','Select ChatGPT model'],profile:['開啟個人檔案選單','打开个人资料菜单','Open profile menu'],send:['傳送','发送','Send prompt']})){
    for(const label of labels){
      fixture('');document.querySelector('form').innerHTML=`<button aria-label="${label}" hidden></button><button aria-label="${label}">target</button>`;
      selector=findControl({selectors:CONTROLS[kind],kind,mark:true});
      assert.ok(selector);assert.equal(document.querySelectorAll(selector).length,1);assert.equal(document.querySelector(selector).textContent,'target');
    }
  }
  fixture('');document.body.insertAdjacentHTML('beforeend','<div role="menuitem">選取模型</div>');
  assert.ok(findControl({selectors:CONTROLS.modelMenu,kind:'modelMenu',mark:true}));
});

test('missing named space is recreated even when its old ID belongs to another space',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function runtimeSpace()'),source.indexOf('\nasync function state('));
  const run=new Function('input','receipt','listTaskSpaces','taskSpace','claimTaskSpace','takeOverTaskSpace','fail',`${fn};return runtimeSpace();`);
  for(const spaces of [[],[{id:2,name:'other',ownership:'user'}]]){
    const receipt={},created=[];
    const task=await run({space_id:2,space_name:'bridge',recreate_missing_space:true,force_space_takeover:true},receipt,
      async()=>spaces,async name=>{created.push(name);return {spaceId:9,name:'bridge'};},
      ()=>assert.fail('must not claim unrelated space'),()=>assert.fail('must not take over unrelated space'),code=>{throw Error(code);});
    assert.equal(task.spaceId,9);assert.deepEqual(created,['bridge']);
    assert.equal(receipt.runtime_space.recreated,true);assert.equal(receipt.runtime_space.history_restored,false);
    assert.equal(receipt.runtime_space.previous_id,2);
    assert.equal(receipt.runtime_space.reason,spaces.length?'configured_name_missing_old_id_reused':'configured_name_missing');
  }
});

test('ambiguous names and disabled recreation never claim or create a space',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function runtimeSpace()'),source.indexOf('\nasync function state('));
  const run=new Function('input','receipt','listTaskSpaces','fail',`${fn};return runtimeSpace();`);
  const input={space_id:2,space_name:'bridge',force_space_takeover:true,recreate_missing_space:false};
  for(const [spaces,code] of [ [[], 'SPACE_MISSING'], [[{id:2,name:'renamed'}],'SPACE_MISSING'], [[{id:2,name:'bridge'},{id:3,name:'bridge'}],'SPACE_AMBIGUOUS'] ]){
    await assert.rejects(run(input,{},async()=>spaces,code=>{throw Error(code);}),new RegExp(code));
  }
});

test('observation reopens its verified conversation after space loss without using a reused page label',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function choosePage('),source.indexOf('\nasync function freshPage('));
  const run=new Function('input','receipt','identity','freshPage','usable','fail',`${fn};return task=>choosePage(task);`);
  const url='https://chatgpt.com/g/project/c/original',visited=[];
  const page={label:'p2',goto:async target=>visited.push(target)};
  const receipt={runtime_space:{recreated:true,space_id:9}};
  const input={action:'observe',space_id:2,page_label:'p1',conversation_url:url,project_id:'project'};
  const choose=run(input,receipt,url=>({conversation:url.split('/c/')[1]}),async()=>page,async()=>{},code=>{throw Error(code);});
  const task={tabs:async()=>[{label:'p1',url:'about:blank'}],page:()=>assert.fail('must ignore old label')};
  assert.equal(await choose(task),page);assert.deepEqual(visited,[url]);assert.equal(receipt.page_label,'p2');
  input.conversation_url=null;
  await assert.rejects(choose(task),/TAB_MISSING/);
  assert.deepEqual(visited,[url]);
});
