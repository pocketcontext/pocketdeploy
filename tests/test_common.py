import subprocess
from types import SimpleNamespace

import pytest

from pocketdeploy import common
from pocketdeploy.common import DeployError


@pytest.mark.parametrize(('failure', 'code'), [
    (subprocess.TimeoutExpired(['tool', 'private-argument'], 1, output='private-output', stderr='private-stderr'), 'command_timeout'),
    (FileNotFoundError('private-path'), 'command_unavailable'),
])
def test_subprocess_errors_are_structured_without_private_details(monkeypatch, failure, code):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(common.subprocess, 'run', fail)
    with pytest.raises(DeployError) as caught:
        common.run(['tool', 'private-argument'])
    assert caught.value.code == code
    assert 'private' not in str(caught.value)


def test_failed_command_does_not_expose_captured_output(monkeypatch):
    monkeypatch.setattr(common.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=23, stdout='private-output', stderr='private-stderr'))
    with pytest.raises(DeployError) as caught:
        common.run(['tool', 'private-argument'])
    assert caught.value.code == 'command_failed'
    assert 'private' not in str(caught.value)
