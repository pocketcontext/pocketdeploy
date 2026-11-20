"""Exclusive VaultContext write lease for one deployment (``state-lock: vault``).

Flow: local flock (caller), vault-wide lease with a whole-set comparison, optional
restore under the lease, heartbeat, fenced checkpoints (files first, SQLite last),
a final fenced save and release. Vault output and file contents are never printed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .common import DeployError, local_path
from .state import LOCK_FD_ENV, CHILD_FD_ENV, inherited_deployment

FENCE_ENV = 'POCKETDEPLOY_FENCE'
# Commands that change deployment state or the recovery set take the lease.
LOCKED_COMMANDS = frozenset({
    'converge', 'delete', 'adopt', 'adopt-app', 'adopt-dns', 'rotate-host-key', 'resolve-step',
    'clear-pending-marker', 'retire-github-target', 'smtp-test', 'kms-converge', 'kms-rotate',
    'kms-resolve-user-create', 'kms-resolve-rotation', 'vault-save', 'exec'})
ID = re.compile(r'[a-z0-9]{15}')
MAX_RECEIPT = 1024 * 1024


def enabled(config):
    return config.get('state-lock', 'local') == 'vault'


def _run(config, args, timeout=300):
    """Run the VaultContext CLI; return (exit code, parsed stdout). Never print output."""
    try:
        completed = subprocess.run([config.get('vault-command', 'vaultcontext'), *map(str, args)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise DeployError('Vault lock command timed out; output suppressed.', code='vault_unavailable') from None
    except OSError:
        raise DeployError('Vault command unavailable; output suppressed.', code='vault_unavailable') from None
    try:
        value = json.loads(completed.stdout) if completed.stdout.strip() else None
    except ValueError:
        value = None
    return completed.returncode, value


def _text(value, limit=120):
    """Plaintext lock metadata for messages: printable, bounded."""
    if not isinstance(value, (str, int)):
        return None
    value = re.sub(r'[^\x20-\x7e]', '?', str(value))
    return value[:limit]


class LockRefused(DeployError):
    """VaultContext refused a lock operation (exit 5) with plaintext lock metadata."""

    MESSAGES = {
        'lock_held': 'Vault is locked by another operation; it touched no state here',
        'local_mismatch': 'Local recovery set differs from the vault; restore first with --restore',
        'lock_lost': 'This run no longer holds the vault lock',
        'stale_fence': 'The vault lock fence is no longer current; the run stopped',
        'lock_required': 'The vault requires a lock fence for writes',
        'generation_changed': 'The vault lock generation changed; inspect it again with lock-status',
        'vault_frozen': 'Vault is frozen until key rotation completes',
        'vault_archived': 'Vault is archived',
    }

    def __init__(self, details):
        self.details = details
        code = details.get('code') if isinstance(details.get('code'), str) else 'refused'
        parts = []
        info = details.get('info') if isinstance(details.get('info'), dict) else {}
        for key, value in (('label', info.get('label')), ('generation', details.get('generation')),
                           ('progress', details.get('progress')), ('expires_at', details.get('expires_at')),
                           ('local_status', details.get('local_status'))):
            if _text(value) not in (None, ''):
                parts.append(f'{key} {_text(value)}')
        message = self.MESSAGES.get(code, 'Vault lock refused')
        super().__init__(message + (' (' + ', '.join(parts) + ')' if parts else '') + '.',
                         code='vault_' + code if code in self.MESSAGES else 'vault_lock_refused')


def _call(config, *args, timeout=300):
    code, value = _run(config, args, timeout=timeout)
    if code == 0 and isinstance(value, dict):
        return value
    if code == 5 and isinstance(value, dict) and isinstance(value.get('error'), dict):
        raise LockRefused(value['error'])
    if code == 6:
        raise DeployError('Vault unlock session is unavailable; unlock before continuing.', code='vault_session_closed')
    raise DeployError('Vault lock command failed; output suppressed. Check vaultcontext login, unlock and client release.',
                      code='vault_command_failed')


def vault_save(config, args):
    """Fenced vault save used by vault.save; lock refusals keep their codes."""
    return _call(config, *args)


# Sync receipt: beside the state file, never inside SQLite (a snapshot cannot
# contain its own version).

def receipt_path(state_path):
    return Path(str(state_path) + '.vault-sync')


def lease_path(state_path):
    return Path(str(state_path) + '.vault-lease')


def _read_private_json(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_size > MAX_RECEIPT or info.st_mode & 0o077):
        raise DeployError('Vault lock files must be owned regular files without hardlinks.')
    try:
        return json.loads(Path(path).read_text())
    except ValueError:
        raise DeployError('Vault lock file is invalid; inspect it before continuing.') from None


def _write_private_json(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.', suffix=path.suffix, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_receipt(state_path):
    value = _read_private_json(receipt_path(state_path))
    if value is None:
        return None
    try:
        if value.get('schema') != 1 or not isinstance(value.get('vault'), str) or not isinstance(value.get('documents'), list):
            raise ValueError()
        seen = set()
        names = set()
        for doc in value['documents']:
            if (not ID.fullmatch(doc.get('id', '')) or doc['id'] in seen or not isinstance(doc.get('name'), str) or doc['name'] in names
                    or type(doc.get('revision')) is not int or doc['revision'] < 1
                    or not ID.fullmatch(doc.get('version', ''))
                    or doc.get('file_sha256_at_sync') is not None and not re.fullmatch(r'[0-9a-f]{64}', doc['file_sha256_at_sync'])):
                raise ValueError()
            seen.add(doc['id'])
            names.add(doc['name'])
    except (ValueError, AttributeError, TypeError):
        raise DeployError('Vault sync receipt is invalid; inspect it, or restore the deployment explicitly.') from None
    return value


def _sha256(path):
    from .vault import MAX_FILE_SIZE
    try:
        with Path(path).open('rb') as stream:
            content = stream.read(MAX_FILE_SIZE + 1)
    except FileNotFoundError:
        return None
    if len(content) > MAX_FILE_SIZE:
        raise DeployError('Recovery file exceeds the vault size limit.')
    return hashlib.sha256(content).hexdigest()


def ledger_hash(path):
    """Hash authoritative rows consistently across SQLite backup and reopen."""
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    try:
        db = sqlite3.connect(f'{path.absolute().as_uri()}?mode=ro', uri=True)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            # Vault acknowledgements are written after a snapshot and checked
            # separately by document/version. They are not provider state.
            for table, key in [('meta', 'key'), ('operations', 'id'), ('resources', 'name'), ('steps', 'id')]:
                where = " WHERE key NOT IN ('vault-files','vault-state-document','vault-config')" if table == 'meta' else ''
                digest.update(table.encode())
                for row in db.execute(f'SELECT * FROM {table}{where} ORDER BY {key}'):
                    digest.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
        finally:
            db.close()
    except (ValueError, TypeError, sqlite3.Error):
        raise DeployError('Deployment ledger cannot be verified.') from None
    return digest.hexdigest()


def local_drift(config, receipt):
    from .vault import _files
    root = Path(config['_root'])
    docs = {doc['name']: doc for doc in (receipt or {}).get('documents', [])}
    if not receipt:
        return []
    if set(docs) - {'state', *_files(config)}:
        raise DeployError('Recovery file configuration differs from the vault receipt.', code='vault_not_dedicated')
    changed = []
    for name, doc in docs.items():
        digest = ledger_hash(local_path(root, config['state-file'])) if name == 'state' else _sha256(local_path(root, name))
        if digest != doc.get('file_sha256_at_sync') or name == 'state' and digest is None:
            changed.append(name)
    changed.extend(name for name in _files(config) if name not in docs and local_path(root, name).exists())
    return sorted(changed)


def _local_checkpoint(config, state_path):
    from .config import scope
    from .state import State
    if not Path(state_path).exists():
        return None, {}
    with State(state_path, config['profile'], scope(config), read_only=True) as state:
        if state.get_meta('kms-scope'):
            from .kms import KMS
            KMS(config, state)  # Constructor verifies recorded authority without a provider call.
        refs = state.get_meta('vault-files', {})
        if not isinstance(refs, dict):
            raise DeployError('Deployment recovery references are invalid.')
        state_doc = state.get_meta('vault-state-document', {})
        if state_doc:
            refs['state'] = state_doc
        for ref in refs.values():
            if (not isinstance(ref, dict) or not ID.fullmatch(str(ref.get('document', '')))
                    or not ID.fullmatch(str(ref.get('version', '')))):
                raise DeployError('Deployment recovery references are invalid.')
        return state.deployment_id, refs


def _adoption(config, deployment_id, refs, remote):
    """Use only revisions proven to have the local immutable version."""
    from .vault import _files, _command
    from .kms import FILES
    names = set(_files(config)) | {'state'}
    active = {d['id']: d for d in remote if not d['archived']}
    if active.keys() - {ref['document'] for ref in refs.values()}:
        raise DeployError('Vault contains documents outside the recorded recovery set.', code='vault_not_dedicated')
    if refs and (set(refs) - names or names - set(refs) - set(FILES)):
        raise DeployError('Recorded recovery set is incomplete; restore a complete checkpoint.', code='vault_missing_remote')
    result = []
    for ref in refs.values():
        doc = active.get(ref['document'])
        if not doc:
            raise DeployError('Recorded document is missing from the vault.', code='vault_missing_remote')
        if doc['version'] != ref['version']:
            raise DeployError('Recorded checkpoint is stale; restore the complete vault checkpoint before adoption.',
                              code='vault_local_mismatch')
        result.append({k: doc[k] for k in ('id', 'revision', 'version')})
    if refs:
        # Prove which ledger owns these immutable pins before a successful
        # acquire changes lock_policy. A copied set of IDs alone cannot do so.
        ref = refs['state']
        with tempfile.TemporaryDirectory(prefix='.vault-restore-', dir=config['_root']) as directory:
            snapshot = Path(directory) / 'state.sqlite'
            _command(config, 'restore', ref['document'], '--version', ref['version'], '--to', snapshot)
            remote_id, committed = _local_checkpoint(config, snapshot)
        file_pins = lambda entries: {k: (v['document'], v['version']) for k, v in entries.items() if k != 'state'}
        if remote_id != deployment_id or file_pins(refs) != file_pins(committed):
            raise DeployError('Local recovery evidence belongs to a different deployment checkpoint.',
                              code='vault_not_dedicated')
    return result


def _restore_status(receipt, remote):
    """Classify a preflight view; force-acquire CASes this exact view again."""
    active = {d['id']: d for d in remote if not d['archived']}
    if set(active) != {d['id'] for d in receipt['documents']}:
        raise DeployError('Active documents differ from the recorded recovery set.', code='vault_local_mismatch')
    status = 'match'
    for local in receipt['documents']:
        doc = active[local['id']]
        if local['revision'] > doc['revision'] or (local['revision'] == doc['revision'] and local['version'] != doc['version']):
            raise DeployError('The local and remote checkpoints diverged.', code='vault_diverged')
        if local['revision'] < doc['revision']:
            status = 'stale'
    return status


def preflight_dedicated(config):
    """Best-effort: no other local deployment configuration names the same vault."""
    from blue.cli import load_yaml
    selected = Path(config['_file']).resolve()
    root = selected.parent
    candidates = set(root.glob('*.yml')) | set(root.glob('*.yaml'))
    candidates |= {path for path in root.parent.glob('*/colors.yml')}
    for path in sorted(candidates):
        try:
            if path.resolve() == selected or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                continue
            raw = load_yaml(path.read_text())
        except Exception:
            continue
        if isinstance(raw, dict) and raw.get('vault-id') == config['vault-id']:
            raise DeployError('Another local deployment configuration uses the same vault-id; each deployment needs a dedicated vault.',
                              code='vault_not_dedicated')


class VaultLease:
    def __init__(self, config, state_path, *, label=None, command=None):
        if not config.get('vault-id'):
            raise DeployError('state-lock: vault requires vault-id.')
        self.config = config
        self.state_path = Path(state_path)
        self.root = Path(config['_root'])
        self.vault = config['vault-id']
        self.ttl = int(config.get('state-lock-ttl', 900))
        self.margin = min(60.0, max(1.0, self.ttl / 10))
        self.label = label if label is not None else config.get('state-lock-label')
        self.command = command
        self.fence = None
        self.nested = False
        self.lost = False
        self.deadline = None
        self.progress = 'starting'
        self.restored = False
        self.final_save = True
        self.failure = None
        self.deployment_id = None
        self.bound = False
        self._stop = threading.Event()
        self._thread = None

    # Nested pocketdeploy commands started by `exec` reuse the parent's lease.
    @classmethod
    def from_env(cls, config, state_path):
        value = os.environ.get(FENCE_ENV)
        if not value:
            return None
        try:
            data = json.loads(value)
            valid = (data['vault'] == config.get('vault-id') and
                     Path(data['state']).resolve() == Path(state_path).resolve() and
                     re.fullmatch(r'[1-9][0-9]*:[a-z0-9]{15}', data['fence']))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid or not inherited_deployment(state_path):
            raise DeployError(f'{FENCE_ENV} does not match this deployment; refusing to run under another lease.',
                              code='vault_fence_mismatch')
        lease = cls(config, state_path)
        lease.fence = data['fence']
        lease.nested = True
        lease._load_shared()
        lease.check()
        # A copied environment and an orphaned sidecar cannot establish a
        # live server fence or an unlocked session.
        lease._renew()
        return lease

    def env(self):
        return json.dumps({'vault': self.vault, 'fence': self.fence, 'state': str(self.state_path.resolve())})

    def _shared(self):
        value = _read_private_json(lease_path(self.state_path))
        if (not isinstance(value, dict) or value.get('fence') != self.fence
                or value.get('vault') != self.vault or not isinstance(value.get('revisions'), dict)
                or type(value.get('deadline')) not in (int, float) or not math.isfinite(value['deadline'])
                or type(value.get('ttl')) is not int or not 0 < value['ttl'] <= 3600):
            raise DeployError('Vault lease state is missing; the parent run no longer holds the lock.', code='vault_lease_lost')
        return value

    def _load_shared(self):
        value = self._shared()
        self.deadline = value.get('deadline')
        self.ttl = value['ttl']
        self.margin = min(60.0, max(1.0, self.ttl / 10))
        self.lost = self.lost or bool(value.get('lost'))
        self.revisions = dict(value['revisions'])

    @contextmanager
    def _metadata_lock(self):
        # The deployment directory stays in place across atomic sidecar
        # replacements. Each caller opens its own file description.
        fd = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _update_shared(self, **changes):
        with self._metadata_lock():
            value = self._shared()
            value.update(changes)
            _write_private_json(lease_path(self.state_path), value)

    def _save_shared(self):
        self._update_shared(revisions=self.revisions.copy())

    def _mark_lost(self):
        self.lost = True
        self._update_shared(lost=True)

    def _start_heartbeat(self):
        with self._metadata_lock():
            _write_private_json(lease_path(self.state_path), {
                'fence': self.fence, 'vault': self.vault, 'deadline': self.deadline,
                'ttl': self.ttl, 'lost': False, 'revisions': self.revisions})
        self.check()
        self._thread = threading.Thread(target=self._heartbeat, name='vault-lease', daemon=True)
        self._thread.start()

    def acquire(self, *, restore=False, force=None):
        preflight_dedicated(self.config)
        receipt = read_receipt(self.state_path)
        if receipt and (receipt['vault'] != self.vault or receipt.get('profile') != self.config.get('profile')):
            raise DeployError('Vault sync receipt belongs to another vault or profile; this vault is not dedicated to this deployment.',
                              code='vault_not_dedicated')
        self.deployment_id, refs = _local_checkpoint(self.config, self.state_path)
        configured = self.config.get('vault-state-document-id')
        if 'state' in refs and configured and refs['state']['document'] != configured:
            raise DeployError('Configured state document differs from the local checkpoint.', code='vault_not_dedicated')
        self.marker = Path(str(self.state_path) + '.vault-restore')
        journal = _read_private_json(self.marker)
        if journal is not None and (not receipt or not restore or not isinstance(journal, dict)
                or journal.get('vault') != self.vault or journal.get('deployment_id') != self.deployment_id
                or not isinstance(journal.get('before'), dict) or not isinstance(journal.get('after'), dict)
                or journal['before'].keys() != journal['after'].keys() or 'original_bindings' not in journal):
            raise DeployError('An interrupted restore requires --restore with its original deployment.', code='vault_restore_pending')
        if receipt and (not self.deployment_id or receipt.get('deployment_id') != self.deployment_id):
            raise DeployError('Vault sync receipt does not match the local deployment identity.', code='vault_not_dedicated')
        dirty = local_drift(self.config, receipt)
        remote = remote_status(self.config, require_unlocked=True)
        try:
            docs = receipt['documents'] if receipt else _adoption(self.config, self.deployment_id, refs, remote['documents'])
        except DeployError:
            if not force and remote['state'] == 'held' and not remote['expired_by_client_clock']:
                raise LockRefused({'code': 'lock_held', 'info': {'label': remote['label']},
                                   'generation': remote['generation'], 'progress': remote['progress'],
                                   'expires_at': remote['expires_at']}) from None
            raise
        forced_restore_status = None
        if receipt and restore and force:
            forced_restore_status = _restore_status(receipt, remote['documents'])
            if forced_restore_status == 'stale' and dirty and not journal:
                raise DeployError('A dirty local fork cannot be force-restored.', code='vault_diverged')
            docs = [d for d in remote['documents'] if not d['archived']]
        if receipt and not journal:
            expected = {d['name']: (d['id'], d['version']) for d in docs}
            known = {name: (ref['document'], ref['version']) for name, ref in refs.items()}
            if expected != known:
                raise DeployError('Local ledger checkpoint differs from the sync receipt; reconcile before use.',
                                  code='vault_diverged')
        info = json.dumps({'command': self.command or 'unknown', 'profile': self.config.get('profile')})
        args = ['lock', 'acquire', self.vault, '--ttl', self.ttl, '--info', info]
        if self.label:
            args += ['--label', self.label]
        fd, manifest = tempfile.mkstemp(prefix='.vault-manifest-', dir=self.state_path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'documents': [{k: d[k] for k in ('id', 'revision', 'version')} for d in docs]}, stream)
            args += ['--local-manifest', manifest, '--on-mismatch', 'lock' if receipt and restore and not force else 'fail']
            if force:
                args += ['--force', '--observed-generation', force['observed_generation'], '--reason', force['reason'], '--yes']
            started = time.monotonic()
            result = _call(self.config, *args)
        finally:
            Path(manifest).unlink(missing_ok=True)
        try:
            if not (type(result.get('generation')) is int and ID.fullmatch(result.get('lease_id', ''))
                    and isinstance(result.get('documents'), list) and type(result.get('ttl_seconds')) is int):
                raise DeployError('Vault returned an invalid lock result; output suppressed.')
            self.fence = f"{result['generation']}:{result['lease_id']}"
            self.deadline = started + result['ttl_seconds']
            self.revisions = {d['id']: d['revision'] for d in result['documents'] if not d.get('archived')}
            self._start_heartbeat()
            if forced_restore_status:
                result['local_status'] = forced_restore_status
            self._reconcile(result, receipt, dirty, restore, journal)
            self.check()
        except BaseException:
            self.release(clean=not self.restored)
            raise
        return result

    def _reconcile(self, result, receipt, dirty, restore, journal):
        status = result.get('local_status')
        if status in ('match', 'empty_remote') and not journal:
            return
        if receipt and restore and (status == 'stale' or journal and status == 'match'):
            if dirty and not journal:
                raise DeployError('Local recovery files changed since the last sync and the vault is newer; resolve the fork manually.',
                                  code='vault_diverged')
            self._restore(result, receipt, journal)
            return
        raise DeployError('Vault recovery manifest differs from the local recovery set.', code='vault_local_mismatch')

    def _restore(self, result, receipt, journal):
        from . import vault
        from .state import State
        from .config import scope
        ids = {d['name']: d['id'] for d in receipt['documents']}
        current = {d['id']: d for d in result['documents'] if not d.get('archived')}
        if 'state' not in ids or set(current) != set(ids.values()):
            raise DeployError('Vault receipt and active document set differ.', code='vault_missing_remote')
        state_doc = current[ids.pop('state')]
        self.restored = True
        before_bindings = (journal['original_bindings'] if journal else _sha256(self.root / '.envrc.private'))
        vault.restore(self.config, self.root, state_doc['id'], state_doc['version'], overwrite=True,
                      sync={'check': self.check, 'journal': journal, 'marker': self.marker,
                            'deployment_id': self.deployment_id, 'file_ids': ids, 'original_bindings': before_bindings})
        _, refs = _local_checkpoint(self.config, self.state_path)
        if any(ref['version'] != current[ref['document']]['version'] for ref in refs.values()):
            # Some file heads advanced without a final SQLite commit. The
            # snapshot pins the complete recovery set; republish those bytes
            # fenced at current revisions before acknowledging the new heads.
            with State(self.state_path, self.config['profile'], scope(self.config)) as state:
                self.checkpoint(state)
        else:
            documents = []
            for name, ref in refs.items():
                doc = current[ref['document']]
                sha = ledger_hash(self.state_path) if name == 'state' else _sha256(local_path(self.root, name))
                documents.append({'id': doc['id'], 'name': name, 'revision': doc['revision'], 'version': doc['version'],
                                  'file_sha256_at_sync': sha})
            self._write_receipt(documents)
        self.marker.unlink()
        vault._fsync_dir(self.marker.parent)
        if before_bindings != _sha256(self.root / '.envrc.private'):
            raise DeployError('Private bindings were restored. Refresh the trusted shell environment before retrying.',
                              code='vault_environment_refresh_required')

    def _write_receipt(self, documents, deployment_id=None):
        from .vault import _fsync_dir
        _write_private_json(receipt_path(self.state_path), {
            'schema': 1, 'vault': self.vault, 'profile': self.config.get('profile'),
            'deployment_id': deployment_id or self.deployment_id, 'documents': sorted(documents, key=lambda d: d['name']),
            'synced_at': datetime.now(timezone.utc).isoformat()})
        _fsync_dir(self.state_path.parent)

    def bind(self, state):
        """Check the open state's identity against the receipt."""
        self.deployment_id = state.deployment_id
        receipt = read_receipt(self.state_path)
        if receipt and receipt.get('deployment_id') != state.deployment_id:
            raise DeployError('Vault sync receipt belongs to a different deployment identity.', code='vault_not_dedicated')
        self.bound = True

    def _renew(self):
        started = time.monotonic()
        result = _call(self.config, 'lock', 'renew', self.vault, '--require-unlocked', '--fence',
                       self.fence, '--ttl', self.ttl, '--progress', (self.progress or '')[:200],
                       timeout=min(30, self.ttl / 3))
        try:
            expires = datetime.fromisoformat(result['expires_at'])
            server_time = datetime.fromisoformat(result['server_time'])
            duration = (expires - server_time).total_seconds()
            if expires.tzinfo is None or server_time.tzinfo is None or not 0 < duration <= 3600:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise DeployError('Vault renewal did not prove an expiry.', code='vault_status_invalid') from None
        self._update_shared(deadline=started + min(self.ttl, duration))

    def _heartbeat(self):
        try:
            while not self._stop.wait(self.ttl / 3):
                try:
                    self._renew()
                except DeployError as exc:
                    if (isinstance(exc, LockRefused) or exc.code in ('vault_session_closed', 'vault_lease_lost', 'vault_status_invalid')
                            or time.monotonic() >= self._shared()['deadline'] - self.margin):
                        self._mark_lost()
                        return
                    # Transient failure. Every step also checks the last proven deadline.
        except Exception:
            # Worker exceptions never reach threading's traceback printer.
            self.lost = True
            try:
                self._mark_lost()
            except (OSError, DeployError):
                pass

    def check(self):
        """Raise unless the lease is provably valid for at least the safety margin."""
        value = self._shared()
        self.deadline = value['deadline']
        parent_gone = self.nested and not inherited_deployment(self.state_path)
        if self.lost or value.get('lost') or parent_gone or time.monotonic() >= self.deadline - self.margin:
            self._mark_lost()
            raise DeployError('Vault lease could not be proven valid; stopped at a step boundary without saving.',
                              code='vault_lease_lost')

    def checkpoint(self, state, *, allow_missing=False):
        """Fenced write-ahead save of the whole set: files first, SQLite last."""
        from . import vault
        self.check()
        if self.nested:
            self._load_shared()
        synced = {}
        try:
            result = vault.save(self.config, state, self.root, fence=self.fence, revisions=self.revisions,
                                allow_missing=allow_missing, synced=synced)
        except LockRefused as refused:
            if refused.details.get('code') in ('stale_fence', 'lock_lost', 'lock_required'):
                self._mark_lost()
            raise
        previous = {d['name']: d for d in (read_receipt(self.state_path) or {}).get('documents', [])}
        documents = []
        for name, ref in synced.items():
            if not isinstance(ref.get('revision'), int):
                raise DeployError('VaultContext client lacks lock support; install a release with vault locks.')
            self.revisions[ref['document']] = ref['revision']
            documents.append({'id': ref['document'], 'name': name, 'revision': ref['revision'],
                              'version': ref['version'], 'file_sha256_at_sync': ref['file_sha256']})
        if allow_missing:
            documents += [{**doc, 'file_sha256_at_sync': None} for name, doc in previous.items()
                          if name not in synced and name in vault._files(self.config)]
        self.deployment_id = state.deployment_id
        self._write_receipt(documents)
        self._save_shared()
        return result

    def finish(self, success, *, allow_missing=False):
        """Bound ledgers get a final fenced save, including failed operations."""
        clean = False
        try:
            self.check()
            if not self.nested and self.bound and self.final_save:
                from .state import State
                from .config import scope
                with State(self.state_path, self.config['profile'], scope(self.config)) as state:
                    if state.deployment_id != self.deployment_id:
                        raise DeployError('Deployment identity changed during the run.', code='vault_not_dedicated')
                    self.progress = 'final-save'
                    self.checkpoint(state, allow_missing=allow_missing)
            self.check()
            clean = success
        finally:
            if not self.nested:
                self.release(clean)

    def release(self, clean):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            if clean and self.lost:
                raise DeployError('Vault lease was lost before release.', code='vault_lease_lost')
            if self.fence and not self.lost:
                _call(self.config, 'lock', 'release', self.vault, '--fence', self.fence,
                      '--clean' if clean else '--dirty', '--progress', 'released')
        finally:
            with self._metadata_lock():
                path = lease_path(self.state_path)
                value = _read_private_json(path)
                if isinstance(value, dict) and value.get('fence') == self.fence:
                    path.unlink(missing_ok=True)


