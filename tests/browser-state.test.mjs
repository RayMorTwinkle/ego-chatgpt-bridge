import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {readChatPage} from '../src/ego_bridge/page-state.mjs';

function fixture(content,state='in_progress'){
  const {window}=parseHTML(`<html><head><title>Fixture</title></head><body><div data-talvt-turn-state="${state}">${content}</div><form><div contenteditable="true"></div><button aria-label="選取 ChatGPT 模型">極高</button></form></body></html>`);
  window.HTMLElement.prototype.getClientRects=function(){for(let e=this;e;e=e.parentElement)if(e.hidden||e.style?.display==='none')return [];return [{}];};
  window.HTMLElement.prototype.getBoundingClientRect=function(){return {width:this.tagName==='SVG'?20:128,height:this.tagName==='SVG'?20:128};};
  globalThis.document=window.document;
  globalThis.location={href:'https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/c/bbbbbbbb-cccc-dddd-eeee-ffffffffffff'};
  globalThis.getComputedStyle=e=>({visibility:e.style.visibility||'visible'});
  return window.document;
}
const prompt='Compare a long research prompt.\n\n[ego-bridge request:msg_fixture]';
const user=()=>`<div data-chatgpt-search-message-ids="u1"><div data-user-message-bubble="true"><div data-search-result-target><div class="whitespace-pre-wrap">${prompt}</div></div><span aria-hidden="true">…</span><button data-thread-find-skip data-markdown-copy="exclude" aria-expanded="false">顯示較多</button></div><button aria-label="複製訊息"></button></div>`;
const thought='<div data-markdown-text-style="assistant-message" data-markdown-text-tone="tertiary" style="display:none">Searching sources</div>';
const body=()=>`<div data-chatgpt-selection-message-id="a1"><div data-markdown-text-style="assistant-message"><h2>Research answer</h2><p>Verified result <a href="https://example.org/official">Official source</a></p><div data-markdown-text-style="assistant-message"><pre><code>print("hello")\nprint("world")</code></pre><button data-markdown-copy="exclude">複製代碼</button></div></div></div>`;

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
