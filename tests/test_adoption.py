"""Synthetic transfer tests: no providers, Docker daemon or application databases."""
import copy
import json
from unittest.mock import patch

import pytest

from pocketdeploy import remote
from pocketdeploy.cli import execute, parser
from pocketdeploy.common import DeployError
from pocketdeploy.host import Host, validate_config


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'BASE', tmp_path)
    monkeypatch.setattr(remote, 'MANIFEST', tmp_path / 'manifest.json')
    old = {'id': 'a' * 64, 'image_id': 'sha256:' + 'b' * 64, 'running': True,
           'restart': 'always', 'oom': False, 'exit_code': 0, 'binds': False,
           'volumes': [('data', '/storage')], 'settings': {
               'env': {'TOKEN': 'synthetic-secret'}, 'autoUpdate': False,
               'backup': {'autoBackup': False}, 'image': 'example/app:old'}}
    app = {'host': 'app.example.com', 'image': 'example/app:latest',
           'deploy-strategy': 'stop-first', 'resolved-env': {'TOKEN': 'new-secret'}}
    evidence = {'host': app['host'], 'container_id': old['id'], 'image_id': old['image_id'],
                'volumes': [['data', '/storage']], 'settings_sha256': remote.settings_hash(old),
                'previous_delivery_disabled': True}
    request = {'action': 'adopt-app', 'deployment_id': 'deployment', 'applications': [app], 'adoption': evidence}
    monkeypatch.setattr(remote, 'containers', lambda: {app['host']: copy.deepcopy(old)})
    monkeypatch.setattr(remote, 'health', lambda app: {'healthy': True})
    with patch.object(remote, 'run', return_value=json.dumps([{
        'Id': old['image_id'], 'RepoDigests': ['other/app@sha256:' + 'd' * 64, 'example/app@sha256:' + 'c' * 64]}])) as run:
        yield request, old, run


def test_transfer_preserves_current_configuration_and_is_idempotent(transfer):
    request, old, run = transfer
    first = remote.adopt_app(request)
    assert remote.adopt_app(request) == first
    assert 'synthetic-secret' not in json.dumps(first)
    record = json.loads(remote.MANIFEST.read_text())['apps'][request['adoption']['host']]
    assert record['desired']['env'] == old['settings']['env']
    assert record['image_digest'] == 'example/app@sha256:' + 'c' * 64
    assert all(call.args == ('docker', 'image', 'inspect', old['image_id']) for call in run.call_args_list)
    assert len(list(remote.BASE.glob('*.adoption'))) == 1


@pytest.mark.parametrize('field,value', [
    ('container_id', 'f' * 64), ('image_id', 'sha256:' + 'f' * 64),
    ('volumes', [['wrong', '/storage']]), ('settings_sha256', 'f' * 64),
    ('previous_delivery_disabled', False), ('host', 'other.example.com')])
def test_transfer_rejects_changed_evidence(transfer, field, value):
    request, _, _ = transfer
    request['adoption'][field] = value
    with pytest.raises(RuntimeError, match='evidence'):
        remote.adopt_app(request)
    assert not remote.MANIFEST.exists()


@pytest.mark.parametrize('field,value', [('binds', True), ('running', False), ('oom', True), ('restart', 'no'), ('volumes', [])])
def test_transfer_rejects_unsafe_container(transfer, field, value):
    request, old, _ = transfer
    old[field] = value
    with pytest.raises(RuntimeError):
        remote.adopt_app(request)
    assert not remote.MANIFEST.exists()


@pytest.mark.parametrize('automatic', ['autoUpdate', 'autoBackup'])
def test_transfer_rejects_automatic_mutators(transfer, automatic):
    request, old, _ = transfer
    if automatic == 'autoUpdate':
        old['settings'][automatic] = True
    else:
        old['settings']['backup'][automatic] = True
    request['adoption']['settings_sha256'] = remote.settings_hash(old)
    with pytest.raises(RuntimeError):
        remote.adopt_app(request)


