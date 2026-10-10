import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.host import Host
from pocketdeploy.host_trust import rotate


class State:
    deployment_id = 'synthetic'

    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_resource(self, name):
        return {'provider_id': 'instance-1'} if getattr(self, 'compute', False) else None


@pytest.fixture
def host(tmp_path):
    state = State()
    result = Host({}, state, tmp_path)
    result.prepare_keys()
    state.compute = True
    return result


def test_all_normal_ssh_requires_verified_rotation(host):
    with pytest.raises(DeployError, match='rotation is incomplete'):
        host._argv({'ip': '192.0.2.1'})
    with pytest.raises(DeployError, match='rotation is incomplete'):
        host.smtp_test({'ip': '192.0.2.1'}, {'password': 'synthetic-secret'}, 'test@example.com')


def test_pending_key_is_durable_before_install_and_fresh_pin_is_verified(host):
    calls = []

    def request(target, connection, public, command, payload=None):
        trust = host.state.meta['ssh-host-trust']
        assert trust['instance_id'] == 'instance-1'
        assert trust['verified'] is False
        calls.append((public, command, payload))
        if len(calls) == 1:
            assert public == trust['public']
            return SimpleNamespace(returncode=1)
        if len(calls) == 2:
            assert public == host.hostpub.read_text()
            assert json.loads(payload)['private'] == trust['private']
        else:
            assert public == trust['public']
        return SimpleNamespace(returncode=0)

    with patch('pocketdeploy.host_trust.request', side_effect=request):
        rotate(host, {'ip': '192.0.2.1'})
    assert host.state.meta['ssh-host-trust']['verified'] is True
    assert len(calls) == 3
    assert host.trusted_public() != host.hostpub.read_text().strip()
    argv = host._argv({'ip': '192.0.2.1'})
    assert 'ControlPath=none' in argv
    assert 'HostKeyAlgorithms=ssh-ed25519' in argv
    assert argv[1:3] == ['-F', '/dev/null']
    assert argv[-4:] == ['-l', 'ubuntu', '--', '192.0.2.1']


def test_lost_install_response_recovers_same_key_without_bootstrap_fallback(host):
    with patch('pocketdeploy.host_trust.request', side_effect=[SimpleNamespace(returncode=1), DeployError('interrupted')]):
        with pytest.raises(DeployError, match='interrupted'):
            rotate(host, {'ip': '192.0.2.1'})
    pending = dict(host.state.meta['ssh-host-trust'])
    assert pending['verified'] is False
    with patch('pocketdeploy.host_trust.request', return_value=SimpleNamespace(returncode=0)) as request:
        rotate(host, {'ip': '192.0.2.1'})
    assert request.call_count == 3
    assert request.call_args_list[1].args[2] == pending['public']
    assert request.call_args.args[2] == pending['public']
    assert host.state.meta['ssh-host-trust']['private'] == pending['private']


def test_successful_install_without_new_pin_proof_never_unlocks_payloads(host):
    with patch('pocketdeploy.host_trust.request', side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]):
        with pytest.raises(DeployError, match='could not be verified'):
            rotate(host, {'ip': '192.0.2.1'})
    assert host.state.meta['ssh-host-trust']['verified'] is False
    with pytest.raises(DeployError, match='rotation is incomplete'):
        host._remote_request({'ip': '192.0.2.1'}, 'converge')


def test_verified_key_never_falls_back_and_cannot_cross_instances(host):
    host.state.meta['ssh-host-trust'] = {'verified': True, 'public': 'ssh-ed25519 synthetic', 'instance_id': 'instance-1'}
    with patch('pocketdeploy.host_trust.request', side_effect=AssertionError('no rebootstrap')):
        rotate(host, {'ip': '192.0.2.1'})
    host.state.meta['ssh-host-trust']['instance_id'] = 'different'
    with pytest.raises(DeployError, match='another instance'):
        rotate(host, {'ip': '192.0.2.1'})
    with pytest.raises(DeployError, match='rotation is incomplete'):
        host._argv({'ip': '192.0.2.1'})


@pytest.mark.parametrize('user', ['-oProxyCommand=touch /tmp/sentinel', 'ubuntu\nroot', 'a@b', 'root;echo'])
def test_destination_cannot_be_ssh_option(host, user):
    with pytest.raises(DeployError, match='username'):
        host._argv({'ip': '192.0.2.1', 'user': user})


