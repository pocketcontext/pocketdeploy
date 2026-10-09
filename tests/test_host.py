import json
from types import SimpleNamespace
from unittest.mock import patch

from pocketdeploy.host import Host


def test_host_key_is_anchored_and_payload_not_in_args(tmp_path):
    host = Host({}, SimpleNamespace(deployment_id='test'), tmp_path)
    public = host.prepare_keys()
    assert public.startswith('ssh-ed25519 ')
    cloud = json.loads(host.cloud_init().split('\n', 1)[1])
    assert cloud['ssh_keys']['ed25519_public'] == host.hostpub.read_text().strip()
    assert host.hostkey.stat().st_mode & 0o777 == 0o600
    argv = host._argv({'ip': '192.0.2.1', 'user': 'ubuntu'})
    assert 'StrictHostKeyChecking=yes' in argv
    assert '192.0.2.1 ssh-ed25519 ' in host.known.read_text()


def test_secret_only_in_stdin(tmp_path):
    host = Host({'once': {'applications': [{'resolved-env': {'SECRET': 'synthetic-value'}}]}},
                SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    with patch('pocketdeploy.host.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as call:
        host._remote({'ip': '192.0.2.1'}, 'plan')
    assert 'synthetic-value' not in repr(call.call_args.args)
    assert 'synthetic-value' in call.call_args.kwargs['input']


def test_init_custom_public_paths_are_preserved_by_converge_preparation(tmp_path):
    from pocketdeploy.cli import initialize
    config = {
        'profile': 'test', 'state-file': '.colors.sqlite', 'workdir': '.colors',
        '_file': str(tmp_path / 'colors.yml'),
        'ssh-public-key-file': '.ssh/client-public',
        'ssh-host-public-key-file': '.ssh/server-public',
    }
    state = SimpleNamespace(deployment_id='test', get_resource=lambda name: None)
    host = Host(config, state, tmp_path)
    initialize(config, state, host, tmp_path)
    files = (host.key, host.pub, host.hostkey, host.hostpub, host.known)
    before = {path: path.read_bytes() for path in files}
    assert not host.key.with_name(host.key.name + '.pub').exists()
    with patch('pocketdeploy.host.subprocess.run', side_effect=AssertionError('Keys must be reused')):
        assert host.prepare_keys() == host.pub.read_text().strip()
    assert {path: path.read_bytes() for path in files} == before


def test_prepare_custom_keys_never_uses_or_overwrites_stale_sidecar(tmp_path):
    config = {'ssh-public-key-file': '.ssh/client-public'}
    host = Host(config, SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    before = host.pub.read_bytes()
    sidecar = host.key.with_name(host.key.name + '.pub')
    sidecar.write_text('stale synthetic sidecar')
    host.prepare_keys()
    assert host.pub.read_bytes() == before
    assert sidecar.read_text() == 'stale synthetic sidecar'


def test_prepare_refuses_partial_pair_without_replacing_existing_public(tmp_path):
    import pytest
    from pocketdeploy.common import DeployError
    host = Host({}, SimpleNamespace(deployment_id='test'), tmp_path)
    host.pub.parent.mkdir()
    host.pub.write_text('existing public authority')
    with pytest.raises(DeployError, match='incomplete'):
        host.prepare_keys()
    assert not host.key.exists()
    assert host.pub.read_text() == 'existing public authority'


def test_init_refuses_configuration_and_lock_path_collisions(tmp_path):
    import pytest
    from pocketdeploy.cli import initialize
    from pocketdeploy.common import DeployError
    state = SimpleNamespace(deployment_id='test', get_resource=lambda name: None)
    for reserved in ('colors.yml', '.colors.sqlite.lock', '.colors', '.envrc'):
        config = {
            'profile': 'test', 'state-file': '.colors.sqlite', 'workdir': '.colors',
            '_file': str(tmp_path / 'colors.yml'), 'ssh-private-key-file': reserved,
        }
        host = Host(config, state, tmp_path)
        with pytest.raises(DeployError, match='distinct paths'):
            initialize(config, state, host, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_verbose_remote_timing_never_emits_payload(tmp_path, capsys):
    from pocketdeploy.output import Reporter
    host = Host({'once': {'applications': [{'resolved-env': {'SECRET': 'payload-sentinel'}}]}},
                SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    with Reporter(verbose=True).activate():
        with patch('pocketdeploy.host.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}')):
            host._remote({'ip': '192.0.2.1'}, 'status')
    output = capsys.readouterr()
    assert output.out == ''
    assert 'SSH: application status: started' in output.err
    assert 'SSH: application status: completed' in output.err
    assert 'payload-sentinel' not in output.err


def test_remote_timeout_has_safe_error_and_verbose_stage(tmp_path, capsys):
    import subprocess
    import pytest
    from pocketdeploy.common import DeployError
    from pocketdeploy.output import Reporter
    host = Host({}, SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    reporter = Reporter(verbose=True)
    with reporter.activate():
        with patch('pocketdeploy.host.subprocess.run', side_effect=subprocess.TimeoutExpired(
                ['secret-argument'], 1200, output='secret-output', stderr='secret-error')):
            with pytest.raises(DeployError) as caught:
                host._remote({'ip': '192.0.2.1'}, 'plan')
    output = capsys.readouterr()
    assert caught.value.code == 'command_timeout'
    assert 'remote work may still be running' in str(caught.value)
    assert reporter.failed_stage == 'SSH: application plan'
    assert 'secret-' not in output.out + output.err + str(caught.value)


def test_application_failure_diagnostics_are_allowlisted(tmp_path):
    import pytest
    from pocketdeploy.common import DeployError
    from pocketdeploy.host import APPLICATION_ERRORS
    host = Host({}, SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    for stage, message in APPLICATION_ERRORS.items():
        result = SimpleNamespace(returncode=1, stdout=json.dumps({
            'stage': stage, 'error': 'secret-value', 'command': 'secret-value'}), stderr='secret-value')
        with patch('pocketdeploy.host.subprocess.run', return_value=result):
            with pytest.raises(DeployError) as caught:
                host._remote({'ip': '192.0.2.1'}, 'converge')
        assert str(caught.value) == message
        assert caught.value.code == stage.replace('-', '_')
        assert 'secret-value' not in str(caught.value)
    result.stdout = json.dumps({'stage': 'secret-value', 'error': 'secret-value'})
    with patch('pocketdeploy.host.subprocess.run', return_value=result):
        with pytest.raises(DeployError, match='output suppressed') as caught:
            host._remote({'ip': '192.0.2.1'}, 'converge')
    assert 'secret-value' not in str(caught.value)
