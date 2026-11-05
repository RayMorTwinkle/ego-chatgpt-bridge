"""WebUI-only read-only exports. No normal Query records or MCP tools are created."""
from __future__ import annotations

from collections import Counter
import asyncio
import contextlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .artifacts import filename,inspect_file
from .model import BridgeError,identifier,now


def conversation_url(value: str) -> str:
    try:
        url=urlsplit(value.strip())
        if url.scheme!='https' or url.hostname!='chatgpt.com' or url.username or url.password or url.port not in (None,443):raise ValueError()
        if not re.fullmatch(r'(?:/g/[^/]+)?/c/[a-fA-F0-9-]{36}/?',url.path):raise ValueError()
        return urlunsplit(('https','chatgpt.com',url.path.rstrip('/'),'',''))
    except (ValueError,AttributeError):
        raise BridgeError('INVALID_CONVERSATION_URL','请粘贴 https://chatgpt.com/c/... 或专案内对话的实际地址。',stage='debug')


class ExportJobs:
    def __init__(self,service):
        self.downloads=Counter()
        self.service=service;self.db=service.store.db;self.root=service.settings.runtime/'exports';self.tasks=set()
        if self.root.is_symlink():raise BridgeError('INVALID_STORAGE','导出目录不能是符号链接。')
        self.root.mkdir(mode=0o700,exist_ok=True)
        self.db.execute('''CREATE TABLE IF NOT EXISTS debug_exports(
         id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,url TEXT NOT NULL,status TEXT NOT NULL,
         manifest TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(owner,request_id))''')
        self.db.execute("UPDATE debug_exports SET status='NEEDS_ATTENTION',error=? WHERE status IN ('QUEUED','RUNNING')",(json.dumps({'error_code':'EXPORT_INTERRUPTED','message':'服务已重启，可重新发起只读导出。'}),))
        columns={r[1] for r in self.db.execute('PRAGMA table_info(debug_exports)')}
        if 'expires_at' not in columns:self.db.execute('ALTER TABLE debug_exports ADD COLUMN expires_at REAL')
        self.db.execute('UPDATE debug_exports SET expires_at=updated_at+? WHERE expires_at IS NULL',(service.settings.export_retention_days*86400,))

    def get(self,owner,export_id):
        row=self.db.execute('SELECT * FROM debug_exports WHERE id=? AND owner=?',(export_id,owner)).fetchone()
        if not row:raise BridgeError('EXPORT_NOT_FOUND','找不到该导出。',stage='debug')
        result=dict(row)
        for key in ['manifest','error']:result[key]=json.loads(result[key]) if result[key] else None
        if result['status'] not in {'QUEUED','RUNNING'} and result['expires_at'] is not None and result['expires_at']<=now():result['status']='EXPIRED'
        result['download_url']=f'/ui/api/exports/{export_id}/download' if result['status'] in {'COMPLETED','PARTIAL'} else None
        return result

    def list(self,owner):
        return [self.get(owner,r[0]) for r in self.db.execute('SELECT id FROM debug_exports WHERE owner=? ORDER BY created_at DESC LIMIT 50',(owner,))]

    def create(self,owner,url,request_id):
        url=conversation_url(url)
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:raise BridgeError('INVALID_ARGUMENT','需要幂等请求标识。')
        old=self.db.execute('SELECT id,url FROM debug_exports WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
        if old:
            if old['url']!=url:raise BridgeError('IDEMPOTENCY_CONFLICT','相同请求标识对应不同对话。')
            return self.get(owner,old['id'])
        count=self.db.execute("SELECT COUNT(*) FROM debug_exports WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
        if count>=4:raise BridgeError('EXPORT_QUEUE_FULL','已有 4 个导出等待执行。')
        job=identifier('export');self.db.execute('INSERT INTO debug_exports(id,owner,request_id,url,status,manifest,error,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(job,owner,request_id,url,'QUEUED',None,None,now(),now(),now()+self.service.settings.export_retention_days*86400))
        task=asyncio.create_task(self.run(owner,job));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        return self.get(owner,job)

    def directory(self,job):
        if not re.fullmatch(r'export_[0-9a-f]{32}',job):raise BridgeError('EXPORT_NOT_FOUND','无效导出标识。')
        return self.root/job

    async def run(self,owner,job):
        page_label=None;url=self.get(owner,job)['url']
        folder=self.directory(job);folder.mkdir(mode=0o700,exist_ok=True)
        try:
            async with self.service.control:
                self.db.execute("UPDATE debug_exports SET status='RUNNING',updated_at=? WHERE id=?",(now(),job))
                observed=await self.service.adapter.call('debug_observe',conversation_url=url)
                page_label=observed['page_label'];state=observed['state'];turns=state['turns']
                manifest={'export_id':job,'conversation_url':url,'created_at':now(),'read_only':True,
                          'history_scope':'rendered_conversation','history_pagination_verified':False,'turn_count':len(turns),
                          'artifacts':[],'warnings':['长对话的服务端历史分页尚未验证；归档记录的是本轮已加载的对话内容。']}
                lines=['# ChatGPT 对话导出',f'\n来源：{url}\n']
                files=[]
                for n,turn in enumerate(turns,1):
                    lines.extend([f'## {n}. 用户',turn.get('user_text',''),f'## {n}. ChatGPT',turn.get('assistant_text','')])
                    for link in turn.get('sources',[]):lines.append(f"- {link.get('title','链接')}: {link['url']}")
                    if turn.get('state')!='complete':manifest['warnings'].append(f'第 {n} 轮尚未完成生成。')
                    if not turn.get('render_ready',True):manifest['warnings'].append(f'第 {n} 轮的回答正文或图片尚未完成网页渲染，不能确认内容完整。')
                    for gap in turn.get('unsupported_outputs',[]):manifest['warnings'].append(f"第 {n} 轮的 {gap['kind']} 预览尚不支持导出原始文件。")
                    for index,candidate in enumerate(turn.get('artifacts',[]),1):
                        item={'turn':n,'filename':filename(candidate['name']),'status':'PENDING'}
                        manifest['artifacts'].append(item)
                        dest=folder/f'{n:04}-{index:03}.part'
                        try:
                            capture=await self.service.adapter.call('debug_capture',conversation_url=url,page_label=page_label,
                                user_message_id=turn['user_id'],candidate=candidate,destination=str(dest.resolve()))
                            if candidate['kind']=='inline_image':
                                ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif'}.get(capture.get('mime_type'))
                                if ext:item['filename']=Path(item['filename']).stem+ext
                            info=inspect_file(dest,item['filename'],self.service.settings.artifact_max_bytes)
                            target=folder/f'{n:04}-{index:03}-{item["filename"]}'
                            os.replace(dest,target);target.chmod(0o600)
                            archive_path='artifacts/'+target.name;files.append((target,archive_path));item.update(info,status='AVAILABLE',archive_path=archive_path)
                        except BridgeError as error:
                            item.update(status='FAILED',error=error.data);dest.unlink(missing_ok=True)
                    self.db.execute('UPDATE debug_exports SET manifest=?,updated_at=? WHERE id=?',(json.dumps(manifest,ensure_ascii=False),now(),job))
                partial=any(a['status']!='AVAILABLE' for a in manifest['artifacts']) or any(t.get('state')!='complete' or t.get('unsupported_outputs') or not t.get('render_ready',True) for t in turns)
                manifest['complete_for_loaded_content']=not partial
                zip_path=folder/'conversation.zip';temporary=folder/'conversation.zip.part'
                with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr('transcript.md','\n\n'.join(lines))
                    archive.writestr('transcript.json',json.dumps({'conversation_url':url,'turns':turns},ensure_ascii=False,indent=2))
                    archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
                    archive.writestr('README.txt','只读调试导出。文件缺失、未完成或未支持的内容见 manifest.json。\n未读取或导出 Cookie、Session Token、请求认证头。\n未核验服务端历史分页；长对话可能存在未加载历史。\n')
                    for source,name in files:archive.write(source,name)
                temporary.chmod(0o600);os.replace(temporary,zip_path)
                self.db.execute('UPDATE debug_exports SET status=?,manifest=?,updated_at=?,expires_at=? WHERE id=?',('PARTIAL' if partial else 'COMPLETED',json.dumps(manifest,ensure_ascii=False),now(),now()+self.service.settings.export_retention_days*86400,job))
        except asyncio.CancelledError:
            self.db.execute("UPDATE debug_exports SET status='NEEDS_ATTENTION',error=?,updated_at=? WHERE id=?",(json.dumps({'error_code':'EXPORT_INTERRUPTED','message':'导出中断，可重新发起只读导出。'}),now(),job));raise
        except Exception as error:
            detail=error.data if isinstance(error,BridgeError) else {'error_code':'EXPORT_FAILED','message':'导出执行异常。','type':type(error).__name__}
            self.db.execute("UPDATE debug_exports SET status='FAILED',error=?,updated_at=? WHERE id=?",(json.dumps(detail),now(),job))
        finally:
            if page_label:
                with contextlib.suppress(Exception):await self.service.adapter.call('debug_close',conversation_url=url,page_label=page_label)

    def delete(self,owner,job):
        row=self.get(owner,job)
        if row['status'] in {'QUEUED','RUNNING'} or self.downloads[job]:raise BridgeError('EXPORT_BUSY','导出执行中。')
        shutil.rmtree(self.directory(job),ignore_errors=True)
        self.db.execute('DELETE FROM debug_exports WHERE id=? AND owner=?',(job,owner))

    async def stop(self):
        tasks=list(self.tasks)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