@contextmanager
def guard(config, state_path, *, command, restore=False, label=None, force=None):
    """Hold the vault lease around a mutating command when state-lock is vault."""
    if not enabled(config):
        yield None
        return
    lease = VaultLease.from_env(config, state_path)
    if lease is None:
        lease = VaultLease(config, state_path, label=label, command=command)
        lease.acquire(restore=restore, force=force)
    try:
        yield lease
    except BaseException:
        try:
            lease.finish(False, allow_missing=command == 'delete')
        except DeployError:
            pass  # Keep the original failure; the lease expires if release failed.
        raise
    lease.finish(True, allow_missing=command == 'delete')


def advice(lease, state):
    """Blue around advice: prove the lease before each step, checkpoint after changes."""
    async def around(step, opts):
        try:
            lease.check()
        except DeployError as exc:
            lease.failure = exc
            return {**opts, 'blue/exit': 1, 'blue/err': str(exc)}
        before = state.db.total_changes
        result = await step(opts)
        if isinstance(result, dict) and not result.get('blue/exit') and state.db.total_changes != before:
            try:
                lease.checkpoint(state)
            except DeployError as exc:
                lease.failure = exc
                return {**result, 'blue/exit': 1, 'blue/err': str(exc)}
        return result
    return around


def remote_status(config, *, require_unlocked=False):
    """Read only the operational fields used for inspection and reconciliation."""
    args = ['lock', 'status', config['vault-id']]
    if require_unlocked:
        args.append('--require-unlocked')
    value = _call(config, *args, timeout=60)
    state, generation = value.get('state'), value.get('generation')
    if (value.get('vault') != config['vault-id'] or state not in ('held', 'free', 'released', 'broken')
            or type(generation) is not int or generation < 0 or not isinstance(value.get('documents'), list)):
        raise DeployError('Vault status is invalid or incompatible; output suppressed.', code='vault_status_invalid')
    info = value.get('info') if isinstance(value.get('info'), dict) else {}
    result = {'available': True, 'state': state, 'generation': generation,
              'label': _text(info.get('label')), 'holder': _text(value.get('holder')),
              'holder_email': _text(value.get('holder_email')), 'progress': _text(value.get('progress')),
              'expires_at': _text(value.get('expires_at')), 'lock_policy': _text(value.get('lock_policy')),
              'expired_by_client_clock': value.get('expired_by_client_clock') is True}
    documents = []
    seen = set()
    for doc in value['documents']:
        if (not isinstance(doc, dict) or not isinstance(doc.get('id'), str) or not ID.fullmatch(doc['id'])
                or doc['id'] in seen or type(doc.get('revision')) is not int or doc['revision'] < 1
                or not isinstance(doc.get('version'), str) or not ID.fullmatch(doc['version'])
                or type(doc.get('archived')) is not bool):
            raise DeployError('Vault lock manifest is invalid; output suppressed.', code='vault_status_invalid')
        seen.add(doc['id'])
        documents.append({key: doc[key] for key in ('id', 'revision', 'version', 'archived')})
    result['documents'] = documents
    return result


