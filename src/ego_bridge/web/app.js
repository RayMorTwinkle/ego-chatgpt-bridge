'use strict';
const $=id=>document.getElementById(id);
const state={owner:null,admin:false,queries:[],query:null,thread:null,view:'new',files:[],drafts:new Map(),expanded:new Set(),pending:null,mcp:null,search:''};
const RUNNING=new Set(['QUEUED','PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING','running']);
const PHASE={QUEUED:'排队中',PREPARING:'准备中',SUBMITTING:'发送中',WAITING_FOR_RESPONSE:'思考中',GENERATING:'生成中'};
const KIND_LABEL={running:'进行中',done:'',attention:'需要处理',failed:'失败',cancelled:'已取消'};
const EFFORT={extra_high:'极高思考',high:'高思考'};
const IMAGE=new Set(['image/png','image/jpeg','image/gif','image/webp']);
const EXPORT_LABEL={QUEUED:'排队中',RUNNING:'导出中',COMPLETED:'已完成',PARTIAL:'部分完成',FAILED:'失败',EXPIRED:'已过期'};

function kind(status){
  if(RUNNING.has(status))return 'running';
  if(status==='COMPLETED'||status==='done')return 'done';
  if(status==='FAILED'||status==='failed')return 'failed';
  if(status==='CANCELLED'||status==='cancelled')return 'cancelled';
  return 'attention';
}
function node(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;}
function button(text,cls,onclick){const b=node('button',text,cls);b.type='button';if(onclick)b.onclick=onclick;return b;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5000);}
function uid(){return crypto.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);}
function size(n){return n==null?'—':n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';}
function clock(seconds){
  const d=new Date(seconds*1000),now=new Date(),hm=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false});
  if(d.toDateString()===now.toDateString())return hm;
  return (d.getMonth()+1)+'月'+d.getDate()+'日 '+hm;
}
function shortDate(seconds){
  const d=new Date(seconds*1000),now=new Date();
  if(d.toDateString()===now.toDateString())return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false});
  return (d.getMonth()+1)+'月'+d.getDate()+'日';
}
function duration(seconds){
  seconds=Math.max(0,Math.round(seconds));const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;
  if(h)return h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  return m+':'+String(s).padStart(2,'0');
}
function spoken(seconds){const m=Math.round(seconds/60);return m<1?'不到 1 分钟':m<60?m+' 分钟':Math.floor(m/60)+' 小时 '+(m%60?m%60+' 分':'');}
// Research prompts often open with a role line ("你是一位……"); the question comes after it.
function smartTitle(text,limit=48){
  if(!text)return '未命名研究';
  let t=text.trim().replace(/^(你是|你现在是|你現在是|作为|作為|请你扮演|請你扮演|扮演)[^。！？\n]{0,90}[。！？\n]\s*/,'');
  t=(t||text).split('\n').find(line=>line.trim())||t;
  t=t.trim().replace(/^[#>*\-\s]+/,'');
  return t.length>limit?t.slice(0,limit)+'…':t;
}
function titleOf(q){return q?.chat_title||smartTitle(q?.title);}

async function api(path,data,method){
  const options={credentials:'same-origin',method:method||(data===undefined?'GET':'POST'),headers:{}};
  if(data!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}
  const response=await fetch('/ui/api/'+path,options);
  const result=await response.json().catch(()=>({ok:false,error:{message:'服务暂时不可用'}}));
  if(response.status===401){showLogin();throw Error(result.error?.message||'请重新登录');}
  if(!response.ok||result.ok===false){const error=Error(result.error?.message||result.error?.error_code||'请求失败');error.code=result.error?.error_code;throw error;}
  return result;
}
const tool=(name,args={})=>api('tool/'+name,args);

/* ---------- Session ---------- */
function showLogin(){$('login').hidden=false;$('app').hidden=true;state.owner=null;state.mcp=null;$('mcp-config').value='';if($('mcp-dialog').open)$('mcp-dialog').close();}
async function enter(){
  let me;
  try{me=await api('me');}catch{showLogin();return;}
  state.owner=me.owner;state.admin=me.admin;
  $('owner').textContent=me.owner;$('nav-exports').hidden=!me.admin;
  if(me.retention)$('export-retention-hint').textContent=' ZIP 保留 '+me.retention.export_days+' 天。';
  $('login').hidden=true;$('app').hidden=false;
  await loadQueries().catch(error=>toast(error.message));
  route();
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const b=event.submitter;b.disabled=true;$('login-error').textContent='';
  try{await api('login',{token:$('token').value});$('token').value='';await enter();}
  catch(error){$('login-error').textContent=error.message;}finally{b.disabled=false;}
});
$('logout').onclick=async()=>{await api('logout',{}).catch(()=>{});showLogin();};

/* ---------- Routing ---------- */
function go(hash){if(location.hash!==hash)location.hash=hash;else route();}
function route(){
  if(!state.owner)return;
  const hash=decodeURIComponent(location.hash.slice(1));
  const narrow=matchMedia('(max-width:820px)').matches;
  if(hash==='exports'&&state.admin)return showExports();
  if(hash.startsWith('q_'))return openThread(hash);
  if(hash==='new'||!narrow)return showNew();
  showList();
}
window.addEventListener('hashchange',route);
function setView(view){
  state.view=view;
  $('thread-view').hidden=view==='exports';$('exports-view').hidden=view!=='exports';
  $('app').classList.toggle('reading',view!=='list');
}
function saveDraft(){if(state.view==='new'||state.view==='thread')state.drafts.set(state.view==='new'?'new':state.query,$('prompt').value);}
function restoreDraft(key){$('prompt').value=state.drafts.get(key)||'';autosize();}
function showList(){saveDraft();state.query=null;setView('list');renderList();}
function showNew(){
  saveDraft();state.query=null;state.thread=null;setView('new');
  $('thread-view').classList.add('is-new');$('thread-head').hidden=true;$('welcome').hidden=false;
  $('thread').replaceChildren();$('doc-title').hidden=true;$('composer').hidden=false;$('send').textContent='提交研究';
  $('prompt').placeholder='写下研究问题……';
  restoreDraft('new');autosize();renderList();document.title='Ego 研究';
  if(!matchMedia('(max-width:820px)').matches)$('prompt').focus();
}
function showExports(){saveDraft();state.query=null;setView('exports');renderList();loadExports().catch(e=>toast(e.message));}
$('new-open').onclick=()=>go('#new');
$('back').onclick=()=>go('#');
document.querySelectorAll('[data-back]').forEach(b=>b.onclick=()=>go('#'));
$('nav-exports').onclick=()=>go('#exports');
document.addEventListener('keydown',event=>{
  if(event.key==='n'&&!event.metaKey&&!event.ctrlKey&&!event.altKey&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)&&!document.querySelector('dialog[open]')){event.preventDefault();go('#new');}
});

