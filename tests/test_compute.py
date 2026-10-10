"""Offline contracts between configuration, state and compute providers."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from pocketdeploy import cli
from pocketdeploy.common import DeployError
from pocketdeploy.compute import provider_class
from pocketdeploy.config import load, scope
from pocketdeploy.deletion import Deletion
from pocketdeploy.state import State


PROVIDERS = {
    'oci': {'oci-config-file-profile': 'test', 'oci-compartment-id': 'compartment',
            'oci-subnet-id': 'subnet', 'oci-availability-domain': 'ad'},
    'digitalocean': {'digitalocean-account-id': 'account', 'digitalocean-region': 'lon1',
                     'digitalocean-vpc-id': 'vpc'},
    'gcp': {'gcp-project': 'project', 'gcp-zone': 'europe-west2-a',
            'gcp-network': 'network', 'gcp-subnet': 'subnet'},
}


def configuration(tmp_path, provider, **extra):
    path = tmp_path / 'colors.yml'
    raw = {'profile': 'synthetic', 'provider-compute': provider, **PROVIDERS[provider], **extra}
    path.write_text(json.dumps(raw))
    return path, load(path, env={})


def test_oci_legacy_resolved_hash_and_scope_are_unchanged(tmp_path):
    path, config = configuration(tmp_path, 'oci')
    # Frozen pre-provider-abstraction defaults: changing these would change old hashes.
    legacy = {'schema-version': 1, 'provider-compute': 'oci', 'workdir': '.colors',
              'state-file': '.colors.sqlite', 'oci-auth': 'security_token',
              'oci-shape': 'VM.Standard.A1.Flex', 'oci-ocpus': 1, 'oci-memory-in-gbs': 6,
              'oci-boot-volume-size-in-gbs': 50, 'oci-boot-volume-vpus-per-gb': 10,
              'compute-prevent-destroy': True, 'compute-require-existing-state': False,
              'compute-ssh-sources': [], 'compute-http-sources': [],
              'provider-dns': 'no-infra', 'provider-smtp': 'no-infra', 'once-version': 'v0.3.3',
              **json.loads(path.read_text())}
    assert config['_desired_hash'] == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert scope(config) == {'provider': 'oci', 'profile': 'test', 'region': '',
                             'compartment': 'compartment', 'subnet': 'subnet'}


@pytest.mark.parametrize('provider', PROVIDERS)
def test_init_stays_local_and_repeatable_for_every_provider(tmp_path, monkeypatch, provider):
    path, config = configuration(tmp_path, provider)
    monkeypatch.setattr(cli, 'provider_class', lambda *a: pytest.fail('init selected cloud provider'))
    monkeypatch.setattr(cli, 'OCI', lambda *a: pytest.fail('init constructed OCI'))
    monkeypatch.setattr(cli.vault, 'save', lambda *a: pytest.fail('init contacted Vault'))
    monkeypatch.delenv('COLORS_PAR_DIGITALOCEAN_ACCESS_TOKEN', raising=False)
    args = cli.parser().parse_args(['init', '-f', str(path)])
    first = cli.execute(args)
    second = cli.execute(args)
    assert first['deployment_id'] == second['deployment_id']
    with State(tmp_path / '.colors.sqlite', 'synthetic', scope(config), read_only=True) as state:
        assert state.resources() == []
        assert state.deployment_id == first['deployment_id']
    assert (tmp_path / '.ssh/id_ed25519').is_file()


@pytest.mark.parametrize('original,replacement', [('oci', 'digitalocean'), ('digitalocean', 'gcp'), ('gcp', 'oci')])
def test_switch_provider_rejected_before_cloud_construction(tmp_path, monkeypatch, original, replacement):
    _, old = configuration(tmp_path, original)
    with State(tmp_path / '.colors.sqlite', 'synthetic', scope(old), create=True):
        pass
    path, _ = configuration(tmp_path, replacement)
    monkeypatch.setattr(cli, 'provider_class', lambda *a: pytest.fail('provider constructed before scope check'))
    monkeypatch.setattr(cli, 'OCI', lambda *a: pytest.fail('OCI constructed before scope check'))
    with pytest.raises(DeployError, match='identity'):
        cli.execute(cli.parser().parse_args(['status', '-f', str(path)]))


@pytest.mark.parametrize('provider', ['digitalocean', 'gcp'])
def test_environment_provider_selects_its_defaults_and_excludes_other_defaults(tmp_path, provider):
    path = tmp_path / 'colors.yml'
    path.write_text(json.dumps({'profile': 'synthetic', **PROVIDERS[provider]}))
    env = {'COLORS_PAR_PROVIDER_COMPUTE': provider,
           'COLORS_PAR_OCI_REGION': 'unrelated-operator-region'}
    field, value = ('digitalocean-size', 's-2vcpu-4gb') if provider == 'digitalocean' else ('gcp-machine-type', 'e2-medium')
    env['COLORS_PAR_' + field.upper().replace('-', '_')] = value
    config = load(path, env=env)
    assert config['provider-compute'] == provider
    assert config[field] == value
    assert not any(key.startswith('oci-') for key in config)
    assert scope(config)['provider'] == provider


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('evidence', ['correct', 'wrong-provider', 'wrong-instance', 'not-quiesced'])
def test_terminating_compute_requires_matching_provider_and_shutdown_evidence(tmp_path, provider, evidence):
    _, config = configuration(tmp_path, provider)
    cls = provider_class(provider)
    cloud = SimpleNamespace(resource_kinds=cls.resource_kinds, delete_pending_key=cls.delete_pending_key,
                            plan_delete=lambda: [{'resource': 'compute', 'id': 'instance', 'state': 'TERMINATING'}])
    host = SimpleNamespace(plan_key_cleanup=lambda: [])
    with State(tmp_path / '.colors.sqlite', 'synthetic', scope(config), create=True) as state:
        pending_key = cls.delete_pending_key if evidence != 'wrong-provider' else 'unrelated-delete-compute'
        state.set_meta(pending_key, {'id': 'other' if evidence == 'wrong-instance' else 'instance'})
        state.set_meta('delete-host', {'instance_id': 'instance', 'quiesced': evidence != 'not-quiesced'})
        deletion = Deletion(config, state, cloud, host)
        deletion.github = SimpleNamespace(plan_delete=lambda: [])
        deletion.services = SimpleNamespace(plan_delete=lambda: [])
        if evidence == 'correct':
            assert deletion.plan()['actions'][0]['state'] == 'TERMINATING'
        else:
            with pytest.raises(DeployError, match='without a recorded deletion'):
                deletion.plan()


@pytest.mark.parametrize('extra', [{'gcp-auth': 'unknown'}, {'gcp-auth': 'application-default', 'gcp-account': 'operator@example.com'}])
def test_invalid_gcp_auth_selection_rejected(tmp_path, extra):
    with pytest.raises(DeployError, match='gcp-auth|gcp-account'):
        configuration(tmp_path, 'gcp', **extra)