@pytest.mark.parametrize('digests', [[], ['other/app@sha256:' + 'c' * 64], ['example/app:latest'], ['example/app@sha256:invalid']])
def test_transfer_requires_verified_repository_digest(transfer, digests):
    request, old, run = transfer
    run.return_value = json.dumps([{'Id': old['image_id'], 'RepoDigests': digests}])
    with pytest.raises(RuntimeError):
        remote.adopt_app(request)
    assert not remote.MANIFEST.exists()


@pytest.mark.parametrize('condition', ['retired', 'pending', 'owned', 'foreign', 'retained', 'unhealthy'])
def test_transfer_rejects_conflicting_state(transfer, monkeypatch, condition):
    request, _, _ = transfer
    host = request['adoption']['host']
    if condition == 'retired':
        remote.save(remote.BASE / 'retirement.json', {})
    elif condition == 'pending':
        remote.save(remote.BASE / 'other.pending', {})
    elif condition == 'unhealthy':
        monkeypatch.setattr(remote, 'health', lambda app: {'healthy': False})
    else:
        remote.save(remote.MANIFEST, {'deployment_id': 'foreign' if condition == 'foreign' else 'deployment',
                                      'apps': {host: {}} if condition == 'owned' else {},
                                      'retained': {host: {}} if condition == 'retained' else {}})
    with pytest.raises(RuntimeError):
        remote.adopt_app(request)


def test_transfer_resumes_after_interrupted_manifest_write(transfer):
    request, _, _ = transfer
    save = remote.save
    def crash(path, data):
        if path == remote.MANIFEST:
            raise OSError('synthetic interruption')
        save(path, data)
    with patch.object(remote, 'save', side_effect=crash):
        with pytest.raises(OSError):
            remote.adopt_app(request)
    assert list(remote.BASE.glob('*.adoption'))
    assert remote.adopt_app(request)['adopted']


def test_transfer_checks_container_again_before_commit(transfer):
    request, old, _ = transfer
    changed = {**old, 'id': 'changed'}
    with patch.object(remote, 'containers', side_effect=[{request['adoption']['host']: old}, {request['adoption']['host']: changed}]):
        with pytest.raises(RuntimeError, match='evidence'):
            remote.adopt_app(request)
    assert not remote.MANIFEST.exists()


def test_adoption_uses_shared_remote_lock(transfer, monkeypatch):
    request, _, _ = transfer
    import io
    monkeypatch.setattr(remote.sys, 'stdin', io.StringIO(json.dumps(request)))
    monkeypatch.setattr(remote.os, 'geteuid', lambda: 0)
    with patch.object(remote.fcntl, 'flock') as lock:
        assert remote.main()['adopted']
    assert lock.call_args.args[1] == remote.fcntl.LOCK_EX


@pytest.mark.parametrize('value', [0, -1, 3601, True, '900'])
def test_readiness_budget_rejects_invalid_values(value):
    with pytest.raises(DeployError, match='Ready timeout'):
        validate_config({'once': {'applications': [{'host': 'app.example.com', 'image': 'example/app:latest', 'deploy-ready-timeout': value}]}})


def test_readiness_honors_budget(monkeypatch):
    monkeypatch.setattr(remote.time, 'monotonic', lambda: 100)
    with patch.object(remote, 'health', return_value={'healthy': True}) as probe:
        assert remote.wait_healthy({'deploy-ready-timeout': 3})
    assert probe.call_args.kwargs['timeout'] == 3


def test_ssh_budget_covers_all_application_lifecycle_budgets(tmp_path):
    host = Host({'once': {'applications': [{'deploy-ready-timeout': 900, 'deploy-stop-timeout': 1200}]}}, None, tmp_path)
    with patch.object(host, '_remote_request', return_value={}) as request:
        host._remote({}, 'converge')
    assert request.call_args.kwargs['timeout'] >= 2100


