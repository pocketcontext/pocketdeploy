"""Exercise real Blue ordering while external adapters stay synthetic."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketdeploy.cli import converge
from pocketdeploy.common import DeployError
from pocketdeploy.output import Reporter


def dependencies(tmp_path, events):
    config = {'_root': str(tmp_path), 'profile': 'test', 'provider-dns': 'cloudflare',
              'provider-smtp': 'resend', 'once': {'applications': [
                  {'host': 'example.test', 'github': 'example/site', 'smtp': True}]}}
    def event(name, result=None):
        def call(*args, **kwargs):
            events.append(name)
            return result
        return call
    cloud = SimpleNamespace(converge=event('compute', {'ip': '192.0.2.1'}))
    host = SimpleNamespace(prepare_keys=event('keys', 'public-key'), cloud_init=event('cloud-init', 'synthetic-private-cloud-init'),
                           bootstrap=event('host', {}), converge=event('apps', {'actions': []}),
                           status=event('status', {'applications': []}), smtp_settings=None)
    services = SimpleNamespace(preflight=event('service-preflight'), plan=event('service-plan', {'actions': []}),
                               prepare_dns=event('services', {'actions': []}),
                               verify_smtp=event('smtp-verification', {'status': 'verified'}),
                               ensure_smtp_credentials=event('smtp-credentials', {'actions': []}),
                               smtp_settings=event('smtp-settings', {
                                   'server': 'smtp.resend.com', 'password': 'synthetic-secret'}))
    github = SimpleNamespace(preflight=event('github-preflight'), converge=event('github', {'environments': []}))
    return config, cloud, host, services, github, event


def test_services_order_and_secret_exclusion(tmp_path, capsys):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=event('https')):
        result = asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter()))
    assert events == ['service-preflight', 'service-plan', 'github-preflight', 'keys', 'cloud-init',
                      'compute', 'host', 'services', 'smtp-verification', 'smtp-credentials',
                      'smtp-settings', 'apps', 'status', 'https', 'github']
    assert host.smtp_settings['password'] == 'synthetic-secret'
    output = capsys.readouterr()
    assert 'synthetic-secret' not in json.dumps(result) + output.out + output.err
    assert 'synthetic-private-cloud-init' not in json.dumps(result) + output.out + output.err
    assert '_cloud_init' not in config


def test_smtp_verification_failure_blocks_credentials_and_deployment(tmp_path):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    def pending(*args, **kwargs):
        events.append('smtp-verification')
        raise DeployError('SMTP DNS verification timed out.', code='smtp_verification_pending')
    services.verify_smtp = pending
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github):
        with pytest.raises(DeployError) as error:
            asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter(quiet=True)))
    assert error.value.code == 'smtp_verification_pending'
    assert error.value.stage == 'smtp-verification'
    assert events == ['service-preflight', 'service-plan', 'github-preflight', 'keys', 'cloud-init',
                      'compute', 'host', 'services', 'smtp-verification']
    assert host.smtp_settings is None
    assert '_cloud_init' not in config


def test_dns_only_configuration_skips_smtp_stages(tmp_path):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    config.pop('provider-smtp')
    config['once']['applications'][0]['smtp'] = False
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=event('https')):
        asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter(quiet=True)))
    assert events == ['service-preflight', 'service-plan', 'github-preflight', 'keys', 'cloud-init',
                      'compute', 'host', 'services', 'apps', 'status', 'https', 'github']
    assert host.smtp_settings is None


def test_smtp_pending_then_verified_continues_same_dag(tmp_path, monkeypatch, capsys):
    from pocketdeploy import services as services_module
    from pocketdeploy.services import Services
    from pocketdeploy.state import State

    events = []
    config, cloud, host, _, github, event = dependencies(tmp_path, events)
    config.update({'smtp-domain': 'notifications.example.test',
                   'smtp-from': 'mail@notifications.example.test'})
    config['once']['applications'][0]['manage-dns'] = True
    domain = {'id': 'domain-id', 'name': config['smtp-domain'], 'status': 'pending',
              'records': [{'type': 'TXT', 'name': config['smtp-domain'], 'value': 'synthetic-dns'}]}
    clock = [0]
    polls = []
    def sleep(seconds):
        assert 'apps' not in events and 'github' not in events
        assert 'smtp-key' not in events
        events.append('wait')
        clock[0] += seconds
    monkeypatch.setattr(services_module, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with State(tmp_path / 'smtp-state', 'test', {}, create=True) as state:
        service = Services(config, state)
        operation = state.begin_operation('converge', 'synthetic-config-hash')
        monkeypatch.setattr(service, 'preflight', event('service-preflight'))
        monkeypatch.setattr(service, 'plan', event('service-plan', {'actions': []}))
        monkeypatch.setattr(service, '_domain', event('domain', domain))
        monkeypatch.setattr(service, '_dns', event('dns', {'action': 'noop'}))
        def resend(args, **kwargs):
            if args[:2] == ['domains', 'verify']:
                events.append('trigger-verification')
                return {}
            if args[:2] == ['domains', 'get']:
                polls.append(clock[0])
                events.append('poll')
                return {**domain, 'status': 'verified' if len(polls) == 3 else 'pending'}
            assert args[:2] == ['api-keys', 'create']
            assert len(polls) == 3
            events.append('smtp-key')
            return {'id': 'key-id', 'token': 'synthetic-smtp-secret'}
        monkeypatch.setattr(service, '_resend', resend)
        with patch('pocketdeploy.services.Services', return_value=service), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=event('https')):
            result = asyncio.run(converge(config, state, cloud, host, operation, Reporter(quiet=True), smtp_verification_timeout=30))
        assert state.get_resource('smtp-key')['provider_id'] == 'key-id'
    assert polls == [0, 10, 20]
    assert events == ['service-preflight', 'service-plan', 'github-preflight', 'keys', 'cloud-init',
                      'compute', 'host', 'dns', 'domain', 'dns', 'trigger-verification', 'poll', 'wait',
                      'poll', 'wait', 'poll', 'smtp-key', 'apps', 'status', 'https', 'github']
    assert host.smtp_settings['password'] == 'synthetic-smtp-secret'
    assert result['services']['actions'][-1]['action'] == 'verified'
    output = capsys.readouterr()
    assert 'synthetic-smtp-secret' not in json.dumps(result) + output.out + output.err


def test_preflight_failure_blocks_keys_compute_and_all_mutations(tmp_path):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    def rejected():
        events.append('github-preflight')
        raise DeployError('GitHub environment is unmanaged.')
    github.preflight = rejected
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github):
        with pytest.raises(DeployError, match='unmanaged'):
            asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter(quiet=True)))
    assert events == ['service-preflight', 'service-plan', 'github-preflight']


def test_https_failure_blocks_environment_publication(tmp_path):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=DeployError('HTTPS failed')):
        with pytest.raises(DeployError, match='HTTPS failed'):
            asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter(quiet=True)))
    assert 'apps' in events
    assert 'github' not in events


def test_removed_github_config_still_checks_recorded_environment(tmp_path):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    config['once']['applications'] = []
    state = SimpleNamespace(resources=lambda: [{'kind': 'github-environment'}])
    def retired():
        events.append('retired-target')
        raise DeployError('Removed GitHub target requires explicit environment retirement.')
    github.preflight = retired
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github):
        with pytest.raises(DeployError, match='retirement'):
            asyncio.run(converge(config, state, cloud, host, 'operation', Reporter(quiet=True)))
    assert events == ['service-preflight', 'service-plan', 'retired-target']


def local_deployment(tmp_path, monkeypatch, extra=''):
    from pocketdeploy import cli
    from pocketdeploy.config import load, scope
    from pocketdeploy.state import State
    config_file = tmp_path / 'colors.yml'
    config_file.write_text('''profile: demo
oci-config-file-profile: test
oci-compartment-id: compartment
oci-subnet-id: subnet
oci-availability-domain: ad
''' + extra)
    monkeypatch.chdir(tmp_path)
    config = load(config_file)
    with State(tmp_path / '.colors.sqlite', 'demo', scope(config), create=True) as state:
        state.put_resource('compute', 'oci-compute', 'synthetic-instance', {})
    return cli, config, scope(config)


def test_plan_new_smtp_defers_app_plan_until_credential_exists(tmp_path, monkeypatch):
    cli, config, scope = local_deployment(tmp_path, monkeypatch, '''provider-dns: cloudflare
cloudflare-zone-id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
provider-smtp: resend
smtp-domain: notifications.example.test
smtp-from: mail@notifications.example.test
once:
  applications:
    - host: www.example.test
      image: ghcr.io/example/site:latest
      smtp: true
''')
    cloud = SimpleNamespace(plan=lambda: [], connection=lambda: {'ip': '192.0.2.1'})
    host = SimpleNamespace(plan=lambda *args: pytest.fail('Plan attempted resolving missing SMTP credential'))
    services = SimpleNamespace(preflight=lambda: None, plan=lambda *args: {'actions': []})
    with patch.object(cli, 'OCI', return_value=cloud), patch.object(cli, 'Host', return_value=host), patch('pocketdeploy.services.Services', return_value=services):
        result = cli.execute(cli.parser().parse_args(['plan']))
    assert result['applications'] == [{'host': 'www.example.test', 'action': 'after-smtp'}]


def test_delete_retires_recorded_github_even_after_app_removed(tmp_path, monkeypatch):
    from pocketdeploy.state import State
    cli, config, scope = local_deployment(tmp_path, monkeypatch, 'compute-prevent-destroy: false\n')
    with State(tmp_path / '.colors.sqlite', 'demo', scope) as state:
        state.put_resource('github:example/site', 'github-environment', '42', {'repository': 'example/site', 'environment': 'demo'})
    events = []
    def github_factory(config, state, root, host):
        def delete(operation):
            events.append('github-delete')
            state.remove_resource('github:example/site')
            return {'deleted_environments': [{'repository': 'example/site', 'environment': 'demo'}]}
        return SimpleNamespace(plan_delete=lambda: [], delete=delete, cleanup_keys=lambda: {})
    def cloud_factory(config, state):
        def delete(operation):
            events.append('compute-delete')
            state.remove_resource('compute')
            return {'deleted': True}
        return SimpleNamespace(plan_delete=lambda: [], delete=delete,
                               resource_kinds={'oci-compute', 'oci-firewall', 'oci-boot-volume'},
                               delete_pending_key='oci-delete-compute')
    with patch.object(cli, 'OCI', side_effect=cloud_factory), patch('pocketdeploy.deletion.GitHub', side_effect=github_factory):
        cli.execute(cli.parser().parse_args(['delete']))
    assert events == ['github-delete', 'compute-delete']


def test_delete_refuses_omitted_recorded_dns_configuration(tmp_path, monkeypatch):
    from pocketdeploy.state import State
    cli, config, scope = local_deployment(tmp_path, monkeypatch, 'compute-prevent-destroy: false\n')
    with State(tmp_path / '.colors.sqlite', 'demo', scope) as state:
        state.put_resource('dns:A:www.example.test', 'cloudflare-dns', 'dns-id', {'type': 'A', 'name': 'www.example.test', 'zone': 'zone-id'})
    cloud = SimpleNamespace(delete=lambda operation: pytest.fail('Deleted compute with dangling website DNS'))
    with patch.object(cli, 'OCI', return_value=cloud):
        with pytest.raises(DeployError, match='Restore the managed DNS configuration'):
            cli.execute(cli.parser().parse_args(['delete']))


def test_init_refuses_github_key_aliasing_host_authority(tmp_path):
    import hashlib
    from pocketdeploy.cli import initialize
    from pocketdeploy.host import Host
    filename = '.ssh/github-' + hashlib.sha256(b'example/site').hexdigest()[:20]
    config = {'profile': 'test', 'state-file': '.colors.sqlite', 'workdir': '.colors',
              '_file': str(tmp_path / 'colors.yml'), 'ssh-private-key-file': filename,
              'once': {'applications': [{'github': 'example/site'}]}}
    host = Host(config, None, tmp_path)
    with pytest.raises(DeployError, match='distinct paths'):
        initialize(config, None, host, tmp_path)
    assert not list(tmp_path.iterdir())


def test_github_external_dns_still_requires_public_https():
    from pocketdeploy.health import verify
    response = SimpleNamespace(status=200, url='https://www.example.test/up')
    from contextlib import nullcontext
    with patch('pocketdeploy.health.urllib.request.urlopen', return_value=nullcontext(response)) as request:
        verify({'once': {'applications': [{'host': 'www.example.test', 'health-path': '/up', 'github': 'example/site', 'manage-dns': False}]}})
    assert request.call_args.args[0] == 'https://www.example.test/up'


def test_transient_service_read_recovers_without_restarting_converge(tmp_path, monkeypatch):
    from pocketdeploy import retry, services as services_module
    from pocketdeploy.services import Services

    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    attempts = []
    real_service = Services(config, None)

    def provider(args, **kwargs):
        assert args[:3] == ['resend', 'domains', 'list']
        attempts.append(args)
        events.append('provider-read')
        if len(attempts) == 1:
            raise DeployError('Temporary provider failure.', code='provider_unavailable', retryable=True)
        return '{"data": [], "has_more": false}'

    def preflight():
        events.append('service-preflight')
        assert real_service._resend(['domains', 'list']) == {'data': [], 'has_more': False}

    services.preflight = preflight
    monkeypatch.setattr(services_module, 'run', provider)
    monkeypatch.setattr(retry.time, 'sleep', lambda seconds: events.append('retry-wait'))
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=event('https')):
        asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter(quiet=True)))
    assert len(attempts) == 2
    assert events[:4] == ['service-preflight', 'provider-read', 'retry-wait', 'provider-read']
    for stage in ('service-preflight', 'service-plan', 'keys', 'compute', 'host', 'services', 'smtp-credentials', 'apps', 'github'):
        assert events.count(stage) == 1


def test_delete_preflight_transient_read_recovers_in_one_cli_invocation(tmp_path, monkeypatch):
    from pocketdeploy import retry, services as services_module
    from pocketdeploy.state import State

    cli, config, scope = local_deployment(tmp_path, monkeypatch, '''compute-prevent-destroy: false
provider-dns: cloudflare
cloudflare-zone-id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
provider-smtp: resend
smtp-domain: notifications.example.test
smtp-from: mail@notifications.example.test
''')
    with State(tmp_path / '.colors.sqlite', 'demo', scope) as state:
        state.put_resource('smtp-domain', 'resend-domain', 'domain-id', {'name': config['smtp-domain']})
    monkeypatch.setenv('RESEND_API_KEY', 'synthetic-management-secret')
    events = []
    read_attempts = []
    deleted = [False]

    def provider(args, **kwargs):
        if args[:3] == ['resend', 'domains', 'list']:
            read_attempts.append(args)
            events.append('provider-read')
            if len(read_attempts) == 1:
                assert not deleted[0]
                raise DeployError('Temporary provider failure.', code='provider_unavailable', retryable=True)
            return json.dumps({'data': [] if deleted[0] else [{'id': 'domain-id', 'name': config['smtp-domain']}], 'has_more': False})
        assert args[:4] == ['resend', 'domains', 'delete', 'domain-id']
        events.append('domain-delete')
        deleted[0] = True
        return '{}'

    def cloud_factory(config, state):
        def plan():
            events.append('cloud-preflight')
            return []
        def delete(operation):
            events.append('compute-delete')
            state.remove_resource('compute')
            return {'deleted': True}
        return SimpleNamespace(plan_delete=plan, delete=delete,
                               resource_kinds={'oci-compute', 'oci-firewall', 'oci-boot-volume'},
                               delete_pending_key='oci-delete-compute')

    monkeypatch.setattr(services_module, 'run', provider)
    monkeypatch.setattr(retry.time, 'sleep', lambda seconds: events.append('retry-wait'))
    with patch.object(cli, 'OCI', side_effect=cloud_factory):
        result = cli.execute(cli.parser().parse_args(['delete', '--provider-read-timeout', '10']))
    assert result['deleted'] is True
    assert result['retained_resources'] == []
    assert events[:4] == ['cloud-preflight', 'provider-read', 'retry-wait', 'provider-read']
    assert events.count('cloud-preflight') == 1
    assert events.count('domain-delete') == 1
    assert events.count('compute-delete') == 1
    assert result['completed_stages'] == ['delete-preflight', 'delete-github', 'delete-applications', 'delete-services', 'delete-infrastructure', 'delete-local-keys']
