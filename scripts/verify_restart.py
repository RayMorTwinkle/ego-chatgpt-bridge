"""Real restart during the durable post-send wait; never re-sends a request."""
import asyncio
import json
import ssl
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from ego_bridge.model import Settings
from ego_bridge.cli import DEFAULT_RUNTIME
from ego_bridge.auth import load_clients

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=DEFAULT_RUNTIME


async def main():
    token=load_clients(RUNTIME)[Settings.load(RUNTIME).default_owner]
    context=ssl.create_default_context(cafile=str(RUNTIME/'tls-cert.pem'))
    report={'test':'real_service_restart_after_submission','started_at':time.time()}
    target=RUNTIME/'verification/restart-integration.json'
    target.parent.mkdir(mode=0o700,exist_ok=True)
    def save():target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    async with httpx.AsyncClient(verify=context,headers={'Authorization':f'Bearer {token}'},timeout=90) as http:
        async def api(name,args):
            r=await http.post(Settings.load(RUNTIME).base_url+'/api/'+name,json=args)
            r.raise_for_status();return r.json()
        task=await api('create_query',dict(text='EGO_RESTART_TEST: 请只回复 RESTART_OK_5050。',request_id='restart-'+str(time.time_ns())))
        report['task']=task;save()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            state=await api('get_query_status',{'query_id':task['query_id']})
            if state['status'] in {'WAITING_FOR_RESPONSE','GENERATING','COMPLETED'}:
                break
            if state['status'] in {'NEEDS_ATTENTION','OUTCOME_UNKNOWN','FAILED'}:
                report['failure']=state;save();raise RuntimeError(state)
            await asyncio.sleep(.1)
        else: raise TimeoutError('No post-send state observed')
        report['before_restart']=state;save()
        for command in ['stop','start']:
            process=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'scripts/manage.py'),command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await process.communicate()
            if process.returncode:raise RuntimeError('Service lifecycle command failed')
        for _ in range(30):
            try:
                r=await api('get_query_result',{'query_id':task['query_id']})
                if r['status']=='COMPLETED':break
                if r['status'] in {'NEEDS_ATTENTION','OUTCOME_UNKNOWN'}:
                    r=await api('recover_session',{'query_id':task['query_id']})
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:raise TimeoutError('Recovery did not finish')
        assert r['complete'] and r['result']['content'].strip()=='RESTART_OK_5050'
        with sqlite3.connect(RUNTIME/'bridge.sqlite3') as db:
            saved=json.loads(db.execute('SELECT result FROM operations WHERE id=?',(task['message_id'],)).fetchone()[0])
        assert len(saved['history'])==1
        report.update(after_restart=r,exactly_one_visible_user_turn=True,unfinished_at_restart=state['status']!='COMPLETED',finished_at=time.time())
        save()
        print(json.dumps({'ok':True,'unfinished_at_restart':report['unfinished_at_restart'],'before_status':state['status'],'after_status':r['status'],'exactly_one_visible_user_turn':True,'report':str(target)},ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
