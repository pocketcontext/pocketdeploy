from unittest.mock import Mock
import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.runtime import recorded_operation, delete_compute
from pocketdeploy.transport import SSHTransport


def test_operation_records_interruptions():
    state = Mock()
    with pytest.raises(KeyboardInterrupt):
        with recorded_operation(state, 'converge', 'hash'):
            raise KeyboardInterrupt()
    state.finish_operation.assert_called_once_with(state.begin_operation.return_value, 'failed')


def test_compute_deletion_retains_keys_until_all_resources_gone():
    state, cloud, host = Mock(), Mock(), Mock()
    cloud.resource_kinds = {'digitalocean-droplet'}
    state.resources.return_value = [{'kind': 'digitalocean-droplet'}]
    with pytest.raises(DeployError, match='unfinished resources'):
        delete_compute({'compute-prevent-destroy': False}, state, cloud, host, 'op')
    host.cleanup_keys.assert_not_called()
    state.resources.side_effect = [[{'kind': 'digitalocean-droplet'}], []]
    delete_compute({'compute-prevent-destroy': False}, state, cloud, host, 'op')
    host.cleanup_keys.assert_called_once()


def test_compute_deletion_rejects_services_before_cloud_calls():
    state, cloud, host = Mock(), Mock(), Mock()
    cloud.resource_kinds = {'digitalocean-droplet'}
    state.resources.return_value = [{'kind': 'dns'}]
    with pytest.raises(DeployError, match='service'):
        delete_compute({'compute-prevent-destroy': False}, state, cloud, host, 'op')
    cloud.plan_delete.assert_not_called()


def test_transport_is_pinned_and_loopback_only():
    t = SSHTransport(('ssh', '-F', '/dev/null', '-l', 'ubuntu', '--', '192.0.2.1'))
    assert t.tunnel_argv(9091, 9091)[-2:] == ['--', '192.0.2.1']
    assert '127.0.0.1:9091:127.0.0.1:9091' in t.tunnel_argv(9091, 9091)
    assert t.rsync_shell() == 'ssh -F /dev/null -l ubuntu'
    with pytest.raises(DeployError, match='destination'):
        t.run_python({'ip': '192.0.2.2'}, '', {})
    with pytest.raises(DeployError, match='ports'):
        t.tunnel_argv(True, 9091)


def test_prepared_transport_does_not_revisit_state_and_keeps_ssh_flags(tmp_path):
    from pocketdeploy.host import Host
    from unittest.mock import patch
    config = {'_root': str(tmp_path), '_file': str(tmp_path / 'colors.yml')}
    host = Host(config, Mock(), tmp_path)
    host.trusted_public = Mock(return_value='ssh-ed25519 AAAATEST')
    connection = {'ip': '192.0.2.1', 'user': 'ubuntu'}
    transport = host.prepare_transport(connection)
    assert 'StrictHostKeyChecking=yes' in transport.argv
    assert 'GlobalKnownHostsFile=/dev/null' in transport.argv
    assert 'ForwardAgent=no' in transport.argv
    host.trusted_public.side_effect = AssertionError('worker accessed controller')
    with patch('pocketdeploy.transport.subprocess.run', return_value=Mock(returncode=0, stdout='{"ok": true}')) as run:
        assert transport.run_python(connection, 'trusted source', {'secret': 'SYNTHETIC_CREDENTIAL_SENTINEL_7f19'}) == {'ok': True}
        assert 'SYNTHETIC_CREDENTIAL_SENTINEL_7f19' not in ' '.join(run.call_args.args[0])
        assert 'SYNTHETIC_CREDENTIAL_SENTINEL_7f19' in run.call_args.kwargs['input']


@pytest.mark.parametrize('failure', [ValueError, OSError])
def test_tunnel_cleans_process_when_application_fails(failure):
    from unittest.mock import patch
    transport = SSHTransport(('ssh', '-l', 'ubuntu', '--', '192.0.2.1'))
    process = Mock()
    process.poll.return_value = None
    socket_context = Mock()
    socket_context.__enter__ = Mock(return_value=socket_context)
    socket_context.__exit__ = Mock(return_value=False)
    with patch('pocketdeploy.transport.socket.socket', return_value=socket_context), patch('pocketdeploy.transport.socket.create_connection', return_value=socket_context), patch('pocketdeploy.transport.subprocess.Popen', return_value=process), patch('pocketdeploy.transport.time.sleep'):
        with pytest.raises(failure):
            with transport.tunnel(9091, 9091):
                raise failure('application failure')
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)


def test_tunnel_rejects_occupied_port_without_starting_ssh():
    import socket
    from unittest.mock import patch
    transport = SSHTransport(('ssh', '-l', 'ubuntu', '--', '192.0.2.1'))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        with patch('pocketdeploy.transport.subprocess.Popen') as spawn:
            with pytest.raises(DeployError, match='unavailable'):
                with transport.tunnel(listener.getsockname()[1], 9091):
                    pytest.fail('occupied port accepted')
        spawn.assert_not_called()


def test_tunnel_port_probe_accepts_previous_forward_time_wait():
    import socket
    from unittest.mock import patch
    transport = SSHTransport(('ssh', '-l', 'ubuntu', '--', '192.0.2.1'))
    # Close the accepted server endpoint first, leaving TIME_WAIT on the
    # forwarding port, exactly as a closed SSH tunnel can do.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port)) as client:
            accepted, _ = listener.accept()
            accepted.close()
            assert client.recv(1) == b''
    with socket.socket() as ordinary_probe:
        with pytest.raises(OSError):
            ordinary_probe.bind(('127.0.0.1', port))
    process = Mock()
    process.poll.return_value = 1
    with patch('pocketdeploy.transport.subprocess.Popen', return_value=process) as spawn:
        with pytest.raises(DeployError, match='failed or timed out'):
            with transport.tunnel(port, 9091):
                pytest.fail('synthetic exited SSH accepted')
    # Getting as far as Popen proves the real TIME_WAIT probe succeeded.
    spawn.assert_called_once()
