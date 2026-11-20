"""Lease boundary tests, using synthetic metadata and no provider requests."""
import asyncio
import fcntl
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from blue.workflow import advice_add_all, run, workflow
import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.state import CHILD_FD_ENV, LOCK_FD_ENV, deployment_lock
from pocketdeploy import state_lock


def test_blue_checks_and_checkpoints_real_async_chain():
    events = []
    state = SimpleNamespace(db=SimpleNamespace(total_changes=0))
    lease = SimpleNamespace(check=lambda: events.append('check'),
                            checkpoint=lambda _: events.append('save'))
    def wire(name, _):
        def step(opts):
            events.append(name)
            if name == 'create':
                state.db.total_changes += 1
            return dict(opts)
        return [step, 'observe'] if name == 'create' else [step]

    flow = workflow(start='create', wire_fn=wire)
    flow = advice_add_all(flow, 'around', 'lease', state_lock.advice(lease, state))
    result = asyncio.run(run(flow, {}))
    assert result.get('blue/exit') == 0
    assert events == ['check', 'create', 'save', 'check', 'observe']


def test_heartbeat_and_nested_writes_preserve_each_others_fields(tmp_path, monkeypatch):
    config = {'_root': str(tmp_path), 'vault-id': 'synthetic'}
    path = tmp_path / 'state'
    owner = state_lock.VaultLease(config, path)
    owner.fence = '1:abcdefghijklmn0'
    owner.deadline = time.monotonic() + 100
    owner.revisions = {'doc': 1}
    owner._start_heartbeat()
    child = state_lock.VaultLease(config, path)
    child.fence = owner.fence
    child._load_shared()
    monkeypatch.setattr(state_lock, '_call', lambda *a, **kw: {
        'expires_at': '2026-11-20T12:15:00Z', 'server_time': '2026-11-20T12:00:00Z'})
    try:
        child.revisions['doc'] = 2
        child._save_shared()
        owner._renew()
        assert owner._shared()['revisions']['doc'] == 2
        new_deadline = owner._shared()['deadline']
        owner._mark_lost()
        child._save_shared()
        assert child._shared()['deadline'] == new_deadline
        assert child._shared()['lost'] is True
        with pytest.raises(DeployError, match='lease could not be proven'):
            owner.finish(True)
    finally:
        owner.release(False)


def test_nested_different_open_description_cannot_reuse_flock(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    with deployment_lock(state), open(str(state) + '.lock', 'rb') as other, tempfile.TemporaryFile(dir=tmp_path) as siblings:
        monkeypatch.setenv(LOCK_FD_ENV, str(other.fileno()))
        monkeypatch.setenv(CHILD_FD_ENV, str(siblings.fileno()))
        with pytest.raises(DeployError, match='Inherited deployment lock'):
            with deployment_lock(state):
                pytest.fail('a different open-file description was accepted')


def test_parallel_nested_calls_compete_on_sibling_descriptor(tmp_path):
    import subprocess
    import sys
    state_path = tmp_path / 'state'
    with deployment_lock(state_path) as fd, tempfile.TemporaryFile(dir=tmp_path) as siblings:
        fcntl.lockf(siblings, fcntl.LOCK_EX)
        env = {**os.environ, LOCK_FD_ENV: str(fd), CHILD_FD_ENV: str(siblings.fileno())}
        result = subprocess.run([sys.executable, '-c', """
import sys
from pocketdeploy.common import DeployError
from pocketdeploy.state import deployment_lock
try:
    with deployment_lock(sys.argv[1]):
        raise SystemExit('second nested operation entered')
except DeployError:
    pass
""", str(state_path)], env=env, pass_fds=(fd, siblings.fileno()), capture_output=True)
        assert result.returncode == 0