/* ---------- List ---------- */
async function loadQueries(){
  const result=await tool('list_queries',{limit:100});
  state.queries=result.queries.sort((a,b)=>b.created_at-a.created_at);
  renderList();
}
function renderList(){
  const list=$('task-list');list.replaceChildren();
  const needle=state.search.trim().toLowerCase();
  const items=state.queries.filter(q=>!needle||(titleOf(q)+' '+(q.title||'')).toLowerCase().includes(needle));
  if(!items.length){list.append(node('p',needle?'没有匹配的研究。':'还没有研究。','list-empty'));return;}
  const today=new Date();today.setHours(0,0,0,0);const t0=today.getTime()/1000;
  const bucket=s=>s>=t0?'今天':s>=t0-86400?'昨天':s>=t0-6*86400?'最近 7 天':'更早';
  let current=null;
  for(const q of items){
    const group=bucket(q.created_at);
    if(group!==current){current=group;list.append(node('div',group,'group-label'));}
    const item=node('button',undefined,'task'+(q.id===state.query?' selected':''));item.type='button';
    item.append(node('div',titleOf(q),'task-title'));
    const sub=node('div',undefined,'task-sub'),k=kind(q.status);
    if(k!=='done')sub.append(node('span',k==='running'?(PHASE[q.status]||'进行中'):KIND_LABEL[k],'s-'+k));
    sub.append(node('span',shortDate(q.updated_at||q.created_at)));
    if(q.rounds>1)sub.append(node('span',q.rounds+' 轮'));
    item.append(sub);item.title=q.title||'';
    item.onclick=()=>go('#'+q.id);
    list.append(item);
  }
}
$('search').addEventListener('input',event=>{state.search=event.target.value;renderList();});

