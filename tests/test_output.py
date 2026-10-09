import json

import pytest

from pocketdeploy.output import Reporter, text_result


def test_json_success_is_one_document_and_progress_is_stderr(capsys):
    reporter = Reporter(json_mode=True, command='plan')
    with reporter.stage('compute'):
        pass
    reporter.success({'actions': []}, 1.23456)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'schema_version': 1, 'command': 'plan', 'ok': True,
                                        'result': {'actions': []}, 'elapsed_seconds': 1.235}
    assert len(captured.out.splitlines()) == 1
    assert 'compute: started' in captured.err
    assert 'compute: completed (' in captured.err


def test_quiet_json_failure_retains_inner_stage(capsys):
    reporter = Reporter(json_mode=True, quiet=True, command='converge')
    with pytest.raises(RuntimeError):
        with reporter.stage('workflow'):
            with reporter.stage('compute'):
                raise RuntimeError('synthetic')
    assert reporter.current_stage is None
    reporter.failure('Safe error.', stage=reporter.failed_stage)
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error'] == {'code': 'operation_failed', 'stage': 'compute', 'message': 'Safe error.'}


def test_text_failure_uses_stderr_even_when_quiet(capsys):
    Reporter(quiet=True, command='plan').failure('Safe error.')
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'pocketdeploy: Safe error.\n'


def test_text_plan_reports_blocked_changes_and_protection():
    lines = text_result('delete', {'profile': 'synthetic', 'protected': True,
                                   'actions': [{'resource': 'compute', 'action': 'blocked', 'changed_fields': ['oci-shape']}]})
    assert '  compute: blocked (oci-shape)' in lines
    assert 'Deletion protection: enabled.' in lines
    assert 'No changes planned.' not in lines


def test_status_reports_unhealthy_and_does_not_dump_unknown_fields():
    lines = text_result('status', {'profile': 'synthetic', 'resources': {'compute': None},
                                  'state': {'pending_steps': 2, 'secret': 'must-not-print'},
                                  'applications': {'applications': [{'host': 'example.test', 'running': True, 'healthy': False}], 'pending_operations': 1}})
    output = '\n'.join(lines)
    assert 'running, unhealthy' in output
    assert 'Pending recorded steps: 2.' in output
    assert 'Pending host operations: 1.' in output
    assert 'must-not-print' not in output


@pytest.mark.parametrize(('command', 'result', 'expected'), [
    ('init', {'deployment_id': 'uuid'}, 'Initialized'),
    ('converge', {'applications': {'actions': []}, 'status': {}}, 'No application changes'),
    ('create', {'applications': {'actions': [{'host': 'example.test', 'action': 'create'}]}}, 'example.test: create'),
    ('adopt', {'instance_id': 'instance', 'ip': '192.0.2.1'}, 'Adopted instance'),
    ('delete', {'deleted': True}, 'Deletion completed'),
    ('vault-save', {'file_count': 7, 'state_document': 'document', 'state_version': 'version'}, 'Saved recovery checkpoint'),
    ('vault-restore', {'file_count': 7, 'state_document': 'document', 'state_version': 'version', 'reconciliation_required': True}, 'Run plan'),
])
def test_command_summaries(command, result, expected):
    assert expected in '\n'.join(text_result(command, result))


@pytest.mark.parametrize('verbose', [False, True])
def test_operation_only_verbose_and_reporter_scope_resets(capsys, verbose):
    from pocketdeploy.output import operation
    reporter = Reporter(json_mode=True, verbose=verbose, command='plan')
    with reporter.activate():
        with operation('oci: inspect'):
            pass
    with operation('outside scope'):
        pass
    reporter.success({'actions': []})
    output = capsys.readouterr()
    assert json.loads(output.out)['ok']
    assert ('oci: inspect: completed' in output.err) == verbose
    assert 'outside scope' not in output.err


@pytest.mark.parametrize('failure', [None, RuntimeError('PRIVATE_SENTINEL'), KeyboardInterrupt()])
def test_verbose_waiting_and_worker_cleanup(capsys, failure):
    import threading
    from pocketdeploy.output import operation
    reporter = Reporter(verbose=True, heartbeat_interval=0.005)
    waiting = threading.Event()
    original = reporter.progress
    def progress(message):
        original(message)
        if 'still waiting' in message:
            waiting.set()
    reporter.progress = progress
    def work():
        with reporter.activate(), reporter.stage('workflow'), operation('ssh: inspect'):
            assert waiting.wait(2), 'Heartbeat was not emitted'
            if failure:
                raise failure
    if failure:
        with pytest.raises(type(failure)):
            work()
    else:
        work()
    output = capsys.readouterr()
    assert not output.out
    assert 'ssh: inspect: still waiting' in output.err
    assert 'PRIVATE_SENTINEL' not in output.err
    assert reporter.current_stage is None
    assert reporter._active_spans == []
    assert not [thread for thread in threading.enumerate() if thread.name == 'pocketdeploy-progress']
    assert output.err.splitlines()[-1].startswith('workflow: failed' if failure else 'workflow: completed')