def status(config, state_path):
    """Report lock holder and whole-set drift without acquiring a lease."""
    result = {'state_lock': config.get('state-lock', 'local')}
    if not enabled(config):
        return result
    receipt = read_receipt(state_path)
    drift = local_drift(config, receipt)
    result['vault_sync'] = {'receipt': receipt is not None, 'synced_at': (receipt or {}).get('synced_at'),
                            'drifted_files': drift}
    warnings = []
    if drift:
        warnings.append('Local recovery files changed since the last vault sync: ' + ', '.join(drift) + '.')
    try:
        lock = remote_status(config)
    except DeployError:
        result['vault_lock'] = {'available': False}
        warnings.append('Vault lock status unavailable; check vaultcontext login and client release.')
    else:
        docs = lock.pop('documents')
        result['vault_lock'] = lock
        if lock['state'] == 'held' and not lock['expired_by_client_clock']:
            warnings.append(f"Vault is locked by {lock['label'] or lock['holder_email'] or 'another operation'} "
                            f"(generation {lock['generation']}, progress {lock['progress'] or '-'}, "
                            f"expires {lock['expires_at']}).")
        if receipt:
            expected = {doc['id']: (doc['revision'], doc['version']) for doc in receipt['documents']}
            active = {doc['id']: (doc['revision'], doc['version']) for doc in docs if not doc['archived']}
            different = receipt['vault'] != config['vault-id'] or active != expected
            result['vault_sync']['remote_drift'] = different
            if different:
                warnings.append('The active vault recovery set differs from the local sync receipt.')
    if receipt is None:
        warnings.append('No vault sync receipt yet; the local recovery set needs verification.')
    if warnings:
        result['warnings'] = warnings
    return result


