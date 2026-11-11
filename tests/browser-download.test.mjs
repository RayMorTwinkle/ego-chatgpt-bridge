import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,mkdtemp,readFile as readBytes,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
const source=await readFile(new URL('../src/ego_bridge/browser.mjs',import.meta.url),'utf8');
const definitions=source.slice(source.indexOf('function classifyDownload('),source.indexOf('async function capture('));
const {classifyDownload,downloadAssociation}=new Function(`${definitions};return {classifyDownload,downloadAssociation};`)();
const success=download_url=>({status:'success',file_name:'SKILL.md',download_url});
for(const url of ['https://chatgpt.com/backend-api/estuary/content?sig=private','https://files.oaiusercontent.com/a','https://chatgpt.com:443/a'])test(`trusted absolute ${new URL(url).hostname}`,()=>assert.equal(classifyDownload(success(url)).url,new URL(url).href));
test('trusted relative paths resolve',()=>{
  for(const path of ['/backend-api/estuary/content?sig=private','/backend-api/files/download/abc'])assert.equal(classifyDownload(success(path)).url,'https://chatgpt.com'+path);
});
for(const [data,status,code] of [
  [success(''),200,'ARTIFACT_URL_MISSING'],[success('  '),200,'ARTIFACT_URL_MISSING'],[{status:'success'},200,'ARTIFACT_URL_MISSING'],
  [{status:'error',error_code:'ace_pod_expired'},200,'ARTIFACT_POD_EXPIRED'],[{status:'error',error_code:'unknown'},200,'ARTIFACT_PROVIDER_ERROR'],
  [{detail:'Library file not found'},404,'ARTIFACT_REFERENCE_NOT_FOUND'],[null,429,'ARTIFACT_HTTP_TRANSIENT'],[null,503,'ARTIFACT_HTTP_TRANSIENT'],
  [null,200,'ARTIFACT_URL_MALFORMED'],[success('broken'),200,'ARTIFACT_URL_MALFORMED'],
  ...['https://evil.example/a','https://user:password@chatgpt.com/a','https://chatgpt.com:444/a','http://chatgpt.com/a','//evil.example/a','/backend-api/estuary/content-evil'].map(url=>[success(url),200,'ARTIFACT_RESOURCE_UNTRUSTED'])
])test(`classifies ${code} ${data?.download_url??status}`,()=>{
  const result=classifyDownload(data,status);assert.equal(result.code,code);
  assert.deepEqual(Object.keys(result.context).sort(),['http_status','provider_status','provider_error_code','download_url_kind','url_host','metadata_keys'].sort());
  assert.doesNotMatch(JSON.stringify(result.context),/password|sig=private/);
});
test('request association accepts missing filename only with matching assistant ID',()=>{
  assert.equal(downloadAssociation('a','a',{},'SKILL.md'),true);
  assert.equal(downloadAssociation('other','a',success(''),'SKILL.md'),false);
  assert.equal(downloadAssociation(null,'a',success(''),'SKILL.md'),true);
  assert.equal(downloadAssociation(null,null,{},'SKILL.md'),false);
});

