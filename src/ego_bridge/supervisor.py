"""Foreground launchd job: private child server, network wait and HTTP watchdog."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .auth import load_clients
from .model import Settings


def private_state(path: Path, value: dict):
    temporary=path.with_name(path.name+'.part')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False)
    os.replace(temporary,path)


def network_ready(host: str) -> bool:
    try:
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,socket.SOCK_STREAM) as s:
            s.bind((host,0))
        return True
    except OSError:return False


class Supervisor:
    def __init__(self,runtime: Path,*,interval=10,timeout=3,failures=3):
        if interval<.1 or timeout<.1 or failures<1:raise ValueError('Invalid watchdog timing')
        self.runtime=runtime;self.interval=interval;self.timeout=timeout;self.threshold=failures
        self.stopping=threading.Event();self.child=None;self.restarts=0;self.last_reason=None
        self.browser_status='not_checked';self.next_browser_check=0

    def ensure_browser(self,settings):
        if not settings.start_ego or sys.platform!='darwin':return
        if time.monotonic()<self.next_browser_check:return
        self.next_browser_check=time.monotonic()+30
        path=Path(settings.ego_app)
        if not path.exists():self.browser_status='app_missing';return
        try:
            found=subprocess.run(['/usr/bin/pgrep','-f',str(path/'Contents/MacOS')],capture_output=True,timeout=5)
            if found.returncode==0:self.browser_status='running';return
            opened=subprocess.run(['/usr/bin/open','-g','-a',str(path)],capture_output=True,timeout=10)
            self.browser_status='launch_requested' if opened.returncode==0 else 'launch_failed'
        except (OSError,subprocess.TimeoutExpired):self.browser_status='launch_failed'

    def state(self,status,**extra):
        private_state(self.runtime/'supervisor.json',dict(supervisor_pid=os.getpid(),server_pid=self.child.pid if self.child and self.child.poll() is None else None,
            status=status,restarts=self.restarts,last_reason=self.last_reason,browser=self.browser_status,updated_at=time.time(),**extra))

    def stop_child(self):
        child=self.child
        if child and child.poll() is None:
            # A stopped process cannot handle SIGTERM until it is continued.
            try:child.send_signal(signal.SIGCONT)
            except ProcessLookupError:pass
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        self.child=None

    def run(self):
        self.runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
        with (self.runtime/'supervisor.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            def stop(signum,frame):self.stopping.set()
            old={s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT)}
            try:
                while not self.stopping.is_set():
                    settings=Settings.load(self.runtime)
                    if not network_ready(settings.host):
                        self.state('WAITING_FOR_NETWORK',host=settings.host);self.stopping.wait(self.interval);continue
                    token=load_clients(self.runtime)[settings.default_owner]
                    self.ensure_browser(settings)
                    context=ssl.create_default_context(cafile=str(self.runtime/'tls-cert.pem')) if settings.tls_enabled else True
                    fd=os.open(self.runtime/'service.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
                    with os.fdopen(fd,'a') as log:
                        self.child=subprocess.Popen([sys.executable,'-m','ego_bridge.cli','--runtime',str(self.runtime),'serve'],
                            stdin=subprocess.DEVNULL,stdout=log,stderr=log,cwd=self.runtime)
                    self.state('STARTING');bad=0;started=time.monotonic()
                    with httpx.Client(verify=context,headers={'Authorization':'Bearer '+token},timeout=self.timeout,trust_env=False) as http:
                        while not self.stopping.wait(self.interval):
                            if self.child.poll() is not None:
                                self.last_reason='SERVER_EXITED';break
                            self.ensure_browser(settings)
                            try:
                                r=http.post(settings.base_url+'/api/diagnose',json={});r.raise_for_status();data=r.json()
                                healthy=data.get('ok') and data.get('worker_running')
                            except (httpx.HTTPError,ValueError):healthy=False
                            bad=0 if healthy else bad+1
                            self.state('HEALTHY' if healthy else 'PROBE_FAILED',consecutive_failures=bad)
                            if bad>=self.threshold:self.last_reason='HTTP_UNRESPONSIVE';break
                    self.stop_child()
                    if self.stopping.is_set():break
                    self.restarts+=1;self.state('RESTARTING')
                    # Avoid a tight crash loop during a bad deployment or occupied port.
                    self.stopping.wait(min(30,2**min(self.restarts,5)) if time.monotonic()-started<60 else 1)
            finally:
                self.stop_child();self.state('STOPPED')
                for s,handler in old.items():signal.signal(s,handler)


def run_supervisor(runtime,**kwargs):Supervisor(runtime,**kwargs).run()
