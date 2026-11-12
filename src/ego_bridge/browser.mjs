// Executed only by `ego-browser nodejs`; this is the installed Ego API, not Playwright.
// `input` is injected as a JSON value by adapter.py, never through a shell.
const CONTROLS = {
  model:['button[data-testid="model-switcher-dropdown-button"]','button[data-testid="model-selector"]','form button[aria-haspopup="menu"]:has([data-model-name])',
    'button[aria-label="選取 ChatGPT 模型"]','button[aria-label="选择 ChatGPT 模型"]','button[aria-label="Select ChatGPT model"]','button[aria-label="Choose model"]'],
  profile:['button[data-testid="accounts-profile-button"]','button[data-testid="profile-button"]',
    'button[aria-label="開啟個人檔案選單"]','button[aria-label="打开个人资料菜单"]','button[aria-label="Open profile menu"]'],
  send:['form button[data-testid="send-button"]','form button[type="submit"]',
    'form button[aria-label="傳送"]','form button[aria-label="发送"]','form button[aria-label="Send prompt"]','form button[aria-label="Send message"]'],
  modelMenu:['[role="menuitem"]:has([data-testid="model-selector"])','[role="menuitem"][aria-label="選取模型"]',
    '[role="menuitem"][aria-label="选择模型"]','[role="menuitem"][aria-label="Select model"]','[role="menuitem"]'],
};
function findControl({selectors,kind,mark=false}) {
  for(const selector of selectors){
    const matches=Array.from(document.querySelectorAll(selector)).filter(e=>
      e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&
      (selector!=='[role="menuitem"]'||/^(選取模型|选择模型|Select model)$/i.test((e.getAttribute('aria-label')||e.innerText||'').trim())));
    if(matches.length!==1)continue;
    // Return a unique locator even when a hidden duplicate shares the selector.
    if(mark){
      for(const e of document.querySelectorAll(`[data-ego-control="${kind}"]`))e.removeAttribute('data-ego-control');
      matches[0].setAttribute('data-ego-control',kind);
    }
    return `[data-ego-control="${kind}"]`;
  }
  return null;
}
async function control(page, kind) {
  const args={selectors:CONTROLS[kind],kind};
  await page.waitForFunction(findControl,args,{timeout:20000});
  const selector=await page.evaluate(findControl,{...args,mark:true});
  if(!selector)fail('UI_CHANGED',`找不到可唯一定位的 ${kind} 控件。`,input.action);
  return 'loc=css:'+selector;
}
const EDITOR = 'loc=css:form [contenteditable="true"]';
const FILE_INPUT = 'loc=css:form input[type=file][multiple]:is(:not([accept]),[accept=""])';
const norm = s => String(s ?? '').replace(/\s+/g, ' ').trim();
const fail = (code, message, stage='browser',context={}) => { const e=new Error(message); e.code=code; e.stage=stage; e.diagnostic_context=context; throw e; };
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
  const named=spaces.filter(s=>s.name===input.space_name);
  if(named.length>1)fail('SPACE_AMBIGUOUS','专用空间名称不唯一，不能猜测目标空间。');
  const persisted=spaces.find(s=>s.id===input.space_id);
  receipt.space_identity={configured_id:input.space_id,configured_name:input.space_name,
    persisted_id_name:persisted?.name??null,named_ids:named.map(s=>s.id)};
  // Ego IDs are instance-local. A reused ID must never override the exact name.
  let entry=named[0];
  if(!entry){
    if(!input.recreate_missing_space)fail('SPACE_MISSING','专用空间不存在，自动重建未开启。');
    const created=await taskSpace(input.space_name);
    if(created.name!==input.space_name)fail('SPACE_IDENTITY_MISMATCH','新建回执的空间名称与配置不一致，停止操作。');
    receipt.runtime_space={previous_id:input.space_id,space_id:created.spaceId,name:created.name,recreated:true,history_restored:false,
      reason:persisted?'configured_name_missing_old_id_reused':'configured_name_missing'};
    return created;
  }
  let task;
  if(input.force_space_takeover){
    task=entry.ownership==='user'||entry.ownership==='inactive'?await claimTaskSpace(entry.id):await takeOverTaskSpace(entry.id);
  }else{
    if(entry.ownership==='user')fail('SPACE_USER_CONTROL','专用空间当前由用户控制。');
    task=await taskSpace(entry.id);
  }
  if(task.name!==input.space_name)fail('SPACE_IDENTITY_MISMATCH','接管回执的空间名称与配置不一致，停止操作。');
  receipt.runtime_space={previous_id:input.space_id,space_id:task.spaceId,name:task.name,recreated:false,exclusive:!!input.force_space_takeover,reason:entry.id!==input.space_id?'named_space_id_changed':'existing_binding'};
  return task;
}