def test_bootstrap_payload_omits_application_secrets(host):
    host.config['once'] = {'applications': [{'resolved-env': {'TOKEN': 'secret-sentinel'}, 'smtp': True}]}
    host.state.meta['ssh-host-trust'] = {'verified': True, 'public': 'ssh-ed25519 synthetic', 'instance_id': 'instance-1'}
    with patch('pocketdeploy.host.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as request:
        host._remote_request({'ip': '192.0.2.1'}, 'bootstrap')
    assert 'secret-sentinel' not in request.call_args.kwargs['input']
    assert json.loads(request.call_args.kwargs['input'])['applications'] == [{'smtp': True}]


def test_remote_rotation_rejects_multiple_active_hostkeys(tmp_path):
    from pocketdeploy import host_trust_remote as remote
    writes = []

    def run(*argv):
        if argv[0] == 'ssh-keygen':
            return 'ssh-ed25519 synthetic\n'
        if argv == ('/usr/sbin/sshd', '-T'):
            return 'hostkey /etc/ssh/pocketdeploy_ed25519\nhostkey /etc/ssh/ssh_host_ed25519_key\n'
        return ''

    with patch.object(remote.os, 'geteuid', return_value=0), patch.object(remote, 'atomic', side_effect=lambda path, value: writes.append((path, value))), patch.object(remote, 'run', side_effect=run) as calls:
        with pytest.raises(RuntimeError, match='ambiguous host keys'):
            remote.install({'private': 'synthetic-private', 'public': 'ssh-ed25519 synthetic'})
    assert ('systemctl', 'reload', 'ssh') not in [call.args for call in calls.call_args_list]


def test_remote_rotation_waits_for_cloud_init_and_reloads_only_valid_identity():
    from pocketdeploy import host_trust_remote as remote

    def run(*argv):
        if argv[0] == 'ssh-keygen':
            return 'ssh-ed25519 synthetic\n'
        if argv == ('/usr/sbin/sshd', '-T'):
            return 'hostkey /etc/ssh/pocketdeploy_ed25519\n'
        return ''

    with patch.object(remote.os, 'geteuid', return_value=0), patch.object(remote, 'atomic'), patch.object(remote, 'run', side_effect=run) as calls:
        assert remote.install({'private': 'synthetic-private', 'public': 'ssh-ed25519 synthetic'}) == {'installed': True}
    assert calls.call_args_list[0].args == ('cloud-init', 'status', '--wait')
    assert calls.call_args_list[-1].args == ('systemctl', 'reload', 'ssh')


def test_metadata_block_is_installed_for_every_docker_start(tmp_path):
    from pocketdeploy import remote
    from pathlib import Path

    def local(path):
        result = tmp_path / str(path).lstrip('/')
        result.parent.mkdir(parents=True, exist_ok=True)
        return result

    with patch.object(remote, 'Path', side_effect=local), patch.object(remote, 'run') as run:
        remote.metadata_firewall()
    script = local('/usr/local/sbin/pocketdeploy-metadata-firewall')
    text = script.read_text()
    assert 'DOCKER-USER FORWARD' in text
    assert '-d 169.254.169.254/32 -j DROP' in text
    assert script.stat().st_mode & 0o777 == 0o755
    unit = local('/etc/systemd/system/docker.service.d/pocketdeploy-metadata.conf').read_text()
    assert 'ExecStartPre=/usr/local/sbin/pocketdeploy-metadata-firewall' in unit
    assert run.call_args_list[0].args == (str(script),)
    assert run.call_args_list[-1].args == ('systemctl', 'daemon-reload')


def test_resumed_new_key_still_requires_sole_effective_identity(host):
    host.state.meta['ssh-host-trust'] = {'verified': False, 'public': 'ssh-ed25519 synthetic',
                                        'private': 'synthetic', 'instance_id': 'instance-1'}
    with patch('pocketdeploy.host_trust.request', side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]) as request:
        with pytest.raises(DeployError, match='rotation failed'):
            rotate(host, {'ip': '192.0.2.1'})
    assert request.call_args.args[2] == 'ssh-ed25519 synthetic'
    assert host.state.meta['ssh-host-trust']['verified'] is False


def test_openssh_effective_hostkey_has_no_implicit_defaults(tmp_path):
    import shutil
    import subprocess
    sshd = shutil.which('sshd') or '/usr/sbin/sshd'
    from pathlib import Path
    if not Path(sshd).exists():
        pytest.skip('OpenSSH server is unavailable')
    key = tmp_path / 'hostkey'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, capture_output=True)
    configuration = tmp_path / 'sshd_config'
    configuration.write_text('HostKey ' + str(key) + '\n')
    result = subprocess.run([sshd, '-T', '-f', str(configuration)], capture_output=True, text=True)
    assert result.returncode == 0, 'Synthetic sshd effective-config check failed'
    assert [line for line in result.stdout.splitlines() if line.startswith('hostkey ')] == ['hostkey ' + str(key)]


def test_rotation_only_reconciles_pending_instance_intents_without_app_delivery(tmp_path):
    from pocketdeploy.state import State as Database
    with Database(tmp_path / 'state', 'test', {}, create=True) as state:
        host = Host({}, state, tmp_path)
        host.prepare_keys()
        state.put_resource('compute', 'instance', 'instance-1', {})
        earlier = state.begin_operation('converge', 'hash')
        pending = state.intent(earlier, 'host-key-rotation', {'instance_id': 'instance-1'})
        other = state.intent(earlier, 'host-key-rotation', {'instance_id': 'instance-2'})
        state.finish_operation(earlier, 'failed')
        current = state.begin_operation('rotate-host-key', 'hash')
        with patch('pocketdeploy.host.subprocess.run', return_value=SimpleNamespace(returncode=0)), patch('pocketdeploy.host_trust.rotate') as rotate, patch.object(host, '_remote', side_effect=AssertionError('no app operations')):
            assert host.rotate_key({'ip': '192.0.2.1'}, current) == {'verified': True}
        rotate.assert_called_once_with(host, {'ip': '192.0.2.1'})
        assert state.db.execute('SELECT status FROM steps WHERE id=?', (pending,)).fetchone()[0] == 'complete'
        assert state.db.execute('SELECT status FROM steps WHERE id=?', (other,)).fetchone()[0] == 'pending'
