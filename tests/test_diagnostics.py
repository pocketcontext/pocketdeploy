import json
import subprocess
from pathlib import Path

import pytest

from pocketdeploy.common import DeployError, run
from pocketdeploy.diagnostics import build_diagnostic, validate_diagnostic
from pocketdeploy.output import Reporter


@pytest.mark.parametrize('patch', [
    {'stderr': 'PRIVATE'}, {'operation': 'PRIVATE\n'}, {'category': 'PRIVATE'},
    {'exit_code': True}, {'exit_code': 999}, {'signal': -1},
    {'elapsed_seconds': float('nan')}, {'elapsed_seconds': float('inf')},
    {'elapsed_seconds': True}, {'http_status': '401'}, {'task': ['PRIVATE']},
])
def test_reject_malformed(patch):
    assert validate_diagnostic({'operation': 'command', 'category': 'unknown', **patch}) is None


def test_remote_allowlists():
    value = build_diagnostic('command', task='setup', user='ubuntu')
    assert validate_diagnostic(value, {'command'}) is None
    assert validate_diagnostic(value, {'command'}, allowed_tasks={'setup'}, allowed_users={'ubuntu'}) == value
    assert validate_diagnostic(value, {'different'}) is None


@pytest.mark.parametrize('failure,category,extra', [
    (subprocess.CompletedProcess([], 7, 'PRIVATE_OUT', 'PRIVATE_ERR'), 'nonzero_exit', {'exit_code': 7}),
    (subprocess.CompletedProcess([], -15, 'PRIVATE_OUT', 'PRIVATE_ERR'), 'signal', {'signal': 15}),
    (subprocess.TimeoutExpired('PRIVATE_ARG', 1, output='PRIVATE_OUT'), 'timeout', {}),
    (OSError('PRIVATE_ERR'), 'unavailable', {}),
])
def test_command_evidence_without_output(monkeypatch, capsys, failure, category, extra):
    def fake(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure
    monkeypatch.setattr(subprocess, 'run', fake)
    with pytest.raises(DeployError) as caught:
        run(['PRIVATE_ARG'])
    diagnostic = caught.value.diagnostic
    assert diagnostic['category'] == category
    assert all(diagnostic[key] == value for key, value in extra.items())
    Reporter(json_mode=True).failure(str(caught.value), diagnostic=diagnostic)
    captured = capsys.readouterr()
    assert 'PRIVATE' not in captured.out + captured.err
    assert json.loads(captured.out)['error']['diagnostic'] == diagnostic


def test_reporter_drops_unknown_fields(capsys):
    Reporter(json_mode=True).failure('Failed.', diagnostic={'operation': 'command', 'category': 'unknown', 'stderr': 'PRIVATE'})
    assert 'diagnostic' not in json.loads(capsys.readouterr().out)['error']


def test_standalone_source():
    import pocketdeploy.diagnostics as module
    namespace = {}
    exec(Path(module.__file__).read_text(), namespace)
    assert namespace['build_diagnostic']('install', returncode=-9)['signal'] == 9


def test_provider_classifier_retains_retry_policy(monkeypatch):
    classified = DeployError('Provider unavailable.', code='provider_unavailable', retryable=True, retry_after=5)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '', 'PRIVATE'))
    with pytest.raises(DeployError) as caught:
        run(['provider'], error_classifier=lambda private: classified)
    assert caught.value is classified
    assert classified.retryable and classified.retry_after == 5
    assert classified.diagnostic['exit_code'] == 1


def test_remote_runner_and_envelope(monkeypatch):
    from pocketdeploy import remote
    monkeypatch.setattr(remote, 'STAGE', 'docker-install')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess([], 42, 'PRIVATE', 'PRIVATE'))
    with pytest.raises(RuntimeError) as caught:
        remote.run('PRIVATE_ARGUMENT')
    assert caught.value.diagnostic['operation'] == 'docker-install'
    assert caught.value.diagnostic['exit_code'] == 42
    assert 'PRIVATE' not in str(caught.value)


def test_transport_timeout_private(monkeypatch):
    from pocketdeploy.transport import SSHTransport
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('PRIVATE', 1, output='PRIVATE', stderr='PRIVATE')
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(DeployError) as caught:
        SSHTransport(('ssh',)).python_process('PRIVATE', {'secret': 'PRIVATE'})
    assert caught.value.diagnostic['category'] == 'timeout'
    assert caught.value.diagnostic['operation'] == 'ssh-python'
    assert 'PRIVATE' not in str(caught.value)


def test_provider_category_and_status(monkeypatch):
    from pocketdeploy.services import _provider_error
    private = json.dumps({'error': {'statusCode': 401, 'message': 'PRIVATE'}})
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '', private))
    with pytest.raises(DeployError) as caught:
        run(['provider'], error_classifier=lambda text: _provider_error('resend', text))
    assert caught.value.diagnostic['category'] == 'authentication'
    assert caught.value.diagnostic['http_status'] == 401
    assert not caught.value.retryable


def test_failed_operation_evidence_survives_retry(tmp_path):
    from pocketdeploy.state import State
    from pocketdeploy.runtime import recorded_operation
    evidence = build_diagnostic('install', 'nonzero_exit', returncode=17)
    with State(tmp_path / 'state', 'test', {}, create=True) as state:
        with pytest.raises(DeployError):
            with recorded_operation(state, 'converge', 'test') as failed:
                raise DeployError('Failed.', diagnostic=evidence)
        with recorded_operation(state, 'converge', 'test'):
            pass
        operations = state.safe_status()['operations']
        assert operations[0]['status'] == 'succeeded'
        assert 'diagnostic' not in operations[0]
        assert operations[1]['id'] == failed
        assert operations[1]['diagnostic'] == evidence
        state.record_diagnostic(failed, {**evidence, 'stderr': 'PRIVATE'})
        assert state.safe_status()['operations'][1]['diagnostic'] == evidence
