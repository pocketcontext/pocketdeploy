"""Real shell parsing of remote source; synthetic sudo, no SSH or privileges."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from pocketdeploy.transport import SSHTransport, python_command


@pytest.mark.parametrize('shell', ['bash', 'fish'])
def test_python_source_survives_remote_login_shell_and_private_stdin(tmp_path, shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(shell + ' is not installed')
    sudo = tmp_path / 'sudo'
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o700)
    # Nested generated Python containing literal backslashes reproduced the
    # live Fish failure. Include shell expansion characters and Unicode too.
    expected = "quote'\" backslash\\\\ newline\n snowman☃ $HOME $(false) `false`"
    nested = 'value = ' + repr(expected)
    source = ('import json, sys\n'
              'namespace = {}\n'
              'exec(' + repr(nested) + ', namespace)\n'
              'assert namespace["value"] == ' + repr(expected) + '\n'
              'payload = json.load(sys.stdin)\n'
              'print(json.dumps({"value": namespace["value"], "stdin_matches": sorted(payload) == ["secret"]}))\n')
    payload = {'secret': 'stdin-only-sentinel'}
    command = python_command(source)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}
    native_run = subprocess.run
    captured = []

    def fake_ssh(argv, **kwargs):
        captured.append((argv, kwargs))
        assert argv[-1] == command
        # SSH executes its command with the account's configured login shell.
        return native_run([executable, '-c', argv[-1]], env=env, **kwargs)

    transport = SSHTransport(('ssh', '-l', 'ubuntu', '--', '192.0.2.1'))
    with patch('pocketdeploy.transport.subprocess.run', side_effect=fake_ssh):
        result = transport.run_python({'ip': '192.0.2.1'}, source, payload)
    assert result == {'value': expected, 'stdin_matches': True}
    assert 'stdin-only-sentinel' not in source
    assert 'stdin-only-sentinel' not in repr(captured[0][0])
    assert json.loads(captured[0][1]['input']) == payload


def test_streaming_progress_is_live_allowlisted_and_output_free():
    import sys
    from pocketdeploy.transport import _stream_python
    events = []
    source = '''import json, sys
payload = json.load(sys.stdin)
assert payload['secret'] == 'PRIVATE'
for line in ['PRIVATE', 'x' * 20000, '{"stage": "PRIVATE", "event": "started"}',
             '{"stage": "install", "event": "started", "secret": "PRIVATE"}',
             '{"stage": [], "event": "started"}',
             '{"stage": "install", "event": "started"}',
             '{"stage": "install", "event": "completed"}']:
    print(line, file=sys.stderr, flush=True)
print('{"ok": true}')
'''
    result = _stream_python([sys.executable, '-c', source], {'secret': 'PRIVATE'}, 5,
                            lambda *event: events.append(event), {'install'})
    assert events == [('install', 'started'), ('install', 'completed')]
    assert json.loads(result.stdout) == {'ok': True}
    assert result.stderr == ''


def test_streaming_timeout_keeps_completed_progress_and_suppresses_output():
    import sys
    from pocketdeploy.transport import _stream_python
    events = []
    source = '''import sys, time
print('{"stage":"install","event":"started"}', file=sys.stderr, flush=True)
print('PRIVATE', file=sys.stderr, flush=True)
time.sleep(10)
'''
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        _stream_python([sys.executable, '-c', source], {}, .3,
                       lambda *event: events.append(event), {'install'})
    assert events == [('install', 'started')]
    assert 'PRIVATE' not in str(failure.value)


def test_remote_progress_unwinds_failed_and_interrupted_spans(capsys):
    from pocketdeploy.output import Reporter, remote_progress
    reporter = Reporter(verbose=True)
    with reporter.activate(), remote_progress({'install'}) as progress:
        progress('PRIVATE', 'started')
        progress('install', 'started')
        progress('install', 'failed')
    assert reporter.failed_stage == 'install'
    assert reporter.current_stage is None
    assert not reporter._active_spans
    reporter = Reporter(verbose=True)
    with pytest.raises(KeyboardInterrupt):
        with reporter.activate(), remote_progress({'install'}) as progress:
            progress('install', 'started')
            raise KeyboardInterrupt
    assert reporter.failed_stage == 'install'
    assert not reporter._active_spans
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'PRIVATE' not in captured.err


def test_streaming_response_size_is_bounded():
    import sys
    from pocketdeploy.common import DeployError
    from pocketdeploy.transport import _stream_python
    with pytest.raises(DeployError) as failure:
        _stream_python([sys.executable, '-c', "import sys; sys.stdout.write('PRIVATE' * 1600000)"],
                       {}, 5, lambda *event: None, {'install'})
    assert failure.value.diagnostic['category'] == 'invalid_response'
    assert 'PRIVATE' not in str(failure.value)


def test_streaming_callback_error_terminates_child():
    import sys
    from pocketdeploy.transport import _stream_python
    source = '''import sys, time
print('{"stage":"install","event":"started"}', file=sys.stderr, flush=True)
time.sleep(10)
'''
    def interrupted(*event):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _stream_python([sys.executable, '-c', source], {}, 5, interrupted, {'install'})


def test_progress_arrives_before_process_exit_on_calling_thread(tmp_path):
    import sys
    import threading
    from pocketdeploy.transport import _stream_python
    acknowledged = tmp_path / 'acknowledged'
    source = '''import json, sys, time
from pathlib import Path
path = Path(json.load(sys.stdin)['acknowledged'])
print('{"stage":"install","event":"started"}', file=sys.stderr, flush=True)
while not path.exists():
    time.sleep(.01)
print('{"ok":true}')
'''
    caller = threading.get_ident()
    def progress(stage, event):
        assert threading.get_ident() == caller
        acknowledged.touch()
    result = _stream_python([sys.executable, '-c', source], {'acknowledged': str(acknowledged)},
                            5, progress, {'install'})
    assert result.returncode == 0
