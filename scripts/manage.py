"""Start/stop only this project's detached server; never kills a browser."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.environ.get('EGO_BRIDGE_RUNTIME',str(Path.home()/'apps/ego-chatgpt-bridge'))).expanduser().resolve()
PIDFILE=RUNTIME/'process.json'
EXECUTABLE=RUNTIME/'.venv/bin/ego-bridge'
LABEL='com.raymor.ego-chatgpt-bridge'
JOB=f'gui/{os.getuid()}/{LABEL}'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'


def launch(*args):return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True)


def running():
    if not PIDFILE.exists():
        return None
    data=json.loads(PIDFILE.read_text())
    command=subprocess.run(['ps','-p',str(data['pid']),'-o','command='],capture_output=True,text=True).stdout
    if str(RUNTIME) in command and (str(EXECUTABLE) in command or '-m ego_bridge.cli' in command):
        return data['pid']
    return None


command=sys.argv[1] if len(sys.argv)>1 else 'status'
pid=running()
managed=bool(sys.platform=='darwin' and launch('print',JOB).returncode==0)
if command=='status':
    print(json.dumps({'running':bool(pid),'pid':pid,'runtime':str(RUNTIME),'launchd_loaded':managed}))
elif command=='stop':
    if managed:
        result=launch('bootout',JOB)
        if result.returncode:raise SystemExit('launchd bootout failed')
        print('LaunchAgent stopped for this login; runtime data and login startup configuration preserved.')
    elif not pid:
        print('No matching server process; nothing stopped.')
    else:
        os.kill(pid,signal.SIGTERM)
        for _ in range(200):
            if not running():
                break
            time.sleep(.1)
        print('Stop requested; in-flight submission intents remain durable.')
elif command=='start':
    if pid:
        print(f'Already running: {pid}')
    else:
        if sys.platform=='darwin' and PLIST.exists():
            if not managed:
                launch('enable',JOB)
                result=launch('bootstrap',f'gui/{os.getuid()}',str(PLIST))
                if result.returncode:raise SystemExit('launchd bootstrap failed')
            print('LaunchAgent started; watchdog waits for the configured network and supervises the server.')
            raise SystemExit(0)
        fd=os.open(RUNTIME/'service.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as log:
            child=subprocess.Popen([str(EXECUTABLE),'--runtime',str(RUNTIME),'serve'],cwd=ROOT,stdin=subprocess.DEVNULL,
                                   stdout=log,stderr=log,start_new_session=True)
        for _ in range(100):
            if running():
                print(f'Started: {child.pid}; log: {RUNTIME / "service.log"}')
                break
            if child.poll() is not None:
                raise SystemExit('Server failed; inspect runtime/service.log')
            time.sleep(.1)
        else:
            raise SystemExit('Startup timed out; inspect runtime/service.log before retrying')
else:
    raise SystemExit('Usage: python scripts/manage.py start|stop|status')
