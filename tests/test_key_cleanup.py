import os

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.host import Host
from pocketdeploy.state import State


def fixture(tmp_path, config=None):
    state = State(tmp_path / '.colors.sqlite', 'test', {}, create=True)
    return Host(config or {}, state, tmp_path), state


def test_generated_authority_deleted_then_recreated(tmp_path):
    host, state = fixture(tmp_path)
    with state:
        host.prepare_keys()
        before = host.pub.read_bytes()
        assert len(host.plan_key_cleanup()[0]['files']) == 5
        assert host.cleanup_keys() == {'deleted_key_files': 5}
        assert host.cleanup_keys() == {'deleted_key_files': 0}
        assert host.plan_key_cleanup() == [{'resource': 'ssh-key-files', 'action': 'absent', 'files': []}]
        assert state.path.exists()
        host.prepare_keys()
        assert host.pub.read_bytes() != before
        assert len(state.get_meta('ssh-generated-files')) == 5


def test_unrecorded_files_never_adopted_or_deleted(tmp_path):
    host, state = fixture(tmp_path)
    with state:
        host.prepare_keys()
        state.set_meta('ssh-generated-files', {})
        host.prepare_keys()
        with pytest.raises(DeployError, match='ownership is unrecorded'):
            host.cleanup_keys()
        assert all(p.exists() for p in (host.key, host.pub, host.hostkey, host.hostpub, host.known))


def test_changed_keys_block_before_any_unlink(tmp_path):
    host, state = fixture(tmp_path)
    with state:
        host.prepare_keys()
        host.hostpub.write_text('replacement')
        with pytest.raises(DeployError, match='contents changed'):
            host.cleanup_keys()
        assert host.key.exists()


def test_mutable_known_hosts_and_partial_cleanup_resume(tmp_path):
    host, state = fixture(tmp_path)
    with state:
        host.prepare_keys()
        host.known.write_text('192.0.2.1 synthetic-trust')
        host.key.unlink()
        assert host.cleanup_keys()['deleted_key_files'] == 4


@pytest.mark.parametrize('attack', ['symlink', 'hardlink', 'state-overlap', 'duplicate', 'github-overlap'])
def test_unsafe_cleanup_paths_preserve_keys(tmp_path, attack):
    host, state = fixture(tmp_path)
    with state:
        host.prepare_keys()
        if attack == 'symlink':
            host.known.unlink()
            host.known.symlink_to(host.pub)
        elif attack == 'hardlink':
            os.link(host.pub, tmp_path / 'extra-link')
        elif attack == 'state-overlap':
            host.known = state.path
        elif attack == 'duplicate':
            host.known = host.pub
        else:
            import hashlib
            path = '.ssh/github-' + hashlib.sha256(b'example/site').hexdigest()[:20]
            state.set_meta('github-delete-keys', ['example/site'])
            host.known = tmp_path / path
        with pytest.raises(DeployError):
            host.cleanup_keys()
        assert host.key.exists()
