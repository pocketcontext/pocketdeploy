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


def test_delete_receipt_and_resource_commit_atomically(tmp_path):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        state.put_resource('compute', 'synthetic', 'vm', {})
        operation = state.begin_operation('delete', 'hash')
        step = state.intent(operation, 'delete-compute', {'id': 'vm'})
        state.set_meta('pending-delete', {'step': step, 'id': 'vm'})
        # Failure after completing the step must roll back the entire receipt.
        state.db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON resources BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            state.complete_delete('compute', step, 'pending-delete')
        assert state.get_resource('compute')['provider_id'] == 'vm'
        assert state.get_meta('pending-delete')['step'] == step
        assert state.safe_status()['pending_steps'] == 1
        state.db.execute('DROP TRIGGER fail_delete')
        state.complete_delete('compute', step, 'pending-delete')
        assert state.get_resource('compute') is None
        assert state.get_meta('pending-delete') is None
        assert state.safe_status()['pending_steps'] == 0


def test_delete_receipt_cannot_remove_different_record(tmp_path):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        state.put_resource('compute', 'synthetic', 'replacement', {})
        operation = state.begin_operation('delete', 'hash')
        step = state.intent(operation, 'delete-compute', {'id': 'old'})
        state.set_meta('pending-delete', {'step': step, 'id': 'old'})
        with pytest.raises(DeployError, match='ownership'):
            state.complete_delete('compute', step, 'pending-delete')
        assert state.get_resource('compute')['provider_id'] == 'replacement'
        assert state.safe_status()['pending_steps'] == 1


def test_legacy_desired_config_cleanup_preserves_identity_and_readonly_state(tmp_path):
    path = tmp_path / 'state'
    marker = 'synthetic-retired-application-secret-' * 100
    with State(path, 'test', {}, create=True) as state:
        identity = state.deployment_id
        state.set_meta('desired-config', {'once': {'env': [marker]}})
        state.set_meta('retained', {'value': 'keep'})
    original = path.read_bytes()
    assert marker.encode() in original
    with State(path, 'test', {}, read_only=True) as state:
        assert state.get_meta('desired-config') is not None
    assert path.read_bytes() == original
    with pytest.raises(DeployError, match='identity'):
        State(path, 'wrong-profile', {})
    assert path.read_bytes() == original
    with State(path, 'test', {}) as state:
        assert state.deployment_id == identity
        assert state.get_meta('desired-config') is None
        assert state.get_meta('retained') == {'value': 'keep'}
        assert state.db.execute('PRAGMA secure_delete').fetchone()[0] == 1
        state.snapshot(tmp_path / 'snapshot')
    assert marker.encode() not in path.read_bytes()
    with State(tmp_path / 'snapshot', 'test', {}, read_only=True) as state:
        assert state.get_meta('desired-config') is None


def test_operation_pruning_removes_only_orphan_diagnostics(tmp_path):
    from pocketdeploy.diagnostics import build_diagnostic
    diagnostic = build_diagnostic('application_deploy', 'unknown')
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        state.set_meta('operation-diagnostic:legacy-orphan', diagnostic)
        state.set_meta('unrelated', {'keep': True})
        running = state.begin_operation('converge', 'hash')
        state.record_diagnostic(running, diagnostic)
        pending = state.begin_operation('converge', 'hash')
        step = state.intent(pending, 'create', {})
        state.record_diagnostic(pending, diagnostic)
        state.finish_operation(pending, 'failed')
        pruned = state.begin_operation('converge', 'hash')
        state.record_diagnostic(pruned, diagnostic)
        state.finish_operation(pruned, 'failed')
        for _ in range(100):
            recent = state.begin_operation('converge', 'hash')
            state.record_diagnostic(recent, diagnostic)
            state.finish_operation(recent, 'failed')
        assert state.get_meta('operation-diagnostic:legacy-orphan') is None
        assert state.get_meta('operation-diagnostic:' + pruned) is None
        for operation in (running, pending, recent):
            assert state.get_meta('operation-diagnostic:' + operation) == diagnostic
        assert state.get_meta('unrelated') == {'keep': True}
        state.complete(step, {})
        state.finish_operation(state.begin_operation('converge', 'hash'), 'succeeded')
        assert state.get_meta('operation-diagnostic:' + pending) is None
        assert state.get_meta('operation-diagnostic:' + running) == diagnostic


def test_fail_settles_pending_step_with_safe_classification(tmp_path):
    from pocketdeploy.state import failure_class
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        operation = state.begin_operation('converge', 'hash')
        step = state.intent(operation, 'applications-converge', {})
        assert failure_class(DeployError('private detail', code='application_deploy')) == 'application_deploy'
        assert failure_class(DeployError('private detail', code='Not Safe!')) == 'unexpected_error'
        assert failure_class(ValueError('private detail')) == 'unexpected_error'
        assert failure_class(KeyboardInterrupt()) == 'interrupted'
        with pytest.raises(DeployError, match='classification'):
            state.fail(step, 'raw provider output')
        state.fail(step, 'application_deploy')
        row = state.db.execute('SELECT status,result FROM steps WHERE id=?', (step,)).fetchone()
        assert (row['status'], json.loads(row['result'])) == ('failed', {'error': 'application_deploy'})
        state.complete(state.intent(operation, 'other', {}), {})
        state.fail(step, 'interrupted')  # Settled steps are never rewritten.
        assert json.loads(state.db.execute('SELECT result FROM steps WHERE id=?', (step,)).fetchone()[0]) == {'error': 'application_deploy'}
        status = state.safe_status()
        assert (status['pending_steps'], status['failed_steps']) == (0, 1)
        state.finish_operation(operation, 'failed')
        for _ in range(101):
            state.finish_operation(state.begin_operation('converge', 'hash'), 'succeeded')
        assert state.db.execute('SELECT id FROM operations WHERE id=?', (operation,)).fetchone() is None


def test_resolve_step_only_settles_whitelisted_pending_kinds_with_receipt(tmp_path):
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        failed = state.begin_operation('converge', 'hash')
        stale = state.intent(failed, 'applications-converge', {})
        guarded = state.intent(failed, 'smtp-domain', {'action': 'create'})
        state.finish_operation(failed, 'failed')
        listed = state.pending_steps()
        assert [(s['id'], s['step'], s['workflow'], s['resolvable']) for s in listed] == [
            (stale, 'applications-converge', 'converge', True), (guarded, 'smtp-domain', 'converge', False)]
        assert 'payload' not in listed[0]
        receipt = state.begin_operation('resolve-step', 'hash')
        with pytest.raises(DeployError, match='uncertain mutation'):
            state.resolve_step(guarded, receipt)
        assert state.resolve_step(stale, receipt) == {'step_id': stale, 'step': 'applications-converge', 'status': 'failed'}
        with pytest.raises(DeployError, match='not pending'):
            state.resolve_step(stale, receipt)
        with pytest.raises(DeployError, match='not pending'):
            state.resolve_step('missing', receipt)
        assert json.loads(state.db.execute('SELECT result FROM steps WHERE id=?', (stale,)).fetchone()[0]) == {
            'error': 'operator_resolved', 'resolved_by': receipt}
        row = state.db.execute("SELECT status,payload FROM steps WHERE operation_id=? AND step='step-resolution'", (receipt,)).fetchone()
        assert (row['status'], json.loads(row['payload'])) == ('complete', {'step_id': stale})
        assert [s['id'] for s in state.pending_steps()] == [guarded]