/* ---------- Thread data ---------- */
function fromLegacy(message,r){
  const res=r?.result||{};
  return {id:message.message_id,seq:message.sequence,status:r?.status||message.status,prompt:message.text||'',attachments:message.attachments||[],
    answer:res.content||'',partial:!!r?.result_is_partial,sources:res.sources||[],images:res.external_images||[],files:res.artifacts||[],
    unsupported:res.unsupported_outputs||[],error:r?.last_error||message.last_error,genDone:!!r?.generation_complete,delivered:!!r?.delivery_complete,
    expired:!!r?.delivery_expired,created:message.created_at,updated:r?.updated_at||message.updated_at,progress:r?.progress||{},
    effort:message.requested_effort,retry:r?.auto_retry||null};
}
function fromThread(t){
  return {title:t.chat_title||null,url:t.conversation_url||null,rounds:(t.messages||[]).map(m=>({
    id:m.message_id,seq:m.round,status:m.raw_status||m.status,agent:m.status,phase:m.status==='running'?m.phase:null,
    prompt:m.prompt||'',attachments:m.attachments||[],answer:m.answer||'',partial:!!m.partial,sources:m.sources||[],
    images:m.external_images||[],files:(m.files||[]).map(f=>({status:'AVAILABLE',...f})),unsupported:m.unsupported_outputs||[],
    error:m.last_error||(m.problem?{error_code:m.problem.code,message:m.problem.message}:null),problem:m.problem||null,
    genDone:!!m.generation_complete,delivered:!!m.delivery_complete,expired:!!m.delivery_expired,
    created:m.created_at,updated:m.updated_at,progress:m.progress||{},effort:m.effort,retry:m.auto_retry||null}))};
}
// Rounds from get_thread carry the server's own five-state view (it knows about pending automatic retries).
function kindOf(r){return r.agent?kind(r.agent):kind(r.status);}
async function fetchThread(qid,previous){
  try{return fromThread(await tool('get_thread',{query_id:qid}));}
  catch(error){if(error.code!=='UNKNOWN_TOOL')throw error;}
  const {messages}=await tool('list_messages',{query_id:qid});
  const old=new Map((previous?.rounds||[]).map(r=>[r.id,r]));
  const rounds=await Promise.all(messages.map(async m=>{
    const prior=old.get(m.message_id);
    if(prior&&kindOf(prior)!=='running'&&prior.updated===m.updated_at&&prior.status===m.status)return prior;
    const r=await tool('get_query_result',{query_id:qid,message_id:m.message_id,content_format:'markdown',include_outputs:false}).catch(()=>null);
    return fromLegacy(m,r);
  }));
  return {title:null,url:[...messages].reverse().find(m=>m.conversation_url)?.conversation_url||null,rounds};
}