async function state(page) {
  const result=await page.evaluate(readChatPage);
  result.identity=identity(result.url);
  return result;
}

function checkAttachments(s, names, stage) {
  const same=JSON.stringify([...s.draft_attachments].sort())===JSON.stringify([...names].sort());
  if(!same||s.draft_uploading) fail('DRAFT_ATTACHMENT_MISMATCH','草稿附件与本轮请求不一致或尚未完成上传；未发送。',stage);
  if(names.length && (s.alerts.some(text=>/上[傳传]失[敗败]|upload failed|failed to upload/i.test(text)) ||
      !s.buttons.some(b=>/^(傳送|发送|Send prompt|Send message)$/.test(b.label??'')&&!b.disabled)))
    fail('ATTACHMENT_UPLOAD_INCOMPLETE','网页未确认附件可发送；未发送。',stage);
}

async function uploadAttachments(page, paths, names) {
  try {
    await page.setInputFiles(FILE_INPUT,paths);
    await page.waitForFunction(files=>{
      const group=document.querySelector('form [data-composer-attachments]');
      const labels=Array.from(group?.querySelectorAll('button[aria-label]')??[],b=>b.getAttribute('aria-label'));
      return files.every(name=>labels.includes('移除 '+name)||labels.includes('删除 '+name)||labels.includes('Remove '+name)) &&
        !group?.querySelector('[role="progressbar"]');
    },names,{timeout:90000});
  } catch(error) {
    fail('ATTACHMENT_UPLOAD_FAILED','网页上传失败或未及时显示附件；未发送。请检查网页草稿。','prepare');
  }
  checkAttachments(await state(page),names,'prepare');
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
  const sameSpace=!receipt.runtime_space?.recreated&&receipt.runtime_space?.space_id===input.space_id;
  let found=sameSpace&&input.page_label && tabs.find(t=>t.label===input.page_label);
  if(found && input.conversation_url && identity(found.url).conversation!==identity(input.conversation_url).conversation)
    fail('CONVERSATION_MISMATCH','已绑定标签页被人工切换；停止操作。');
  if(!found && input.conversation_url) found=tabs.find(t=>identity(t.url).conversation===identity(input.conversation_url).conversation);
  if(!found && input.action==='health') found=tabs.find(t=>identity(t.url).project===input.project_id);
  if(found) {
    const page=found.label?task.page(found.label):await task.adopt(found.page);
    if(input.action==='prepare') await usable(page);
    return page;
  }
  // A saved, verified conversation URL survives the browser/space. Reopening it
  // for observation is read-only; an unbound send must still be reconciled.
  if(!create&&!(input.action==='observe'&&input.conversation_url)) fail('TAB_MISSING','找不到关联标签页，不能猜测另一条会话。');
  const page=await freshPage(task);
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

async function freshPage(task){
  const tabs=await task.tabs();
  const blank=tabs.length===1&&tabs[0].label==='p1'&&['about:blank','chrome://newtab/'].includes(tabs[0].url)&&tabs[0].openedBy==='agent';
  return blank?task.page('p1'):await task.newPage();
}

async function account(page) {
  await page.click(await control(page,'profile'));
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('[role="menu"]')).some(e=>/登出|退出登录|Log out|Sign out/i.test(e.innerText)),undefined,{timeout:8000});
  const confirmed=await page.evaluate(expected=>Array.from(document.querySelectorAll('[role="menu"]')).some(e=>e.innerText.split('\n')[0].trim()===expected),input.expected_account);
  await page.keyboard.press('Escape');
  if(!confirmed) fail('ACCOUNT_MISMATCH','当前账户显示名称与配置不符。','account');
  return {level:'display_name_only',confirmed:true};
}

