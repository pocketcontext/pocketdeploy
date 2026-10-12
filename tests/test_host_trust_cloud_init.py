import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketdeploy import host_trust_remote as remote

COMMAND = ('cloud-init', 'status', '--wait', '--format', 'json')
GOOD = {'status': 'done', 'errors': [], 'recoverable_errors': {'DEPRECATED': ['synthetic private notice']}}


def test_completed_deprecations_allow_rotation_wait():
    with patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stdout=json.dumps(GOOD))):
        assert json.loads(remote.run(*COMMAND)) == GOOD


@pytest.mark.parametrize('code,status', [
    (1, GOOD), (3, GOOD),
    (2, {**GOOD, 'status': 'running'}),
    (2, {**GOOD, 'status': 'error'}),
    (2, {**GOOD, 'errors': ['synthetic secret failure']}),
    (2, {**GOOD, 'errors': None}),
    (2, {**GOOD, 'recoverable_errors': {'WARNING': ['failure']}}),
    (2, {**GOOD, 'recoverable_errors': {'DEPRECATED': ['notice'], 'WARNING': ['failure']}}),
    (2, {**GOOD, 'recoverable_errors': {}}),
    (2, {**GOOD, 'recoverable_errors': {'DEPRECATED': []}}),
    (2, {**GOOD, 'recoverable_errors': {'DEPRECATED': 'notice'}}),
    (2, {**GOOD, 'recoverable_errors': {'DEPRECATED': [None]}}),
    (2, {}), (2, []), (2, None),
])
def test_cloud_init_failure_remains_private(code, status):
    with patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=json.dumps(status))):
        with pytest.raises(RuntimeError, match='^host trust setup failed$'):
            remote.run(*COMMAND)


def test_invalid_json_and_other_commands_never_allow_exit_two():
    for argv, output in [(COMMAND, 'synthetic private malformed output'),
                         (('systemctl', 'reload', 'ssh'), json.dumps(GOOD))]:
        with patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stdout=output)):
            with pytest.raises(RuntimeError, match='^host trust setup failed$'):
                remote.run(*argv)