def confirm_break(config, observed_generation, reason, yes):
    if (type(observed_generation) is not int or observed_generation < 0
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 500):
        raise DeployError('Breaking a vault lock requires --observed-generation and --reason.', code='invalid_usage')
    lock = remote_status(config)
    summary = (f"Vault lock: state {lock['state']}, label {lock['label'] or '-'}, "
               f"holder {lock['holder_email'] or lock['holder'] or '-'}, generation {lock['generation']}, "
               f"progress {lock['progress'] or '-'}, expires {lock['expires_at'] or '-'}.")
    print(summary, file=sys.stderr)
    if yes:
        return
    if not sys.stdin.isatty():
        raise DeployError('Breaking a vault lock needs confirmation in an interactive terminal or an explicit --yes.',
                          code='invalid_usage')
    if input('Type "break" to retire this lock: ').strip() != 'break':
        raise DeployError('Lock break cancelled.', code='cancelled')


def lock_break(config, observed_generation, reason, yes):
    confirm_break(config, observed_generation, reason, yes)
    result = _call(config, 'lock', 'break', config['vault-id'], '--observed-generation', observed_generation,
                   '--reason', reason, '--yes')
    return {'vault_lock': {'broken': bool(result.get('broken')), 'generation': result.get('generation'),
                           'state': _text(result.get('state'))}}


