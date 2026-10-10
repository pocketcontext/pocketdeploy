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
    monkeypatch.setattr(remote, 'health', lambda app, **kwargs: {'healthy': True, 'http_status': 200})


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
         patch.object(remote, 'run'), patch.object(remote, 'wait_healthy', return_value=False):
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
                                        'pending': True, 'healthy': True, 'http_status': 200,
                                        'container_id': 'old', 'image_id': 'image-id',
                                            'volumes': current()['volumes'],
                                            'settings_sha256': remote.settings_hash(current())}
    image.assert_not_called()
    run.assert_not_called()


def test_once_nil_env_serialization_matches_empty_desired_env():
    desired = app(); desired['resolved-env'] = {}
    actual = current(); actual['settings']['env'] = None
    assert remote.matching(desired, actual, {'desired': remote.normalized(desired), 'image_id': 'image-id'})


def test_smtp_drift_and_explicit_clear():
    desired = app()
    desired['resolved-smtp'] = {'server': 'smtp.resend.com', 'port': '465', 'username': 'resend', 'password': 'synthetic', 'from': 'mail@example.test'}
    actual = current()
    actual['settings']['smtp'] = desired['resolved-smtp'].copy()
    previous = {'desired': remote.normalized(desired), 'image_id': 'image-id'}
    assert remote.matching(desired, actual, previous)
    actual['settings']['smtp']['password'] = 'changed'
    assert not remote.matching(desired, actual, previous)
    args = remote.arguments(app())
    assert args[args.index('--smtp-password') + 1] == ''


