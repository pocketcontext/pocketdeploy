"""state-lock: vault with a synthetic VaultContext CLI (tests/fake_vaultcontext.py)."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pocketdeploy import state_lock, vault
from pocketdeploy.cli import _execute, parser
from pocketdeploy.common import DeployError
from pocketdeploy.config import normalize, scope
from pocketdeploy.state import State, deployment_lock

FAKE = Path(__file__).with_name('fake_vaultcontext.py')


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / 'deploy'
    root.mkdir()
    command = tmp_path / 'vaultcontext'
    command.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    command.chmod(0o755)
    monkeypatch.setenv('FAKE_VAULT_DB', str(tmp_path / 'vault.json'))
    monkeypatch.delenv(state_lock.FENCE_ENV, raising=False)
    monkeypatch.delenv(state_lock.LOCK_FD_ENV, raising=False)
    raw = {'profile': 'synthetic', 'provider-compute': 'oci', 'oci-config-file-profile': 'test',
           'oci-compartment-id': 'test', 'oci-subnet-id': 'test', 'oci-availability-domain': 'AD-1',
           'vault-id': 'syntheticvault1', 'vault-command': str(command), 'state-lock': 'vault',
           'state-lock-label': 'alice@laptop'}
    (root / 'colors.yml').write_text(json.dumps(raw))
    config = normalize(raw, path=root / 'colors.yml', env={})
    for name in vault._files(config):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('synthetic private ' + name)
    state_path = root / '.colors.sqlite'
    State(state_path, config['profile'], scope(config), create=True).__exit__()
    return config, state_path, tmp_path / 'vault.json'


def db(path):
    return json.loads(path.read_text())


def run_locked(config, state_path, body=None, **kw):
    with deployment_lock(state_path), state_lock.guard(config, state_path, command='converge', **kw) as lease:
        with State(state_path, config['profile'], scope(config)) as state:
            lease.bind(state)
            if body:
                body(lease, state)
    return lease


def test_local_default_takes_no_vault_lock(deployment):
    config, state_path, store = deployment
    local = {**config, 'state-lock': 'local'}
    with state_lock.guard(local, state_path, command='converge') as lease:
        assert lease is None
    assert not store.exists()
    assert normalize({k: v for k, v in config.items() if not k.startswith('_') and not k.startswith('state-lock')},
                     path=Path(config['_file']), env={}).get('state-lock') is None


def test_config_validation(deployment):
    config, _, _ = deployment
    raw = {k: v for k, v in config.items() if not k.startswith('_')}
    for bad in ({'state-lock': 'remote'}, {'state-lock-ttl': 10}, {'state-lock-ttl': '900'},
                {'state-lock-label': 'x' * 101}, {'state-lock-label': 'tab\there'}):
        with pytest.raises(DeployError):
            normalize({**raw, **bad}, path=Path(config['_file']), env={})
    with pytest.raises(DeployError, match='requires vault-id'):
        normalize({**{k: v for k, v in raw.items() if k != 'vault-id'}}, path=Path(config['_file']), env={})


def test_happy_path_checkpoints_files_then_sqlite_and_releases_clean(deployment):
    config, state_path, store = deployment

    def body(lease, state):
        state.set_meta('step', 'one')
        lease.checkpoint(state)

    lease = run_locked(config, state_path, body)
    data = db(store)
    assert data['lock']['state'] == 'released' and data['lock']['clean'] is True
    assert data['lock']['info'] == {'command': 'converge', 'profile': 'synthetic', 'label': 'alice@laptop'}
    saved = [entry[1] for entry in data['log'] if entry[0] == 'saved']
    # Two complete sets (checkpoint and final save), each ending with the SQLite snapshot.
    assert len(saved) == 2 * (len(vault._files(config)) + 1)
    assert saved[len(vault._files(config))] == 'synthetic-state' and saved[-1] == 'synthetic-state'
    assert all(entry[-1] == 'fenced' for entry in data['log'] if entry[:1] == ['save'])
    receipt = state_lock.read_receipt(state_path)
    assert receipt['vault'] == 'syntheticvault1' and receipt['deployment_id']
    assert {d['name'] for d in receipt['documents']} == {*vault._files(config), 'state'}
    assert oct(state_lock.receipt_path(state_path).stat().st_mode & 0o777) == '0o600'
    assert not state_lock.lease_path(state_path).exists() and lease.fence
    # Second run: the receipt matches the vault, so acquire compares and succeeds.
    run_locked(config, state_path)
    assert db(store)['lock']['generation'] == 2


def test_concurrent_acquire_refused_with_label_and_touches_nothing(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    before = state_lock.read_receipt(state_path)
    with deployment_lock(state_path), state_lock.guard(config, state_path, command='converge'):
        other = {**config, 'state-lock-label': 'bob@desk'}
        with pytest.raises(state_lock.LockRefused) as refused:
            state_lock.VaultLease(other, state_path, command='converge').acquire()
        assert refused.value.code == 'vault_lock_held' and 'alice@laptop' in str(refused.value)
        assert state_lock.read_receipt(state_path)['documents'] == before['documents']


def test_mismatch_fails_without_lock_and_restore_opt_in_restores_whole_set(deployment, tmp_path):
    config, state_path, store = deployment
    run_locked(config, state_path, lambda lease, state: state.set_meta('marker', 'synced'))
    receipt = state_lock.read_receipt(state_path)
    # Another machine saved a newer set under its own lock.
    envrc = Path(config['_root']) / '.envrc.private'
    envrc.write_text('newer secret from another machine')
    with State(state_path, config['profile'], scope(config)) as state:
        state.set_meta('marker', 'remote-newer')
        run = {**config, 'state-lock-label': 'bob@desk'}
        lease = state_lock.VaultLease(run, state_path, command='converge')
        lease.acquire()
        lease.bind(state)
        lease.checkpoint(state)
        lease.release(clean=True)
    # Roll the local copy back to the old receipt and old content.
    state_lock._write_private_json(state_lock.receipt_path(state_path), receipt)
    envrc.write_text('synthetic private .envrc.private')
    with State(state_path, config['profile'], scope(config)) as state:
        state.set_meta('marker', 'synced')
    generation = db(store)['lock']['generation']
    with pytest.raises(state_lock.LockRefused) as refused:
        run_locked(config, state_path)
    assert refused.value.code == 'vault_local_mismatch' and 'stale' in str(refused.value)
    assert db(store)['lock']['generation'] == generation  # lock not taken
    run_locked(config, state_path, restore=True)
    assert envrc.read_text() == 'newer secret from another machine'
    assert Path(str(envrc) + '.pre-restore').read_text() == 'synthetic private .envrc.private'
    with State(state_path, config['profile'], scope(config), read_only=True) as state:
        assert state.get_meta('marker') == 'remote-newer'
    assert state_lock.local_drift(config, state_lock.read_receipt(state_path)) == []


def test_restore_refuses_dirty_local_fork(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    receipt = state_lock.read_receipt(state_path)
    other = state_lock.VaultLease(config, state_path, command='converge')
    with State(state_path, config['profile'], scope(config)) as state:
        other.acquire()
        other.checkpoint(state)
        other.release(clean=True)
    state_lock._write_private_json(state_lock.receipt_path(state_path), receipt)
    (Path(config['_root']) / '.envrc.private').write_text('unsaved local edit')
    with pytest.raises(DeployError) as error:
        run_locked(config, state_path, restore=True)
    assert error.value.code == 'vault_diverged'
    assert db(store)['lock']['state'] == 'released'


def test_heartbeat_renews_and_lost_lease_stops_without_unfenced_save(deployment):
    config, state_path, store = deployment
    quick = {**config, 'state-lock-ttl': 3}

    def body(lease, state):
        time.sleep(2.5)
        assert db(store)['lock']['renewals'] >= 1
        assert db(store)['lock']['progress'] == 'starting'
    run_locked(quick, state_path, body)

    def broken(lease, state):
        lock = db(store)['lock']
        subprocess.run([config['vault-command'], 'lock', 'break', 'syntheticvault1', '--observed-generation',
                        str(lock['generation']), '--reason', 'test', '--yes'], check=True, capture_output=True)
        time.sleep(1.5)  # heartbeat sees lock_lost
        with pytest.raises(DeployError) as error:
            lease.check()
        assert error.value.code == 'vault_lease_lost'
        raise RuntimeError('stopped')
    saves = len([e for e in db(store)['log'] if e[0] == 'saved'])
    with pytest.raises(RuntimeError):
        run_locked(quick, state_path, broken)
    assert len([e for e in db(store)['log'] if e[0] == 'saved']) == saves


def test_failure_saves_journal_and_releases_dirty(deployment):
    config, state_path, store = deployment

    def body(lease, state):
        state.set_meta('pending', 'step')
        raise DeployError('provider failed')
    with pytest.raises(DeployError, match='provider failed'):
        run_locked(config, state_path, body)
    assert db(store)['lock']['state'] == 'released' and db(store)['lock']['clean'] is False
    assert [e for e in db(store)['log'] if e[0] == 'saved'][-1][1] == 'synthetic-state'


def test_preflight_rejects_extras_and_sibling_configuration(deployment):
    config, state_path, store = deployment
    extra = Path(config['_root']) / 'other.bin'
    extra.write_text('another deployment')
    subprocess.run([config['vault-command'], 'save', 'syntheticvault1', str(extra), '--name', 'other'],
                   check=True, capture_output=True)
    with pytest.raises(DeployError) as error:
        run_locked(config, state_path)
    assert error.value.code == 'vault_not_dedicated'
    assert db(store)['lock']['state'] == 'released'
    sibling = Path(config['_root']).parent / 'second'
    sibling.mkdir()
    (sibling / 'colors.yml').write_text(json.dumps({'profile': 'second', 'vault-id': 'syntheticvault1'}))
    with pytest.raises(DeployError) as error:
        state_lock.preflight_dedicated(config)
    assert error.value.code == 'vault_not_dedicated'


def test_exec_holds_one_lease_and_nested_commands_reuse_fence(deployment, monkeypatch):
    config, state_path, store = deployment
    script = Path(config['_root']) / 'nested.py'
    script.write_text(textwrap.dedent(f'''
        import json, os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})
        from pocketdeploy import state_lock
        from pocketdeploy.config import normalize, scope
        from pocketdeploy.state import State, deployment_lock
        config = normalize(json.loads(open('colors.yml').read()), path=os.path.abspath('colors.yml'), env={{}})
        path = os.path.abspath('.colors.sqlite')
        with deployment_lock(path), state_lock.guard(config, path, command='converge') as lease:
            assert lease.nested
            with State(path, config['profile'], scope(config)) as state:
                state.set_meta('nested', 'yes')
                lease.checkpoint(state)
        print(os.environ['{state_lock.FENCE_ENV}'])
    '''))
    with deployment_lock(state_path), state_lock.guard(config, state_path, command='exec') as lease:
        with state_lock.child_lock_fd(state_path) as fd:
            assert state_lock.run_child(config, state_path, lease, [sys.executable, str(script)], fd) == 0
            generation = db(store)['lock']['generation']
    data = db(store)
    acquires = [e for e in data['log'] if e[:2] == ['lock', 'acquire']]
    assert len(acquires) == 1 and data['lock']['generation'] == generation
    assert data['lock']['state'] == 'released' and data['lock']['clean'] is True
    with State(state_path, config['profile'], scope(config), read_only=True) as state:
        assert state.get_meta('nested') == 'yes'
    monkeypatch.setenv(state_lock.FENCE_ENV, json.dumps({'vault': 'othervault00000', 'fence': '1:abcdefghijklmno',
                                                         'state': str(state_path)}))
    with pytest.raises(DeployError) as error:
        state_lock.VaultLease.from_env(config, state_path)
    assert error.value.code == 'vault_fence_mismatch'


def test_status_warns_without_locking(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    with deployment_lock(state_path), state_lock.guard(config, state_path, command='converge'):
        before = db(store)['lock']['generation']
        result = state_lock.status(config, state_path)
        assert result['vault_lock']['label'] == 'alice@laptop'
        assert any('locked by alice@laptop' in w for w in result['warnings'])
        assert db(store)['lock']['generation'] == before
    (Path(config['_root']) / '.envrc.private').write_text('drifted')
    assert any('.envrc.private' in w for w in state_lock.status(config, state_path)['warnings'])


def test_lock_break_and_force_require_confirmation(deployment, monkeypatch):
    config, state_path, store = deployment
    lease = state_lock.VaultLease(config, state_path, command='converge')
    lease.acquire()
    generation = db(store)['lock']['generation']
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    with pytest.raises(DeployError, match='--yes'):
        state_lock.lock_break(config, generation, 'crashed laptop', False)
    with pytest.raises(DeployError, match='observed-generation'):
        state_lock.lock_break(config, generation, '', True)
    assert state_lock.lock_break(config, generation, 'crashed laptop', True)['vault_lock']['broken'] is True
    lease._stop.set()
    # Force: break + acquire in one step via the CLI flag plumbing.
    first = state_lock.VaultLease(config, state_path, command='converge')
    first.acquire()
    current = db(store)['lock']['generation']
    forced = state_lock.VaultLease(config, state_path, command='converge')
    forced.acquire(force={'observed_generation': current, 'reason': 'coordinated'})
    assert db(store)['lock']['generation'] == current + 2
    first._stop.set()
    forced.release(clean=True)


def test_cli_argument_rules(deployment, monkeypatch):
    config, state_path, _ = deployment
    monkeypatch.chdir(config['_root'])

    def args(*words, child=None):
        parsed = parser().parse_args(list(words))
        parsed.child = child
        return parsed
    for words, child in ((['exec'], None), (['plan'], ['true']), (['plan', '--restore'], None),
                         (['plan', '--label', 'x'], None), (['converge', '--yes'], None),
                         (['converge', '--dry-run', '--restore'], None)):
        with pytest.raises(DeployError) as error:
            _execute(args(*words, child=child))
        assert error.value.code == 'invalid_usage'
    result = _execute(args('lock-status'))
    assert result['vault_lock']['state'] == 'free'
    assert _execute(args('exec', child=['true'])) == {'exec_exit': 0}
    from pocketdeploy.cli import ChildFailed
    with pytest.raises(ChildFailed) as failed:
        _execute(args('exec', child=['false']))
    assert failed.value.exit_code == 1
    assert state_lock.read_receipt(state_path) is not None
