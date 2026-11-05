'use strict';
const $=id=>document.getElementById(id);
const state={owner:null,admin:false,query:null,message:null,queries:[],mcp:null,view:'queries',titles:new Map()};
const labels={QUEUED:'排队中',PREPARING:'准备中',SUBMITTING:'发送中',WAITING_FOR_RESPONSE:'等待回答',GENERATING:'生成中',COMPLETED:'已完成',NEEDS_ATTENTION:'需要处理',OUTCOME_UNKNOWN:'结果不确定',FAILED:'失败',CANCELLED:'已取消',RUNNING:'导出中',PARTIAL:'部分完成',AVAILABLE:'可下载',EXPIRED:'已过期',DOWNLOADING:'下载中',PENDING:'等待中'};
const active=new Set(['PREPARING','SUBMITTING','WAITING_FOR_RESPONSE','GENERATING','QUEUED']);
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,6000);}
function uid(){return crypto.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);}
function statusClass(status){return ['NEEDS_ATTENTION','OUTCOME_UNKNOWN','PARTIAL'].includes(status)?'badge warn':['FAILED','CANCELLED'].includes(status)?'badge error-badge':'badge';}
function date(seconds){return new Date(seconds*1000).toLocaleString(undefined,{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function size(n){return n==null?'尚无有效字节':n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(2)+' MB';}
async function api(path,data,method){const options={credentials:'same-origin',method:method||(data===undefined?'GET':'POST'),headers:{}};if(data!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}const response=await fetch('/ui/api/'+path,options);const result=await response.json();if(response.status===401){showLogin();throw Error(result.error?.message||'请重新登录');}if(!response.ok||result.ok===false)throw Error(result.error?.message||result.error?.error_code||'请求失败');return result;}
const tool=(name,args={})=>api('tool/'+name,args);
function showLogin(){$('login').hidden=false;$('app').hidden=true;state.owner=null;state.mcp=null;$('mcp-config').value='';if($('mcp-dialog').open)$('mcp-dialog').close();}
async function enter(){try{const me=await api('me');state.owner=me.owner;state.admin=me.admin;if(me.retention){$('retention-policy').textContent='文件保留 '+me.retention.artifact_days+' 天 · 文字记录保留';$('export-retention-hint').textContent='调试 ZIP 与工作文件保留 '+me.retention.export_days+' 天，到期自动清理。';}$('owner').textContent=me.owner;$('project-name').textContent=me.project_name;$('runtime-path').textContent=me.runtime_path||'';$('nav-exports').hidden=!me.admin;$('login').hidden=true;$('app').hidden=false;await loadQueries();}catch(error){showLogin();}}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{await api('login',{token:$('token').value});$('token').value='';await enter();}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{await api('logout',{});state.mcp=null;showLogin();});
function navigate(view){state.view=view;$('queries-view').hidden=view!=='queries';$('exports-view').hidden=view!=='exports';$('nav-queries').classList.toggle('active',view==='queries');$('nav-exports').classList.toggle('active',view==='exports');if(view==='exports')loadExports().catch(e=>toast(e.message));}
$('nav-queries').onclick=()=>navigate('queries');$('nav-exports').onclick=()=>navigate('exports');
$('new-open').onclick=()=>$('new-dialog').showModal();document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
let createRequest=null;
$('question').addEventListener('input',()=>{createRequest=null;});
$('create-form').addEventListener('submit',async event=>{event.preventDefault();event.submitter.disabled=true;try{createRequest??=uid();const text=$('question').value;const task=await tool('create_query',{text,effort:$('effort').value,request_id:createRequest});state.titles.set(task.query_id,text);state.query=task.query_id;state.message=null;createRequest=null;$('question').value='';$('new-dialog').close();await loadQueries();await loadDetail();toast('研究已创建，后台开始执行。');}catch(error){toast(error.message);}finally{event.submitter.disabled=false;}});
async function loadQueries(){if(!state.owner)return;const result=await tool('list_queries',{limit:100});state.queries=result.queries;$('count-total').textContent=result.queries.length;$('count-running').textContent=result.queries.filter(q=>active.has(q.status)).length;$('count-attention').textContent=result.queries.filter(q=>['NEEDS_ATTENTION','OUTCOME_UNKNOWN','FAILED'].includes(q.status)).length;renderQueries();}
function renderQueries(){const list=$('task-list');list.replaceChildren();const filtered=state.queries.filter(q=>!$('status-filter').value||q.status===$('status-filter').value);if(!filtered.length){list.append(node('div','暂无任务，创建一个研究开始吧。','empty'));return;}for(const q of filtered){const button=node('button',undefined,'task'+(q.id===state.query?' selected':''));const head=node('div',undefined,'task-head');head.append(node('span',labels[q.status]||q.status,statusClass(q.status)),node('span',date(q.created_at),'small muted'));button.append(head,node('div',state.titles.get(q.id)||q.title||'研究 '+q.id.slice(2,10),'task-title'));const meta=node('div',undefined,'task-meta');meta.append(node('span',q.conversation_id?'已关联网页会话':'待建立会话'),node('span',q.id.slice(0,14)+'…'));button.append(meta);button.onclick=async()=>{state.query=q.id;state.message=null;renderQueries();try{await loadDetail();}catch(error){toast(error.message);}};list.append(button);}}
$('status-filter').onchange=renderQueries;
async function loadDetail(){if(!state.query)return;const requestedQuery=state.query,requestedMessage=state.message;const [r,messages]=await Promise.all([tool('get_query_result',{query_id:state.query,...(state.message?{message_id:state.message}:{})}),tool('list_messages',{query_id:state.query})]);if(state.query!==requestedQuery||state.message!==requestedMessage)return;$('empty-detail').hidden=true;$('detail').hidden=false;$('detail-badge').textContent=labels[r.status]||r.status;$('detail-badge').className=statusClass(r.status);$('query-id').textContent=r.query_id;const first=messages.messages[0];if(first){state.titles.set(r.query_id,first.text);$('detail-title').textContent=first.text.length>70?first.text.slice(0,70)+'…':first.text;renderQueries();}const select=$('message-select');select.replaceChildren();for(const m of messages.messages){const option=node('option','第 '+m.sequence+' 轮 · '+(labels[m.status]||m.status));option.value=m.message_id;option.selected=m.message_id===r.message_id;select.append(option);}const link=$('conversation-link');link.hidden=!r.conversation_url;if(r.conversation_url)link.href=r.conversation_url;$('answer').textContent=r.result?.content||((active.has(r.status))?'任务正在执行，回答与产物会自动更新。':'此轮尚无文字回答。');$('delivery-info').hidden=!(r.generation_complete&&!r.delivery_complete);$('delivery-info').textContent=r.delivery_expired?'部分产物已超过保留期，下载已停用。可尝试重新收集产物，网页源文件可能也已失效。':'回答已生成，部分产物尚未完成回传。可查看错误或重新收集产物，无需重新提问。';$('error-detail').hidden=!r.last_error;$('error-text').textContent=r.last_error?JSON.stringify(r.last_error,null,2):'';$('retry').disabled=!(r.status==='NEEDS_ATTENTION'&&r.last_error?.safe_to_retry);$('sources').replaceChildren();for(const source of r.result?.sources||[]){try{const url=new URL(source.url);if(!['http:','https:'].includes(url.protocol))continue;const a=node('a',source.title||url.hostname);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';$('sources').append(a);}catch{}}
const artifacts=$('artifacts');artifacts.replaceChildren();for(const artifact of r.result?.artifacts||[]){const card=node('div',undefined,'artifact');if(artifact.status==='AVAILABLE'&&['image/png','image/jpeg','image/gif','image/webp'].includes(artifact.mime_type)){const img=node('img');img.alt=artifact.filename;img.loading='lazy';img.src='/ui/api/artifacts/'+encodeURIComponent(artifact.artifact_id);card.append(img);}const body=node('div',undefined,'artifact-body');body.append(node('div',artifact.filename,'artifact-name'),node('div',(artifact.mime_type||'产物')+' · '+size(artifact.size_bytes),'artifact-info'));if(artifact.status==='EXPIRED')body.append(node('p','已超过保留期，可尝试重新收集。','small muted'));if(artifact.expires_at&&artifact.status==='AVAILABLE')body.append(node('p','保留至 '+new Date(artifact.expires_at*1000).toLocaleString(),'small muted'));if(artifact.status==='AVAILABLE'){const a=node('a','下载原始文件 ↗','text-link');a.href='/ui/api/artifacts/'+encodeURIComponent(artifact.artifact_id)+'?download=1';body.append(a,node('code','SHA-256 '+artifact.sha256));}else{body.append(node('div',artifact.error?.message||labels[artifact.status]||artifact.status,'small error'));}card.append(body);artifacts.append(card);}$('unsupported').hidden=!(r.result?.unsupported_outputs?.length);$('unsupported').textContent=(r.result?.unsupported_outputs||[]).map(o=>o.kind+' 预览尚未导出原始内容').join('；');}
$('message-select').onchange=()=>{state.message=$('message-select').value;loadDetail().catch(e=>toast(e.message));};
let followRequest=null;
$('followup').addEventListener('input',()=>{followRequest=null;});
$('continue-form').onsubmit=async event=>{event.preventDefault();event.submitter.disabled=true;try{followRequest??={query:state.query,id:uid()};if(followRequest.query!==state.query)followRequest={query:state.query,id:uid()};await tool('continue_query',{query_id:state.query,text:$('followup').value,request_id:followRequest.id});followRequest=null;state.message=null;$('followup').value='';await loadQueries();await loadDetail();toast('追问已加入同一会话。');}catch(error){toast(error.message);}finally{event.submitter.disabled=false;}};
for(const [id,name] of [['recover','recover_session'],['retry','retry_query'],['collect','collect_artifacts'],['cancel','cancel_query']])$(id).onclick=async()=>{const button=$(id);button.disabled=true;try{const r=await tool(name,{query_id:state.query,...(state.message?{message_id:state.message}:{})});toast(name==='cancel_query'&&!r.cancelled?'已尝试停止；请以任务后续状态为准。':'操作已执行。');await loadQueries();await loadDetail();}catch(error){toast(error.message);}finally{button.disabled=false;}};
$('diagnose').onclick=async()=>{try{const r=await tool('diagnose',{query_id:state.query,...(state.message?{message_id:state.message}:{})});$('diagnose-text').textContent=JSON.stringify(r,null,2);$('diagnose-dialog').showModal();}catch(error){toast(error.message);}};
$('health').onclick=async()=>{const button=$('health');button.disabled=true;$('health-status').textContent='正在核对专用空间…';try{const r=await tool('health_check');$('health-status').textContent=r.runtime.healthy?'浏览器与账户可用':'需要查看配置与模型状态';}catch(error){$('health-status').textContent=error.message;}finally{button.disabled=false;}};
$('mcp-open').onclick=async()=>{
  try{
    state.mcp=await api('mcp-info',{});
    $('mcp-url').value=state.mcp.mcp_url;
    $('mcp-config').value=JSON.stringify(state.mcp.configuration,null,2);
    $('connection-hint').textContent=state.mcp.tls_enabled?'直连已启用 HTTPS。Token 配置包含当前身份的私密凭据。':'EasyTier 内 HTTP 无需导入 CA。Token 配置包含当前身份的私密凭据。';
    $('certificate-section').hidden=!state.mcp.tls_enabled;
    $('certificate-path').textContent=state.mcp.certificate_path?'客户端证书：'+state.mcp.certificate_path:'';
    $('credential-section').hidden=!state.mcp.token_file;
    $('token-path').value=state.mcp.token_file||'';
    const enabled=Boolean(state.mcp.oauth_mcp_url);
    $('public-unavailable').hidden=enabled;
    $('oauth-section').hidden=!enabled;
    $('oauth-grants-section').hidden=!enabled;
    $('oauth-url').value=state.mcp.oauth_mcp_url||'';
    $('oauth-config').value=enabled?JSON.stringify(state.mcp.oauth_configuration,null,2):'';
    $('copy-status').textContent='';$('manual-copy').value='';$('manual-copy').hidden=true;
    $('mcp-dialog').showModal();
    if(enabled){$('oauth-grants').textContent='正在读取授权…';loadGrants().catch(()=>{$('oauth-grants').textContent='暂时无法读取授权记录，请稍后重新打开。';});}
  }catch(error){toast(error.message);}
};
async function copy(text,sourceId){
  if(!text)return;
  $('manual-copy').value='';$('manual-copy').hidden=true;
  try{
    if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);}
    else{
      const field=$(sourceId),original=field.value;
      try{field.value=text;field.focus();field.select();if(!document.execCommand('copy'))throw Error('Copy unavailable');}
      finally{field.value=original;}
    }
    $('copy-status').textContent='已复制。';
  }catch{
    const field=$('manual-copy');field.hidden=false;field.value=text;field.focus();field.select();
    $('copy-status').textContent='自动复制不可用，请复制下面已选中的内容。';
  }
}
$('mcp-copy').onclick=()=>copy($('mcp-config').value,'mcp-config');
$('mcp-url-copy').onclick=()=>copy($('mcp-url').value,'mcp-url');
$('token-copy').onclick=()=>copy(state.mcp.authorization.slice(7),'mcp-config');
$('oauth-copy').onclick=()=>copy($('oauth-url').value,'oauth-url');
$('oauth-config-copy').onclick=()=>copy($('oauth-config').value,'oauth-config');
$('token-path-copy').onclick=()=>copy($('token-path').value,'token-path');
$('mcp-dialog').addEventListener('close',()=>{$('manual-copy').value='';$('manual-copy').hidden=true;});
$('export-form').onsubmit=async event=>{event.preventDefault();event.submitter.disabled=true;try{await api('exports',{conversation_url:$('export-url').value,request_id:uid()});toast('只读导出已加入队列。');await loadExports();}catch(error){toast(error.message);}finally{event.submitter.disabled=false;}};
async function loadExports(){if(!state.admin)return;const r=await api('exports');const list=$('export-list');list.replaceChildren();if(!r.exports.length){list.append(node('div','暂无导出记录。','empty'));return;}for(const job of r.exports){const item=node('div',undefined,'export-job');const head=node('div',undefined,'task-head');head.append(node('span',labels[job.status]||job.status,statusClass(job.status)),node('span',date(job.created_at),'small muted'));item.append(head,node('div',job.url,'job-url'));if(job.status==='EXPIRED')item.append(node('p','归档已超过保留期，下载已停用。'));else if(job.manifest)item.append(node('p','已读取 '+job.manifest.turn_count+' 轮 · '+job.manifest.artifacts.filter(a=>a.status==='AVAILABLE').length+' 个可用产物'));if(job.error)item.append(node('p',job.error.message||job.error.error_code,'error'));if(job.manifest?.warnings?.length)item.append(node('p',job.manifest.warnings.join(' '),'small muted'));const actions=node('div',undefined,'button-row');if(job.download_url){const a=node('a','下载 ZIP ↗','text-link');a.href=job.download_url;actions.append(a);}if(!['QUEUED','RUNNING'].includes(job.status)){const remove=node('button','清理','quiet');remove.onclick=async()=>{try{await api('exports/'+job.id,undefined,'DELETE');await loadExports();}catch(error){toast(error.message);}};actions.append(remove);}item.append(actions);list.append(item);}}
$('export-refresh').onclick=()=>loadExports().catch(e=>toast(e.message));
let refreshing=false;setInterval(async()=>{if(!state.owner||refreshing)return;refreshing=true;try{if(state.view==='queries'){await loadQueries();if(state.query)await loadDetail();}else await loadExports();}catch(error){if(state.owner)toast(error.message);}finally{refreshing=false;}},5000);
enter();

async function loadGrants(){const r=await api('oauth/grants');const list=$('oauth-grants');list.replaceChildren();for(const grant of r.grants){const row=node('div',undefined,'button-row');row.append(node('span',grant.client_name+' · '+(grant.revoked?'已撤销':grant.scopes.join(', '))));if(!grant.revoked){const button=node('button','撤销','quiet');button.onclick=async()=>{try{await api('oauth/grants',{id:grant.id});await loadGrants();}catch(error){toast(error.message);}};row.append(button);}list.append(row);}if(!r.grants.length)list.append(node('p','暂无授权。','small muted'));}