def test_github_target_retains_other_owned_apps(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    with patch.object(remote, 'containers', return_value={app()['host']: current()}):
        result = remote.reconcile({'deployment_id': 'test', 'action': 'plan', 'applications': [], 'retain_other_apps': True})
    assert result['actions'] == []


def test_github_dispatcher_restricts_key_and_command(tmp_path, monkeypatch):
    from types import SimpleNamespace
    setup(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(remote.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_dir=str(home), pw_uid=remote.os.getuid(), pw_gid=remote.os.getgid()))
    monkeypatch.setattr(remote.os, 'chown', lambda *args: None)
    request = {'deployment_id': 'test', 'user': 'ubuntu', 'source': '# synthetic reconciler', 'targets': [
        {'app': app(), 'repository': 'example/app', 'public_key': 'ssh-ed25519 c3ludGhldGlj comment'}]}
    remote.install_github(request)
    line = (home / '.ssh/authorized_keys').read_text()
    assert line.startswith('restrict,command="sudo -n --preserve-env=SSH_ORIGINAL_COMMAND /usr/bin/python3 ')
    scripts = [p for p in (tmp_path / 'github').glob('*.py') if p.name != 'reconciler.py']
    assert len(scripts) == 1
    compile(scripts[0].read_text(), str(scripts[0]), 'exec')
    assert "re.fullmatch('deploy '" in scripts[0].read_text()
    remote.install_github(request)
    assert (home / '.ssh/authorized_keys').read_text() == line


def test_github_rotation_keeps_old_key_until_explicit_prune(tmp_path, monkeypatch):
    from types import SimpleNamespace
    setup(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(remote.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_dir=str(home), pw_uid=remote.os.getuid(), pw_gid=remote.os.getgid()))
    monkeypatch.setattr(remote.os, 'chown', lambda *args: None)
    target = {'app': app(), 'repository': 'example/app', 'public_key': 'ssh-ed25519 b2xk comment'}
    request = {'deployment_id': 'test', 'user': 'ubuntu', 'source': '# synthetic', 'targets': [target]}
    remote.install_github(request)
    authorized = home / '.ssh/authorized_keys'
    with authorized.open('a') as output:
        output.write('ssh-ed25519 b3BlcmF0b3I operator\n')
    target.update(public_key='ssh-ed25519 bmV3 comment', preserve_existing_keys=True)
    remote.install_github(request)
    remote.install_github(request)
    assert len(authorized.read_text().splitlines()) == 3
    assert ' b2xk ' in authorized.read_text()
    target['preserve_existing_keys'] = False
    remote.install_github(request)
    assert ' b2xk ' not in authorized.read_text()
    assert ' bmV3 ' in authorized.read_text()
    assert 'operator' in authorized.read_text()


def test_smtp_arguments_match_pinned_once_v033_settings_flags():
    # basecamp/once v0.3.3 internal/command/settings_flags.go defines five
    # StringVar settings; application_settings.go stores them under smtp.
    desired = app()
    desired['resolved-smtp'] = {'server': 'smtp.resend.com', 'port': '465',
                               'username': 'resend', 'password': 'synthetic-password',
                               'from': 'mail@notifications.example.test'}
    arguments = remote.arguments(desired)
    for key, value in desired['resolved-smtp'].items():
        assert arguments[arguments.index('--smtp-' + key) + 1] == value
    assert all(isinstance(value, str) for value in arguments)
    assert remote.normalized(desired)['smtp'] == desired['resolved-smtp']


def test_new_application_health_retries_until_proxy_ready():
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    with patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(remote.time, 'sleep', side_effect=sleep), patch.object(remote, 'health', side_effect=[{'healthy': False}, {'healthy': False}, {'healthy': True}]) as health:
        assert remote.wait_healthy(app())
    assert health.call_count == 3
    assert clock[0] == 4
    assert all(call.kwargs['timeout'] <= 5 for call in health.call_args_list)


def test_new_application_health_retry_exhausts_budget():
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    def unhealthy(*args, timeout):
        clock[0] += timeout
        return {'healthy': False}
    with patch.object(remote.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(remote.time, 'sleep', side_effect=sleep), patch.object(remote, 'health', side_effect=unhealthy):
        assert not remote.wait_healthy(app(), budget=60)
    assert clock[0] == 60


def test_image_pull_failure_sets_safe_stage(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(remote.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='synthetic-secret', stderr='synthetic-secret'))
    with pytest.raises(RuntimeError, match='output suppressed') as caught:
        remote.resolve_image('private-registry/synthetic-secret')
    assert remote.STAGE == 'application-image-pull'
    assert 'synthetic-secret' not in str(caught.value)


def test_once_deploy_failure_preserves_pending_and_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'BASE', tmp_path)
    monkeypatch.setattr(remote, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(remote, 'containers', lambda: {})
    monkeypatch.setattr(remote, 'resolve_image', lambda image: ('example/app@sha256:abc', 'image-id'))
    def fail(*args, **kwargs):
        raise RuntimeError('subprocess failed; output suppressed')
    monkeypatch.setattr(remote, 'run', fail)
    with pytest.raises(RuntimeError):
        remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [app()]})
    assert remote.STAGE == 'application-deploy'
    assert len(list(tmp_path.glob('*.pending'))) == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_release_digest_then_tag_converge_is_noop(tmp_path, monkeypatch, legacy):
    setup(tmp_path, monkeypatch)
    digest = 'example/app@sha256:' + 'a' * 64
    desired = app()
    if legacy:
        previous = app()
        previous['image'] = digest
        remote.save(remote.MANIFEST, {'deployment_id': 'test', 'apps': {
            desired['host']: {'desired': remote.normalized(previous), 'image_id': 'image-id'}}})
    with patch.object(remote, 'containers', return_value={desired['host']: current()}), \
         patch.object(remote, 'resolve_image', return_value=(digest, 'image-id')) as resolve, \
         patch.object(remote, 'run') as run:
        release = {**desired, 'deploy-image': digest}
        if not legacy:
            assert remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [release]})['actions'] == []
            resolve.assert_called_with(digest)
        assert remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})['actions'] == []
        resolve.assert_called_with(desired['image'])
        run.assert_not_called()
    record = json.loads(remote.MANIFEST.read_text())['apps'][desired['host']]
    assert record['desired']['image'] == desired['image']
    assert record['image_digest'] == digest