/* ---------- Thread view ---------- */
async function openThread(qid){
  const switching=state.query!==qid;
  if(switching){saveDraft();state.query=qid;state.thread=null;$('thread').replaceChildren();restoreDraft(qid);}
  setView('thread');
  $('thread-view').classList.remove('is-new');$('welcome').hidden=true;$('thread-head').hidden=false;$('composer').hidden=false;
  autosize();$('doc-title').hidden=false;$('send').textContent='追问';$('prompt').placeholder='继续追问，沿用同一对话的上下文';
  renderList();renderHead();
  try{
    const thread=await fetchThread(qid,state.thread);
    if(state.query!==qid)return;
    state.thread=thread;renderThread(switching);renderHead();
  }catch(error){toast(error.message);}
}
async function refreshThread(){
  if(state.view!=='thread'||!state.query)return;
  const qid=state.query,thread=await fetchThread(qid,state.thread);
  if(state.query!==qid)return;
  state.thread=thread;renderThread(false);renderHead();
}
function currentQuery(){return state.queries.find(q=>q.id===state.query);}
function renderHead(){
  const q=currentQuery(),t=state.thread,last=t?.rounds.at(-1);
  const title=t?.title||titleOf(q)||(t?.rounds[0]?smartTitle(t.rounds[0].prompt):'研究');
  $('thread-title').textContent=title;
  const full=t?.title||q?.chat_title||smartTitle(t?.rounds[0]?.prompt||q?.title,56);
  $('doc-title').textContent=full;$('doc-title').classList.toggle('long',full.length>30);document.title=title+' · Ego 研究';
  const status=last?.status||q?.status,k=last?kindOf(last):kind(status);
  const el=$('thread-status');el.className='status-text s-'+k;
  el.textContent=k==='running'?(last?.phase||PHASE[status]||'进行中'):k==='done'?((t?.rounds.length||0)>1?t.rounds.length+' 轮':''):KIND_LABEL[k];
  const url=t?.url||q?.conversation_url;$('conversation-link').hidden=!url;if(url)$('conversation-link').href=url;
}
function signature(r){return [r.status,r.agent,r.phase,r.answer.length,r.files.map(f=>f.status).join(),r.error?.error_code||'',r.updated,r.retry?.attempts||''].join('|');}
function renderThread(scrollToLatest){
  const box=$('thread'),scroller=$('scroller');
  const nearBottom=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<160;
  const existing=new Map([...box.children].map(n=>[n.dataset.id,n]));
  state.thread.rounds.forEach((r,index)=>{
    let el=existing.get(r.id);existing.delete(r.id);
    const sig=signature(r);
    if(!el||el.dataset.sig!==sig){const fresh=renderRound(r);fresh.dataset.id=r.id;fresh.dataset.sig=sig;if(el)el.replaceWith(fresh);el=fresh;}
    if(box.children[index]!==el)box.insertBefore(el,box.children[index]||null);
  });
  for(const el of existing.values())el.remove();
  if(scrollToLatest){
    const last=box.lastElementChild;
    scroller.scrollTop=last&&state.thread.rounds.length>1?last.offsetTop-12:0;
  }else if(nearBottom&&state.thread.rounds.length&&kindOf(state.thread.rounds.at(-1))==='running')scroller.scrollTop=scroller.scrollHeight;
  tick();
}

