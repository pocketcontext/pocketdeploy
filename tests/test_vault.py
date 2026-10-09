import json
import shutil
from pathlib import Path

import pytest

from pocketdeploy import vault
from pocketdeploy.common import DeployError
from pocketdeploy.state import State


def setup_files(root):
    root.mkdir(exist_ok=True)
    config = {'profile': 'synthetic', 'vault-id': 'synthetic-vault',
              'oci-config-file-profile': 'test', 'oci-compartment-id': 'test', 'oci-subnet-id': 'test'}
    for name in vault._files(config):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('synthetic private data')
    return config


def scope():
    return {'provider': 'oci', 'profile': 'test', 'region': '', 'compartment': 'test', 'subnet': 'test'}


def test_versioned_roundtrip(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    config = setup_files(root)
    storage = {}
    calls = []

    def command(config, *args):
        args = list(map(str, args))
        calls.append(args)
        if args[0] == 'save':
            document = args[args.index('--document') + 1] if '--document' in args else 'doc-' + str(len(storage))
            version = 'ver-' + str(len(storage))
            storage[document, version] = Path(args[2]).read_bytes()
            return {'id': document, 'version': version}
        assert args[0] == 'restore'
        Path(args[args.index('--to') + 1]).write_bytes(storage[args[1], args[args.index('--version') + 1]])
        return {'restored': True}

    monkeypatch.setattr(vault, '_command', command)
    with State(root / '.colors.sqlite', 'synthetic', scope(), create=True) as state:
        state.set_meta('private', 'synthetic-secret')
        first = vault.save(config, state, root)
        second = vault.save(config, state, root)
        assert first['state_document'] == second['state_document']
        assert all('--document' in call for call in calls[8:16])
        old_ref = state.get_meta('vault-files')['.envrc.private']
        (root / '.envrc.private').write_text('newer unrelated credential generation')
        vault._save_file(config, root / '.envrc.private', 'test', old_ref['document'])
    restored = tmp_path / 'restored'
    restored.mkdir()
    result = vault.restore(config, restored, second['state_document'], second['state_version'])
    assert result['reconciliation_required']
    with State(restored / '.colors.sqlite', 'synthetic', scope()) as state:
        assert state.get_meta('private') == 'synthetic-secret'
        assert len(state.get_meta('vault-files')) == 7
    assert (restored / '.envrc.private').read_text() == 'synthetic private data'
    assert (restored / '.envrc.private').stat().st_mode & 0o777 == 0o600
    with pytest.raises(DeployError, match='overwrite'):
        vault.restore(config, restored, second['state_document'], second['state_version'])


def test_manifest_cannot_restore_arbitrary_paths(tmp_path, monkeypatch):
    config = setup_files(tmp_path / 'source')
    with State(tmp_path / 'bad.sqlite', 'synthetic', scope(), create=True) as state:
        state.set_meta('vault-files', {'../escape': {'document': 'doc', 'version': 'ver'}})
    def command(config, *args):
        shutil.copyfile(tmp_path / 'bad.sqlite', args[args.index('--to') + 1])
        return {}
    monkeypatch.setattr(vault, '_command', command)
    dest = tmp_path / 'dest'
    dest.mkdir()
    with pytest.raises(DeployError, match='manifest'):
        vault.restore(config, dest, 'doc', 'ver')
    assert not (tmp_path / 'escape').exists()


def test_command_failure_output_suppressed(monkeypatch):
    monkeypatch.setattr(vault, 'run', lambda *args, **kw: 'private invalid response')
    with pytest.raises(DeployError) as error:
        vault._command({}, 'status')
    assert 'private invalid response' not in str(error.value)


def test_failed_download_leaves_destinations_untouched(tmp_path, monkeypatch):
    config = setup_files(tmp_path / 'source')
    refs = {name: {'document': 'doc', 'version': 'version'} for name in vault._files(config)}
    with State(tmp_path / 'state', 'synthetic', scope(), create=True) as state:
        state.set_meta('vault-files', refs)
    count = 0
    def command(config, *args):
        nonlocal count
        count += 1
        target = Path(args[args.index('--to') + 1])
        if count == 1:
            shutil.copyfile(tmp_path / 'state', target)
        elif count == 2:
            target.write_text('synthetic')
        else:
            raise DeployError('Synthetic failure.')
        return {}
    monkeypatch.setattr(vault, '_command', command)
    dest = tmp_path / 'dest'
    dest.mkdir()
    sentinel = dest / 'colors.yml'
    sentinel.write_text('existing synthetic config')
    with pytest.raises(DeployError, match='Synthetic failure'):
        vault.restore(config, dest, 'doc', 'version', overwrite=True)
    assert sentinel.read_text() == 'existing synthetic config'
    assert set(p.name for p in dest.iterdir()) == {'colors.yml'}


def test_recovery_paths_are_portable_distinct_and_use_configured_host_keys():
    config = {'ssh-host-private-key-file': '.keys/host', 'ssh-host-public-key-file': '.keys/host.pub'}
    assert '.keys/host' in vault._files(config)
    assert '.keys/host.pub' in vault._files(config)
    for bad in ('/tmp/key', '../key'):
        with pytest.raises(DeployError, match='relative'):
            vault._files({'ssh-private-key-file': bad})
    with pytest.raises(DeployError, match='distinct'):
        vault._files({'ssh-private-key-file': '.envrc.private'})
    with pytest.raises(DeployError, match='distinct'):
        vault._files({'ssh-private-key-file': './.envrc.private'})