def test_legacy_manifest_does_not_force_replacement():
    from test_remote import app, current
    desired = remote.normalized(app())
    desired.pop('ready_timeout')
    assert remote.matching(app(), current(), {'desired': desired, 'image_id': 'image-id'})


@pytest.mark.parametrize('arguments', [
    ['adopt-app'], ['status', '--app-host', 'app.example.com'],
    ['adopt-app', '--app-host', 'app.example.com', '--container-id', 'short']])
def test_cli_requires_complete_explicit_evidence_before_io(arguments):
    with pytest.raises(DeployError, match='adopt'):
        execute(parser().parse_args(arguments))


def test_transfer_resumes_after_commit_but_before_response(transfer):
    request, _, _ = transfer
    save = remote.save
    def crash_after_commit(path, data):
        save(path, data)
        if path == remote.MANIFEST:
            raise OSError('synthetic lost response')
    with patch.object(remote, 'save', side_effect=crash_after_commit):
        with pytest.raises(OSError):
            remote.adopt_app(request)
    assert remote.MANIFEST.exists()
    assert remote.adopt_app(request)['adopted']


def adoption_arguments():
    return ['adopt-app', '--app-host', 'app.example.com', '--container-id', 'a' * 64,
            '--image-id', 'sha256:' + 'b' * 64, '--volume', 'data:/storage',
            '--settings-sha256', 'c' * 64, '--previous-delivery-disabled']


@pytest.mark.parametrize('flag,value', [('--container-id', 'short'), ('--image-id', 'example/app:latest'),
                                       ('--settings-sha256', 'bad'), ('--volume', 'data:relative'),
                                       ('--volume', 'data:/storage\nunsafe')])
def test_cli_rejects_malformed_complete_evidence_before_config(flag, value):
    arguments = adoption_arguments()
    arguments[arguments.index(flag) + 1] = value
    with pytest.raises(DeployError, match='exact host'):
        execute(parser().parse_args(arguments))


def test_cli_rejects_duplicate_volume_evidence():
    with pytest.raises(DeployError, match='exact host'):
        execute(parser().parse_args(adoption_arguments() + ['--volume', 'data:/storage']))


@pytest.mark.parametrize('configured,owned', [(False, True), (True, False), (True, True)])
def test_cli_dispatch_requires_configured_application_and_owned_compute(tmp_path, monkeypatch, configured, owned):
    from pocketdeploy import cli
    from pocketdeploy.config import load, scope
    from test_cli import SYNTHETIC_CONFIG
    text = SYNTHETIC_CONFIG
    if configured:
        text += 'once:\n  applications:\n    - host: app.example.com\n      image: example/app:latest\n'
    (tmp_path / 'colors.yml').write_text(text)
    monkeypatch.chdir(tmp_path)
    config = load(tmp_path / 'colors.yml', env={}, resolve=False)
    with cli.State(tmp_path / '.colors.sqlite', 'demo', scope(config), create=True) as state:
        if owned:
            state.put_resource('compute', 'oci-compute', 'compute-id', {}, owned=True)
    calls = []
    class FakeHost:
        def __init__(self, *args): pass
        def adopt_app(self, connection, operation, evidence):
            calls.append('adopt')
            assert evidence['volumes'] == [['data', '/storage']]
            assert evidence['previous_delivery_disabled'] is True
            return {'adopted': True}
    class FakeCloud:
        def __init__(self, *args): pass
        def connection(self):
            calls.append('connection')
            return {'instance_id': 'compute-id'}
    monkeypatch.setattr(cli, 'Host', FakeHost)
    monkeypatch.setattr(cli, 'OCI', FakeCloud)
    if configured and owned:
        assert execute(parser().parse_args(adoption_arguments())) == {'adopted': True}
        assert calls == ['connection', 'adopt']
    else:
        with pytest.raises(DeployError, match='configured matching host' if not configured else 'recorded owned compute'):
            execute(parser().parse_args(adoption_arguments()))
        assert calls == []