function renderRound(r){
  const turn=node('article',undefined,'turn'),k=kindOf(r);
  turn.append(renderTurnHead(r,k),renderBrief(r));
  if(k==='attention'||k==='failed')turn.append(problemCard(r));
  if(r.answer){const md=node('div',undefined,'answer md');md.append(window.renderMarkdown(r.answer));turn.append(md);}
  else if(k==='running'){
    const line=node('p',undefined,'status-line');
    line.append(node('span',r.phase||PHASE[r.status]||'进行中'),node('span','·','sep'),since(r.created));
    if(r.retry?.attempts)line.append(node('span','·','sep'),node('span','自动重试第 '+r.retry.attempts+' 次'));
    turn.append(line);
  }else if(k==='done')turn.append(node('p','这一轮没有文字回答。','answer muted'));
  if(r.unsupported.length)turn.append(node('p',r.unsupported.map(o=>o.kind).join('、')+' 这类预览还不能导出原始内容。','notice'));
  if(r.files.length)turn.append(renderFiles(r));
  const sources=[...r.sources,...r.images.map(i=>({...i,title:'引用图片 · '+(i.title||'图片')}))];
  if(sources.length)turn.append(renderSources(sources));
  const foot=renderFoot(r,k);if(foot.childElementCount)turn.append(foot);
  return turn;
}
function renderTurnHead(r,k){
  const head=node('header',undefined,'turn-head');
  head.append(node('span',r.seq>1?'追问 · 第 '+r.seq+' 轮':'提问','section-label'));
  const meta=node('span',undefined,'turn-meta');
  const parts=[clock(r.created)];
  if(r.effort)parts.push(EFFORT[r.effort]||r.effort);
  if(k==='done'&&r.updated&&r.created&&r.updated-r.created<4*3600)parts.push('用时 '+spoken(r.updated-r.created));
  if(k==='cancelled')parts.push('已取消');
  meta.textContent=parts.join(' · ');head.append(meta);return head;
}
function renderBrief(r){
  const brief=node('blockquote',undefined,'brief');brief.append(node('div',r.prompt,'brief-text'));
  if(r.prompt.split('\n').length>5||r.prompt.length>260){
    const open=state.expanded.has(r.id);brief.classList.toggle('clamped',!open);
    brief.append(button(open?'收起':'展开全文','brief-toggle',event=>{
      const now=!state.expanded.has(r.id);now?state.expanded.add(r.id):state.expanded.delete(r.id);
      brief.classList.toggle('clamped',!now);event.target.textContent=now?'收起':'展开全文';
    }));
  }
  if(r.attachments.length){const files=node('div',undefined,'brief-files');for(const f of r.attachments)files.append(node('span',f.filename+' · '+size(f.size_bytes),'chip'));brief.append(files);}
  return brief;
}
function since(start){const s=node('span',duration(Date.now()/1000-start));s.dataset.since=start;return s;}
function tick(){for(const el of document.querySelectorAll('[data-since]'))el.textContent=duration(Date.now()/1000-Number(el.dataset.since));}
setInterval(tick,1000);

