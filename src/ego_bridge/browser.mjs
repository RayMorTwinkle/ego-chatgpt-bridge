// Executed only by `ego-browser nodejs`; this is the installed Ego API, not Playwright.
// `input` is injected as a JSON value by adapter.py, never through a shell.
const MODEL_BUTTON = 'loc=css:button[aria-label="選取 ChatGPT 模型"]';
const EDITOR = 'loc=css:form [contenteditable="true"]';
const norm = s => String(s ?? '').replace(/\s+/g, ' ').trim();
const fail = (code, message, stage='browser') => { const e=new Error(message); e.code=code; e.stage=stage; throw e; };
const identity = url => {
  const u=new URL(url);
  if(u.origin!=='https://chatgpt.com') return {};
  return {project:u.pathname.match(/\/g\/(g-p-[a-f0-9]{32})(?:-|\/)/)?.[1], conversation:u.pathname.match(/\/c\/([^/]+)/)?.[1]};
};
let receipt={};

async function usable(page) {
  try { await page.waitForSelector(EDITOR,{state:'visible',timeout:8000}); }
  catch(error) {
    const s=await state(page);
    if(s.login) fail('LOGIN_REQUIRED','页面要求重新登录。','prepare');
    if(s.identity.project!==input.project_id) fail('PROJECT_MISMATCH','加载后项目地址不匹配。','prepare');
    receipt.reloaded=true;
    try { await page.reload({timeout:12000}); } catch(error) {receipt.navigation_wait_failed=true;}
    try { await page.waitForSelector(EDITOR,{state:'visible',timeout:12000}); }
    catch(error) {fail('PAGE_LOAD_FAILED','固定项目页面刷新后仍无法加载编辑器。','prepare');}
  }
}

async function runtimeSpace() {
  const spaces=await listTaskSpaces();
  const entry=spaces.find(s=>s.id===input.space_id);
  if(!entry) fail('SPACE_MISSING','专用空间不存在；不能通过创建空空间伪装恢复。');
  if(entry.name!==input.space_name) fail('SPACE_IDENTITY_MISMATCH','空间名称和持久化标识不一致。');
  if(entry.ownership==='user') fail('SPACE_USER_CONTROL','空间当前由用户控制；服务不能自动夺回。');
  return await taskSpace(entry.id);
}

async function state(page) {
  const result=await page.evaluate(()=>{
    const visible=e=>!!e?.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
    const turns=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).map(turn=>{
      const user=turn.querySelector('[data-chatgpt-search-message-ids]');
      const response=turn.querySelector('[data-markdown-text-style="assistant-message"]');
      const selected=turn.querySelector('[data-chatgpt-selection-message-id]');
      const files=Array.from(turn.querySelectorAll('button[aria-label^="開啟 "][aria-label$=" 預覽"]')).filter(e=>!user?.contains(e));
      const artifacts=files.map((e,index)=>({kind:'file_card',index,name:e.getAttribute('aria-label').slice(3,-3)}));
      const images=Array.from(turn.querySelectorAll('img')).filter(e=>!user?.contains(e)&&visible(e)&&e.getBoundingClientRect().width>32&&e.getBoundingClientRect().height>32);
      for(const [index,image] of images.entries()) artifacts.push({kind:'inline_image',index,name:`image-${index+1}.png`});
      const unsupported_outputs=Array.from(turn.querySelectorAll('audio,video,canvas,iframe,svg')).filter(e=>!user?.contains(e)&&visible(e)&&e.getBoundingClientRect().width>64&&e.getBoundingClientRect().height>32).map(e=>({kind:e.tagName.toLowerCase(),title:(e.getAttribute('aria-label')||e.getAttribute('title')||'').slice(0,200),export_status:'UNSUPPORTED'}));
      return {state:turn.getAttribute('data-talvt-turn-state'),user_id:user?.getAttribute('data-chatgpt-search-message-ids')??null,
        user_text:user?.innerText??'',assistant_id:selected?.getAttribute('data-chatgpt-selection-message-id')??null,
        assistant_text:response?.innerText??'',sources:response?Array.from(response.querySelectorAll('a[href]')).filter(visible).map(a=>({title:a.innerText,url:a.href})):[],
        artifacts,unsupported_outputs,regenerate:!!turn.querySelector('button[aria-label="重新產生回覆"]')};
    });
    const editor=document.querySelector('form [contenteditable="true"]');
    return {url:location.href,title:document.title,turns,editor_present:visible(editor),draft:editor?.innerText??'',
      model_label:document.querySelector('button[aria-label="選取 ChatGPT 模型"]')?.innerText,
      buttons:Array.from(document.querySelectorAll('form button')).filter(visible).map(b=>({label:b.getAttribute('aria-label'),disabled:b.disabled})),
      dialogs:Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
      alerts:Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
      login:!editor && Array.from(document.querySelectorAll('button,a')).filter(visible).some(e=>/^(登入|登录|Log in|Sign in)$/.test(e.innerText.trim())),
      breadcrumb:Array.from(document.querySelectorAll('a[href]')).some(a=>a.getAttribute('href')?.endsWith('/project')&&visible(a))};
  });
  result.identity=identity(result.url);
  return result;
}

