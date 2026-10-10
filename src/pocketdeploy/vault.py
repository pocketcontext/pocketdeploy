"""Versioned recovery sets through VaultContext; never execute restored files."""
import json
import hashlib
import os
import sqlite3
import subprocess
import re
import tempfile
from datetime import datetime, timezone
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
    private_key = config.get('ssh-private-key-file', '.ssh/id_ed25519')
    host_private_key = config.get('ssh-host-private-key-file', '.ssh/host_ed25519')
    names = ['.envrc.private',
            private_key,
            config.get('ssh-public-key-file', f'{private_key}.pub'),
            config.get('ssh-known-hosts-file', '.ssh/known_hosts'),
            host_private_key,
            config.get('ssh-host-public-key-file', f'{host_private_key}.pub')]
    if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts for name in names):
        raise DeployError('Recovery file configuration must use relative deployment paths.')
    reserved = {Path('colors.yml'), Path('.envrc'), Path(config.get('state-file', '.colors.sqlite'))}
    selected = Path(config.get('_file', 'colors.yml'))
    if selected.is_absolute():
        try:
            selected = selected.relative_to(Path(config.get('_root', selected.parent)))
        except ValueError:
            raise DeployError('Configuration must stay inside the deployment directory.') from None
    reserved.add(selected)
    if (reserved.intersection(Path(name) for name in names)
            or any(Path(name).as_posix().startswith('.ssh/github-') for name in names)):
        raise DeployError('Recovery file paths must not include Git configuration, state or disposable GitHub keys.')
    if len({Path(name) for name in names}) != len(names):
        raise DeployError('Recovery file paths must be distinct.')
    return names


def _legacy_files(config):
    names = ['colors.yml', *_files(config)]
    for app in config.get('once', {}).get('applications', []):
        if app.get('github'):
            token = hashlib.sha256(app['github'].lower().encode()).hexdigest()[:20]
            names.extend(['.ssh/github-' + token, '.ssh/github-' + token + '.pub'])
    return names


def _config_hash(path):
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise DeployError('Restore matching configuration from Git before saving or restoring private state.')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _provenance(config, root):
    path = local_path(root, config.get('_file', 'colors.yml'))
    result = {'schema': 1, 'sha256': _config_hash(path), 'git_commit': None, 'git_config_matches': None}
    try:
        completed = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                                   capture_output=True, text=True, timeout=10)
        commit = completed.stdout.strip()
        if completed.returncode == 0 and re.fullmatch(r'[0-9a-f]{40,64}', commit):
            result['git_commit'] = commit
            tracked = subprocess.run(['git', '-C', str(root), 'show',
                                      commit + ':./' + path.relative_to(root).as_posix()],
                                     capture_output=True, timeout=10)
            result['git_config_matches'] = (tracked.returncode == 0 and
                hashlib.sha256(tracked.stdout).hexdigest() == result['sha256'])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def _check_configuration(config, root, expected_hash):
    caller_root = Path(config.get('_root', root)).resolve()
    caller = local_path(caller_root, config.get('_file', 'colors.yml'))
    destination = local_path(root, caller.relative_to(caller_root))
    if _config_hash(caller) != expected_hash or _config_hash(destination) != expected_hash:
        raise DeployError('Recovery configuration differs from the checkpoint; restore matching configuration from Git first.')
    return destination


def save(config, state, root):
    if not config.get('vault-id'):
        raise DeployError('Vault is not configured.')
    root = Path(root).resolve()
    provenance = _provenance(config, root)
    previous = state.get_meta('vault-files', {})
    refs = {}
    # Validate every source before performing any upload. Files are opaque bytes.
    paths = [(str(name), local_path(root, name)) for name in _files(config)]
    if any(not path.is_file() or path.stat().st_size > MAX_FILE_SIZE for _, path in paths):
        raise DeployError('Recovery file is missing or exceeds the Vault size limit.')
    for name, path in paths:
        ref = _save_file(config, path, config['profile'] + '-' + name.replace('/', '-'),
                         previous.get(name, {}).get('document'))
        refs[name] = ref
        # Persist acknowledged document IDs even if a later upload fails.
        state.set_meta('vault-files', {**previous, **refs})
    state.set_meta('vault-files', refs)
    state.set_meta('vault-config', provenance)
    state_document = config.get('vault-state-document-id') or state.get_meta('vault-state-document', {}).get('document')
    with tempfile.TemporaryDirectory(prefix='.vault-save-', dir=root) as directory:
        snapshot = Path(directory) / 'state.sqlite'
        state.snapshot(snapshot)
        result = _save_file(config, snapshot, config['profile'] + '-state', state_document)
    # The published snapshot deliberately does not reference its own new version.
    state.set_meta('vault-state-document', {**result, 'saved_at': datetime.now(timezone.utc).isoformat()})
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
                row = db.execute("SELECT value FROM meta WHERE key='vault-config'").fetchone()
                provenance = json.loads(row[0]) if row else None
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError()
            finally:
                db.close()
            expected_scope = {'provider': 'oci', 'profile': config['oci-config-file-profile'],
                              'region': config.get('oci-region', '') or '',
                              'compartment': config['oci-compartment-id'], 'subnet': config['oci-subnet-id']}
            if (not isinstance(identity, dict) or identity.get('schema') != 1 or identity.get('profile') != config['profile']
                    or identity.get('scope') != expected_scope):
                raise ValueError()
            if not isinstance(refs, dict):
                raise ValueError()
            legacy = provenance is None
            if legacy:
                if set(refs) not in (set(_legacy_files(config)), {'colors.yml', *_files(config)}):
                    raise ValueError()
            elif (set(refs) != set(_files(config)) or not isinstance(provenance, dict)
                  or provenance.get('schema') != 1
                  or not isinstance(provenance.get('sha256'), str)
                  or not re.fullmatch(r'[0-9a-f]{64}', provenance['sha256'])):
                raise ValueError()
            for ref in refs.values():
                if (not isinstance(ref, dict) or not isinstance(ref.get('document'), str)
                        or not ref['document'] or not isinstance(ref.get('version'), str) or not ref['version']):
                    raise ValueError()
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
            raise DeployError('Recovery state has an invalid or incompatible manifest.') from None
        if legacy:
            old_config = Path(directory) / 'legacy-colors.yml'
            ref = refs['colors.yml']
            _command(config, 'restore', ref['document'], '--version', ref['version'], '--to', old_config)
            expected_hash = _config_hash(old_config)
        else:
            expected_hash = provenance['sha256']
        configuration = _check_configuration(config, root, expected_hash)
        destinations = [(name, local_path(root, name)) for name in _files(config)]
        targets = [destination, *(path for _, path in destinations)]
        if (len(set(targets)) != len(targets)
                or {configuration, local_path(root, 'colors.yml')}.intersection(targets)):
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
    return {'state_document': document, 'state_version': version, 'file_count': len(destinations),
            'legacy_manifest': legacy,
            'reconciliation_required': True}
