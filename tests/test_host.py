import json
from types import SimpleNamespace
from unittest.mock import patch

from pocketdeploy.host import Host


def test_host_key_is_anchored_and_payload_not_in_args(tmp_path):
    host = Host({}, SimpleNamespace(deployment_id='test'), tmp_path)
    public = host.prepare_keys()
    assert public.startswith('ssh-ed25519 ')
    cloud = json.loads(host.cloud_init().split('\n', 1)[1])
    assert cloud['ssh_keys']['ed25519_public'] == host.hostpub.read_text().strip()
    assert host.hostkey.stat().st_mode & 0o777 == 0o600
    argv = host._argv({'ip': '192.0.2.1', 'user': 'ubuntu'})
    assert 'StrictHostKeyChecking=yes' in argv
    assert '192.0.2.1 ssh-ed25519 ' in host.known.read_text()


def test_secret_only_in_stdin(tmp_path):
    host = Host({'once': {'applications': [{'resolved-env': {'SECRET': 'synthetic-value'}}]}},
                SimpleNamespace(deployment_id='test'), tmp_path)
    host.prepare_keys()
    with patch('pocketdeploy.host.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as call:
        host._remote({'ip': '192.0.2.1'}, 'plan')
    assert 'synthetic-value' not in repr(call.call_args.args)
    assert 'synthetic-value' in call.call_args.kwargs['input']
