"""Versioned recovery sets through VaultContext; never execute restored files."""
import json
import hashlib
import os
import sqlite3
import subprocess
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .common import DeployError, local_path, run
from .config import scope

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


def _save_file(config, path, name, document=None, fence=None, expect_revision=None):
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise DeployError('Recovery file is missing or exceeds the Vault size limit.')
    args = ['save', config['vault-id'], str(path), '--name', name]
    if document:
        args.extend(['--document', document])
    if fence:
        # Under state-lock: vault every write carries the current lease fence.
        args.extend(['--fence', fence])
    if expect_revision is not None:
        args.extend(['--expect-revision', str(expect_revision)])
    if fence:
        from .state_lock import vault_save
        result = vault_save(config, args)
    else:
        result = _command(config, *args)
    if not isinstance(result.get('id'), str) or not isinstance(result.get('version'), str):
        raise DeployError('Vault save did not return document and version identifiers.')
    if fence and (type(result.get('revision')) is not int or result['revision'] < 1
                  or document and result['id'] != document):
        raise DeployError('Vault save returned an invalid document revision.')
    ref = {'document': result['id'], 'version': result['version']}
    if isinstance(result.get('revision'), int):
        ref['revision'] = result['revision']
    return ref


def _files(config):
    private_key = config.get('ssh-private-key-file', '.ssh/id_ed25519')
    host_private_key = config.get('ssh-host-private-key-file', '.ssh/host_ed25519')
    from .kms import FILES
    names = [*(FILES if config.get('oci-kms') else []), '.envrc.private',
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
    if config.get('state-lock') == 'vault' and 'state' in names:
        raise DeployError('A recovery file name collides with the state document in the sync receipt.')
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


def save(config, state, root, fence=None, revisions=None, allow_missing=False, synced=None):
    """Save the recovery set: files first, SQLite snapshot last.

    With a lease ``fence`` every upload is fenced and ``revisions`` (document ID ->
    expected revision) adds a compare-and-set. ``allow_missing`` keeps the previous
    reference for files removed locally (after deletion). ``synced`` receives
    per-file sync details for the state-lock receipt.
    """
    if not config.get('vault-id'):
        raise DeployError('Vault is not configured.')
    if config.get('state-lock') == 'vault' and not fence:
        raise DeployError('A recovery checkpoint requires the active vault lease.', code='vault_lock_required')
    root = Path(root).resolve()
    provenance = _provenance(config, root)
    previous = state.get_meta('vault-files', {})
    if revisions is None:
        revisions = {}
    state_document = config.get('vault-state-document-id') or state.get_meta('vault-state-document', {}).get('document')
    if fence:
        documents = [ref['document'] for ref in previous.values()]
        if state_document:
            documents.append(state_document)
        if len(documents) != len(set(documents)) or set(documents) - revisions.keys():
            raise DeployError('Recovery document is outside the locked manifest or referenced twice.')
    refs = {}
    # Validate every source before performing any upload. Files are opaque bytes.
    paths = [(str(name), local_path(root, name)) for name in _files(config)]
    from .kms import FILES
    missing = {name for name, path in paths if not path.is_file()}
    uncreated = {name for name in missing if fence and config.get('oci-kms') and name in FILES
                 and name not in previous}
    if FILES[0] in uncreated and (state.get_resource('kms-api-key') or state.get_meta('kms-pending-api-key')):
        uncreated.remove(FILES[0])
    if FILES[1] in uncreated and state.get_meta('kms-signing-version'):
        uncreated.remove(FILES[1])
    if uncreated:
        provenance['uncreated_kms_files'] = sorted(uncreated)
    if (any(path.is_file() and path.stat().st_size > MAX_FILE_SIZE for _, path in paths)
            or any(name not in uncreated and not (allow_missing and name not in {'.envrc.private', *FILES}
                                                   and previous.get(name, {}).get('document'))
                   for name in missing)):
        raise DeployError('Recovery file is missing or exceeds the Vault size limit.')
    for name, path in paths:
        if name in uncreated:
            continue
        if name in missing:
            refs[name] = {key: previous[name][key] for key in ('document', 'version') if key in previous[name]}
            continue
        document = previous.get(name, {}).get('document')
        display_name = config['profile'] + '-' + name.replace('/', '-')
        if fence:
            # The CLI reopens its input. Hash and upload a private, immutable copy.
            with tempfile.TemporaryDirectory(prefix='.vault-save-', dir=root) as directory:
                staged = Path(directory) / 'file'
                shutil.copyfile(path, staged)
                os.chmod(staged, 0o600)
                ref = _save_file(config, staged, display_name, document, fence, revisions.get(document, 0))
                revisions[ref['document']] = ref['revision']
                if synced is not None:
                    synced[name] = {**ref, 'file_sha256': hashlib.sha256(staged.read_bytes()).hexdigest()}
        else:
            ref = _save_file(config, path, display_name, document)
        refs[name] = {'document': ref['document'], 'version': ref['version']}
        # Persist acknowledged document IDs even if a later upload fails.
        state.set_meta('vault-files', {**previous, **refs})
    state.set_meta('vault-files', refs)
    state.set_meta('vault-config', provenance)
    with tempfile.TemporaryDirectory(prefix='.vault-save-', dir=root) as directory:
        snapshot = Path(directory) / 'state.sqlite'
        state.snapshot(snapshot)
        result = _save_file(config, snapshot, config['profile'] + '-state', state_document, fence,
                            revisions.get(state_document, 0 if fence and not state_document else None) if fence else None)
    # The published snapshot deliberately does not reference its own new version.
    state.set_meta('vault-state-document', {'document': result['document'], 'version': result['version'],
                                            'saved_at': datetime.now(timezone.utc).isoformat()})
    if synced is not None:
        from .state_lock import ledger_hash
        synced['state'] = {**result, 'file_sha256': ledger_hash(state.path)}
    return {'state_document': result['document'], 'state_version': result['version'], 'file_count': len(refs)}


def restore(config, root, document, version, overwrite=False, sync=None):
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
            expected_scope = scope(config)
            if (not isinstance(identity, dict) or identity.get('schema') != 1 or identity.get('profile') != config['profile']
                    or identity.get('scope') != expected_scope):
                raise ValueError()
            if not isinstance(refs, dict):
                raise ValueError()
            legacy = provenance is None
            uncreated = set((provenance or {}).get('uncreated_kms_files', []))
            from .kms import FILES
            if uncreated and (not config.get('oci-kms') or not uncreated.issubset(FILES)):
                raise ValueError()
            if legacy:
                if set(refs) not in (set(_legacy_files(config)), {'colors.yml', *_files(config)}):
                    raise ValueError()
            elif (set(refs) != set(_files(config)) - uncreated or not isinstance(provenance, dict)
                  or provenance.get('schema') != 1
                  or not isinstance(provenance.get('sha256'), str)
                  or not re.fullmatch(r'[0-9a-f]{64}', provenance['sha256'])):
                raise ValueError()
            for ref in refs.values():
                if (not isinstance(ref, dict) or not isinstance(ref.get('document'), str)
                        or not ref['document'] or not isinstance(ref.get('version'), str) or not ref['version']):
                    raise ValueError()
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise DeployError('Recovery state has an invalid or incompatible manifest.') from None
        if sync is not None:
            from .state_lock import _local_checkpoint
            _local_checkpoint(config, staged_db)  # Verify the staged ledger and any recorded KMS authority.
            if (legacy or identity.get('deployment_id') != sync['deployment_id']
                    or {name: ref['document'] for name, ref in refs.items()} != sync['file_ids']):
                raise DeployError('Vault recovery set does not match the deployment identity and locked documents.')
        if legacy:
            old_config = Path(directory) / 'legacy-colors.yml'
            ref = refs['colors.yml']
            _command(config, 'restore', ref['document'], '--version', ref['version'], '--to', old_config)
            expected_hash = _config_hash(old_config)
        else:
            expected_hash = provenance['sha256']
        if sync is None:
            configuration = _check_configuration(config, root, expected_hash)
        else:
            caller_root = Path(config.get('_root', root)).resolve()
            configuration = local_path(root, local_path(caller_root, config.get('_file', 'colors.yml')).relative_to(caller_root))
        if any(local_path(root, name).exists() for name in uncreated):
            raise DeployError('Local KMS authority conflicts with the incomplete checkpoint.')
        destinations = [(name, local_path(root, name)) for name in _files(config) if name not in uncreated]
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
        # The snapshot cannot contain its own version. Record its verified
        # provenance locally, including on the first disaster recovery.
        if sync is not None or config.get('state-lock') == 'vault':
            db = sqlite3.connect(staged_db)
            try:
                with db:
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('vault-state-document',?)",
                               (json.dumps({'document': document, 'version': version}),))
            finally:
                db.close()
        publication = [*staged, (staged_db, destination)]
        if sync is not None:
            from .state_lock import _sha256, _write_private_json, ledger_hash
            journal = sync['journal']
            old = {}
            new = {}
            for source, target in publication:
                name = target.relative_to(root).as_posix()
                digest = ledger_hash if target == destination else _sha256
                old[name] = digest(target)
                new[name] = digest(source)
            if journal:
                if (set(journal['before']) != set(old) or any(old[n] not in
                        (journal['before'][n], journal['after'][n]) for n in old)):
                    raise DeployError('Local files changed during interrupted restore; preserve the fork.', code='vault_diverged')
            else:
                for _, target in publication:
                    if target.exists():
                        backup = local_path(root, str(target) + '.pre-restore')
                        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.', suffix='.pre-restore', delete=False) as temp:
                            try:
                                with target.open('rb') as original:
                                    shutil.copyfileobj(original, temp)
                                temp.flush()
                                os.fsync(temp.fileno())
                                os.replace(temp.name, backup)
                            finally:
                                Path(temp.name).unlink(missing_ok=True)
                        _fsync_dir(target.parent)
            sync['check']()
            # Keep the initial backups through retries. Allow only bytes from
            # this verified transition; an intervening edit must be reconciled.
            _write_private_json(sync['marker'], {'vault': config['vault-id'], 'deployment_id': sync['deployment_id'],
                                'original_bindings': sync['original_bindings'], 'before': old, 'after': new})
            _fsync_dir(sync['marker'].parent)
        for source, target in publication:
            local_path(root, target)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and not overwrite:
                raise DeployError('Recovery destination appeared during restoration.')
            os.chmod(source, 0o600)
            if sync is not None:
                sync['check']()
                with source.open('rb') as stream:
                    os.fsync(stream.fileno())
            if overwrite:
                os.replace(source, target)
            else:
                os.link(source, target)
                source.unlink()
            if sync is not None:
                _fsync_dir(target.parent)
    return {'state_document': document, 'state_version': version, 'file_count': len(destinations),
            'legacy_manifest': legacy,
            'reconciliation_required': True}


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
