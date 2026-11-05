"""Install a user LaunchAgent. No root access or credentials in the plist."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .model import BridgeError

LABEL='com.raymor.ego-chatgpt-bridge'


def launch_config(runtime: Path) -> dict:
    python=runtime/'.venv/bin/python'
    return {'Label':LABEL,'ProgramArguments':[str(python),'-m','ego_bridge.cli','--runtime',str(runtime),'supervise'],
        'WorkingDirectory':str(runtime),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
        'ProcessType':'Background','ExitTimeOut':25,
        'EnvironmentVariables':{'PATH':os.pathsep.join([str(runtime/'.venv/bin'),str(Path.home()/'.local/bin'),'/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin'])},
        'StandardOutPath':str(runtime/'supervisor.log'),'StandardErrorPath':str(runtime/'supervisor.log')}


def manage_autostart(runtime: Path,action: str):
    if sys.platform!='darwin':raise SystemExit('Autostart currently supports macOS launchd only.')
    target=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist';domain=f'gui/{os.getuid()}';job=domain+'/'+LABEL
    def launch(*args,check=False):
        result=subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True)
        if check and result.returncode:
            raise BridgeError('LAUNCHD_ACTION_FAILED',result.stderr.strip()[:1000] or 'launchctl 操作失败。',context={'action':args[0],'exit_code':result.returncode})
        return result
    if action=='status':
        r=launch('print',job)
        print(json.dumps({'installed':target.exists(),'loaded':r.returncode==0,'plist':str(target),
            'supervisor':json.loads((runtime/'supervisor.json').read_text()) if (runtime/'supervisor.json').exists() else None},ensure_ascii=False,indent=2));return
    if action=='install':
        if not (runtime/'.venv/bin/python').exists():raise SystemExit('Runtime virtualenv is missing; run scripts/setup.sh first.')
        # Never silently race an already running manually-started server.
        pidfile=runtime/'process.json'
        if pidfile.exists():
            pid=json.loads(pidfile.read_text())['pid']
            command=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
            if (str(runtime) in command and 'ego_bridge' in command) or str(runtime/'.venv/bin/ego-bridge') in command:
                if launch('print',job).returncode!=0:raise SystemExit('Stop the manual server before installing autostart.')
        target.parent.mkdir(parents=True,exist_ok=True)
        for name in ['supervisor.log','service.log']:
            fd=os.open(runtime/name,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.close(fd)
        launch('bootout',job)
        target.write_bytes(plistlib.dumps(launch_config(runtime)));target.chmod(0o600)
        launch('enable',job,check=True);launch('bootstrap',domain,str(target),check=True)
        print(json.dumps({'installed':True,'label':LABEL,'plist':str(target),'starts':'user_login','keepalive':True}));return
    if action=='uninstall':
        launch('bootout',job);target.unlink(missing_ok=True)
        print(json.dumps({'installed':False,'runtime_data_preserved':True}));return
    if action=='restart':
        launch('kill','SIGTERM',job,check=True)
        print(json.dumps({'restart_requested':True}));return
    raise ValueError('Unknown autostart action')
