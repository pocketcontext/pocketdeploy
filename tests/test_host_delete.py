import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketdeploy import remote


@pytest.fixture
def retired_host(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'BASE', tmp_path)
    monkeypatch.setattr(remote, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(remote.shutil, 'which', lambda _: '/usr/bin/docker')
    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    monkeypatch.setattr(remote.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_dir=str(home), pw_uid=0, pw_gid=0))
    monkeypatch.setattr(remote.os, 'chown', lambda *args: None)
    token = 'a' * 20
    directory = tmp_path / 'github'
    directory.mkdir()
    remote.save(directory / (token + '.json'), {'deployment_id': 'test'})
    authorized = home / '.ssh/authorized_keys'
    authorized.write_text('ssh-ed25519 operator operator\nssh-ed25519 synthetic pocketdeploy-github-' + token + '\n')
    remote.save(remote.MANIFEST, {'deployment_id': 'test', 'apps': {
        'removed.example.test': {'desired': {'timeout': 123}, 'image_id': 'sha256:synthetic'}}})
    actual = {'id': 'container', 'image_id': 'sha256:synthetic', 'binds': False,
              'settings': {'autoUpdate': False}, 'running': True, 'restart': 'always', 'exit_code': 0, 'oom': False}
    monkeypatch.setattr(remote, 'containers', lambda: {'removed.example.test': actual.copy()})
    return tmp_path, authorized, actual


def request():
    return {'deployment_id': 'test', 'applications': [], 'user': 'ubuntu'}


def test_retirement_plan_is_read_only_and_includes_removed_desired_app(retired_host):
    root, authorized, actual = retired_host
    with patch.object(remote, 'run') as run:
        plan = remote.retire(request())
    assert plan['actions'] == [{'host': 'removed.example.test', 'action': 'stop-retain-data', 'timeout': 123}]
    assert plan['github_keys_to_revoke'] == 1
    assert not (root / 'retirement.json').exists()
    assert 'synthetic' in authorized.read_text()
    run.assert_not_called()


def test_retirement_fences_legacy_dispatcher_revokes_keys_and_stops_in_order(retired_host):
    root, authorized, actual = retired_host
    def run(*args, **kwargs):
        assert not (root / 'github').exists()  # Old queued dispatcher reads this after flock.
        assert 'synthetic' not in authorized.read_text()
        if args[:2] == ('docker', 'update'):
            actual['restart'] = 'no'
        elif args[:2] == ('docker', 'stop'):
            assert args[2:4] == ('--time', '123')
            assert kwargs['timeout'] == 153
            actual['running'] = False
    with patch.object(remote, 'run', side_effect=run) as commands:
        result = remote.retire(request(), mutate=True)
    assert result['quiesced']
    assert len(commands.call_args_list) == 2
    assert authorized.read_text() == 'ssh-ed25519 operator operator\n'
    assert json.loads((root / 'retirement.json').read_text())['phase'] == 'quiesced'
    assert remote.MANIFEST.exists()
    with patch.object(remote, 'run', side_effect=run) as commands:
        assert remote.retire(request(), mutate=True)['quiesced']
    assert all(call.args[:2] != ('docker', 'stop') for call in commands.call_args_list)
    with pytest.raises(RuntimeError, match='retired'):
        remote.reconcile({**request(), 'action': 'converge'})
    with pytest.raises(RuntimeError, match='retired'):
        remote.install_github(request())


@pytest.mark.parametrize('hazard', ['pending', 'unmanaged', 'wrong-deployment', 'unclean', 'changed-image'])
def test_retirement_hazards_fail_before_mutation(retired_host, monkeypatch, hazard):
    root, authorized, actual = retired_host
    if hazard == 'pending':
        remote.save(root / 'app.pending', {'action': 'converge'})
    elif hazard == 'unmanaged':
        monkeypatch.setattr(remote, 'containers', lambda: {'unmanaged.example.test': actual})
    elif hazard == 'wrong-deployment':
        manifest = json.loads(remote.MANIFEST.read_text()); manifest['deployment_id'] = 'another'
        remote.save(remote.MANIFEST, manifest)
    elif hazard == 'unclean':
        actual.update(running=False, exit_code=137)
    else:
        actual['image_id'] = 'sha256:another'
    with patch.object(remote, 'run') as commands:
        with pytest.raises(RuntimeError):
            remote.retire(request(), mutate=True)
    commands.assert_not_called()
    assert 'synthetic' in authorized.read_text()
    assert not (root / 'retirement.json').exists()


def test_forced_stop_stays_retired_and_fails_retry(retired_host):
    root, authorized, actual = retired_host
    def run(*args, **kwargs):
        actual.update(running=False, restart='no', exit_code=137)
    with patch.object(remote, 'run', side_effect=run):
        with pytest.raises(RuntimeError, match='did not stop cleanly'):
            remote.retire(request(), mutate=True)
    assert json.loads((root / 'retirement.json').read_text())['phase'] == 'stopping'
    assert 'synthetic' not in authorized.read_text()
    with pytest.raises(RuntimeError, match='did not stop cleanly'):
        remote.retire(request())


def test_interrupted_stop_resumes_and_rejects_changed_container(retired_host):
    root, authorized, actual = retired_host
    with patch.object(remote, 'run', side_effect=RuntimeError('synthetic interruption')):
        with pytest.raises(RuntimeError):
            remote.retire(request(), mutate=True)
    assert remote.retire(request())['retired']
    actual['id'] = 'replacement'
    with pytest.raises(RuntimeError, match='ownership'):
        remote.retire(request())


def test_host_retry_completes_previous_intent_for_same_instance(tmp_path, monkeypatch):
    from pocketdeploy.host import Host
    from pocketdeploy.state import State
    with State(tmp_path / '.state', 'test', {}, create=True) as state:
        state.put_resource('compute', 'instance', 'instance-id', {})
        failed = state.begin_operation('delete', 'synthetic')
        previous = state.intent(failed, 'host-quiesce', {'instance_id': 'instance-id'})
        unrelated = state.intent(failed, 'host-quiesce', {'instance_id': 'other-instance'})
        host = Host({}, state, tmp_path)
        monkeypatch.setattr(host, 'plan_delete', lambda connection: {'actions': [{'timeout': 3600}]})
        result = {'quiesced': True, 'applications': ['app.example.test'], 'revoked_github_keys': 0}
        with patch.object(host, '_remote_request', return_value=result) as request_remote:
            assert host.quiesce({'ip': '192.0.2.1'}, state.begin_operation('delete', 'synthetic')) == result
        assert request_remote.call_args.kwargs['timeout'] == 3780
        statuses = dict(state.db.execute('SELECT id,status FROM steps'))
        assert statuses[previous] == 'complete'
        assert statuses[unrelated] == 'pending'
