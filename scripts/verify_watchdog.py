"""Explicit live fault injection against this runtime's launchd-managed processes."""
import json
import os
import signal
import ssl
import subprocess
import time
from pathlib import Path

import httpx

from ego_bridge.auth import load_clients
from ego_bridge.cli import DEFAULT_RUNTIME
from ego_bridge.model import Settings


def main():
    runtime=DEFAULT_RUNTIME;settings=Settings.load(runtime)
    lifecycle=subprocess.run([str(runtime/'.venv/bin/ego-bridge'),'autostart','status'],capture_output=True,text=True,check=True)
    if not json.loads(lifecycle.stdout)['loaded']:raise SystemExit('LaunchAgent must be loaded before this explicit fault test.')
    context=ssl.create_default_context(cafile=str(runtime/'tls-cert.pem')) if settings.tls_enabled else True
    report={'transport':'HTTPS' if settings.tls_enabled else 'HTTP','ca_required':settings.tls_enabled,'checks':[]}
    target=runtime/'verification/watchdog-acceptance.json';target.parent.mkdir(mode=0o700,exist_ok=True)
    def save():target.write_text(json.dumps(report,ensure_ascii=False,indent=2));target.chmod(0o600)
    def state():return json.loads((runtime/'supervisor.json').read_text()) if (runtime/'supervisor.json').exists() else {}
    def verify_pid(pid,role):
        command=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','command=']).decode()
        assert str(runtime) in command and '-m ego_bridge.cli' in command and (' '+role) in command
    with httpx.Client(verify=context,headers={'Authorization':'Bearer '+load_clients(runtime)[settings.default_owner]},timeout=2,trust_env=False) as http:
        check=http.post(settings.base_url+'/api/diagnose',json={}).json()
        if not check.get('ok') or check['active_count']:raise SystemExit('Active work exists; do not perform the fault test now.')
        def wait_healthy(predicate=lambda value:True,timeout=100):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                current=state()
                if current.get('status')=='HEALTHY' and predicate(current):
                    try:
                        response=http.post(settings.base_url+'/api/diagnose',json={});response.raise_for_status();value=response.json()
                        if value.get('ok') and value.get('worker_running'):return current
                    except httpx.HTTPError:pass
                time.sleep(.5)
            raise TimeoutError(state())
        initial=wait_healthy();report['initial']=initial;save()
        start=time.monotonic();verify_pid(initial['server_pid'],'serve');os.kill(initial['server_pid'],signal.SIGKILL)
        recovered=wait_healthy(lambda value:value.get('server_pid')!=initial['server_pid'] and value.get('restarts',0)>initial['restarts'])
        report['checks'].append({'test':'server_SIGKILL','recovered':True,'seconds':round(time.monotonic()-start,2)});save();print(json.dumps(report['checks'][-1]),flush=True)
        stopped=recovered['server_pid']
        try:
            start=time.monotonic();verify_pid(stopped,'serve');os.kill(stopped,signal.SIGSTOP)
            after_freeze=wait_healthy(lambda value:value.get('server_pid')!=stopped and value.get('last_reason')=='HTTP_UNRESPONSIVE')
            report['checks'].append({'test':'server_SIGSTOP_HTTP_unresponsive','recovered':True,'seconds':round(time.monotonic()-start,2)});save();print(json.dumps(report['checks'][-1]),flush=True)
        finally:
            try:
                command=subprocess.check_output(['/bin/ps','-p',str(stopped),'-o','command=']).decode()
                if str(runtime) in command and ' serve' in command:os.kill(stopped,signal.SIGCONT)
            except (subprocess.CalledProcessError,ProcessLookupError):pass
        start=time.monotonic();verify_pid(after_freeze['supervisor_pid'],'supervise');os.kill(after_freeze['supervisor_pid'],signal.SIGKILL)
        last=wait_healthy(lambda value:value.get('supervisor_pid')!=after_freeze['supervisor_pid'] and value.get('server_pid')!=after_freeze['server_pid'])
        report['checks'].append({'test':'supervisor_SIGKILL_launchd_respawn','recovered':True,'seconds':round(time.monotonic()-start,2)})
        report.update(ok=True,final=last);save();print(json.dumps(report['checks'][-1]),flush=True)
        print(json.dumps({'ok':True,'transport':report['transport'],'tests':len(report['checks']),'status':last['status']}),flush=True)


if __name__=='__main__':main()