function problemCard(r){
  const e=r.error||{},k=kindOf(r),box=node('div',undefined,'problem'+(k==='failed'?' failed':''));
  let title,hint='',actions=[];
  if(r.genDone&&!r.delivered){
    title=r.expired?'部分文件已过保留期':'回答已生成，有文件没取回来';
    hint='重新收集只下载文件，不会重新提问。';actions.push(['重新收集文件','collect_artifacts']);
  }else if(r.status==='OUTCOME_UNKNOWN'){
    title='还不确定消息有没有发出去';hint='已发送的消息不会被重发。先让服务端去网页上核对。';actions.push(['核对网页','recover_session']);
  }else if(e.safe_to_retry){
    title='消息还没发出去就失败了';hint='重试是安全的，不会重复提问。';actions.push(['重试','retry_query']);
  }else if(k==='failed')title='这一轮失败了';
  else{title='需要你看一下';hint='网页可能还在生成，先让服务端核对一次；不会重发消息。';actions.push(['核对网页','recover_session']);}
  box.append(node('div',title,'problem-title'));
  // what_to_do from the server is written for agents; people get the hint above.
  const detail=[e.message||r.problem?.message,hint].filter(Boolean).join(' ');
  if(detail)box.append(node('div',detail));
  const row=node('div',undefined,'button-row');
  actions.forEach(([label,name],n)=>row.append(button(label,n?'secondary':'primary',event=>act(name,r,event.target))));
  row.append(button('诊断记录','secondary',()=>showDiagnose(r.id)));
  box.append(row);return box;
}
function renderFiles(r){
  const box=node('div',undefined,'files'),images=node('div',undefined,'images'),rows=node('div',undefined,'file-rows');
  for(const f of r.files){
    const url='/ui/api/artifacts/'+encodeURIComponent(f.artifact_id);
    if(f.status==='AVAILABLE'&&IMAGE.has(f.mime_type)){
      const a=node('a',undefined,'image');a.href=url;a.target='_blank';a.rel='noopener';a.title=f.filename;
      const img=node('img');img.alt=f.filename;img.src=url;a.append(img);images.append(a);continue;
    }
    const row=node('div',undefined,'file-row');
    const ext=(f.filename.match(/\.([a-z0-9]{1,6})$/i)?.[1]||f.mime_type?.split('/')[1]||'文件').toUpperCase();
    row.append(node('span',ext.slice(0,4),'file-type'),node('span',f.filename,'file-name'),node('span',size(f.size_bytes),'file-info'));
    if(f.status==='AVAILABLE'){const a=node('a','下载');a.href=url+'?download=1';row.append(a);}
    else row.append(node('span',f.status==='EXPIRED'?'已过保留期':f.error?.message||'还没取回','file-info error'));
    rows.append(row);
  }
  if(images.childElementCount)box.append(images);
  if(rows.childElementCount)box.append(rows);
  if(r.expired)box.append(node('p','部分文件已过保留期，下载已停用。','notice'));
  return box;
}
function renderSources(sources){
  const details=node('details',undefined,'sources');details.append(node('summary','来源 · '+sources.length));
  const ol=node('ol');
  for(const s of sources){
    try{
      const url=new URL(s.url);if(!['http:','https:'].includes(url.protocol))continue;
      const li=node('li'),a=node('a',s.title||url.hostname);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';
      li.append(a,node('span',url.hostname.replace(/^www\./,''),'host'));ol.append(li);
    }catch{}
  }
  details.append(ol);return details;
}
function renderFoot(r,k){
  const foot=node('footer',undefined,'turn-foot');
  if(k==='running'){
    if(r.answer){const live=node('span',undefined,'live');live.append((r.phase||PHASE[r.status]||'生成中')+' · '+(r.progress?.answer_characters||r.answer.length).toLocaleString()+' 字 · ',since(r.created));foot.append(live);}
    foot.append(button('停止',undefined,event=>act('cancel_query',r,event.target)));
    return foot;
  }
  if(r.answer){
    foot.append(button('复制',undefined,()=>copyText(r.answer).then(()=>toast('已复制回答。'))));
    foot.append(button('下载 Markdown',undefined,()=>downloadMarkdown(r)));
  }
  return foot;
}
async function act(name,r,el){
  if(el)el.disabled=true;
  try{
    const result=await tool(name,{query_id:state.query,message_id:r.id});
    toast(name==='cancel_query'?(result.cancelled?'已停止。':'已请求网页停止，以后续状态为准。'):name==='collect_artifacts'?'正在重新收集文件。':name==='retry_query'?'已重新排队。':'正在核对网页。');
    await Promise.all([loadQueries(),refreshThread()]);
  }catch(error){toast(error.message);}finally{if(el)el.disabled=false;}
}
async function showDiagnose(messageId){
  try{
    const r=await tool('diagnose',{query_id:state.query,...(messageId?{message_id:messageId}:{})});
    $('diagnose-text').textContent=JSON.stringify(r,null,2);$('diagnose-dialog').showModal();
  }catch(error){toast(error.message);}
}
$('diagnose').onclick=()=>showDiagnose(state.thread?.rounds.at(-1)?.id);
function downloadMarkdown(r){
  const title=titleOf(currentQuery())||'research';
  const blob=new Blob(['# '+title+'\n\n'+r.answer+'\n'],{type:'text/markdown;charset=utf-8'});
  const a=node('a');a.href=URL.createObjectURL(blob);a.download=title.replace(/[\\/:*?"<>|\s]+/g,'-').slice(0,60)+(r.seq>1?'-'+r.seq:'')+'.md';
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}

/* ---------- Composer ---------- */
function autosize(){const t=$('prompt');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,window.innerHeight*.4)+'px';}
$('prompt').addEventListener('input',autosize);
$('prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();$('composer').requestSubmit();}});
$('files').addEventListener('change',event=>{state.files.push(...event.target.files);event.target.value='';renderChips();});
function renderChips(){
  const box=$('file-chips');box.replaceChildren();box.hidden=!state.files.length;
  state.files.forEach((file,n)=>{const chip=node('span',undefined,'chip');chip.append(node('span',file.name+' · '+size(file.size)),button('×',undefined,()=>{state.files.splice(n,1);renderChips();}));box.append(chip);});
}
async function encodeFiles(){
  const files=state.files;
  if(files.length>4||files.reduce((t,f)=>t+f.size,0)>40*1024*1024||files.some(f=>!f.size))throw Error('附件最多 4 个、合计 40 MiB，且不能是空文件。');
  return Promise.all(files.map(file=>new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve({filename:file.name,content_base64:reader.result.split(',')[1]});
    reader.onerror=()=>reject(Error('无法读取附件 '+file.name));
    reader.readAsDataURL(file);
  })));
}
$('composer').addEventListener('submit',async event=>{
  event.preventDefault();
  const text=$('prompt').value.trim();if(!text){$('prompt').focus();return;}
  const creating=state.view==='new',qid=state.query,send=$('send');
  // Same content resubmitted after a network error reuses the idempotency key.
  const key=JSON.stringify([creating?'new':qid,text,$('effort').value,state.files.map(f=>[f.name,f.size,f.lastModified])]);
  if(state.pending?.key!==key)state.pending={key,id:uid()};
  send.disabled=true;
  try{
    const args={text,effort:$('effort').value,request_id:state.pending.id,attachments:await encodeFiles()};
    const result=creating?await tool('create_query',args):await tool('continue_query',{...args,query_id:qid});
    state.pending=null;state.files=[];renderChips();$('prompt').value='';autosize();
    state.drafts.delete(creating?'new':qid);
    await loadQueries();
    if(creating)go('#'+result.query_id);else await refreshThread();
    $('scroller').scrollTop=$('scroller').scrollHeight;
  }catch(error){toast(error.message);}finally{send.disabled=false;}
});