def test_legacy_digest_migration_preserves_settings_drift_detection():
    previous = {'desired': remote.normalized(app()), 'image_id': 'image-id'}
    previous['desired']['image'] = 'example/app@sha256:' + 'a' * 64
    actual = current()
    assert not remote.matching(app(), actual, previous)
    assert not remote.matching(app(), actual, previous, ('digest', 'different-id'))
    actual['settings']['env']['FOO'] = 'drift'
    assert not remote.matching(app(), actual, previous, ('digest', 'image-id'))


@pytest.mark.parametrize('attack', ['directory', 'authorized-symlink', 'authorized-hardlink', 'temporary-symlink', 'temporary-hardlink'])
def test_authorized_keys_never_follows_links(tmp_path, attack):
    from types import SimpleNamespace
    account = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=remote.os.getuid(), pw_gid=remote.os.getgid())
    victim = tmp_path / 'victim'
    victim.write_text('preserve me')
    ssh = tmp_path / '.ssh'
    if attack == 'directory':
        target = tmp_path / 'other'
        target.mkdir()
        ssh.symlink_to(target, target_is_directory=True)
    else:
        ssh.mkdir()
        name = 'authorized_keys' if attack.startswith('authorized') else 'authorized_keys.pocketdeploy.tmp'
        path = ssh / name
        if attack.endswith('symlink'):
            path.symlink_to(victim)
        else:
            remote.os.link(victim, path)
    if attack.startswith('temporary'):
        remote.authorized_keys(account, ['ssh-ed25519 synthetic operator'])
        assert (ssh / 'authorized_keys').read_text() == 'ssh-ed25519 synthetic operator\n'
    else:
        with pytest.raises((OSError, RuntimeError)):
            remote.authorized_keys(account, ['ssh-ed25519 synthetic operator'])
    assert victim.read_text() == 'preserve me'


def test_authorized_keys_read_does_not_create_directory(tmp_path):
    from types import SimpleNamespace
    account = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=remote.os.getuid(), pw_gid=remote.os.getgid())
    assert remote.authorized_keys(account) == []
    assert not (tmp_path / '.ssh').exists()


def test_ci_release_keeps_configured_tag_after_replacement(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    digest = 'example/app@sha256:' + 'b' * 64
    desired = {**app(), 'deploy-image': digest}
    old = current()
    stopped = {**old, 'running': False, 'restart': 'no'}
    new = {**old, 'id': 'replacement', 'image_id': 'new-image'}
    with patch.object(remote, 'containers', side_effect=[{app()['host']: old}, {app()['host']: stopped},
                                                       {app()['host']: new}, {app()['host']: new}]), \
         patch.object(remote, 'resolve_image', return_value=(digest, 'new-image')), \
         patch.object(remote, 'run') as run:
        assert remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [desired]})['actions'] == [
            {'host': app()['host'], 'action': 'update'}]
    record = json.loads(remote.MANIFEST.read_text())['apps'][app()['host']]
    assert record['desired']['image'] == app()['image']
    assert record['image_digest'] == digest
    assert record['image_id'] == 'new-image'
    assert any('--image' in call.args and digest in call.args for call in run.call_args_list)
    with patch.object(remote, 'containers', return_value={app()['host']: new}), \
         patch.object(remote, 'resolve_image', return_value=(digest, 'new-image')), patch.object(remote, 'run') as run:
        assert remote.reconcile({'action': 'converge', 'deployment_id': 'test', 'applications': [app()]})['actions'] == []
        run.assert_not_called()


@pytest.mark.parametrize('image', ['registry.example:5000/team/app', 'registry.example:5000/team/app:latest',
                                  'registry.example:5000/team/app@sha256:' + 'a' * 64])
def test_image_repository_preserves_registry_port(image):
    assert remote.image_repository(image) == 'registry.example:5000/team/app'