function check(s, conversation) {
  if(s.login) fail('LOGIN_REQUIRED','ChatGPT 登录已失效。','page');
  if(s.identity.project!==input.project_id) fail('PROJECT_MISMATCH','当前页面不属于固定项目。','page');
  if(conversation && s.identity.conversation!==conversation) fail('CONVERSATION_MISMATCH','当前标签页已切换到其他会话。','page');
  if(s.dialogs.length) fail('UNEXPECTED_DIALOG','出现阻塞对话框；需要人工查看。','page');
  if(!s.editor_present) fail('UI_CHANGED','找不到已验证的消息编辑器。','page');
}

async function choosePage(task, create=false) {
  const tabs=await task.tabs();
  let found=input.page_label && tabs.find(t=>t.label===input.page_label);
  if(found && input.conversation_url && identity(found.url).conversation!==identity(input.conversation_url).conversation)
    fail('CONVERSATION_MISMATCH','已绑定标签页被人工切换；停止操作。');
  if(!found && input.conversation_url) found=tabs.find(t=>identity(t.url).conversation===identity(input.conversation_url).conversation);
  if(!found && input.action==='health') found=tabs.find(t=>identity(t.url).project===input.project_id);
  if(found) {
    const page=found.label?task.page(found.label):await task.adopt(found.page);
    if(input.action==='prepare') await usable(page);
    return page;
  }
  if(!create) fail('TAB_MISSING','找不到关联标签页，不能猜测另一条会话。');
  const page=await task.newPage();
  receipt.page_label=page.label;
  try {await page.goto(input.conversation_url??input.project_url,{timeout:10000});}
  catch(error) {
    // A load timeout may still leave the correct committed document. Inspect it;
    // never infer failure or create a different space from this timeout alone.
    if(identity(await page.url()).project!==input.project_id) throw error;
    receipt.navigation_wait_failed=true;
  }
  await usable(page);
  return page;
}

async function account(page) {
  await page.click('loc=css:button[aria-label="開啟個人檔案選單"]');
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('[role="menu"]')).some(e=>/登出/.test(e.innerText)),undefined,{timeout:8000});
  const confirmed=await page.evaluate(expected=>Array.from(document.querySelectorAll('[role="menu"]')).some(e=>e.innerText.split('\n')[0].trim()===expected),input.expected_account);
  await page.keyboard.press('Escape');
  if(!confirmed) fail('ACCOUNT_MISMATCH','当前账户显示名称与配置不符。','account');
  return {level:'display_name_only',confirmed:true};
}

async function modelInfo(page) {
  await page.keyboard.press('Escape');
  await page.waitForSelector(MODEL_BUTTON,{state:'visible',timeout:10000});
  await page.click(MODEL_BUTTON);
  return await page.evaluate(()=>({
    models:Array.from(document.querySelectorAll('[role="menuitemradio"]')).map(e=>({label:e.textContent?.trim(),checked:e.getAttribute('aria-checked')==='true'})),
    effort_value:Number(document.querySelector('[role="slider"]')?.getAttribute('aria-valuenow')),
    label:document.querySelector('button[aria-label="選取 ChatGPT 模型"]')?.innerText,
    disabled:document.querySelector('[aria-label="效能"]')?.getAttribute('aria-disabled')==='true'
  }));
}