/* ---------- Health ---------- */
$('health').onclick=async()=>{
  const b=$('health'),dot=b.querySelector('.dot');b.disabled=true;$('health-status').textContent='正在检查服务端浏览器…';
  try{const r=await tool('health_check');const ok=r.runtime?.healthy;dot.className='dot '+(ok?'ok':'warn');$('health-status').textContent=ok?'GPT-6 · 浏览器正常':'浏览器需要检查';}
  catch(error){dot.className='dot warn';$('health-status').textContent=error.message;}
  finally{b.disabled=false;}
};

/* ---------- Polling ---------- */
let polling=false;
setInterval(async()=>{
  if(!state.owner||polling||document.hidden)return;polling=true;
  try{
    if(state.view==='exports'){await loadExports();return;}
    const before=currentQuery()?.updated_at;await loadQueries();
    const running=state.thread?.rounds.some(r=>kindOf(r)==='running');
    if(state.view==='thread'&&(running||currentQuery()?.updated_at!==before)){await refreshThread();}
  }catch(error){if(state.owner)toast(error.message);}finally{polling=false;}
},4000);

/* ---------- Agent connection ---------- */
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('mcp-open').onclick=async()=>{
  try{
    state.mcp=await api('mcp-info',{});
    $('mcp-url').value=state.mcp.mcp_url;
    $('mcp-config').value=JSON.stringify(state.mcp.configuration,null,2);
    $('connection-hint').textContent=state.mcp.tls_enabled?'直连已启用 HTTPS。':'';
    $('certificate-section').hidden=!state.mcp.tls_enabled;
    $('certificate-path').textContent=state.mcp.certificate_path?'证书：'+state.mcp.certificate_path:'';
    $('credential-section').hidden=!state.mcp.token_file;
    $('token-path').value=state.mcp.token_file||'';
    const enabled=Boolean(state.mcp.oauth_mcp_url);
    $('public-unavailable').hidden=enabled;$('oauth-section').hidden=!enabled;$('oauth-grants-section').hidden=!enabled;
    $('oauth-url').value=state.mcp.oauth_mcp_url||'';
    $('oauth-config').value=enabled?JSON.stringify(state.mcp.oauth_configuration,null,2):'';
    $('copy-status').textContent='';$('manual-copy').value='';$('manual-copy').hidden=true;
    $('mcp-dialog').showModal();
    if(enabled){$('oauth-grants').textContent='正在读取授权…';loadGrants().catch(()=>{$('oauth-grants').textContent='暂时读不到授权记录。';});}
  }catch(error){toast(error.message);}
};
async function copyText(text,sourceId){
  if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);return;}
  const field=$(sourceId||'manual-copy'),original=field.value,wasHidden=field.hidden;
  try{field.hidden=false;field.value=text;field.focus();field.select();if(!document.execCommand('copy'))throw Error('复制不可用');}
  finally{field.value=original;field.hidden=wasHidden;}
}
async function copy(text,sourceId){
  if(!text)return;$('manual-copy').value='';$('manual-copy').hidden=true;
  try{await copyText(text,sourceId);$('copy-status').textContent='已复制。';}
  catch{const field=$('manual-copy');field.hidden=false;field.value=text;field.focus();field.select();$('copy-status').textContent='自动复制不可用，请手动复制下面已选中的内容。';}
}
$('mcp-copy').onclick=()=>copy($('mcp-config').value,'mcp-config');
$('mcp-url-copy').onclick=()=>copy($('mcp-url').value,'mcp-url');
$('token-copy').onclick=()=>copy(state.mcp.authorization.slice(7),'mcp-config');
$('oauth-copy').onclick=()=>copy($('oauth-url').value,'oauth-url');
$('oauth-config-copy').onclick=()=>copy($('oauth-config').value,'oauth-config');
$('token-path-copy').onclick=()=>copy($('token-path').value,'token-path');
$('mcp-dialog').addEventListener('close',()=>{$('manual-copy').value='';$('manual-copy').hidden=true;});
async function loadGrants(){
  const r=await api('oauth/grants'),list=$('oauth-grants');list.replaceChildren();
  for(const grant of r.grants){
    const row=node('div',undefined,'button-row');row.append(node('span',grant.client_name+' · '+(grant.revoked?'已撤销':grant.scopes.join(', '))));
    if(!grant.revoked)row.append(button('撤销','secondary',async()=>{try{await api('oauth/grants',{id:grant.id});await loadGrants();}catch(error){toast(error.message);}}));
    list.append(row);
  }
  if(!r.grants.length)list.append(node('p','暂无授权。','small muted'));
}

