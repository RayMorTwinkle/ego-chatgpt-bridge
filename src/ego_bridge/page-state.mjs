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
  // Serialize only observed answer DOM; no clipboard, hidden application state or API.
  function markdown(node){
    if(node.nodeType===3)return (node.textContent||'').replace(/([\\`*_[\]<>])/g,'\\$1');
    if(node.nodeType!==1||node.matches(ignored))return '';
    const tag=node.tagName.toLowerCase();
    const children=()=>Array.from(node.childNodes).map(markdown).join('');
    if(tag==='br')return '\n';
    if(tag==='a'){
      let href;try{href=new URL(node.getAttribute('href'),location.href);}catch{return children();}
      return ['http:','https:'].includes(href.protocol)?`[${children().trim()}](${href.href.replace(/\(/g,'%28').replace(/\)/g,'%29')})`:children();
    }
    if(tag==='pre'){const raw=node.textContent||'';const fence='`'.repeat(Math.max(3,...Array.from(raw.matchAll(/`+/g),m=>m[0].length+1)));return `\n\n${fence}\n${raw}\n${fence}\n\n`;}
    if(tag==='code'){const raw=node.textContent||'';const fence='`'.repeat(Math.max(1,...Array.from(raw.matchAll(/`+/g),m=>m[0].length+1)));return `${fence} ${raw} ${fence}`;}
    if(tag==='table'){
      const rows=Array.from(node.querySelectorAll('tr')).filter(r=>r.closest('table')===node).map(r=>Array.from(r.children).filter(c=>['TH','TD'].includes(c.tagName)).map(c=>markdown(c).trim().replace(/\n+/g,'<br>').replace(/\|/g,'\\|')));
      if(!rows.length)return '';
      const width=Math.max(...rows.map(r=>r.length));const line=r=>'| '+Array.from({length:width},(_,i)=>r[i]||'').join(' | ')+' |';
      const hasHeader=!!node.querySelector('tr th');
      const header=hasHeader?rows.shift():Array(width).fill('');
      return '\n\n'+[line(header),line(Array(width).fill('---')),...rows.map(line)].join('\n')+'\n\n';
    }
    const value=children();
    if(/^h[1-6]$/.test(tag))return '\n\n'+'#'.repeat(Number(tag[1]))+' '+value.trim()+'\n\n';
    if(tag==='strong'||tag==='b')return '**'+value+'**';
    if(tag==='em'||tag==='i')return '*'+value+'*';
    if(tag==='li')return '\n'+(node.parentElement?.tagName==='OL'?`${Array.from(node.parentElement.children).indexOf(node)+Number(node.parentElement.getAttribute('start')||1)}. `:'- ')+value.trim().replace(/\n/g,'\n  ');
    if(tag==='blockquote')return '\n\n'+value.trim().split('\n').map(l=>'> '+l).join('\n')+'\n\n';
    if(tag==='hr')return '\n\n---\n\n';
    return /^(p|div|section|article|ul|ol)$/.test(tag)?'\n\n'+value.trim()+'\n\n':value;
  }
  const generation_active=Array.from(document.querySelectorAll('form button')).some(e=>visible(e)&&/stop|停止|停止串流/i.test(e.getAttribute('aria-label')||''));
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
    const answerAnchors=responses.flatMap(e=>Array.from(e.querySelectorAll('a[href]')).filter(visible));
    const extraction={scope:'visible_answer_dom',history_pagination_verified:false,truncation_status:'not_proven_absent',
      answer_anchor_count:answerAnchors.length,turn_anchor_count:turn.querySelectorAll('a[href]').length,
      source_status:sources.length?'sources_present':answerAnchors.length?'anchors_not_extracted':'no_sources_present',
      plain_text_characters:answer.length,dom_inner_text_characters:responses.reduce((n,e)=>n+(e.innerText||e.textContent||'').length,0),
      table_count:responses.reduce((n,e)=>n+e.querySelectorAll('table').length,0),
      blocks:responses.map((e,index)=>({index,message_id:(e.closest('[data-chatgpt-selection-message-id]')||e.querySelector('[data-chatgpt-selection-message-id]'))?.getAttribute('data-chatgpt-selection-message-id')||null,characters:text(e).length}))};
    return {assistant_markdown:responses.map(markdown).join('\n\n').replace(/\n{3,}/g,'\n\n').trim(),extraction,generation_active,state:turn.getAttribute('data-talvt-turn-state'),user_id:user?.getAttribute('data-chatgpt-search-message-ids')??null,
      user_text:text(user?.querySelector('[data-search-result-target]')||user),assistant_id:selected?.getAttribute('data-chatgpt-selection-message-id')??null,
      assistant_text:answer,sources,external_images,render_ready:!!(answer||native_images.some(i=>i.complete&&i.naturalWidth>0)||external_images.length||unsupported_outputs.length)&&native_images.every(i=>i.complete&&i.naturalWidth>0),
      artifacts,unsupported_outputs,regenerate:!!turn.querySelector('button[aria-label="重新產生回覆"]'),
      reader_diagnostics:{version:3,progress_images_ignored,answer_roots:responses.length,reasoning_nodes_excluded:all.filter(e=>e.closest('[data-markdown-text-tone="tertiary"]')).length,user_controls_excluded:user?.querySelectorAll(ignored).length??0}};
  });
  const editor=document.querySelector('form [contenteditable="true"]');
  return {url:location.href,title:document.title,turns,editor_present:visible(editor),draft:text(editor),draft_has_autolinks:!!editor?.querySelector('[data-rich-text-generated-autolink]'),
    model_label:document.querySelector('button[aria-label="選取 ChatGPT 模型"]')?.innerText,
    buttons:Array.from(document.querySelectorAll('form button')).filter(visible).map(b=>({label:b.getAttribute('aria-label'),disabled:b.disabled})),
    dialogs:Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
    alerts:Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
    login:!editor&&Array.from(document.querySelectorAll('button,a')).filter(visible).some(e=>/^(登入|登录|Log in|Sign in)$/.test(e.innerText.trim())),
    breadcrumb:Array.from(document.querySelectorAll('a[href]')).some(a=>a.getAttribute('href')?.endsWith('/project')&&visible(a))};
}