async function capture(page) {
  const fs=await import('node:fs/promises');
  const c=input.candidate;
  if(c.kind==='inline_image') {
    const src=await page.evaluate(({userId,index})=>{
      const turn=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).find(t=>t.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')===userId);
      if(!turn)return null;const user=turn.querySelector('[data-chatgpt-search-message-ids]');
      const images=Array.from(turn.querySelectorAll('img')).filter(e=>!user?.contains(e)&&e.getClientRects().length&&e.getBoundingClientRect().width>32&&e.getBoundingClientRect().height>32);
      return images[index]?.currentSrc||images[index]?.src;
    },{userId:input.user_message_id,index:c.index});
    if(!src)fail('ARTIFACT_ASSOCIATION_FAILED','当前回答找不到对应图片。','artifact');
    const url=new URL(src);
    if(!['blob:','data:'].includes(url.protocol)&&!(url.protocol==='https:'&&(url.hostname==='chatgpt.com'||url.hostname.endsWith('.oaiusercontent.com'))))
      fail('EXTERNAL_IMAGE_NOT_COPIED','外部站点图片保留为网页内容，不由服务自动抓取。','artifact');
    const response=await page.fetch(src,{saveAs:input.destination});
    const type=response.headers['content-type']??response.headers['Content-Type']??'';
    if(!response.ok||!/^image\/(png|jpeg|gif|webp)(;|$)/.test(type))fail('ARTIFACT_TYPE_MISMATCH','图片响应状态或 MIME 不符合可回传的图像类型。','artifact');
    return {saved:true,transport:'rendered_image_url',mime_type:type.split(';')[0]};
  }
  if(c.kind!=='file_card') fail('ARTIFACT_CAPTURE_UNSUPPORTED','该产物呈现方式尚未验证可安全导出原始字节。','artifact');
  const token='artifact-'+Date.now().toString(36);
  const selected=await page.evaluate(({userId,candidate,token})=>{
    const turn=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).find(t=>t.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')===userId);
    if(!turn)return false;
    const user=turn.querySelector('[data-chatgpt-search-message-ids]');
    const previews=Array.from(turn.querySelectorAll('button[aria-label^="開啟 "][aria-label$=" 預覽"]')).filter(e=>!user?.contains(e));
    const preview=previews[candidate.index];
    if(!preview||preview.getAttribute('aria-label')!==`開啟 ${candidate.name} 預覽`)return false;
    let card=preview.parentElement;
    while(card&&card!==turn){
      const downloads=card.querySelectorAll('button[aria-label="下載檔案"]');
      if(downloads.length===1){preview.setAttribute('data-ego-preview',token);downloads[0].setAttribute('data-ego-download',token);return true;}
      if(downloads.length>1)return false;
      card=card.parentElement;
    }
    return false;
  },{userId:input.user_message_id,candidate:c,token});
  if(!selected)fail('ARTIFACT_ASSOCIATION_FAILED','找不到该消息中对应的唯一文件卡片。','artifact');
  await page.evaluate(token=>Array.from(document.querySelectorAll('[data-ego-preview]')).find(e=>e.getAttribute('data-ego-preview')===token)?.scrollIntoView({block:'center',inline:'nearest'}),token);
  await page.hover(`loc=css:[data-ego-preview="${token}"]`);
  await page.cdp('Network.enable');await page.events();
  try {
    await page.click(`loc=css:[data-ego-download="${token}"]`);
    const responses=new Map(),finished=new Set(),checked=new Set();let metadata,resourceFetchAttempted=false;
    const deadline=Date.now()+30000;
    while(Date.now()<deadline){
      for(const event of await page.events()){
        if(event.method==='Network.responseReceived')responses.set(event.params.requestId,event.params.response);
        if(event.method==='Network.loadingFinished')finished.add(event.params.requestId);
      }
      for(const [id,response] of responses){
        if(!finished.has(id)||checked.has(id))continue;
        // Observe only the actual UI-triggered metadata response. No hidden endpoint
        // is called/replayed and no cookies, headers or signed URL are exported.
        if(response.status===200&&response.mimeType==='application/json'&&new URL(response.url).pathname.endsWith('/interpreter/download')){
          checked.add(id);
          const body=await page.cdp('Network.getResponseBody',{requestId:id});
          const data=JSON.parse(body.base64Encoded?Buffer.from(body.body,'base64').toString():body.body);
          if(data.file_name===c.name&&typeof data.download_url==='string')metadata=data;
        }
      }
      if(metadata){
        if(!resourceFetchAttempted){
          resourceFetchAttempted=true;
          const resourceUrl=new URL(metadata.download_url);
          if(resourceUrl.protocol!=='https:'||!(resourceUrl.hostname==='chatgpt.com'||resourceUrl.hostname.endsWith('.oaiusercontent.com')))
            fail('ARTIFACT_RESOURCE_UNTRUSTED','网页下载元数据指向了未核验的文件来源。','artifact');
          // Download only the asset URL returned by the user's real UI action,
          // using browser semantics. No auth headers are read or copied.
          const fetched=await page.fetch(metadata.download_url,{saveAs:input.destination});
          const stat=await fs.stat(input.destination).catch(()=>null);
          if(fetched.ok&&stat?.size>0){
            if(Number.isFinite(metadata.file_size_bytes)&&metadata.file_size_bytes>0&&metadata.file_size_bytes!==stat.size)fail('ARTIFACT_INCOMPLETE','文件字节数与非零下载元数据不一致。','artifact');
            return {saved:true,transport:'ui_download_asset_url',size_bytes:stat.size,mime_type:fetched.headers['content-type']??metadata.mime_type,provider_size_bytes:metadata.file_size_bytes};
          }
        }
        const resource=Array.from(responses.entries()).find(([id,r])=>r.url===metadata.download_url&&finished.has(id));
        if(resource){
          const [id,r]=resource;
          if(r.status!==200)fail('ARTIFACT_DOWNLOAD_FAILED',`网页下载返回 HTTP ${r.status}。`,'artifact');
          const body=await page.cdp('Network.getResponseBody',{requestId:id});
          const bytes=Buffer.from(body.body,body.base64Encoded?'base64':'utf8');
          if(Number.isFinite(metadata.file_size_bytes)&&metadata.file_size_bytes!==bytes.length)fail('ARTIFACT_INCOMPLETE','收到的字节数与网页文件元数据不一致。','artifact');
          await fs.writeFile(input.destination,bytes,{mode:0o600});
          return {saved:true,transport:'ui_download_response',size_bytes:bytes.length,mime_type:r.mimeType,provider_size_bytes:metadata.file_size_bytes};
        }
      }
      await new Promise(r=>setTimeout(r,150));
    }
    fail('ARTIFACT_DOWNLOAD_UNCONFIRMED','已点击下载，但未观察到完整的原始文件字节。','artifact');
  } finally {await page.cdp('Network.disable');}
}