async function modelInfo(page) {
  await page.keyboard.press('Escape');
  const button=await control(page,'model');
  await page.waitForSelector(button,{state:'visible',timeout:20000});
  await page.click(button);
  return await page.evaluate(selectors=>({
    models:Array.from(document.querySelectorAll('[role="menuitemradio"]')).map(e=>({label:e.textContent?.trim(),checked:e.getAttribute('aria-checked')==='true'})),
    effort_value:Number(document.querySelector('[role="slider"]')?.getAttribute('aria-valuenow')),
    label:selectors.map(selector=>Array.from(document.querySelectorAll(selector)).filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'))
      .find(matches=>matches.length===1)?.[0]?.innerText,
    disabled:document.querySelector('[role="slider"]')?.getAttribute('aria-disabled')==='true'||
      document.querySelector('[aria-label="效能"],[aria-label="性能"],[aria-label="Performance"]')?.getAttribute('aria-disabled')==='true'
  }),CONTROLS.model);
}

function classifyDownload(data,httpStatus=200) {
  const raw=data?.download_url;
  const kind=raw==null?'missing':typeof raw!=='string'?'other':!raw.trim()?'empty':raw.startsWith('/')?'relative':/^[a-z][a-z0-9+.-]*:/i.test(raw)?'absolute':'other';
  let url;
  try {url=new URL(raw);} catch {}
  const field=value=>typeof value==='string'&&/^[a-z0-9_.-]{1,80}$/i.test(value)?value:null;
  const context={http_status:httpStatus,provider_status:field(data?.status),provider_error_code:field(data?.error_code),
    download_url_kind:kind,url_host:url?.hostname??null,metadata_keys:data&&typeof data==='object'?Object.keys(data).filter(k=>/^[a-z_]{1,64}$/i.test(k)).slice(0,40):[]};
  const error=code=>({code,context});
  if(httpStatus===404)return error('ARTIFACT_REFERENCE_NOT_FOUND');
  if(httpStatus===429||httpStatus>=500)return error('ARTIFACT_HTTP_TRANSIENT');
  if(data?.status==='error')return error(data.error_code==='ace_pod_expired'?'ARTIFACT_POD_EXPIRED':'ARTIFACT_PROVIDER_ERROR');
  if(!data||typeof data!=='object'||Array.isArray(data))return error('ARTIFACT_URL_MALFORMED');
  if(kind==='missing'||kind==='empty')return error('ARTIFACT_URL_MISSING');
  if(typeof raw!=='string')return error('ARTIFACT_URL_MALFORMED');
  if(kind==='relative'){
    // Path shape only: id-like segments masked, query (signature) dropped.
    context.url_path_shape=raw.split(/[?#]/)[0].slice(0,200).replace(/[A-Za-z0-9_-]{16,}/g,':id');
    if(!/^\/backend-api\/(?:estuary\/content(?:[?]|$)|files\/download\/|conversation\/[A-Za-z0-9-]{8,64}\/executor\/file(?:[?]|$))/.test(raw))return error('ARTIFACT_RESOURCE_UNTRUSTED');
    try {url=new URL(raw,'https://chatgpt.com');} catch {}
  }
  if(!url)return error('ARTIFACT_URL_MALFORMED');
  context.url_host=url.hostname;
  if(url.protocol!=='https:'||!(url.hostname==='chatgpt.com'||url.hostname.endsWith('.oaiusercontent.com'))||url.username||url.password||url.port&&url.port!=='443')
    return error('ARTIFACT_RESOURCE_UNTRUSTED');
  return {url:url.href,context};
}
function downloadAssociation(messageId,assistantId,data,name) {
  if(assistantId&&messageId)return messageId===assistantId&&(!data?.file_name||data.file_name===name);
  return data?.file_name===name;
}

async function capture(page) {
  const fs=await import('node:fs/promises');
  const c=input.candidate,started=Date.now();
  let diagnostic={...classifyDownload(null,null).context,attempt:0,elapsed_ms:0};
  const artifactFail=(code,message)=>fail(code,message,'artifact',{...diagnostic,elapsed_ms:Date.now()-started});
  try {
    if(c.kind==='inline_image') {
      const src=await page.evaluate(({userId,index})=>{
        const turn=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).find(t=>t.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')===userId);
        if(!turn)return null;const user=turn.querySelector('[data-chatgpt-search-message-ids]');
        const images=Array.from(turn.querySelectorAll('img')).filter(e=>!user?.contains(e)&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[data-markdown-text-tone="tertiary"]')&&e.getBoundingClientRect().width>32&&e.getBoundingClientRect().height>32);
        return images[index]?.currentSrc||images[index]?.src;
      },{userId:input.user_message_id,index:c.index});
      if(!src)artifactFail('ARTIFACT_ASSOCIATION_FAILED','当前回答找不到对应图片。');
      const url=new URL(src);
      if(!['blob:','data:'].includes(url.protocol)&&!(url.protocol==='https:'&&(url.hostname==='chatgpt.com'||url.hostname.endsWith('.oaiusercontent.com'))))
        artifactFail('EXTERNAL_IMAGE_NOT_COPIED','外部站点图片保留为网页内容，不由服务自动抓取。');
      const response=await page.fetch(src,{saveAs:input.destination});
      const type=response.headers['content-type']??response.headers['Content-Type']??'';
      if(!response.ok||!/^image\/(png|jpeg|gif|webp)(;|$)/.test(type))artifactFail('ARTIFACT_TYPE_MISMATCH','图片响应状态或 MIME 不符合可回传的图像类型。');
      return {saved:true,transport:'rendered_image_url',mime_type:type.split(';')[0]};
    }
    if(c.kind!=='file_card') artifactFail('ARTIFACT_CAPTURE_UNSUPPORTED','该产物呈现方式尚未验证可安全导出原始字节。');
    const token='artifact-'+Date.now().toString(36);
    const selected=await page.evaluate(({userId,candidate,token})=>{
      const turn=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).find(t=>t.querySelector('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')===userId);
      if(!turn)return false;
      const user=turn.querySelector('[data-chatgpt-search-message-ids]');
      const previews=Array.from(turn.querySelectorAll('button:is([aria-label^="開啟 "][aria-label$=" 預覽"],[aria-label^="打开 "][aria-label$=" 预览"],[aria-label^="Open "][aria-label$=" preview"])')).filter(e=>!user?.contains(e)&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
      const preview=previews[candidate.index];
      if(!preview||!['開啟 '+candidate.name+' 預覽','打开 '+candidate.name+' 预览','Open '+candidate.name+' preview'].includes(preview.getAttribute('aria-label')))return false;
      let card=preview.parentElement;
      while(card&&card!==turn){
        const downloads=card.querySelectorAll('button:is([data-testid="download-file"],[aria-label="下載檔案"],[aria-label="下载文件"],[aria-label="Download file"])');
        if(downloads.length===1){preview.setAttribute('data-ego-preview',token);downloads[0].setAttribute('data-ego-download',token);return true;}
        if(downloads.length>1)return false;
        card=card.parentElement;
      }
      return false;
    },{userId:input.user_message_id,candidate:c,token});
    if(!selected)artifactFail('ARTIFACT_ASSOCIATION_FAILED','找不到该消息中对应的唯一文件卡片。');
    await page.evaluate(token=>Array.from(document.querySelectorAll('[data-ego-preview]')).find(e=>e.getAttribute('data-ego-preview')===token)?.scrollIntoView({block:'center',inline:'nearest'}),token);
    await page.hover(`loc=css:[data-ego-preview="${token}"]`);
    await page.cdp('Network.enable');await page.events();
    try {
      const budget=Date.now()+75000;
      const retryable=new Set(['ARTIFACT_URL_MISSING','ARTIFACT_POD_EXPIRED','ARTIFACT_REFERENCE_NOT_FOUND','ARTIFACT_HTTP_TRANSIENT','ARTIFACT_DOWNLOAD_UNOBSERVED']);
      for(let attempt=1;attempt<=3&&Date.now()<budget;attempt++){
        diagnostic={...classifyDownload(null,null).context,attempt,elapsed_ms:Date.now()-started};
        await page.events();
        // The UI saves files as a real browser download (often a blob: built with its own auth);
        // taking that download is the same path as a person clicking, and needs no headers from us.
        let browserDownload=null;
        page.waitForEvent('download',{timeout:Math.max(1000,Math.min(budget,Date.now()+45000)-Date.now())}).then(d=>{browserDownload=d;},()=>{});
        await page.click(`loc=css:[data-ego-download="${token}"]`);
        const responses=new Map(),requests=new Map(),finished=new Set(),checked=new Set();
        let metadata,resourceUrl,metadataAt=0,resourceFetchAttempted=false,failure,retryAfter=2000;
        let deadline=Math.min(budget,Date.now()+12000);
        while(Date.now()<deadline&&!failure){
          for(const event of await page.events()){
            const p=event.params;
            if(event.method==='Network.requestWillBeSent'){
              try {requests.set(p.requestId,new URL(p.request.url).searchParams.get('message_id'));} catch {}
            }
            if(event.method==='Network.responseReceived')responses.set(p.requestId,p.response);
            if(event.method==='Network.loadingFinished')finished.add(p.requestId);
          }
          const candidates=[];
          for(const [id,response] of responses){
            if(!finished.has(id)||checked.has(id))continue;
            let path;try {path=new URL(response.url).pathname;} catch {continue;}
            if(!path.endsWith('/interpreter/download'))continue;
            checked.add(id);
            let data=null;
            try {
              if(response.encodedDataLength>262144)throw Error('metadata too large');
              const body=await page.cdp('Network.getResponseBody',{requestId:id});
              if(body.body.length>349528)throw Error('metadata too large');
              const text=body.base64Encoded?Buffer.from(body.body,'base64').toString():body.body;
              if(Buffer.byteLength(text)>262144)throw Error('metadata too large');
              data=JSON.parse(text);
            } catch {}
            const messageId=requests.get(id);
            if(input.assistant_message_id&&messageId&&messageId!==input.assistant_message_id)continue;
            if(data?.file_name&&!downloadAssociation(messageId,input.assistant_message_id,data,c.name))continue;
            if(!data?.file_name&&!(input.assistant_message_id&&messageId===input.assistant_message_id)&&data?.status==='success')continue;
            candidates.push({data,response,matched:!!(input.assistant_message_id&&messageId===input.assistant_message_id)});
          }
          candidates.sort((a,b)=>Number(b.matched)-Number(a.matched));
          if(!metadata&&candidates.length){
            const {data,response}=candidates[0],classified=classifyDownload(data,response.status);
            diagnostic={...classified.context,attempt,elapsed_ms:Date.now()-started};
            if(classified.code){
              failure=classified.code;
              if(response.status===429){
                const value=Object.entries(response.headers??{}).find(([k])=>k.toLowerCase()==='retry-after')?.[1];
                const delay=/^\d+(?:\.\d+)?$/.test(String(value))?Number(value)*1000:Date.parse(value)-Date.now();
                if(Number.isFinite(delay))retryAfter=Math.max(2000,delay);
              }
              break;
            }
            metadata=data;resourceUrl=classified.url;metadataAt=Date.now();deadline=Math.min(budget,Date.now()+30000);
          }
          if(browserDownload){
            const download=browserDownload;browserDownload=null;
            if(download.suggestedFilename()===c.name){
              await download.saveAs(input.destination);
              const stat=await fs.stat(input.destination).catch(()=>null);
              if(stat?.size>0){
                if(Number.isFinite(metadata?.file_size_bytes)&&metadata.file_size_bytes>0&&metadata.file_size_bytes!==stat.size)artifactFail('ARTIFACT_INCOMPLETE','文件字节数与非零下载元数据不一致。');
                return {saved:true,transport:'ui_browser_download',size_bytes:stat.size,mime_type:metadata?.mime_type??null,provider_size_bytes:metadata?.file_size_bytes??null};
              }
            }
          }
          if(metadata){
            const samePath=url=>{try {const a=new URL(url),b=new URL(resourceUrl);return a.origin===b.origin&&a.pathname===b.pathname;} catch {return false;}};
            const seen=Array.from(responses.entries()).filter(([id,r])=>samePath(r.url)&&finished.has(id));
            diagnostic.resource_statuses=seen.map(([,r])=>r.status).slice(0,6);
            const uiResource=seen.find(([,r])=>r.status===200);
            if(!resourceFetchAttempted&&!uiResource&&Date.now()-metadataAt<8000){await new Promise(r=>setTimeout(r,150));continue;}
            if(!resourceFetchAttempted&&!uiResource){
              resourceFetchAttempted=true;
              let fetched;
              try {fetched=await page.fetch(resourceUrl,{saveAs:input.destination});} catch {}
              const stat=await fs.stat(input.destination).catch(()=>null);
              if(fetched?.ok&&stat?.size>0){
                if(Number.isFinite(metadata.file_size_bytes)&&metadata.file_size_bytes>0&&metadata.file_size_bytes!==stat.size)artifactFail('ARTIFACT_INCOMPLETE','文件字节数与非零下载元数据不一致。');
                return {saved:true,transport:'ui_download_asset_url',size_bytes:stat.size,mime_type:fetched.headers?.['content-type']??metadata.mime_type,provider_size_bytes:metadata.file_size_bytes};
              }
            }
            const resource=uiResource??(resourceFetchAttempted&&Date.now()-metadataAt>15000?seen[0]:null);
            if(resource){
              const [id,r]=resource;
              if(r.status!==200)artifactFail('ARTIFACT_DOWNLOAD_FAILED',`网页下载返回 HTTP ${r.status}。`);
              let body;
              try {body=await page.cdp('Network.getResponseBody',{requestId:id});} catch {artifactFail('ARTIFACT_DOWNLOAD_UNCONFIRMED','无法读取已观察到的文件响应。');}
              const bytes=Buffer.from(body.body,body.base64Encoded?'base64':'utf8');
              if(Number.isFinite(metadata.file_size_bytes)&&metadata.file_size_bytes>0&&metadata.file_size_bytes!==bytes.length)artifactFail('ARTIFACT_INCOMPLETE','收到的字节数与网页文件元数据不一致。');
              await fs.writeFile(input.destination,bytes,{mode:0o600});
              return {saved:true,transport:'ui_download_response',size_bytes:bytes.length,mime_type:r.mimeType,provider_size_bytes:metadata.file_size_bytes};
            }
          }
          await new Promise(r=>setTimeout(r,150));
        }
        failure??=metadata?'ARTIFACT_DOWNLOAD_UNCONFIRMED':'ARTIFACT_DOWNLOAD_UNOBSERVED';
        if(!retryable.has(failure)||attempt===3||Date.now()+retryAfter>=budget)artifactFail(failure,'未取得可交付的文件；请查看下载诊断。');
        await new Promise(r=>setTimeout(r,retryAfter));
      }
      artifactFail('ARTIFACT_DOWNLOAD_UNOBSERVED','文件下载观察预算已用尽。');
    } finally {await page.cdp('Network.disable');}
  } catch(error) {
    error.diagnostic_context??={...diagnostic,elapsed_ms:Date.now()-started};
    throw error;
  }
}

async function configure(page, change=true) {
  let info=await modelInfo(page);
  if(!info.models.some(m=>m.label==='GPT-6'&&m.checked)) {
    if(!change || !info.models.some(m=>m.label==='GPT-6')) fail('MODEL_UNAVAILABLE','默认 GPT-6 不可验证。','model');
    await page.click(await control(page,'modelMenu'));
    await page.click('text="GPT-6"');
    info=await modelInfo(page);
  }
  let target=input.effort==='high'?2:3;
  let fallback=false;
  const selectedHigh=()=>info.effort_value===2&&/(^|\s)High($|\s)|(?:推理強度|推理强度|思考强度)\s*高$/.test(info.label);
  if(change&&target===3&&info.disabled&&selectedHigh()) {target=2;fallback=true;}
  if(info.effort_value!==target) {
    if(!change || info.disabled || ![2,3].includes(info.effort_value)) fail('EFFORT_UNAVAILABLE','无法在高／极高之间安全设置。','model');
    await page.press('loc=css:[role="slider"]',target===3?'ArrowRight':'ArrowLeft');
    info=await modelInfo(page);
    if(change&&target===3&&selectedHigh()) {target=2;fallback=true;}
  }
  const labelOk=target===3?/Extra High|極高|极高/.test(info.label):/(^|\s)High($|\s)|(?:推理強度|推理强度|思考强度)\s*高$/.test(info.label);
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
      page=await freshPage(task);receipt.page_label=page.label;
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
    // Historical turn markers can say complete before the reply body and image
    // hydrate. Never equate the turn count with readable historical content.
    let s,previous=null,stable=0;const deadline=Date.now()+20000;
    while(Date.now()<deadline){
      s=await state(page);
      const ready=s.turns.length>0&&s.turns.every(t=>t.state!=='complete'||t.render_ready);
      const signature=JSON.stringify(s.turns);
      stable=ready&&signature===previous?stable+1:0;
      if(stable>=2)break;
      previous=signature;await new Promise(r=>setTimeout(r,200));
    }
    if(s.turns.some(t=>t.state==='complete'&&!t.render_ready))receipt.history_render_incomplete=true;
    if(s.login)fail('LOGIN_REQUIRED','请先在专用空间登录。','debug');
    return {...receipt,state:s,read_only:true,history_pagination_verified:false};
  }
  if(input.action==='inventory') return {space_id:task.spaceId,tabs:(await task.tabs()).filter(t=>identity(t.url).project===input.project_id).map(t=>({label:t.label,url:t.url,openedBy:t.openedBy}))};
  if(input.action==='health') {
    // Never borrow an active research page or an unrelated stale project tab.
    const probe=await freshPage(task);receipt.page_label=probe.label;
    try {
      await probe.goto(input.project_url,{timeout:15000});await usable(probe);
      const s=await state(probe);check(s);
      const a=await account(probe);const m=await modelInfo(probe);await probe.keyboard.press('Escape');
      return {...receipt,probe_scope:'dedicated_page',space_id:task.spaceId,project_id:s.identity.project,account:a,model_options:m,editor_present:s.editor_present,healthy:m.models.some(m=>m.label==='GPT-6'&&m.checked)};
    } finally {try{await probe.close();}catch{receipt.probe_cleanup_failed=true;}}
  }
  const page=await choosePage(task,['prepare','recover','capture_artifact'].includes(input.action));
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
    const expected=JSON.stringify(input.expected_history);let confirmed=false;const until=Date.now()+15000;
    while(Date.now()<until){
      const current=await state(page);
      const actual=current.turns.map(t=>({user_id:t.user_id,user_text:norm(t.user_text),assistant_id:t.assistant_id,assistant_text:norm(t.assistant_text)}));
      if(JSON.stringify(actual)===expected){confirmed=true;break;}
      await new Promise(r=>setTimeout(r,200));
    }
    if(!confirmed)fail('HISTORY_NOT_CONFIRMED','会话历史尚未完整加载，或已被修改；未发送新消息。','prepare');
  }
  let s=await state(page);
  check(s,input.conversation_url?identity(input.conversation_url).conversation:null);
  if(input.action==='capture_artifact')return {...receipt,...await capture(page)};
  if(input.action==='close') {
    if(page.openedBy!=='agent') return {closed:false,reason:'user_origin_tab_retained'};
    await page.close();return {closed:true};
  }

  if(input.action==='prepare') {
    if(!input.conversation_url&&(s.identity.conversation||s.turns.length)) fail('CONVERSATION_MISMATCH','新查询只能从空的项目输入页发起，不能追加到未绑定的会话。','prepare');
    if(s.turns.some(t=>t.state!=='complete')) fail('CONVERSATION_BUSY','页面中存在尚未完成的消息。','prepare');
    if(s.draft_attachments.length||s.draft_uploading) {
      if(norm(s.draft)!==norm(input.send_text)||!input.attachment_paths?.length||page.openedBy!=='agent')
        fail('DRAFT_ATTACHMENTS_PRESENT','输入框已有附件。请核对并清理草稿后重试。','prepare');
      // Only this operation's nonce-bearing, unsent draft can be replaced.
      for(const name of s.draft_attachments){
        const button=['移除 ','删除 ','Remove '].map(prefix=>`button[aria-label=${JSON.stringify(prefix+name)}]`).join(',');
        await page.hover(`loc=css:form [data-composer-attachments] *:has(> :is(${button}))`,{label:'Show pending attachment controls'});
        await page.click('loc=css:form :is('+button+')',{label:'Replace this operation attachment'});
      }
      s=await state(page);
      if(s.draft_attachments.length||s.draft_uploading)fail('DRAFT_ATTACHMENTS_PRESENT','旧附件尚未移除，未发送。','prepare');
    }
    if(norm(s.draft) && norm(s.draft)!==norm(input.send_text)) {
      if(!input.conversation_url&&(input.discardable_drafts??[]).some(d=>norm(d)===norm(s.draft))){
        await page.fill(EDITOR,'');receipt.discarded_unsent_bridge_draft=true;s=await state(page);
      }else fail('DRAFT_PRESENT','编辑器存在非空草稿；不覆盖用户输入。','prepare');
    }
    if(input.expected_history && JSON.stringify(s.turns.map(t=>({user_id:t.user_id,user_text:norm(t.user_text),assistant_id:t.assistant_id,assistant_text:norm(t.assistant_text)})))!==JSON.stringify(input.expected_history))
      fail('HISTORY_CHANGED','会话历史与上次完成记录不一致，可能被人工修改。','prepare');
    const accountState=await account(page);
    const model=await configure(page);
    if(norm(s.draft)!==norm(input.send_text)) {
      try {await page.fill(EDITOR,input.send_text);}
      catch(error) {
        // Ego's fill readback can include an autolink icon's layout newline.
        // Only this specific readback failure is reconciled below; timeouts and
        // control loss are never swallowed. No second edit or send is attempted.
        if(!String(error.message).includes('page.fill did not accept the text'))throw error;
        receipt.fill_readback_reconciled=true;
      }
    }
    let after=await state(page);
    if(after.draft_has_autolinks) {
      // The rich composer serializes generated autolinks into Markdown, changing
      // the request after send. A literal code block preserves the actual text.
      if(norm(after.draft)!==norm(input.send_text))fail('DRAFT_MISMATCH','富链接草稿与请求不一致；未发送。','prepare');
      const html='<pre><code>'+input.send_text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</code></pre>';
      await page.click(EDITOR);await page.keyboard.press('ControlOrMeta+a');
      await page.keyboard.paste({text:input.send_text,html});
      after=await state(page);model.prompt_input='literal_code_block';
      if(after.draft_has_autolinks)fail('DRAFT_SERIALIZATION_UNSAFE','网页仍将提示词自动转换成富链接；未发送。','prepare');
    }
    if(norm(after.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','草稿读回与请求不一致。','prepare');
    if(input.attachment_paths?.length) await uploadAttachments(page,input.attachment_paths,input.attachment_names);
    return {...receipt,baseline:s,observed:model,account:accountState};
  }
  if(input.action==='submit') {
    if(s.draft_has_autolinks)fail('DRAFT_SERIALIZATION_UNSAFE','待发送草稿包含会改变请求的自动富链接。','submit_precondition');
    if(norm(s.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','待发送内容已改变。','submit_precondition');
    if(s.turns.length!==input.baseline.turns.length) fail('HISTORY_CHANGED','提交前会话消息数量变化。','submit_precondition');
    if(s.identity.conversation!==input.baseline.identity.conversation) fail('CONVERSATION_MISMATCH','准备完成后标签页会话身份发生变化。','submit_precondition');
    const h=turns=>turns.map(t=>({user_id:t.user_id,user_text:norm(t.user_text),assistant_id:t.assistant_id,assistant_text:norm(t.assistant_text)}));
    if(JSON.stringify(h(s.turns))!==JSON.stringify(h(input.baseline.turns))) fail('HISTORY_CHANGED','提交前既有消息被编辑。','submit_precondition');
    const m=await configure(page,false);
    const before=await state(page);check(before,input.conversation_url?identity(input.conversation_url).conversation:null);
    if(before.draft_has_autolinks)fail('DRAFT_SERIALIZATION_UNSAFE','模型核验后出现自动富链接；未发送。','submit_precondition');
    if(norm(before.draft)!==norm(input.send_text)) fail('DRAFT_MISMATCH','模型验证后草稿发生变化。','submit_precondition');
    checkAttachments(before,input.attachment_names??[],'submit_precondition');
    const send=await control(page,'send');
    receipt.click_attempted=true;
    await page.click(send);
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
    diagnostic_context:{...receipt,...error.diagnostic_context,cause_name:error.name,reason}},receipt}));
}
