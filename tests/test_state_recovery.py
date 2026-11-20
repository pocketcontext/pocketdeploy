"""Whole-set recovery tests with a synthetic vault and isolated ledgers."""
import json
import os
from pathlib import Path

import pytest

from pocketdeploy import state_lock, vault
from pocketdeploy.common import DeployError
from pocketdeploy.config import normalize, scope
from pocketdeploy.state import State
from test_state_lock import db, deployment, run_locked


def local_copy(state_path):
    return state_path.read_bytes(), state_lock.receipt_path(state_path).read_bytes()


def replace_copy(state_path, saved):
    state_path.write_bytes(saved[0])
    state_lock.receipt_path(state_path).write_bytes(saved[1])


def test_rejected_identity_never_writes_remote(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    before = db(store)
    state_path.unlink()
    with State(state_path, config['profile'], scope(config), create=True):
        pass
    with pytest.raises(DeployError) as rejected:
        run_locked(config, state_path, restore=True)
    assert rejected.value.code == 'vault_not_dedicated'
    assert db(store)['documents'] == before['documents']
    assert db(store)['lock'] == before['lock']


def test_adoption_rejects_stale_clone_before_acquiring(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    old = state_path.read_bytes()
    run_locked(config, state_path)
    previous = db(store)
    state_path.write_bytes(old)
    state_lock.receipt_path(state_path).unlink()
    with pytest.raises(DeployError) as error:
        run_locked(config, state_path)
    assert error.value.code == 'vault_local_mismatch'
    assert db(store)['lock'] == previous['lock']
    assert db(store)['documents'] == previous['documents']


def test_first_unlocked_checkpoint_can_be_recovered_and_adopted(deployment):
    config, state_path, _ = deployment
    with State(state_path, config['profile'], scope(config)) as state:
        checkpoint = vault.save({**config, 'state-lock': 'local'}, state, state_path.parent)
    state_path.unlink()
    vault.restore(config, state_path.parent, checkpoint['state_document'], checkpoint['state_version'], overwrite=True)
    run_locked(config, state_path)
    assert state_lock.read_receipt(state_path)['deployment_id']


def test_receipt_free_clone_cannot_substitute_a_different_identity(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    state_lock.receipt_path(state_path).unlink()
    before = db(store)
    with State(state_path, config['profile'], scope(config)) as state:
        state.set_meta('identity', {**state.get_meta('identity'), 'deployment_id': 'other-deployment'})
    with pytest.raises(DeployError) as rejected:
        run_locked(config, state_path)
    assert rejected.value.code == 'vault_not_dedicated'
    assert db(store)['lock'] == before['lock']
    assert db(store)['documents'] == before['documents']


@pytest.mark.parametrize('acknowledged', [False, True])
def test_no_complete_initial_snapshot_is_never_adopted(deployment, acknowledged):
    config, state_path, store = deployment
    lease = state_lock.VaultLease(config, state_path, command='vault-save')
    lease.acquire()
    try:
        name = '.envrc.private'
        saved = vault._save_file(config, state_path.parent / name, 'synthetic', fence=lease.fence, expect_revision=0)
        if acknowledged:
            with State(state_path, config['profile'], scope(config)) as state:
                state.set_meta('vault-files', {name: {'document': saved['document'], 'version': saved['version']}})
    finally:
        lease.release(clean=False)
    before = db(store)
    with pytest.raises(DeployError) as error:
        run_locked(config, state_path, restore=True)
    assert error.value.code == ('vault_missing_remote' if acknowledged else 'vault_not_dedicated')
    assert db(store)['documents'] == before['documents'] and db(store)['lock'] == before['lock']


def test_dirty_ledger_blocks_stale_restore(deployment):
    config, state_path, _ = deployment
    run_locked(config, state_path)
    old = local_copy(state_path)
    run_locked(config, state_path, lambda lease, state: state.set_meta('remote', 'newer'))
    replace_copy(state_path, old)
    with State(state_path, config['profile'], scope(config)) as state:
        state.set_meta('local-intent', 'pending')
    with pytest.raises(DeployError) as error:
        run_locked(config, state_path, restore=True)
    assert error.value.code == 'vault_diverged'
    with State(state_path, config['profile'], scope(config), read_only=True) as state:
        assert state.get_meta('local-intent') == 'pending'


def test_partial_remote_checkpoint_recovers_last_committed_pins(deployment):
    config, state_path, store = deployment
    run_locked(config, state_path)
    previous = state_lock.read_receipt(state_path)
    file_doc = next(d for d in previous['documents'] if d['name'] == '.envrc.private')
    old_bytes = (state_path.parent / '.envrc.private').read_bytes()
    partial = state_path.parent / 'unpublished'
    partial.write_bytes(b'partial uncommitted file bytes')
    lease = state_lock.VaultLease(config, state_path, command='exec')
    lease.acquire()
    try:
        vault._save_file(config, partial, 'synthetic', file_doc['id'], lease.fence, file_doc['revision'])
    finally:
        lease.release(clean=False)
    run_locked(config, state_path, restore=True)
    current = db(store)
    assert (state_path.parent / '.envrc.private').read_bytes() == old_bytes
    assert bytes.fromhex(current['versions'][current['documents'][file_doc['id']]['version']]) == old_bytes
    assert current['lock']['clean'] is True
    assert state_lock.local_drift(config, state_lock.read_receipt(state_path)) == []


def test_interrupted_publication_preserves_originals_and_resumes(deployment, monkeypatch):
    config, state_path, _ = deployment
    run_locked(config, state_path)
    old = local_copy(state_path)
    keys = [state_path.parent / name for name in vault._files(config) if name != '.envrc.private']
    original = [path.read_bytes() for path in keys]
    for path in keys:
        path.write_bytes(b'new synthetic recovery bytes')
    run_locked(config, state_path, lambda lease, state: state.set_meta('remote', 'complete'))
    for path, data in zip(keys, original):
        path.write_bytes(data)
    replace_copy(state_path, old)
    real_replace = os.replace
    def interrupt(source, target):
        if Path(target) == keys[1]:
            raise OSError('synthetic interruption')
        return real_replace(source, target)
    with monkeypatch.context() as changes:
        changes.setattr(vault.os, 'replace', interrupt)
        with pytest.raises(OSError):
            run_locked(config, state_path, restore=True)
    assert state_path.exists() and keys[0].read_bytes() != original[0]
    assert Path(str(keys[0]) + '.pre-restore').read_bytes() == original[0]
    with pytest.raises(DeployError) as pending:
        run_locked(config, state_path)
    assert pending.value.code == 'vault_restore_pending'
    run_locked(config, state_path, restore=True)
    assert all(path.read_bytes() == b'new synthetic recovery bytes' for path in keys)
    assert all(Path(str(path) + '.pre-restore').read_bytes() == data for path, data in zip(keys, original))
    assert not Path(str(state_path) + '.vault-restore').exists()


def test_lock_metadata_does_not_change_hash_and_reserves_private_paths(deployment):
    config, state_path, _ = deployment
    filename = Path(config['_file'])
    raw = json.loads(filename.read_text())
    unlocked = {k: v for k, v in raw.items() if not k.startswith('state-lock')}
    for setting in ({'state-lock': 'local'}, {'state-lock': 'vault', 'state-lock-label': 'label', 'state-lock-ttl': 60}):
        assert normalize({**unlocked, **setting}, path=filename, env={})['_desired_hash'] == normalize(
            unlocked, path=filename, env={})['_desired_hash']
    for name in (state_path.name + '.vault-sync', state_path.name + '.vault-lease',
                 state_path.name + '.vault-restore', '.envrc.private.pre-restore'):
        with pytest.raises(DeployError):
            normalize({**raw, 'ssh-private-key-file': name}, path=filename, env={})


def test_kms_bootstrap_only_omits_uncreated_files(deployment):
    from pocketdeploy.kms import FILES
    config, state_path, _ = deployment
    config = {**config, 'oci-kms': {'name': 'synthetic'}}
    run_locked(config, state_path)
    first = state_lock.read_receipt(state_path)
    assert all(d['name'] not in FILES for d in first['documents'])
    key = state_path.parent / FILES[0]
    key.write_bytes(b'synthetic material')
    run_locked(config, state_path)
    receipt = state_lock.read_receipt(state_path)
    assert any(d['name'] == FILES[0] for d in receipt['documents'])
    key.unlink()
    with pytest.raises(DeployError, match='missing'):
        run_locked(config, state_path)
