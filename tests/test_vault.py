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
              '_root': str(root), '_file': str(root / 'colors.yml'),
              'oci-config-file-profile': 'test', 'oci-compartment-id': 'test', 'oci-subnet-id': 'test'}
    (root / 'colors.yml').write_text('synthetic public configuration')
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
        receipt = state.safe_status()['last_vault_backup']
        assert receipt['document'] == second['state_document']
        assert receipt['version'] == second['state_version']
        assert receipt['saved_at']
        assert first['state_document'] == second['state_document']
        assert all('--document' in call for call in calls[7:14])
        old_ref = state.get_meta('vault-files')['.envrc.private']
        (root / '.envrc.private').write_text('newer unrelated credential generation')
        vault._save_file(config, root / '.envrc.private', 'test', old_ref['document'])
    restored = tmp_path / 'restored'
    restored.mkdir()
    shutil.copyfile(root / 'colors.yml', restored / 'colors.yml')
    result = vault.restore(config, restored, second['state_document'], second['state_version'])
    assert result['reconciliation_required']
    with State(restored / '.colors.sqlite', 'synthetic', scope()) as state:
        assert state.get_meta('private') == 'synthetic-secret'
        assert len(state.get_meta('vault-files')) == 6
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
        state.set_meta('vault-config', vault._provenance(config, tmp_path / 'source'))
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
    sentinel.write_text('synthetic public configuration')
    with pytest.raises(DeployError, match='Synthetic failure'):
        vault.restore(config, dest, 'doc', 'version', overwrite=True)
    assert sentinel.read_text() == 'synthetic public configuration'
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


def stored_checkpoint(tmp_path, monkeypatch, *, legacy=False, github=False):
    root = tmp_path / 'source'
    config = setup_files(root)
    if github:
        config['once'] = {'applications': [{'github': 'synthetic/site'}]}
    names = vault._legacy_files(config) if legacy else vault._files(config)
    refs = {name: {'document': name, 'version': 'v1'} for name in names}
    snapshot = tmp_path / 'checkpoint.sqlite'
    with State(snapshot, 'synthetic', scope(), create=True) as state:
        state.set_meta('vault-files', refs)
        if not legacy:
            state.set_meta('vault-config', vault._provenance(config, root))
    calls = []

    def command(config, *args):
        calls.append(args[1])
        target = Path(args[args.index('--to') + 1])
        if args[1] == 'checkpoint':
            shutil.copyfile(snapshot, target)
        elif args[1] == 'colors.yml':
            target.write_text('synthetic public configuration')
        else:
            assert not args[1].startswith('.ssh/github-')
            target.write_text('synthetic private data')
        return {}

    monkeypatch.setattr(vault, '_command', command)
    destination = tmp_path / 'destination'
    destination.mkdir()
    shutil.copyfile(root / 'colors.yml', destination / 'colors.yml')
    return config, destination, snapshot, calls


@pytest.mark.parametrize('legacy', [False, True])
def test_restore_requires_matching_git_configuration_before_private_downloads(tmp_path, monkeypatch, legacy):
    config, destination, _, calls = stored_checkpoint(tmp_path, monkeypatch, legacy=legacy)
    (destination / 'colors.yml').write_text('different config')
    with pytest.raises(DeployError, match='differs'):
        vault.restore(config, destination, 'checkpoint', 'v1', overwrite=True)
    assert calls == (['checkpoint', 'colors.yml'] if legacy else ['checkpoint'])
    assert (destination / 'colors.yml').read_text() == 'different config'
    assert not (destination / '.colors.sqlite').exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_restore_requires_configuration_checkout_and_matching_caller(tmp_path, monkeypatch, legacy):
    config, destination, _, _ = stored_checkpoint(tmp_path, monkeypatch, legacy=legacy)
    (destination / 'colors.yml').unlink()
    with pytest.raises(DeployError, match='Git'):
        vault.restore(config, destination, 'checkpoint', 'v1')
    (destination / 'colors.yml').write_text('synthetic public configuration')
    Path(config['_file']).write_text('different selected config')
    with pytest.raises(DeployError, match='differs'):
        vault.restore(config, destination, 'checkpoint', 'v1')


@pytest.mark.parametrize('github', [False, True])
def test_legacy_restore_preserves_git_config_and_skips_disposable_keys(tmp_path, monkeypatch, github):
    config, destination, _, calls = stored_checkpoint(tmp_path, monkeypatch, legacy=True, github=github)
    original = (destination / 'colors.yml').stat().st_ino
    result = vault.restore(config, destination, 'checkpoint', 'v1')
    assert result['legacy_manifest'] is True
    assert result['file_count'] == 6
    assert (destination / 'colors.yml').stat().st_ino == original
    assert not list((destination / '.ssh').glob('github-*'))
    assert len(calls) == 8


def test_legacy_manifest_rejects_partial_key_pair(tmp_path, monkeypatch):
    config, destination, snapshot, _ = stored_checkpoint(tmp_path, monkeypatch, legacy=True, github=True)
    with State(snapshot, 'synthetic', scope()) as state:
        refs = state.get_meta('vault-files')
        del refs[next(name for name in refs if name.startswith('.ssh/github-') and name.endswith('.pub'))]
        state.set_meta('vault-files', refs)
    with pytest.raises(DeployError, match='manifest'):
        vault.restore(config, destination, 'checkpoint', 'v1')


def test_new_snapshot_does_not_require_or_save_github_keys(tmp_path, monkeypatch):
    config = setup_files(tmp_path)
    config['once'] = {'applications': [{'github': 'synthetic/site'}]}
    uploaded = []

    def command(config, *args):
        uploaded.append(Path(args[2]).name)
        return {'id': 'document-' + str(len(uploaded)), 'version': 'v1'}

    monkeypatch.setattr(vault, '_command', command)
    with State(tmp_path / '.colors.sqlite', 'synthetic', scope(), create=True) as state:
        state.set_meta('vault-files', {'colors.yml': {'document': 'old', 'version': 'v1'}})
        vault.save(config, state, tmp_path)
        assert set(state.get_meta('vault-files')) == set(vault._files(config))
        assert state.get_meta('vault-config')['sha256'] == vault._config_hash(tmp_path / 'colors.yml')
    assert 'colors.yml' not in uploaded
    assert not any(name.startswith('github-') for name in uploaded)
    assert len(uploaded) == 7


def test_config_provenance_records_dirty_content_without_falsely_attributing_it(tmp_path):
    import subprocess
    config = setup_files(tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'colors.yml'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Synthetic',
                    '-c', 'user.email=synthetic@example.com', 'commit', '-qm', 'fixture'], check=True)
    initial = vault._provenance(config, tmp_path)
    assert len(initial['git_commit']) == 40
    assert initial['git_config_matches'] is True
    (tmp_path / 'colors.yml').write_text('changed configuration')
    changed = vault._provenance(config, tmp_path)
    assert changed['git_commit'] == initial['git_commit']
    assert changed['git_config_matches'] is False
    assert changed['sha256'] != initial['sha256']


@pytest.mark.parametrize('name', ['colors.yml', './colors.yml', '.envrc', '.colors.sqlite',
                                  '.ssh/github-01234567890123456789',
                                  '.ssh/github-01234567890123456789.pub', 'custom.yml'])
def test_backup_paths_cannot_alias_excluded_files(tmp_path, name):
    config = setup_files(tmp_path)
    config['_file'] = str(tmp_path / 'custom.yml')
    config['ssh-private-key-file'] = name
    with pytest.raises(DeployError, match='must not include'):
        vault._files(config)
