"""Offline contracts for the configuration-only existing-machine provider."""
import json

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.compute import provider_class
from pocketdeploy.config import load, scope

KEY = 'SHA256:' + 'A' * 42 + 'c'


def write(tmp_path, **raw):
    path = tmp_path / 'colors.yml'
    path.write_text(json.dumps({'profile': 'synthetic', **raw}))
    return path


def existing(tmp_path, **extra):
    return write(tmp_path, **{'provider-compute': 'existing', 'existing-ip': '192.0.2.10',
                              'existing-host-key': KEY, **extra})


def test_existing_config_is_accepted_and_scoped_by_address(tmp_path):
    config = load(existing(tmp_path), env={})
    assert config['provider-compute'] == 'existing'
    assert config['existing-ip'] == '192.0.2.10'
    assert config['existing-host-key'] == KEY
    assert scope(config) == {'provider': 'existing', 'address': '192.0.2.10'}


@pytest.mark.parametrize('ip', ['', 'example.com', '192.0.2', '2001:db8::1', '192.0.2.010', 10])
def test_existing_ip_must_be_canonical_ipv4(tmp_path, ip):
    with pytest.raises(DeployError):
        load(existing(tmp_path, **{'existing-ip': ip}), env={})


@pytest.mark.parametrize('key', ['', 'A' * 43, 'SHA256:' + 'A' * 42, 'SHA256:' + 'A' * 43 + '=',
                                 'MD5:' + 'A' * 43, 'SHA256:' + 'A' * 42 + '-'])
def test_existing_host_key_must_be_sha256_fingerprint(tmp_path, key):
    with pytest.raises(DeployError):
        load(existing(tmp_path, **{'existing-host-key': key}), env={})


def test_existing_requires_both_fields(tmp_path):
    with pytest.raises(DeployError):
        load(write(tmp_path, **{'provider-compute': 'existing', 'existing-ip': '192.0.2.10'}), env={})


def test_existing_fields_are_foreign_to_other_providers(tmp_path):
    path = write(tmp_path, **{'provider-compute': 'hcloud', 'hcloud-location': 'nbg1',
                              'existing-ip': '192.0.2.10'})
    with pytest.raises(DeployError, match='different provider'):
        load(path, env={})
    with pytest.raises(DeployError, match='different provider'):
        load(existing(tmp_path, **{'hcloud-location': 'nbg1'}), env={})


def test_existing_rejects_ingress(tmp_path):
    rule = {'protocol': 'tcp', 'ports': ['443'], 'sources': ['0.0.0.0/0']}
    with pytest.raises(DeployError):
        load(existing(tmp_path, **{'compute-ingress': [rule]}), env={})


def test_existing_has_no_builtin_provider():
    with pytest.raises(DeployError, match='application-supplied'):
        provider_class('existing')
