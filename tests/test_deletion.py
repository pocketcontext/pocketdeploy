import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.deletion import Deletion
from pocketdeploy.output import Reporter, text_result
from pocketdeploy.state import State


def fixture(tmp_path):
    events = []
    state = State(tmp_path / '.state', 'test', {}, create=True)
    config = {'_root': str(tmp_path), 'profile': 'test', 'compute-prevent-destroy': False}
    def action(name, value=None):
        def call(*args, **kwargs):
            events.append(name)
            return value
        return call
    cloud = SimpleNamespace(plan_delete=action('oci-check', [{'resource': 'compute', 'id': 'vm', 'state': 'RUNNING', 'action': 'delete'}]),
                            resource_kinds={'oci-compute', 'oci-firewall', 'oci-boot-volume'},
                            delete_pending_key='oci-delete-compute',
                            connection=action('connection', {'instance_id': 'vm', 'ip': '192.0.2.1'}),
                            delete=action('oci-delete', {'deleted': True}))
    host = SimpleNamespace(plan_delete=action('host-check', {'actions': [{'host': 'app.test', 'action': 'stop-retain-data'}]}),
                           quiesce=action('quiesce', {'quiesced': True}),
                           plan_key_cleanup=action('ssh-key-check', [{'resource': 'ssh-key-files', 'action': 'delete'}]),
                           cleanup_keys=action('ssh-key-cleanup', {'deleted_key_files': 5}))
    github = SimpleNamespace(plan_delete=action('github-check', [{'resource': 'github:example/site', 'action': 'delete'}]),
                             delete=action('github-delete', {'deleted_environments': []}), cleanup_keys=action('key-cleanup', {}))
    services = SimpleNamespace(plan_delete=action('dns-check', [{'resource': 'dns:A:app.test', 'action': 'delete'}]),
                               delete=action('dns-delete', {'actions': []}))
    with patch('pocketdeploy.deletion.GitHub', return_value=github), patch('pocketdeploy.deletion.Services', return_value=services):
        deletion = Deletion(config, state, cloud, host, Reporter(quiet=True))
    return deletion, state, events


def test_delete_dag_checks_everything_before_retirement(tmp_path):
    deletion, state, events = fixture(tmp_path)
    with state:
        result = asyncio.run(deletion.run('op'))
        assert events == ['oci-check', 'github-check', 'dns-check', 'ssh-key-check', 'connection', 'host-check',
                          'github-delete', 'quiesce', 'dns-delete', 'oci-delete', 'github-check', 'ssh-key-check', 'key-cleanup', 'ssh-key-cleanup']
        assert state.get_meta('delete-host') == {'instance_id': 'vm', 'quiesced': True}
        assert result['deleted_resources'] == ['github:example/site', 'dns:A:app.test', 'compute', 'ssh-key-files']
        assert 'deleted: compute' in '\n'.join(text_result('delete', result))


@pytest.mark.parametrize('provider', ['cloud', 'github', 'services', 'host'])
def test_preflight_failure_never_mutates_any_provider(tmp_path, provider):
    deletion, state, events = fixture(tmp_path)
    def fail(*args):
        raise DeployError('synthetic expired token or ownership mismatch')
    getattr(deletion, provider).plan_delete = fail
    with state, pytest.raises(DeployError, match='expired token'):
        asyncio.run(deletion.run('op'))
    assert not set(events).intersection({'github-delete', 'quiesce', 'dns-delete', 'oci-delete', 'key-cleanup'})


def test_dry_run_checks_same_readiness_without_mutations(tmp_path):
    deletion, state, events = fixture(tmp_path)
    deletion.config['compute-prevent-destroy'] = True
    with state:
        result = deletion.plan()
        assert result['protected'] and result['dry_run']
        assert events == ['oci-check', 'github-check', 'dns-check', 'ssh-key-check', 'connection', 'host-check']
        assert state.get_meta('delete-host') is None


def test_protection_stops_before_any_external_call(tmp_path):
    deletion, state, events = fixture(tmp_path)
    deletion.config['compute-prevent-destroy'] = True
    with state, pytest.raises(DeployError, match='protection'):
        asyncio.run(deletion.run('op'))
    assert not events


