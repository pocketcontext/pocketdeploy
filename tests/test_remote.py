import json
from unittest.mock import patch
import pytest
from pocketdeploy import remote


def app():
    return {'deploy-strategy': 'stop-first', 'host': 'demo.example.com', 'image': 'example/app:1', 'resolved-env': {'FOO': 'bar'}}


def current():
    return {'id': 'old', 'restart': 'always', 'running': True, 'image_id': 'image-id', 'settings': {
        'env': {'FOO': 'bar'}, 'autoUpdate': False, 'disableTLS': False, 'resources': {}, 'backup': {}},
        'volumes': [('data', '/data')], 'binds': False, 'exit_code': 0, 'oom': False}


@pytest.fixture(autouse=True)
def healthy(monkeypatch):
    monkeypatch.setattr(remote, 'health', lambda app: {'healthy': True, 'http_status': 200})


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'BASE', tmp_path)
    monkeypatch.setattr(remote, 'MANIFEST', tmp_path / 'manifest.json')
    remote.save(remote.MANIFEST, {'deployment_id': 'test', 'apps': {
        'demo.example.com': {'desired': remote.normalized(app()), 'image_id': 'image-id'}}})


def test_noop_and_environment_drift():
    actual = current()
    previous = {'desired': remote.normalized(app()), 'image_id': 'image-id'}
    assert remote.matching(app(), actual, previous)
    actual['settings']['env']['FOO'] = 'changed'
    assert not remote.matching(app(), actual, previous)


def test_pending_blocks_retry(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    (tmp_path / (remote.hashlib.sha256(app()['host'].encode()).hexdigest() + '.pending')).write_text('{}')
    with patch.object(remote, 'containers', return_value={app()['host']: current()}):
        with pytest.raises(RuntimeError, match='unfinished deployment'):
            remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [app()]})


def test_stop_failure_never_updates(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired['resolved-env'] = {'FOO': 'new'}
    old = current(); stopped = {**old, 'running': False, 'exit_code': 137}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:abc', 'image-id')), \
         patch.object(remote, 'run') as run:
        with pytest.raises(RuntimeError, match='did not stop cleanly'):
            remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
        assert not any(str(remote.ONCE) in c.args for c in run.call_args_list)
        assert list(tmp_path.glob('*.pending'))


def test_volume_mismatch_keeps_pending(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired['resolved-env'] = {'FOO': 'new'}
    old = current(); stopped = {**old, 'running': False, 'restart': 'no'}
    replacement = {**old, 'id': 'new', 'volumes': [('wrong', '/data')]}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped}, {app()['host']: replacement}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:abc', 'image-id')), \
         patch.object(remote, 'run'):
        with pytest.raises(RuntimeError, match='volume continuity'):
            remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
    assert list(tmp_path.glob('*.pending'))


def test_unmanaged_host_not_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'BASE', tmp_path)
    monkeypatch.setattr(remote, 'MANIFEST', tmp_path / 'manifest.json')
    with patch.object(remote, 'containers', return_value={app()['host']: current()}):
        with pytest.raises(RuntimeError, match='explicit adoption'):
            remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [app()]})


def test_successful_update_preserves_volumes_and_commits(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired['resolved-env'] = {'FOO': 'new'}
    old = current(); stopped = {**old, 'running': False, 'restart': 'no'}
    replacement = {**old, 'id': 'new', 'settings': {**old['settings'], 'env': {'FOO': 'new'}}}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped},
                                                       {app()['host']: replacement}, {app()['host']: replacement}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:abc', 'image-id')), \
         patch.object(remote, 'run') as run:
        result = remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
    assert result['actions'] == [{'host': app()['host'], 'action': 'update'}]
    assert not list(tmp_path.glob('*.pending'))
    assert json.loads(remote.MANIFEST.read_text())['apps'][app()['host']]['desired']['env'] == {'FOO': 'new'}
    commands = [call.args for call in run.call_args_list]
    assert commands[0][:3] == ('docker', 'update', '--restart=no')
    assert commands[1][:2] == ('docker', 'stop')
    assert commands[2][3] == 'update'


def test_removal_retains_data_and_ownership_history(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    old = current(); stopped = {**old, 'running': False, 'restart': 'no'}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped}, {}, {}]), \
         patch.object(remote, 'run') as run:
        remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': []})
    commands = [call.args for call in run.call_args_list]
    assert commands[-1] == (str(remote.ONCE), '-n', 'once', 'remove', app()['host'])
    manifest = json.loads(remote.MANIFEST.read_text())
    assert not manifest['apps']
    assert manifest['retained'][app()['host']]['volumes'] == [['data', '/data']]


def test_rolling_update_does_not_stop_old_container(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired['deploy-strategy'] = 'rolling'
    old = current(); replacement = {**old, 'id': 'new'}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: replacement}, {app()['host']: replacement}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:abc', 'image-id')), \
         patch.object(remote, 'run') as run:
        remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
    assert all(call.args[:2] != ('docker', 'stop') for call in run.call_args_list)


def test_mutable_image_change_is_not_skipped(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); old = current(); stopped = {**old, 'running': False, 'restart': 'no'}
    replacement = {**old, 'id': 'new', 'image_id': 'new-image'}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped}, {app()['host']: replacement}, {app()['host']: replacement}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:new', 'new-image')), \
         patch.object(remote, 'run') as run:
        result = remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
    assert result['actions'] == [{'host': app()['host'], 'action': 'update'}]
    assert any(call.args[3:4] == ('update',) for call in run.call_args_list)


def test_mutable_image_noop_does_not_redeploy(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    with patch.object(remote, 'containers', return_value={app()['host']: current()}), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:old', 'image-id')), \
         patch.object(remote, 'run') as run:
        result = remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [app()]})
    assert result['actions'] == []
    run.assert_not_called()


def test_http_failure_keeps_pending_and_previous_manifest(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired['resolved-env'] = {'FOO': 'new'}
    old = current(); stopped = {**old, 'running': False, 'restart': 'no'}
    replacement = {**old, 'id': 'new', 'settings': {**old['settings'], 'env': {'FOO': 'new'}}}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped}, {app()['host']: replacement}]), \
         patch.object(remote, 'resolve_image', return_value=('example/app@sha256:abc', 'image-id')), \
         patch.object(remote, 'run'), patch.object(remote, 'health', return_value={'healthy': False, 'http_status': 503}):
        with pytest.raises(RuntimeError, match='HTTP health'):
            remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})
    assert list(tmp_path.glob('*.pending'))
    assert json.loads(remote.MANIFEST.read_text())['apps'][app()['host']]['desired']['env'] == {'FOO': 'bar'}


def test_status_needs_no_resolved_secrets_and_reports_pending(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    desired = app(); desired.pop('resolved-env')
    (tmp_path / (remote.hashlib.sha256(app()['host'].encode()).hexdigest() + '.pending')).write_text('{}')
    with patch.object(remote, 'containers', return_value={app()['host']: current()}), \
         patch.object(remote, 'resolve_image') as image, patch.object(remote, 'run') as run:
        result = remote.reconcile({'action': 'status', 'deployment_id': 'test', 'applications': [desired]})
    assert 'actions' not in result
    assert result['pending_operations'] == 1
    assert result['applications'][0] == {'host': app()['host'], 'running': True, 'managed': True,
                                        'pending': True, 'healthy': True, 'http_status': 200}
    image.assert_not_called()
    run.assert_not_called()
