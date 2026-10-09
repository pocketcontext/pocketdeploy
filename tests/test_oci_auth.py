import base64
import json
import subprocess
import time

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.oci import OCI


def token_config(tmp_path, monkeypatch, payload, profile='session'):
    monkeypatch.delenv('OCI_CLI_SECURITY_TOKEN_FILE', raising=False)
    token = tmp_path / 'token'
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    token.write_text('header.' + encoded + '.signature')
    config = tmp_path / 'config'
    config.write_text(f'[{profile}]\nsecurity_token_file={token}\n')
    monkeypatch.setenv('OCI_CLI_CONFIG_FILE', str(config))
    return OCI({'oci-config-file-profile': profile}), token


def test_expired_token_fails_before_any_subprocess(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': time.time() - 1, 'secret': 'never-print'})
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('OCI started'))
    started = time.monotonic()
    with pytest.raises(DeployError) as caught:
        adapter._call('network', 'subnet', 'get')
    assert time.monotonic() - started < 1
    assert caught.value.code == 'oci_token_expired'
    assert 'oci session refresh --profile session' in str(caught.value)
    assert 'never-print' not in str(caught.value)


@pytest.mark.parametrize('payload', [{'exp': time.time() + 3600}, {}, {'exp': True}, {'exp': '1'}, {'exp': float('nan')}])
def test_unknown_or_valid_expiry_delegates_to_oci(tmp_path, monkeypatch, payload):
    adapter, _ = token_config(tmp_path, monkeypatch, payload)
    calls = []
    monkeypatch.setattr('pocketdeploy.oci.run', lambda *a, **kw: calls.append(a) or '{"data":{}}')
    assert adapter._call('network', 'subnet', 'get') == {}
    assert len(calls) == 1


def test_api_key_does_not_use_token(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': 1})
    adapter.config['oci-auth'] = 'api_key'
    adapter._check_token()


def test_default_profile_inheritance(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': 1}, profile='DEFAULT')
    path = tmp_path / 'config'
    path.write_text(path.read_text() + '[session]\nregion=test\n')
    adapter.config['oci-config-file-profile'] = 'session'
    with pytest.raises(DeployError) as caught:
        adapter._check_token()
    assert caught.value.code == 'oci_token_expired'


def test_malformed_token_does_not_leak(tmp_path, monkeypatch):
    adapter, token = token_config(tmp_path, monkeypatch, {})
    token.write_text('private-invalid-token')
    adapter._check_token()


def test_token_rechecked_after_refresh(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': 1})
    with pytest.raises(DeployError):
        adapter._check_token()
    token_config(tmp_path, monkeypatch, {'exp': time.time() + 3600})
    adapter._check_token()


def test_401_is_authentication_failure_not_proven_expiry(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': time.time() + 3600})
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, '', 'ServiceError:\n' + json.dumps({'status': 401, 'code': 'NotAuthenticated', 'message': 'private-secret'})))
    with pytest.raises(DeployError) as caught:
        adapter._call('network', 'subnet', 'get')
    assert caught.value.code == 'oci_authentication_failed'
    assert 'expired' not in str(caught.value)
    assert 'private-secret' not in str(caught.value)


@pytest.mark.parametrize('stderr', ['private-failure', 'ServiceError: invalid-private-json', 'ServiceError: {"status":404,"message":"private"}'])
def test_other_failures_stay_suppressed(tmp_path, monkeypatch, stderr):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': time.time() + 3600})
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', stderr))
    with pytest.raises(DeployError) as caught:
        adapter._call('network', 'subnet', 'get')
    assert caught.value.code == 'command_failed'
    assert 'private' not in str(caught.value)


def test_token_environment_override_takes_precedence(tmp_path, monkeypatch):
    adapter, token = token_config(tmp_path, monkeypatch, {'exp': time.time() + 3600})
    override = tmp_path / 'override'
    override.write_text('header.' + base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=') + '.sig')
    monkeypatch.setenv('OCI_CLI_SECURITY_TOKEN_FILE', str(override))
    monkeypatch.setenv('OCI_CLI_CONFIG_FILE', str(tmp_path / 'missing'))
    with pytest.raises(DeployError) as caught:
        adapter._check_token()
    assert caught.value.code == 'oci_token_expired'


def test_explicit_profile_ignores_environment_profile(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': 1})
    monkeypatch.setenv('OCI_CLI_PROFILE', 'different')
    with pytest.raises(DeployError) as caught:
        adapter._check_token()
    assert 'refresh --profile session' in str(caught.value)


def test_unrecognized_profile_does_not_inject_error_output(tmp_path, monkeypatch):
    adapter, _ = token_config(tmp_path, monkeypatch, {'exp': 1}, profile='unsafe;secret')
    with pytest.raises(DeployError) as caught:
        adapter._check_token()
    assert 'unsafe' not in str(caught.value)
    assert 'secret' not in str(caught.value)