async function runCapture(sequence,{fetchThrows=false,assistantId='a'}={}){
  const dir=await mkdtemp(tmpdir()+'/bridge-capture-'),destination=dir+'/file';
  let clicks=0,polled=false,disabled=false,clock=0;
  const nativeDate=Date;
  class FakeDate extends nativeDate {static now(){return clock;}}
  const input={candidate:{kind:'file_card',name:'SKILL.md',index:0},user_message_id:'u',assistant_message_id:assistantId,destination};
  const page={evaluate:async()=>true,hover:async()=>{},click:async()=>{clicks++;polled=false;},
    events:async()=>{
      if(!clicks||polled)return [];polled=true;
      return (sequence[clicks-1]??[]).flatMap((item,i)=>[
        {method:'Network.requestWillBeSent',params:{requestId:String(i),request:{url:'https://chatgpt.com/backend-api/interpreter/download?message_id='+item.messageId}}},
        {method:'Network.responseReceived',params:{requestId:String(i),response:{url:'https://chatgpt.com/backend-api/interpreter/download',status:item.status??200,headers:item.headers??{}}}},
        {method:'Network.loadingFinished',params:{requestId:String(i)}},
        ...(item.asset?[{method:'Network.responseReceived',params:{requestId:'asset',response:{url:item.data.download_url,status:200,mimeType:'text/plain'}}},{method:'Network.loadingFinished',params:{requestId:'asset'}}]:[])
      ]);
    },
    cdp:async(method,{requestId}={})=>{
      if(method==='Network.disable')disabled=true;
      if(method==='Network.getResponseBody')return {body:requestId==='asset'?'file bytes':JSON.stringify(sequence[clicks-1][Number(requestId)].data)};
    },
    fetch:async()=>{if(fetchThrows)throw Error('https://signed.example/?secret=private');return {ok:false};}
  };
  const fn=source.slice(source.indexOf('async function capture('),source.indexOf('\nasync function configure('));
  const fail=(code,message,stage,context)=>{throw Object.assign(Error(message),{code,diagnostic_context:context});};
  const run=new Function('input','classifyDownload','downloadAssociation','fail','Date','setTimeout',`${fn};return capture;`)(input,classifyDownload,downloadAssociation,fail,FakeDate,(cb,ms)=>{clock+=ms;cb();});
  try {return {result:await run(page),bytes:await readBytes(destination,'utf8'),clicks,disabled};}
  catch(error){return {error,clicks,disabled};}
  finally {await rm(dir,{recursive:true,force:true});}
}
test('reclicks with fresh metadata, associates assistant without filename, and falls back after fetch throws',async()=>{
  const r=await runCapture([[{messageId:'a',data:success('')}],[{messageId:'wrong',data:success('https://evil.example/a')},{messageId:'a',data:{status:'success',download_url:'https://chatgpt.com/a',file_size_bytes:null},asset:true}]],{fetchThrows:true});
  assert.equal(r.clicks,2);assert.equal(r.bytes,'file bytes');assert.equal(r.result.transport,'ui_download_response');assert.equal(r.disabled,true);
});
test('transient metadata retries three clicks with Retry-After and safe diagnostics',async()=>{
  const item={messageId:'a',status:429,headers:{'Retry-After':'3'},data:{status:'error',error_code:'limited'}};
  const r=await runCapture([[item],[item],[item]]);
  assert.equal(r.clicks,3);assert.equal(r.error.code,'ARTIFACT_HTTP_TRANSIENT');
  assert.equal(r.error.diagnostic_context.attempt,3);assert.equal(r.error.diagnostic_context.elapsed_ms,6000);assert.equal(r.disabled,true);
});
test('untrusted resource does not reclick',async()=>{
  const r=await runCapture([[{messageId:'a',data:success('https://evil.example/a') }]]);
  assert.equal(r.clicks,1);assert.equal(r.error.code,'ARTIFACT_RESOURCE_UNTRUSTED');
});
test('no observed metadata retries at most three clicks',async()=>{
  const r=await runCapture([[],[],[]]);assert.equal(r.clicks,3);assert.equal(r.error.code,'ARTIFACT_DOWNLOAD_UNOBSERVED');
});
test('pod expiration and missing reference recover with fresh UI metadata',async()=>{
  for(const item of [{status:200,data:{status:'error',error_code:'ace_pod_expired'}},{status:404,data:{detail:'Library file not found'}}]){
    const r=await runCapture([[{messageId:'a',...item}],[{messageId:'a',data:success('https://chatgpt.com/a'),asset:true}]],{fetchThrows:true});
    assert.equal(r.clicks,2);assert.equal(r.bytes,'file bytes');
  }
});
test('provider failure without filename stops immediately',async()=>{
  const r=await runCapture([[{messageId:'a',data:{status:'error',error_code:'unexpected'}}]]);
  assert.equal(r.clicks,1);assert.equal(r.error.code,'ARTIFACT_PROVIDER_ERROR');
});
