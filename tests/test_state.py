import json
import os
import sqlite3

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.state import State, deployment_lock


def test_identity_and_snapshot(tmp_path):
    path = tmp_path / 'private' / 'state.sqlite'
    with State(path, 'test', {'region': 'test'}, create=True) as state:
        identity = state.deployment_id
        state.put_resource('compute', 'instance', 'synthetic-id', {'secret': 'private-value'})
        operation = state.begin_operation('converge', 'hash')
        step = state.intent(operation, 'create', {'password': 'private-value'})
        assert state.safe_status()['pending_steps'] == 1
        assert 'private-value' not in json.dumps(state.safe_status())
        assert state.safe_status()['last_vault_backup'] is None
        state.set_meta('vault-state-document', {'document': 'doc', 'version': 'ver',
                                               'secret': 'private-value'})
        assert state.safe_status()['last_vault_backup'] == {'document': 'doc', 'version': 'ver'}
        assert 'private-value' not in json.dumps(state.safe_status())
        state.complete(step, {'secret': 'private-value'})
        state.finish_operation(operation, 'succeeded')
        assert not state.db.in_transaction
        state.snapshot(tmp_path / 'snapshot')
        assert state.db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with State(tmp_path / 'snapshot', 'test', {'region': 'test'}) as state:
        assert state.deployment_id == identity
        assert state.get_resource('compute')['attributes']['secret'] == 'private-value'
    with pytest.raises(DeployError, match='identity'):
        State(path, 'other', {'region': 'test'})
    with pytest.raises(DeployError, match='identity'):
        State(path, 'test', {'region': 'other'})


def test_lock_missing_and_symlink(tmp_path):
    path = tmp_path / 'state.sqlite'
    with pytest.raises(DeployError, match='missing'):
        State(path, 'test', {})
    with deployment_lock(path):
        with pytest.raises(DeployError, match='Another'):
            with deployment_lock(path):
                pass
    (tmp_path / 'link').symlink_to(path)
    with pytest.raises(DeployError, match='symlinks'):
        State(tmp_path / 'link', 'test', {}, create=True)


def test_pruning_keeps_pending_and_foreign_keys(tmp_path):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        pending = state.begin_operation('converge', 'hash')
        state.intent(pending, 'create', {})
        for _ in range(103):
            operation = state.begin_operation('converge', 'hash')
            step = state.intent(operation, 'create', {})
            state.complete(step, {})
            state.finish_operation(operation, 'succeeded')
        assert state.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 101
        assert state.db.execute('SELECT count(*) FROM steps').fetchone()[0] == 101
        assert state.safe_status()['pending_steps'] == 1


@pytest.mark.parametrize('outcome', ['failed', 'interrupted', 'succeeded'])
def test_pruning_preserves_finished_operations_until_pending_steps_resolve(tmp_path, outcome):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        unresolved = state.begin_operation('converge', 'hash')
        first_step = state.intent(unresolved, 'create-instance', {'name': 'synthetic'})
        last_step = state.intent(unresolved, 'create-firewall', {})
        state.finish_operation(unresolved, outcome)
        for _ in range(103):
            operation = state.begin_operation('converge', 'hash')
            state.finish_operation(operation, 'succeeded')
        assert state.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 101
        assert state.safe_status()['pending_steps'] == 2
        assert state.db.execute('SELECT payload FROM steps WHERE id=?', (first_step,)).fetchone()[0] == json.dumps({'name': 'synthetic'})

        state.complete(first_step, {'id': 'synthetic-instance'})
        state.finish_operation(state.begin_operation('converge', 'hash'), 'succeeded')
        assert state.safe_status()['pending_steps'] == 1
        assert state.db.execute('SELECT count(*) FROM steps WHERE operation_id=?', (unresolved,)).fetchone()[0] == 2

        state.complete(last_step, {'id': 'synthetic-firewall'})
        state.finish_operation(state.begin_operation('converge', 'hash'), 'succeeded')
        assert state.safe_status()['pending_steps'] == 0
        assert state.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 100
        assert state.db.execute('SELECT id FROM operations WHERE id=?', (unresolved,)).fetchone() is None
        assert state.db.execute('SELECT count(*) FROM steps WHERE operation_id=?', (unresolved,)).fetchone()[0] == 0


def test_readonly_and_invalid_state_unchanged(tmp_path):
    path = tmp_path / 'state'
    with State(path, 'test', {}, create=True):
        pass
    original = path.read_bytes()
    with State(path, 'test', {}, read_only=True) as state:
        assert state.safe_status()['resource_count'] == 0
        with pytest.raises(DeployError, match='read-only'):
            state.set_meta('secret', 'value')
    assert path.read_bytes() == original
    invalid = tmp_path / 'invalid'
    with sqlite3.connect(invalid) as db:
        db.execute('CREATE TABLE unrelated(value TEXT)')
    original = invalid.read_bytes()
    with pytest.raises(DeployError, match='invalid'):
        State(invalid, 'test', {}, create=True)
    assert invalid.read_bytes() == original
    os.link(path, tmp_path / 'alias')
    with pytest.raises(DeployError, match='hardlinks'):
        State(path, 'test', {})


def test_lock_rejects_hardlinks_and_nonregular_files(tmp_path):
    victim = tmp_path / 'victim'
    victim.write_text('private synthetic text')
    victim.chmod(0o644)
    lock = tmp_path / 'state.lock'
    os.link(victim, lock)
    with pytest.raises(DeployError, match='hardlinks'):
        with deployment_lock(tmp_path / 'state'):
            pass
    assert victim.stat().st_mode & 0o777 == 0o644
    lock.unlink()
    os.mkfifo(lock)
    with pytest.raises(DeployError, match='regular'):
        with deployment_lock(tmp_path / 'state'):
            pass


def test_snapshot_rejects_existing_and_symlink(tmp_path):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        destination = tmp_path / 'snapshot'
        destination.write_text('retain this')
        with pytest.raises(DeployError, match='exists'):
            state.snapshot(destination)
        assert destination.read_text() == 'retain this'
        symlink = tmp_path / 'link'
        symlink.symlink_to(destination)
        with pytest.raises(DeployError, match='symlinks'):
            state.snapshot(symlink)