def test_failed_shutdown_keeps_dns_instance_and_keys_and_reports_progress(tmp_path):
    deletion, state, events = fixture(tmp_path)
    def fail(*args):
        raise DeployError('Application did not stop cleanly.')
    deletion.host.quiesce = fail
    with state, pytest.raises(DeployError, match='Completed stages: delete-github') as error:
        asyncio.run(deletion.run('op'))
    assert error.value.stage == 'delete-applications'
    assert not set(events).intersection({'dns-delete', 'oci-delete', 'key-cleanup'})


@pytest.mark.parametrize('status', ['STOPPED', 'TERMINATING'])
def test_nonrunning_instance_requires_recorded_shutdown(tmp_path, status):
    deletion, state, events = fixture(tmp_path)
    deletion.cloud.plan_delete = lambda: [{'resource': 'compute', 'id': 'vm', 'state': status, 'action': 'delete'}]
    with state:
        with pytest.raises(DeployError, match='shutdown'):
            deletion.plan()
        state.set_meta('delete-host', {'instance_id': 'vm', 'quiesced': True})
        if status == 'TERMINATING':
            state.set_meta('oci-delete-compute', {'id': 'vm'})
        deletion.plan()
        assert deletion.connection is None
        assert 'host-check' not in events


def test_absent_compute_retry_does_not_need_ssh(tmp_path):
    deletion, state, events = fixture(tmp_path)
    deletion.cloud.plan_delete = lambda: [{'resource': 'compute', 'state': None, 'action': 'absent'}]
    with state:
        assert asyncio.run(deletion.run('op'))['deleted']
    assert not set(events).intersection({'host-check', 'connection', 'quiesce'})
    assert events[-1] == 'ssh-key-cleanup'


def test_expired_oci_token_fails_before_other_providers(tmp_path, monkeypatch):
    import base64
    import json
    from pocketdeploy.oci import OCI
    deletion, state, events = fixture(tmp_path)
    token = tmp_path / 'synthetic-token'
    payload = base64.urlsafe_b64encode(json.dumps({'exp': 1}).encode()).decode().rstrip('=')
    token.write_text('synthetic.' + payload + '.synthetic')
    monkeypatch.setenv('OCI_CLI_SECURITY_TOKEN_FILE', str(token))
    deletion.cloud = OCI({'oci-auth': 'security_token', 'oci-config-file-profile': 'synthetic',
                          'oci-region': 'eu-frankfurt-1'}, state)
    with state, patch('pocketdeploy.oci.run', side_effect=AssertionError('No OCI command should run')):
        with pytest.raises(DeployError, match='expired') as error:
            asyncio.run(deletion.run('op'))
    assert error.value.code == 'oci_token_expired'
    assert error.value.stage == 'delete-preflight'
    assert not events


def test_unknown_owned_resource_blocks_before_remote_calls(tmp_path):
    deletion, state, events = fixture(tmp_path)
    with state:
        state.put_resource('unexpected', 'future-provider', 'id', {}, owned=True)
        with pytest.raises(DeployError, match='unsupported owned'):
            asyncio.run(deletion.run('op'))
        assert events == []


def test_remaining_owned_resources_preserve_all_keys(tmp_path):
    deletion, state, events = fixture(tmp_path)
    with state:
        # The stub cloud intentionally leaves this owned resource in state.
        state.put_resource('compute', 'oci-compute', 'vm', {}, owned=True)
        with pytest.raises(DeployError, match='Owned remote resources remain') as error:
            asyncio.run(deletion.run('op'))
        assert error.value.stage == 'delete-local-keys'
        assert not set(events).intersection({'key-cleanup', 'ssh-key-cleanup'})


def test_unsafe_local_keys_block_remote_retirement(tmp_path):
    deletion, state, events = fixture(tmp_path)
    def fail():
        raise DeployError('SSH ownership mismatch')
    deletion.host.plan_key_cleanup = fail
    with state, pytest.raises(DeployError, match='SSH ownership mismatch'):
        asyncio.run(deletion.run('op'))
    assert not set(events).intersection({'github-delete', 'quiesce', 'dns-delete', 'oci-delete'})


def test_external_resources_do_not_block_key_cleanup(tmp_path):
    deletion, state, events = fixture(tmp_path)
    with state:
        state.put_resource('shared-network', 'external-network', 'id', {}, owned=False)
        result = asyncio.run(deletion.run('op'))
        assert result['deleted']
        assert result['local_keys']['deleted_key_files'] == 5
        assert result['retained_local'] == ['configuration', 'private-bindings', 'sqlite-state']
        assert 'retain_boot_volume' not in deletion.prepared
        assert 'delete-services' in result['completed_stages']
