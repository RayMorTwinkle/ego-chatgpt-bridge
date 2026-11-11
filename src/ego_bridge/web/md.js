'use strict';
// Small Markdown renderer for ChatGPT answers. Builds DOM nodes only (no innerHTML),
// so answer text can never inject markup. Covers what the bridge extracts:
// headings, paragraphs, nested lists, tables, fenced code, quotes, rules, inline styles, links.
(()=>{
const el=(tag,cls)=>{const e=document.createElement(tag);if(cls)e.className=cls;return e;};
const LIST=/^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const FENCE=/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING=/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE=/^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const TABLE_SEP=/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const indent=line=>line.match(/^\s*/)[0].replace(/\t/g,'    ').length;

function safeUrl(raw){
  try{const url=new URL(raw.trim(),location.href);return ['http:','https:','mailto:'].includes(url.protocol)?url.href:null;}catch{return null;}
}
function link(href,parent,build){
  const url=safeUrl(href);if(!url){build(parent);return;}
  const a=el('a');a.href=url;a.target='_blank';a.rel='noopener noreferrer';build(a);parent.append(a);
}
// Find the closing ")" of a link target, allowing balanced parentheses inside URLs.
function closeParen(text,from){
  let depth=0;
  for(let i=from;i<text.length;i++){
    if(text[i]==='(')depth++;
    else if(text[i]===')'){if(!depth)return i;depth--;}
    else if(text[i]===' '&&depth===0&&text[i+1]!=='"')return -1;
  }
  return -1;
}
function closeBracket(text,from){
  let depth=0;
  for(let i=from;i<text.length;i++){
    if(text[i]==='\\'){i++;continue;}
    if(text[i]==='[')depth++;
    else if(text[i]===']'){if(!depth)return i;depth--;}
  }
  return -1;
}

function inline(text,parent){
  let i=0,buf='';
  const flush=()=>{if(buf){parent.append(document.createTextNode(buf));buf='';}};
  const wrap=(tag,content)=>{flush();const e=el(tag);inline(content,e);parent.append(e);};
  while(i<text.length){
    const c=text[i],rest=text.slice(i);
    if(c==='\\'&&/[\\`*_{}\[\]()#+\-.!|~>]/.test(text[i+1]||'')){buf+=text[i+1];i+=2;continue;}
    if(c==='\n'){flush();parent.append(el('br'));i++;continue;}
    if(c==='`'){
      const ticks=rest.match(/^`+/)[0];const end=text.indexOf(ticks,i+ticks.length);
      if(end>0){flush();const code=el('code');code.textContent=text.slice(i+ticks.length,end).trim();parent.append(code);i=end+ticks.length;continue;}
    }
    if(rest.startsWith('**')||rest.startsWith('__')){
      const mark=rest.slice(0,2),end=text.indexOf(mark,i+2);
      if(end>i+2){wrap('strong',text.slice(i+2,end));i=end+2;continue;}
    }
    if(rest.startsWith('~~')){
      const end=text.indexOf('~~',i+2);
      if(end>i+2){wrap('del',text.slice(i+2,end));i=end+2;continue;}
    }
    if(c==='*'&&text[i+1]&&!/\s|\*/.test(text[i+1])){
      const end=text.indexOf('*',i+1);
      if(end>i+1&&!/\s/.test(text[end-1])){wrap('em',text.slice(i+1,end));i=end+1;continue;}
    }
    if(c==='['||(c==='!'&&text[i+1]==='[')){
      const open=c==='!'?i+1:i,close=closeBracket(text,open+1);
      if(close>0&&text[close+1]==='('){
        const end=closeParen(text,close+2);
        if(end>0){
          flush();
          const label=text.slice(open+1,close),target=text.slice(close+2,end).replace(/\s+"[^"]*"$/,'');
          link(target,parent,a=>c==='!'?a.append(document.createTextNode(label||'图片')):inline(label,a));
          i=end+1;continue;
        }
      }
    }
    if((c==='h'&&/^https?:\/\//.test(rest))&&(!i||/[\s(（「：:]/.test(text[i-1]))){
      const url=rest.match(/^https?:\/\/[^\s<>（）「」，。、；！？"]+/)[0].replace(/[.,;:!?)\]]+$/,'');
      flush();link(url,parent,a=>{a.textContent=url.replace(/^https?:\/\//,'').replace(/\?utm_source=chatgpt\.com$/,'');});
      i+=url.length;continue;
    }
    buf+=c;i++;
  }
  flush();
}

function cells(line){
  const trimmed=line.trim().replace(/^\|/,'').replace(/\|$/,'');
  const out=[];let cur='',code=false;
  for(let i=0;i<trimmed.length;i++){
    const c=trimmed[i];
    if(c==='\\'&&trimmed[i+1]==='|'){cur+='|';i++;continue;}
    if(c==='`')code=!code;
    if(c==='|'&&!code){out.push(cur.trim());cur='';continue;}
    cur+=c;
  }
  out.push(cur.trim());return out;
}
function isTable(lines,i){return lines[i].includes('|')&&i+1<lines.length&&TABLE_SEP.test(lines[i+1])&&lines[i+1].includes('-');}
function startsBlock(lines,i){
  const line=lines[i];
  return FENCE.test(line)||HEADING.test(line)||RULE.test(line)||/^\s{0,3}>/.test(line)||LIST.test(line)||isTable(lines,i);
}

function table(lines,i,parent){
  const head=cells(lines[i]),align=cells(lines[i+1]).map(s=>s.startsWith(':')&&s.endsWith(':')?'center':s.endsWith(':')?'right':'');
  const wrap=el('div','md-table'),t=el('table'),thead=el('thead'),tbody=el('tbody'),hr=el('tr');
  head.forEach((cell,n)=>{const th=el('th');if(align[n])th.style.textAlign=align[n];inline(cell,th);hr.append(th);});
  thead.append(hr);i+=2;
  while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){
    const tr=el('tr'),row=cells(lines[i]);
    head.forEach((_,n)=>{const td=el('td');if(align[n])td.style.textAlign=align[n];inline(row[n]||'',td);tr.append(td);});
    tbody.append(tr);i++;
  }
  t.append(thead,tbody);wrap.append(t);parent.append(wrap);return i;
}

function list(lines,i,parent){
  const first=lines[i].match(LIST),base=indent(lines[i]),ordered=/\d/.test(first[2]);
  const root=el(ordered?'ol':'ul');
  if(ordered&&parseInt(first[2],10)!==1)root.start=parseInt(first[2],10);
  const items=[];let item=null,loose=false;
  while(i<lines.length){
    const line=lines[i],m=line.match(LIST),depth=indent(line);
    if(!line.trim()){
      let j=i+1;while(j<lines.length&&!lines[j].trim())j++;
      if(j>=lines.length)break;
      const next=lines[j].match(LIST);
      if(indent(lines[j])>base||(next&&indent(lines[j])===base&&/\d/.test(next[2])===ordered)){if(item)item.lines.push('');loose=loose||indent(lines[j])===base;i++;continue;}
      break;
    }
    if(m&&depth<=base+1&&depth>=base){
      if(/\d/.test(m[2])!==ordered)break;
      item={lines:[m[3]],offset:depth+m[2].length+1};items.push(item);i++;continue;
    }
    if(!item)break;
    if(depth>base){item.lines.push(line.slice(Math.min(depth,item.offset)));i++;continue;}
    if(item.lines[item.lines.length-1]!==''&&!startsBlock(lines,i)){item.lines.push(line.trim());i++;continue;}
    break;
  }
  for(const entry of items){
    const li=el('li');let lines2=entry.lines;
    const task=lines2[0].match(/^\[([ xX])\]\s+(.*)$/);
    if(task){li.className='md-task';li.dataset.done=task[1]!==' ';lines2=[task[2],...lines2.slice(1)];}
    blocks(lines2,li);
    if(!loose)for(const p of li.querySelectorAll(':scope > p'))p.classList.add('tight');
    root.append(li);
  }
  parent.append(root);return i;
}

function blocks(lines,parent){
  let i=0;
  while(i<lines.length){
    const line=lines[i];let m;
    if(!line.trim()){i++;continue;}
    if((m=line.match(FENCE))){
      const fence=m[1],body=[];i++;
      while(i<lines.length&&!lines[i].trim().startsWith(fence)){body.push(lines[i]);i++;}
      i++;
      const pre=el('pre','md-code'),code=el('code');code.textContent=body.join('\n');
      if(m[2])pre.dataset.lang=m[2];pre.append(code);parent.append(pre);continue;
    }
    if((m=line.match(HEADING))){const h=el('h'+Math.min(6,m[1].length+1));inline(m[2],h);parent.append(h);i++;continue;}
    if(RULE.test(line)){parent.append(el('hr'));i++;continue;}
    if(/^\s{0,3}>/.test(line)){
      const body=[];
      while(i<lines.length&&lines[i].trim()&&(/^\s{0,3}>/.test(lines[i])||!startsBlock(lines,i))){body.push(lines[i].replace(/^\s{0,3}>\s?/,''));i++;}
      const q=el('blockquote');blocks(body,q);parent.append(q);continue;
    }
    if(isTable(lines,i)){i=table(lines,i,parent);continue;}
    if(LIST.test(line)){i=list(lines,i,parent);continue;}
    const body=[line.trim()];i++;
    while(i<lines.length&&lines[i].trim()&&!startsBlock(lines,i)){body.push(lines[i].trim());i++;}
    const p=el('p');inline(body.join('\n'),p);parent.append(p);
  }
}

window.renderMarkdown=text=>{
  const root=document.createDocumentFragment();
  blocks(String(text||'').replace(/\r\n?/g,'\n').split('\n'),root);
  return root;
};
})();
