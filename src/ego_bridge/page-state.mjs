// Pure, read-only browser DOM reader. No network, submission, or browser API dependencies.
export function readChatPage() {
  const visible=e=>!!e?.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const ignored='button,[role="button"],input,textarea,select,svg,[aria-hidden="true"],[hidden],[data-markdown-copy="exclude"],[data-thread-find-skip]';
  const answerLoadError=/^(?:無法載入這則回覆|无法加载此回复|无法加载这条回复|Unable to load (?:this )?(?:response|message)|Could not load (?:this )?(?:response|message))[。.!]?$/i;
  const readyMermaid='[data-chatgpt-mermaid-preview] [data-mermaid-render-status="ready"] svg[role="img"]';
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
  function codeText(node, lineBreaks=true){
    const copy=node.cloneNode(true);
    for(const e of copy.querySelectorAll(ignored))e.remove();
    if(lineBreaks)for(const br of copy.querySelectorAll('br'))br.replaceWith('\n');
    return copy.textContent??'';
  }
  function widgetCodeSource(node){
    const source=node.querySelector('[data-code-block-source="true"],pre');
    if(source&&visible(source))return source;
    const editor=node.querySelector('.cm-content[data-language]');
    return visible(editor)&&editor.querySelector(':scope > .cm-line')&&!editor.querySelector('.cm-gap')?editor:null;
  }
  function codeFence(raw,minimum){
    let length=minimum;
    for(const match of raw.matchAll(/`+/g))length=Math.max(length,match[0].length+1);
    return '`'.repeat(length);
  }
  function joinMarkdown(parts){
    // Coalesce only fragment boundaries. Code and math keep their internal newlines.
    const chunks=[];
    let breaks=0;
    for(const part of parts){
      const leading=part.match(/^\n+/)?.[0].length??0;
      breaks=Math.min(2,breaks+leading);
      const body=part.slice(leading);
      if(!body||(breaks&&/^[ \t]+$/.test(body)))continue;
      const trailing=body.match(/\n+$/)?.[0].length??0;
      chunks.push('\n'.repeat(breaks),body.slice(0,body.length-trailing));
      breaks=Math.min(2,trailing);
    }
    return chunks.join('')+'\n'.repeat(breaks);
  }
  function markdown(node){
    if(node.nodeType===3){
      const raw=node.textContent||'';
      // Indentation in the HTML source is whitespace between inline or block siblings.
      if(/^\s+$/.test(raw)&&raw.includes('\n'))return ' ';
      return raw.replace(/([\\`*_[\]<>])/g,'\\$1');
    }
    if(node.nodeType!==1||node.matches(ignored))return '';
    const tag=node.tagName.toLowerCase();
    const children=()=>joinMarkdown(Array.from(node.childNodes,markdown));
    if(node.matches('[data-chatgpt-mermaid-preview]'))return '\n\n[Mermaid 圖表，請在原對話查看]\n\n';
    if(node.matches('.katex-display,.katex,math,[data-d-component="math"]')){
      const display=node.matches('.katex-display,[display="block"],[data-d-block]');
      let tex=node.querySelector('annotation[encoding="application/x-tex"]')?.textContent?.trim();
      if(!tex&&node.matches('[data-d-component="math"]')){
        const raw=node.textContent?.trim()||'';
        const wrapped=display?/^\\?\[([\s\S]*?)\\?\]$/:/^\\?\(([\s\S]*?)\\?\)$/;
        tex=(raw.match(wrapped)?.[1]??raw).trim();
      }
      if(tex){
        return display?`\n\n$$\n${tex}\n$$\n\n`:`$${tex}$`;
      }
    }
    if(tag==='br')return '\n';
    if(tag==='a'){
      let href;try{href=new URL(node.getAttribute('href'),location.href);}catch{return children();}
      return ['http:','https:'].includes(href.protocol)?`[${children().trim()}](${href.href.replace(/\(/g,'%28').replace(/\)/g,'%29')})`:children();
    }
    if(tag==='pre'||node.matches('[data-code-block="true"]')){
      const widget=tag!=='pre';
      const source=widget?widgetCodeSource(node):node;
      if(!source)return '';
      const code=source.querySelector('code')||source;
      const raw=code.matches('.cm-content')?
        Array.from(code.querySelectorAll(':scope > .cm-line'),line=>codeText(line,false)).join('\n'):
        codeText(code);
      const toolbar=widget?node.querySelector(':scope > [data-markdown-copy="exclude"]'):null;
      const language=code.getAttribute('data-language')||node.getAttribute('data-language')||
        Array.from(code.classList).find(name=>name.startsWith('language-'))?.slice(9)||
        (toolbar?codeText(toolbar).trim().toLowerCase():'');
      const info=/^[\w.+#-]+$/.test(language)?language:'';
      const fence=codeFence(raw,3);
      return `\n\n${fence}${info}\n${raw}${raw.endsWith('\n')?'':'\n'}${fence}\n\n`;
    }
    if(tag==='code'){
      const raw=codeText(node);
      const fence=codeFence(raw,1);
      return `${fence} ${raw} ${fence}`;
    }
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
    if(tag==='li'){
      const prefix=node.parentElement?.tagName==='OL'?`${Array.from(node.parentElement.children).indexOf(node)+Number(node.parentElement.getAttribute('start')||1)}. `:'- ';
      return '\n'+prefix+value.trim().replace(/\n/g,'\n'+' '.repeat(prefix.length));
    }
    if(tag==='blockquote')return '\n\n'+value.trim().split('\n').map(l=>'> '+l).join('\n')+'\n\n';
    if(tag==='hr')return '\n\n---\n\n';
    return /^(p|div|section|article|ul|ol)$/.test(tag)?(value.trim()?'\n\n'+value.trim()+'\n\n':''):value;
  }
  const generation_active=Array.from(document.querySelectorAll('form button')).some(e=>visible(e)&&/stop|停止|停止串流/i.test(e.getAttribute('aria-label')||''));
  const turns=Array.from(document.querySelectorAll('[data-talvt-turn-state]')).map(turn=>{
    const user=turn.querySelector('[data-chatgpt-search-message-ids]');
    const all=Array.from(turn.querySelectorAll('[data-markdown-text-style="assistant-message"]'));
    const candidates=all.filter(e=>visible(e)&&!user?.contains(e)&&!e.closest('[data-markdown-text-tone="tertiary"]'));
    // Search/reasoning progress uses the same markdown attribute. Only answer roots
    // count, and nested markdown nodes must not duplicate the enclosing answer.
    const responses=candidates.filter(e=>!candidates.some(parent=>parent!==e&&parent.contains(e)));
    const rawAnswer=responses.map(text).filter(Boolean).join('\n\n');
    const render_error=answerLoadError.test(rawAnswer)?'ANSWER_LOAD_FAILED':null;
    const answer=render_error?'':rawAnswer;
    // A hidden pending marker may remain after hydration. Check for the actual code source.
    const pendingCode=responses.some(root=>Array.from(root.querySelectorAll('[data-client-defined-widget="code_block"]')).some(
      widget=>visible(widget)&&!widgetCodeSource(widget)&&!visible(widget.querySelector(readyMermaid))
    ));
    const final_actions=Array.from(turn.querySelectorAll('.turn-action-controls')).some(row=>visible(row)&&
      visible(row.querySelector('button[aria-label="複製"],button[aria-label="Copy"]'))&&
      visible(row.querySelector('button[aria-label="更多操作"],button[aria-label="More actions"]')));
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
    const unsupported_outputs=Array.from(turn.querySelectorAll('audio,video,canvas,iframe,svg')).filter(e=>!user?.contains(e)&&visible(e)&&!e.closest('[data-markdown-text-tone="tertiary"]')&&(e.matches(readyMermaid)||(e.getBoundingClientRect().width>64&&e.getBoundingClientRect().height>32))).map(e=>({kind:e.matches(readyMermaid)?'mermaid':e.tagName.toLowerCase(),title:(e.getAttribute('aria-label')||e.getAttribute('title')||'').slice(0,200),export_status:'UNSUPPORTED'}));
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
    return {assistant_markdown:render_error?'':joinMarkdown(responses.flatMap((response,index)=>index?['\n\n',markdown(response)]:[markdown(response)])).trim(),extraction,generation_active,render_error,state:turn.getAttribute('data-talvt-turn-state'),user_id:user?.getAttribute('data-chatgpt-search-message-ids')??null,
      user_text:text(user?.querySelector('[data-search-result-target]')||user),assistant_id:selected?.getAttribute('data-chatgpt-selection-message-id')??null,
      assistant_text:answer,sources,external_images,render_ready:!render_error&&!pendingCode&&!!(answer||native_images.some(i=>i.complete&&i.naturalWidth>0)||external_images.length||unsupported_outputs.length)&&native_images.every(i=>i.complete&&i.naturalWidth>0),
      artifacts,unsupported_outputs,final_actions,regenerate:!!turn.querySelector('button[aria-label="重新產生回覆"]'),
      reader_diagnostics:{version:4,pending_code:pendingCode,progress_images_ignored,answer_roots:responses.length,reasoning_nodes_excluded:all.filter(e=>e.closest('[data-markdown-text-tone="tertiary"]')).length,user_controls_excluded:user?.querySelectorAll(ignored).length??0}};
  });
  const editor=document.querySelector('form [contenteditable="true"]');
  const attachmentGroup=editor?.closest('form')?.querySelector('[data-composer-attachments]');
  const draft_attachments=Array.from(attachmentGroup?.querySelectorAll('button[aria-label]')??[])
    .map(button=>button.getAttribute('aria-label').match(/^(?:移除|Remove) (.+)$/)?.[1]).filter(Boolean);
  return {url:location.href,title:document.title,turns,editor_present:visible(editor),draft:text(editor),draft_has_autolinks:!!editor?.querySelector('[data-rich-text-generated-autolink]'),
    draft_attachments,draft_uploading:!!attachmentGroup?.querySelector('[role="progressbar"]'),
    model_label:document.querySelector('button[aria-label="選取 ChatGPT 模型"]')?.innerText,
    buttons:Array.from(document.querySelectorAll('form button')).filter(visible).map(b=>({label:b.getAttribute('aria-label'),disabled:b.disabled})),
    dialogs:Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).filter(e=>visible(e)&&!(
      e.matches('[role="dialog"][data-slot="popover-content"]:not([aria-modal="true"])')&&
      e.querySelector('[data-summary-panel-variant="dynamic-isle"]')
    )).map(e=>e.innerText.slice(0,800)),
    alerts:Array.from(document.querySelectorAll('[role="alert"]')).filter(visible).map(e=>e.innerText.slice(0,800)),
    login:!editor&&Array.from(document.querySelectorAll('button,a')).filter(visible).some(e=>/^(登入|登录|Log in|Sign in)$/.test(e.innerText.trim())),
    breadcrumb:Array.from(document.querySelectorAll('a[href]')).some(a=>a.getAttribute('href')?.endsWith('/project')&&visible(a))};
}