/* ---------- Exports ---------- */
$('export-form').onsubmit=async event=>{
  event.preventDefault();event.submitter.disabled=true;
  try{await api('exports',{conversation_url:$('export-url').value,request_id:uid()});$('export-url').value='';toast('已加入导出队列。');await loadExports();}
  catch(error){toast(error.message);}finally{event.submitter.disabled=false;}
};
$('export-refresh').onclick=()=>loadExports().catch(e=>toast(e.message));
async function loadExports(){
  if(!state.admin)return;
  const r=await api('exports'),list=$('export-list');list.replaceChildren();
  if(!r.exports.length){list.append(node('p','还没有导出记录。','list-empty'));return;}
  for(const job of r.exports){
    const item=node('div',undefined,'export-job'),head=node('div',undefined,'job-head');
    head.append(node('span',EXPORT_LABEL[job.status]||job.status),node('span',clock(job.created_at)));
    item.append(head,node('div',job.url,'job-url'));
    if(job.status==='EXPIRED')item.append(node('p','已过保留期，下载已停用。'));
    else if(job.manifest)item.append(node('p','读取 '+job.manifest.turn_count+' 轮 · '+job.manifest.artifacts.filter(a=>a.status==='AVAILABLE').length+' 个文件'));
    if(job.error)item.append(node('p',job.error.message||job.error.error_code,'error'));
    if(job.manifest?.warnings?.length)item.append(node('p',job.manifest.warnings.join(' ')));
    const actions=node('div',undefined,'button-row');
    if(job.download_url){const a=node('a','下载 ZIP','quiet-link');a.href=job.download_url;actions.append(a);}
    if(!['QUEUED','RUNNING'].includes(job.status))actions.append(button('清理','quiet-link',async()=>{try{await api('exports/'+job.id,undefined,'DELETE');await loadExports();}catch(error){toast(error.message);}}));
    item.append(actions);list.append(item);
  }
}

enter();
