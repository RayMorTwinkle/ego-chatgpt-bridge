// Pure, read-only browser DOM reader. No network, submission, or browser API dependencies.
export function readChatPage() {
  const visible=e=>!!e?.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const ignored='button,[role="button"],input,textarea,select,svg,[aria-hidden="true"],[hidden],[data-markdown-copy="exclude"],[data-thread-find-skip]';
  const block='div,p,h1,h2,h3,h4,h5,h6,li,ul,ol,pre,blockquote,table,tr,section,article';
  function text(node){
    if(!node)return '';
    const copy=node.cloneNode(true);
    for(const e of copy.querySelectorAll(ignored))e.remove();
    for(const br of copy.querySelectorAll('br'))br.replaceWith('\n');
    for(const e of copy.querySelectorAll(block)){e.prepend('\n');e.append('\n');}
    return (copy.textContent??'').replace(/\r\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  }
  const turns=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).map(turn=>{
    const user=turn.querySelector('[data-chatgpt-search-message-ids]');
    const all=Array.from(turn.querySelectorAll('[data-markdown-text-style="assistant-message"]'));
    const candidates=all.filter(e=>visible(e)&&!user?.contains(e)&&!e.closest('[data-markdown-text-tone="tertiary"]'));
    // Search/reasoning progress uses the same markdown attribute. Only answer roots
    // count, and nested markdown nodes must not duplicate the enclosing answer.
    const responses=candidates.filter(e=>!candidates.some(parent=>parent!==e&&parent.contains(e)));
    const answer=responses.map(text).filter(Boolean).join('\n\n');
    const selected=responses.map(e=>e.closest('[data-chatgpt-selection-message-id]')||e.querySelector('[data-chatgpt-selection-message-id]')).find(Boolean)||
      Array.from(turn.querySelectorAll('[data-chatgpt-selection-message-id]')).find(e=>!user?.contains(e)&&visible(e)&&!e.closest('[data-markdown-text-tone="tertiary"]'));
    const files=Array.from(turn.querySelectorAll('button[aria-label^="開啟 "][aria-label$=" 預覽"]')).filter(e=>!user?.contains(e)&&visible(e));
    const artifacts=files.map((e,index)=>({kind:'file_card',index,name:e.getAttribute('aria-label').slice(3,-3)}));
    const images=Array.from(turn.querySelectorAll('img')).filter(e=>!user?.contains(e)&&visible(e)&&!e.closest('[data-markdown-text-tone="tertiary"]')&&e.getBoundingClientRect().width>32&&e.getBoundingClientRect().height>32);
    const external_images=[],native_images=[];let progress_images_ignored=0;
    for(const [index,image] of images.entries()){
      let uri;
      try{uri=new URL(image.currentSrc||image.src,location.href);}catch{continue;}
      const native=uri.protocol==='blob:'||uri.protocol==='data:'||uri.protocol==='https:'&&(uri.hostname==='chatgpt.com'||uri.hostname.endsWith('.oaiusercontent.com'));
      if(native){artifacts.push({kind:'inline_image',index,name:`image-${index+1}.png`});native_images.push(image);}
      else if(['http:','https:'].includes(uri.protocol)&&!uri.username&&!uri.password&&responses.some(root=>root.contains(image))){
        external_images.push({index,title:image.alt||`网页引用图片 ${index+1}`,url:uri.href,delivery:'reference_only'});
      }else progress_images_ignored++;
    }
    const unsupported_outputs=Array.from(turn.querySelectorAll('audio,video,canvas,iframe,svg')).filter(e=>!user?.contains(e)&&visible(e)&&!e.closest('[data-markdown-text-tone="tertiary"]')&&e.getBoundingClientRect().width>64&&e.getBoundingClientRect().height>32).map(e=>({kind:e.tagName.toLowerCase(),title:(e.getAttribute('aria-label')||e.getAttribute('title')||'').slice(0,200),export_status:'UNSUPPORTED'}));
    const sources=[];const seen=new Set();
    for(const response of responses)for(const a of response.querySelectorAll('a[href]')){
      if(!visible(a)||seen.has(a.href))continue;
      seen.add(a.href);sources.push({title:text(a),url:a.href});
    }
    return {state:turn.getAttribute('data-talvt-turn-state'),user_id:user?.getAttribute('data-chatgpt-search-message-ids')??null,
      user_text:text(user?.querySelector('[data-search-result-target]')||user),assistant_id:selected?.getAttribute('data-chatgpt-selection-message-id')??null,
      assistant_text:answer,sources,external_images,render_ready:!!(answer||native_images.some(i=>i.complete&&i.naturalWidth>0)||external_images.length||unsupported_outputs.length)&&native_images.every(i=>i.complete&&i.naturalWidth>0),
      artifacts,unsupported_outputs,regenerate:!!turn.querySelector('button[aria-label="重新產生回覆"]'),
      reader_diagnostics:{version:2,progress_images_ignored,answer_roots:responses.length,reasoning_nodes_excluded:all.filter(e=>e.closest('[data-markdown-text-tone="tertiary"]')).length,user_controls_excluded:user?.querySelectorAll(ignored).length??0}};
  });
  const editor=document.querySelector('form [contenteditable="true"]');
  return {url:location.href,title:document.title,turns,editor_present:visible(editor),draft:editor?.innerText??'',
    model_label:document.querySelector('button[aria-label="選取 ChatGPT 模型"]')?.innerText,
    buttons:Array.from(document.querySelectorAll('form button')).filter(visible).map(b=>({label:b.getAttribute('aria-label'),disabled:b.disabled})),
    dialogs:Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
    alerts:Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
    login:!editor&&Array.from(document.querySelectorAll('button,a')).filter(visible).some(e=>/^(登入|登录|Log in|Sign in)$/.test(e.innerText.trim())),
    breadcrumb:Array.from(document.querySelectorAll('a[href]')).some(a=>a.getAttribute('href')?.endsWith('/project')&&visible(a))};
}
