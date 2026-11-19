// Capture real ChatGPT page states as DOM contract canary snapshots.
// Maintenance tool: the ego-chatgpt-bridge service must be STOPPED before
// running (it holds the space lock). The embedded Node runtime does not
// inherit shell env or cwd, so paths are constants — edit them if the checkout
// or runtime moves. Snapshots land in verification/dom-snapshots/*.json (0600).
const fs=await import('node:fs');
const os=await import('node:os');
const path=await import('node:path');
const REPO='/Users/Ray/Documents/Fork/ego-chatgpt-bridge';
const RUNTIME=path.join(os.homedir(),'apps/ego-chatgpt-bridge');
const config=JSON.parse(fs.readFileSync(path.join(RUNTIME,'config.json'),'utf8'));
const SPACE_NAME=config.space_name||'ego-research-runtime';
const PROJECT_URL=`https://chatgpt.com/g/${config.project_id}/project`;
// Canary conversations: rename or re-point these after old ones expire.
const CONVERSATIONS=[
  {name:'conversation-complete',url:'https://chatgpt.com/g/'+config.project_id+'/c/6afd3369-bcb8-83e9-be4d-2b9f7bd1cbc3'},
];
const out={captured:[]};
try{
  // Load the reader as a self-contained function (toString survives evaluate).
  const helper=fs.readFileSync(path.join(REPO,'src/ego_bridge/page-state.mjs'),'utf8')
    .replace('export function readChatPage','function readChatPage').replace('export function domCapabilities','function domCapabilities');
  const readChatPage=new Function(helper+'\nreturn readChatPage;')();
  const state=page=>page.evaluate(readChatPage);
  const spaces=await listTaskSpaces();
  const entry=spaces.find(s=>s.name===SPACE_NAME);
  // Mirror the service's recreate_missing_space policy: a browser restart can
  // leave the named space absent; taskSpace(name) recreates it.
  const task=entry?(entry.ownership==='user'||entry.ownership==='inactive'?await claimTaskSpace(entry.id):await takeOverTaskSpace(entry.id))
    :await taskSpace(SPACE_NAME);
  if(task.name!==SPACE_NAME)throw Error('space identity mismatch: '+task.name);
  const directory=path.join(RUNTIME,'verification/dom-snapshots');
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  async function snapshot(name,url,kind){
    const page=await task.newPage();
    try{
      await page.goto(url,{timeout:25000});
      await page.waitForSelector('loc=css:form [contenteditable="true"]',{state:'visible',timeout:25000});
      if(kind==='conversation'){
        // Wait until at least one turn exists and complete turns report render_ready.
        const deadline=Date.now()+30000;let turns=0;
        while(Date.now()<deadline){
          const s=await state(page);
          turns=s.turns.length;
          if(turns>0&&s.turns.every(t=>t.state!=='complete'||t.render_ready))break;
          await new Promise(r=>setTimeout(r,1000));
        }
        if(!turns)throw Error('no turns loaded for '+name);
      }
      const raw=await state(page);
      const file=path.join(directory,name+'.json');
      fs.writeFileSync(file,JSON.stringify({kind,url:raw.url,captured_at:Date.now()/1000,state:raw},null,1),{mode:0o600});
      out.captured.push({name,kind,url:raw.url,turns:raw.turns.length,bytes:fs.statSync(file).size});
    } finally { try{await page.close()}catch{} }
  }
  await snapshot('project-page',PROJECT_URL,'project');
  for(const target of CONVERSATIONS)await snapshot(target.name,target.url,'conversation');
  console.log('EGO_BRIDGE_JSON:'+JSON.stringify({ok:true,value:out}));
}catch(error){
  console.log('EGO_BRIDGE_JSON:'+JSON.stringify({ok:false,error:{error_code:'CAPTURE_FAILED',message:String(error.message||error).slice(0,300)}}));
}