async function configure(page, change=true) {
  let info=await modelInfo(page);
  if(!info.models.some(m=>m.label==='GPT-6'&&m.checked)) {
    if(!change || !info.models.some(m=>m.label==='GPT-6')) fail('MODEL_UNAVAILABLE','默认 GPT-6 不可验证。','model');
    await page.click('loc=role:menuitem[name="選取模型"]');
    await page.click('text="GPT-6"');
    info=await modelInfo(page);
  }
  let target=input.effort==='high'?2:3;
  let fallback=false;
  const selectedHigh=()=>info.effort_value===2&&/(^|\s)High($|\s)|推理強度\s*高$/.test(info.label);
  if(change&&target===3&&info.disabled&&selectedHigh()) {target=2;fallback=true;}
  if(info.effort_value!==target) {
    if(!change || info.disabled || ![2,3].includes(info.effort_value)) fail('EFFORT_UNAVAILABLE','无法在高／极高之间安全设置。','model');
    await page.press('loc=css:[role="slider"]',target===3?'ArrowRight':'ArrowLeft');
    info=await modelInfo(page);
    if(change&&target===3&&selectedHigh()) {target=2;fallback=true;}
  }
  const labelOk=target===3?/Extra High|極高|极高/.test(info.label):/(^|\s)High($|\s)|推理強度\s*高$/.test(info.label);
  if(!info.models.some(m=>m.label==='GPT-6'&&m.checked)||info.effort_value!==target||!labelOk)
    fail('MODEL_MISMATCH','页面读回的 GPT-6／思考等级与请求不一致。','model');
  await page.keyboard.press('Escape');
  return {model:'GPT-6',effort:target===3?'extra_high':'high',verification:'ui_selection',backend_verified:false,
    ...(fallback?{warning:'EXTRA_HIGH_UNAVAILABLE_USING_HIGH'}:{})};
}