def _foreground(tty, group):
    # tcsetpgrp from a background process otherwise stops the controller
    # before it can return the terminal to its own process group.
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(tty, group)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def _stop_child(child, *, owns_group):
    if child.returncode is not None:
        return
    try:
        send = os.killpg if owns_group else os.kill
        send(child.pid, signal.SIGTERM)
        # Keep the group leader unreaped until the last group signal; this
        # prevents PID recycling while descendants receive the grace period.
        time.sleep(1)
        send(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        child.wait()


def run_child(config, state_path, lease, argv, flock_fd):
    env = dict(os.environ)
    fresh = not Path(state_path).exists()
    if lease is not None:
        lease.check()
        env[FENCE_ENV] = lease.env()
        lease.progress = 'exec'
    env[LOCK_FD_ENV] = str(flock_fd)
    # Direct nested commands contend on this anonymous file via a per-process
    # lock. A nested exec creates a new file for its own children while
    # retaining its place among its siblings.
    with tempfile.TemporaryFile(dir=Path(state_path).parent) as siblings:
        fcntl.lockf(siblings, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 1)
        env[CHILD_FD_ENV] = str(siblings.fileno())
        args = dict(env=env, pass_fds=(flock_fd, siblings.fileno()), cwd=config['_root'])
        try:
            if lease is None:
                return subprocess.run(argv, **args).returncode
            # Keep nested exec descendants in the outer controller's group
            # so revocation stops the entire ordinary child process tree.
            owns_group = not lease.nested
            child = subprocess.Popen(argv, process_group=0 if owns_group else None, **args)
        except OSError:
            raise DeployError('exec command could not be started.', code='command_unavailable') from None
        tty = None
        try:
            try:
                foreground = owns_group and os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()
            except OSError:
                foreground = False
            if foreground:
                tty = 0
                try:
                    _foreground(tty, child.pid)
                    os.killpg(child.pid, signal.SIGCONT)
                except OSError:
                    if child.poll() is None:
                        raise
            while True:
                try:
                    code = child.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    lease.check()
        except BaseException:
            _stop_child(child, owns_group=owns_group)
            raise
        finally:
            if tty is not None:
                _foreground(tty, os.getpgrp())
        lease._load_shared()
        lease.check()
        if fresh and Path(state_path).exists():
            from .state import State
            from .config import scope
            with State(state_path, config['profile'], scope(config), read_only=True) as state:
                lease.bind(state)
        return code
