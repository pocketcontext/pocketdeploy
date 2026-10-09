"""Versioned recovery sets through VaultContext; never execute restored files."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .common import DeployError, local_path, run

MAX_FILE_SIZE = 8 * 1024 * 1024


def _command(config, *args):
    output = run([config.get('vault-command', 'vaultcontext'), *map(str, args)], timeout=300)
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        raise DeployError('Vault returned invalid metadata; output suppressed.') from None
    if not isinstance(value, dict):
        raise DeployError('Vault returned invalid metadata; output suppressed.')
    return value


def _save_file(config, path, name, document=None):
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise DeployError('Recovery file is missing or exceeds the Vault size limit.')
    args = ['save', config['vault-id'], str(path), '--name', name]
    if document:
        args.extend(['--document', document])
    result = _command(config, *args)
    if not isinstance(result.get('id'), str) or not isinstance(result.get('version'), str):
        raise DeployError('Vault save did not return document and version identifiers.')
    return {'document': result['id'], 'version': result['version']}


def _files(config):
    names = ['colors.yml', '.envrc.private',
            config.get('ssh-private-key-file', '.ssh/id_ed25519'),
            config.get('ssh-public-key-file', '.ssh/id_ed25519.pub'),
            config.get('ssh-known-hosts-file', '.ssh/known_hosts'),
            config.get('ssh-host-private-key-file', '.ssh/host_ed25519'),
            config.get('ssh-host-public-key-file', '.ssh/host_ed25519.pub')]
    if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts for name in names):
        raise DeployError('Recovery file configuration must use relative deployment paths.')
    if len({Path(name) for name in names}) != len(names):
        raise DeployError('Recovery file paths must be distinct.')
    return names


def save(config, state, root):
    if not config.get('vault-id'):
        raise DeployError('Vault is not configured.')
    root = Path(root).resolve()
    previous = state.get_meta('vault-files', {})
    refs = {}
    # Validate every source before performing any upload. Files are opaque bytes.
    paths = [(str(name), local_path(root, config.get('_file', name) if name == 'colors.yml' else name))
             for name in _files(config)]
    if any(not path.is_file() or path.stat().st_size > MAX_FILE_SIZE for _, path in paths):
        raise DeployError('Recovery file is missing or exceeds the Vault size limit.')
    for name, path in paths:
        ref = _save_file(config, path, config['profile'] + '-' + name.replace('/', '-'),
                         previous.get(name, {}).get('document'))
        refs[name] = ref
        # Persist acknowledged document IDs even if a later upload fails.
        state.set_meta('vault-files', {**previous, **refs})
    state.set_meta('vault-files', refs)
    state_document = config.get('vault-state-document-id') or state.get_meta('vault-state-document', {}).get('document')
    with tempfile.TemporaryDirectory(prefix='.vault-save-', dir=root) as directory:
        snapshot = Path(directory) / 'state.sqlite'
        state.snapshot(snapshot)
        result = _save_file(config, snapshot, config['profile'] + '-state', state_document)
    # The published snapshot deliberately does not reference its own new version.
    state.set_meta('vault-state-document', result)
    return {'state_document': result['document'], 'state_version': result['version'], 'file_count': len(refs)}


def restore(config, root, document, version, overwrite=False):
    """Stage and validate a full recovery set before publishing local files.

    The database is application state: this code reads its fixed recovery manifest,
    never exposes file contents, and never invokes a recovered configuration.
    """
    root = Path(root).resolve()
    destination = local_path(root, config.get('state-file', '.colors.sqlite'))
    if not document or not version:
        raise DeployError('Restore requires explicit state document and version identifiers.')
    with tempfile.TemporaryDirectory(prefix='.vault-restore-', dir=root) as directory:
        staged_db = Path(directory) / 'state.sqlite'
        _command(config, 'restore', document, '--version', version, '--to', staged_db)
        try:
            db = sqlite3.connect(f'{staged_db.as_uri()}?mode=ro', uri=True)
            db.execute('PRAGMA trusted_schema=OFF')
            try:
                identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
                refs = json.loads(db.execute("SELECT value FROM meta WHERE key='vault-files'").fetchone()[0])
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError()
            finally:
                db.close()
            expected_scope = {'provider': 'oci', 'profile': config['oci-config-file-profile'],
                              'region': config.get('oci-region', '') or '',
                              'compartment': config['oci-compartment-id'], 'subnet': config['oci-subnet-id']}
            if (identity.get('schema') != 1 or identity.get('profile') != config['profile']
                    or identity.get('scope') != expected_scope):
                raise ValueError()
            if not isinstance(refs, dict) or set(refs) != set(_files(config)):
                raise ValueError()
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise DeployError('Recovery state has an invalid or incompatible manifest.') from None
        destinations = [(name, local_path(root, name)) for name in refs]
        targets = [destination, *(path for _, path in destinations)]
        if len(set(targets)) != len(targets):
            raise DeployError('Recovery destinations must be distinct.')
        if any(path.exists() for path in targets) and not overwrite:
            raise DeployError('Recovery destination exists; explicit overwrite is required.')
        staged = []
        for index, (name, target) in enumerate(destinations):
            ref = refs[name]
            if not isinstance(ref, dict) or not isinstance(ref.get('document'), str) or not isinstance(ref.get('version'), str):
                raise DeployError('Recovery manifest contains invalid file references.')
            temp = Path(directory) / str(index)
            _command(config, 'restore', ref['document'], '--version', ref['version'], '--to', temp)
            staged.append((temp, target))
        # All downloads succeed before any destination changes. Database is last.
        # A filesystem crash during publication may require repeating restore.
        for source, target in [*staged, (staged_db, destination)]:
            local_path(root, target)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and not overwrite:
                raise DeployError('Recovery destination appeared during restoration.')
            os.chmod(source, 0o600)
            if overwrite:
                os.replace(source, target)
            else:
                os.link(source, target)
                source.unlink()
    return {'state_document': document, 'state_version': version, 'file_count': len(refs),
            'reconciliation_required': True}
