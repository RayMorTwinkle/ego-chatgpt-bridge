import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ego_bridge.adapter import EgoAdapter
from ego_bridge.autostart import launch_config
from ego_bridge.client import download_artifact
from ego_bridge.model import BridgeError,Settings
from ego_bridge.supervisor import network_ready


def test_cli_download_verifies_bytes_refuses_overwrite_and_never_follows_returned_url(tmp_path):
    body=b'hello\n';artifact='art_'+'a'*32;seen=[];corrupt=False
    def respond(request):
        seen.append(str(request.url))
        if request.method=='POST':return httpx.Response(200,json={'ok':True,'artifact':{'status':'AVAILABLE','filename':'real.txt','size_bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'mime_type':'text/plain','download_url':'https://evil.invalid/private'}})
        return httpx.Response(200,content=b'wrong!' if corrupt else body)
    with httpx.Client(transport=httpx.MockTransport(respond),headers={'Authorization':'Bearer private'}) as c:
        dest=tmp_path/'file.txt';result=download_artifact(c,'https://bridge.local',artifact,dest)
        assert result['sha256']==hashlib.sha256(body).hexdigest() and dest.read_bytes()==body
        assert all(u.startswith('https://bridge.local/') for u in seen)
        assert dest.stat().st_mode&0o777==0o600
        with pytest.raises(BridgeError,match='目标已存在'):download_artifact(c,'https://bridge.local',artifact,dest)
        corrupt=True
        with pytest.raises(BridgeError,match='SHA-256'):download_artifact(c,'https://bridge.local',artifact,tmp_path/'bad.txt')
        assert not (tmp_path/'bad.txt').exists() and not list(tmp_path.glob('.ego-download-*'))


def test_launch_config_uses_runtime_without_tokens_and_keeps_job_alive(tmp_path):
    config=launch_config(tmp_path)
    assert config['KeepAlive'] and config['RunAtLoad'] and config['ThrottleInterval']>=10
    assert config['ProgramArguments'][0]==str(tmp_path/'.venv/bin/python')
    assert config['ProgramArguments'][-1]=='supervise'
    assert not any('TOKEN' in k for k in config['EnvironmentVariables'])
    assert network_ready('127.0.0.1') and not network_ready('192.0.2.199')


@pytest.mark.parametrize('bootstrap_error,transient', [
    ('Bootstrap failed: 5: Input/output error', True),
    ('Bootstrap failed: 37: Operation already in progress', False),
])
def test_autostart_install_retries_pending_launchd_teardown_only(tmp_path, monkeypatch, bootstrap_error, transient):
    import ego_bridge.autostart as autostart

    runtime = tmp_path/'runtime'
    python = runtime/'.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(autostart.sys, 'platform', 'darwin')
    clock = [0.0]
    monkeypatch.setattr(autostart, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds)), raising=False)
    attempts = []

    def run(command, **kwargs):
        if command[1] == 'bootstrap':
            attempts.append(command)
            if len(attempts) == 1:
                return SimpleNamespace(returncode=5, stderr=bootstrap_error)
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(autostart.subprocess, 'run', run)
    if transient:
        autostart.manage_autostart(runtime, 'install')
        assert len(attempts) == 2 and clock[0] > 0
    else:
        with pytest.raises(BridgeError, match='Operation already in progress'):
            autostart.manage_autostart(runtime, 'install')
        assert len(attempts) == 1


def test_autostart_install_bootstrap_retry_is_bounded(tmp_path, monkeypatch):
    import ego_bridge.autostart as autostart

    runtime = tmp_path/'runtime'
    python = runtime/'.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(autostart.sys, 'platform', 'darwin')
    clock = [0.0]
    monkeypatch.setattr(autostart, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds)), raising=False)

    def run(command, **kwargs):
        return SimpleNamespace(returncode=5 if command[1] == 'bootstrap' else 0,
                               stderr='Bootstrap failed: 5: Input/output error')

    monkeypatch.setattr(autostart.subprocess, 'run', run)
    with pytest.raises(BridgeError, match='Input/output error'):
        autostart.manage_autostart(runtime, 'install')
    assert 0 < clock[0] <= 31


def test_same_named_space_is_exclusive_across_runtime_dirs_and_rebinding_is_audited(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    a=tmp_path/'first';b=tmp_path/'second';a.mkdir();b.mkdir()
    settings=Settings(a,space_id=3);adapter=EgoAdapter(settings)
    try:
        with pytest.raises(BridgeError,match='另一个桥接进程'):EgoAdapter(Settings(b,space_id=99))
        adapter.accept_space({'runtime_space':{'previous_id':3,'space_id':8,'name':settings.space_name,'recreated':True,'history_restored':False}})
        assert settings.space_id==8 and json.loads((a/'config.json').read_text())['space_id']==8
        event=json.loads((a/'runtime-events.jsonl').read_text());assert event['recreated'] and not event['history_restored']
    finally:adapter.close()
