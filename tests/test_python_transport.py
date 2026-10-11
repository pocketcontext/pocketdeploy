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