async function main() {
  const task=await runtimeSpace();
  if(['debug_observe','debug_capture','debug_close'].includes(input.action)){
    const requested=new URL(input.conversation_url);
    const cid=identity(requested.href).conversation;
    if(requested.origin!=='https://chatgpt.com'||!cid||!(/^[a-f0-9-]{36}$/i.test(cid)))fail('INVALID_CONVERSATION_URL','调试导出只接受 ChatGPT 对话地址。','debug');
    let page;
    if(input.page_label){
      page=task.page(input.page_label);
      if(identity(await page.url()).conversation!==cid)fail('CONVERSATION_MISMATCH','调试标签页已切换，停止导出。','debug');
    }else{
      page=await task.newPage();receipt.page_label=page.label;
      try{await page.goto(requested.origin+requested.pathname,{timeout:15000});}catch(error){if(identity(await page.url()).conversation!==cid)throw error;}
    }
    receipt.page_label=page.label;
    if(input.action==='debug_close'){
      if(page.openedBy==='agent')await page.close();
      return {closed:page.openedBy==='agent'};
    }
    await page.waitForFunction(()=>document.querySelectorAll('[data-talvt-turn-state]').length>0,undefined,{timeout:20000});
    if(identity(await page.url()).conversation!==cid)fail('CONVERSATION_MISMATCH','调试目标对话身份不一致。','debug');
    if(input.action==='debug_capture')return {...receipt,...await capture(page)};
    await account(page);
    await page.snapshot({scope:'full_page'});
    const s=await state(page);
    if(s.login)fail('LOGIN_REQUIRED','请先在专用空间登录。','debug');
    return {...receipt,state:s,read_only:true,history_pagination_verified:false};
  }
  if(input.action==='inventory') return {space_id:task.spaceId,tabs:(await task.tabs()).filter(t=>identity(t.url).project===input.project_id).map(t=>({label:t.label,url:t.url,openedBy:t.openedBy}))};
  const page=await choosePage(task,['prepare','recover','health','capture_artifact'].includes(input.action));
  receipt.page_label=page.label;
  if(input.target_user_message_id){
    try {
      await page.waitForFunction(({userId,assistantId})=>Array.from(document.querySelectorAll('[data-talvt-turn-state]')).some(t=>
        t.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')===userId&&
        (!assistantId||Array.from(t.querySelectorAll('[data-chatgpt-selection-message-id]')).some(e=>e.getAttribute('data-chatgpt-selection-message-id')===assistantId))),
        {userId:input.target_user_message_id,assistantId:input.target_assistant_message_id},{timeout:15000});
    } catch(error){fail('MESSAGE_NOT_LOADED','目标回答尚未完整加载，或消息已发生变化；未执行下载。','artifact');}
  }
  if(input.action==='prepare'&&input.expected_history?.length) {
    try {
      await page.waitForFunction(expected=>{
        const n=s=>String(s??'').replace(/\s+/g,' ').trim();
        const actual=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).map(turn=>({
          user_id:turn.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')??null,
          user_text:n(turn.querySelector('[data-chatgpt-search-message-ids]')?.innerText),
          assistant_id:turn.querySelector('[data-chatgpt-selection-message-id]')?.getAttribute('data-chatgpt-selection-message-id')??null,
          assistant_text:n(turn.querySelector('[data-markdown-text-style="assistant-message"]')?.innerText)}));
        return JSON.stringify(actual)===JSON.stringify(expected);
      },input.expected_history,{timeout:15000});
    } catch(error) {fail('HISTORY_NOT_CONFIRMED','会话历史尚未完整加载，或已被修改；未发送新消息。','prepare');}
  }
  let s=await state(page);
  check(s,input.conversation_url?identity(input.conversation_url).conversation:null);
  if(input.action==='capture_artifact')return {...receipt,...await capture(page)};
  if(input.action==='close') {
    if(page.openedBy!=='agent') return {closed:false,reason:'user_origin_tab_retained'};
    await page.close();return {closed:true};
  }
  if(input.action==='health') {
    const a=await account(page);const m=await modelInfo(page);await page.keyboard.press('Escape');
    return {...receipt,space_id:task.spaceId,project_id:s.identity.project,account:a,model_options:m,editor_present:s.editor_present,healthy:m.models.some(m=>m.label==='GPT-6'&&m.checked)};
  }
  if(input.action==='prepare') {
    if(!input.conversation_url&&(s.identity.conversation||s.turns.length)) fail('CONVERSATION_MISMATCH','新查询只能从空的项目输入页发起，不能追加到未绑定的会话。','prepare');
    if(s.turns.some(t=>t.state!=='complete')) fail('CONVERSATION_BUSY','页面中存在尚未完成的消息。','prepare');
    if(norm(s.draft) && norm(s.draft)!==norm(input.send_text)) fail('DRAFT_PRESENT','编辑器存在非空草稿；不覆盖用户输入。','prepare');
    if(input.expected_history && JSON.stringify(s.turns.map(t=>({user_id:t.user_id,user_text:norm(t.user_text),assistant_id:t.assistant_id,assistant_text:norm(t.assistant_text)})))!==JSON.stringify(input.expected_history))
      fail('HISTORY_CHANGED','会话历史与上次完成记录不一致，可能被人工修改。','prepare');
    const accountState=await account(page);
    const model=await configure(page);
    await page.fill(EDITOR,input.send_text);
    const after=await state(page);
    if(norm(after.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','草稿读回与请求不一致。','prepare');
    return {...receipt,baseline:s,observed:model,account:accountState};
  }
  if(input.action==='submit') {
    if(norm(s.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','待发送内容已改变。','submit_precondition');
    if(s.turns.length!==input.baseline.turns.length) fail('HISTORY_CHANGED','提交前会话消息数量变化。','submit_precondition');
    if(s.identity.conversation!==input.baseline.identity.conversation) fail('CONVERSATION_MISMATCH','准备完成后标签页会话身份发生变化。','submit_precondition');
    const h=turns=>turns.map(t=>({user_id:t.user_id,user_text:norm(t.user_text),assistant_id:t.assistant_id,assistant_text:norm(t.assistant_text)}));
    if(JSON.stringify(h(s.turns))!==JSON.stringify(h(input.baseline.turns))) fail('HISTORY_CHANGED','提交前既有消息被编辑。','submit_precondition');
    const m=await configure(page,false);
    const before=await state(page);check(before,input.conversation_url?identity(input.conversation_url).conversation:null);
    if(norm(before.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','模型验证后草稿发生变化。','submit_precondition');
    receipt.click_attempted=true;
    await page.click('loc=css:button[aria-label="傳送"]');
    receipt.clicked=true;
    // Do not wait for generation. Return the receipt, then reconcile in a separate call.
    return {...receipt,observed:m,state:await state(page)};
  }
  if(['observe','recover'].includes(input.action)) return {...receipt,state:s};
  if(input.action==='cancel') {
    const stop=s.buttons.find(b=>/停止|Stop/.test(b.label??'')&&!b.disabled);
    if(!stop) return {...receipt,stopped:false,state:s};
    await page.click(`loc=css:button[aria-label=${JSON.stringify(stop.label)}]`);
    return {...receipt,stopped:true,state:await state(page)};
  }
  fail('INVALID_ACTION','未知浏览器动作。');
}

try { console.log('EGO_BRIDGE_JSON:'+JSON.stringify({ok:true,value:await main()})); }
catch(error) {
  // Do not print page HTML, credentials, query text, or arbitrary browser error payloads.
  let reason=String(error.message??error.name);
  for(const sensitive of [input.send_text,input.expected_account]) if(sensitive) reason=reason.split(sensitive).join('[redacted]');
  reason=reason.replace(/Bearer\s+\S+/gi,'Bearer [redacted]').replace(/((?:cookie|token|authorization)\s*[:=]\s*)[^\s,;]+/gi,'$1[redacted]').slice(0,1200);
  console.log('EGO_BRIDGE_JSON:'+JSON.stringify({ok:false,error:{error_code:error.code??'BROWSER_OPERATION_FAILED',
    message:error.code?error.message:reason,stage:error.stage??input.action,
    diagnostic_context:{...receipt,cause_name:error.name,reason}},receipt}));
}
