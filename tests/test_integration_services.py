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
                               converge=event('services', {'actions': []}), smtp_settings=event('smtp-settings', {
                                   'server': 'smtp.resend.com', 'password': 'synthetic-secret'}))
    github = SimpleNamespace(preflight=event('github-preflight'), converge=event('github', {'environments': []}))
    return config, cloud, host, services, github, event


def test_services_order_and_secret_exclusion(tmp_path, capsys):
    events = []
    config, cloud, host, services, github, event = dependencies(tmp_path, events)
    with patch('pocketdeploy.services.Services', return_value=services), patch('pocketdeploy.github.GitHub', return_value=github), patch('pocketdeploy.health.verify', side_effect=event('https')):
        result = asyncio.run(converge(config, object(), cloud, host, 'operation', Reporter()))
    assert events == ['service-preflight', 'service-plan', 'github-preflight', 'keys', 'cloud-init',
                      'compute', 'host', 'services', 'smtp-settings', 'apps', 'status', 'https', 'github']
    assert host.smtp_settings['password'] == 'synthetic-secret'
    output = capsys.readouterr()
    assert 'synthetic-secret' not in json.dumps(result) + output.out + output.err
    assert 'synthetic-private-cloud-init' not in json.dumps(result) + output.out + output.err
    assert '_cloud_init' not in config


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
        return SimpleNamespace(plan_delete=lambda: [], delete=delete)
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
