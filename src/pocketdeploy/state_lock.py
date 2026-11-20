"""Exclusive VaultContext write lease for one deployment (``state-lock: vault``).

Flow: local flock (caller), vault-wide lease with a whole-set comparison, optional
restore under the lease, heartbeat, fenced checkpoints (files first, SQLite last),
a final fenced save and release. Vault output and file contents are never printed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .common import DeployError, local_path

FENCE_ENV = 'POCKETDEPLOY_FENCE'
LOCK_FD_ENV = 'POCKETDEPLOY_LOCK_FD'
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
                                   capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
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


def _call(config, *args):
    code, value = _run(config, args)
    if code == 0 and isinstance(value, dict):
        return value
    if code == 5 and isinstance(value, dict) and isinstance(value.get('error'), dict):
        raise LockRefused(value['error'])
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
            or info.st_size > MAX_RECEIPT):
        raise DeployError('Vault lock files must be owned regular files without hardlinks.')
    try:
        return json.loads(Path(path).read_text())
    except ValueError:
        raise DeployError('Vault lock file is invalid; inspect it before continuing.') from None


def _write_private_json(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
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
        for doc in value['documents']:
            if (not ID.fullmatch(doc.get('id', '')) or doc['id'] in seen or not isinstance(doc.get('name'), str)
                    or type(doc.get('revision')) is not int or doc['revision'] < 1
                    or not ID.fullmatch(doc.get('version', ''))
                    or doc.get('file_sha256_at_sync') is not None and not re.fullmatch(r'[0-9a-f]{64}', doc['file_sha256_at_sync'])):
                raise ValueError()
            seen.add(doc['id'])
    except (ValueError, AttributeError, TypeError):
        raise DeployError('Vault sync receipt is invalid; inspect it, or restore the deployment explicitly.') from None
    return value


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def local_drift(config, receipt):
    """Recovery files whose local bytes changed since the last sync (SQLite excluded)."""
    root = Path(config['_root'])
    return sorted(doc['name'] for doc in (receipt or {}).get('documents', [])
                  if doc.get('file_sha256_at_sync') and _sha256(local_path(root, doc['name'])) != doc['file_sha256_at_sync'])


def _known_documents(state_path):
    """Document IDs recorded by earlier vault-save checkpoints in local SQLite."""
    if not Path(state_path).exists():
        return {}
    try:
        db = sqlite3.connect(f'{Path(state_path).absolute().as_uri()}?mode=ro', uri=True)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            refs = {}
            for key in ('vault-files', 'vault-state-document'):
                row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
                value = json.loads(row[0]) if row else None
                if key == 'vault-files' and isinstance(value, dict):
                    refs.update({ref['document']: name for name, ref in value.items()
                                 if isinstance(ref, dict) and isinstance(ref.get('document'), str)})
                elif isinstance(value, dict) and isinstance(value.get('document'), str):
                    refs[value['document']] = 'state'
            return refs
        finally:
            db.close()
    except (sqlite3.Error, ValueError):
        raise DeployError('Deployment state is invalid or incompatible.') from None


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
        if isinstance(raw, dict) and raw.get('vault-id') == config['vault-id'] and raw.get('profile') != config.get('profile'):
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
        self.label = label or config.get('state-lock-label') or default_label()
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
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

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
        if not valid:
            raise DeployError(f'{FENCE_ENV} does not match this deployment; refusing to run under another lease.',
                              code='vault_fence_mismatch')
        lease = cls(config, state_path)
        lease.fence = data['fence']
        lease.nested = True
        lease._load_shared()
        lease.check()
        return lease

    def env(self):
        return json.dumps({'vault': self.vault, 'fence': self.fence, 'state': str(self.state_path.resolve())})

    # Shared lease file: deadline (system-wide monotonic clock), lost flag and
    # the vault revisions this lease has written. Parent and nested commands
    # never write concurrently: the parent waits for its child.
    def _load_shared(self):
        value = _read_private_json(lease_path(self.state_path))
        if not isinstance(value, dict) or value.get('fence') != self.fence:
            raise DeployError('Vault lease state is missing; the parent run no longer holds the lock.', code='vault_lease_lost')
        self.deadline = value.get('deadline')
        self.lost = bool(value.get('lost'))
        self.revisions = {k: v for k, v in (value.get('revisions') or {}).items() if type(v) is int}

    def _save_shared(self):
        _write_private_json(lease_path(self.state_path), {
            'fence': self.fence, 'vault': self.vault, 'deadline': self.deadline, 'lost': self.lost,
            'revisions': self.revisions})

    def acquire(self, *, restore=False, force=None):
        preflight_dedicated(self.config)
        receipt = read_receipt(self.state_path)
        if receipt and (receipt['vault'] != self.vault or receipt.get('profile') != self.config.get('profile')):
            raise DeployError('Vault sync receipt belongs to another vault or profile; this vault is not dedicated to this deployment.',
                              code='vault_not_dedicated')
        dirty = local_drift(self.config, receipt)
        info = json.dumps({'command': self.command or 'unknown', 'profile': self.config.get('profile')})
        args = ['lock', 'acquire', self.vault, '--ttl', self.ttl, '--label', self.label, '--info', info]
        manifest = None
        try:
            if receipt is not None:
                fd, manifest = tempfile.mkstemp(prefix='.vault-manifest-', dir=self.state_path.parent)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'documents': [{k: d[k] for k in ('id', 'revision', 'version')} for d in receipt['documents']]}, stream)
                args += ['--local-manifest', manifest, '--on-mismatch', 'lock' if restore else 'fail']
            if force:
                args += ['--force', '--observed-generation', force['observed_generation'], '--reason', force['reason'], '--yes']
            started = time.monotonic()
            result = _call(self.config, *args)
        finally:
            if manifest:
                Path(manifest).unlink(missing_ok=True)
        try:
            if not (isinstance(result.get('generation'), int) and ID.fullmatch(result.get('lease_id', ''))
                    and isinstance(result.get('documents'), list) and isinstance(result.get('ttl_seconds'), int)):
                raise DeployError('Vault returned an invalid lock result; output suppressed.')
            self.fence = f"{result['generation']}:{result['lease_id']}"
            self.deadline = started + result['ttl_seconds']
            self.revisions = {d['id']: d['revision'] for d in result['documents']}
            self._save_shared()
            self._reconcile(result, receipt, dirty, restore)
        except BaseException:
            self.release(clean=not self.restored)
            raise
        self._thread = threading.Thread(target=self._heartbeat, name='vault-lease', daemon=True)
        self._thread.start()
        return result

    def _reconcile(self, result, receipt, dirty, restore):
        active = {d['id']: d for d in result['documents'] if not d.get('archived')}
        status = result.get('local_status')
        if receipt is None:
            # First locked run: local state is authoritative, but the vault must
            # contain nothing beyond this deployment's recorded recovery set.
            known = _known_documents(self.state_path)
            if set(active) - set(known):
                raise DeployError('Vault contains active documents outside this deployment\'s recovery set; archive them or use a dedicated vault.',
                                  code='vault_not_dedicated')
            if set(known) - set(active):
                raise DeployError('Recorded recovery documents are missing or archived in the vault; inspect before continuing.',
                                  code='vault_missing_remote')
            return
        if status in ('match', 'empty_remote'):
            return
        if status == 'stale' and restore:
            if dirty:
                raise DeployError('Local recovery files changed since the last sync and the vault is newer; resolve the fork manually.',
                                  code='vault_diverged')
            self._restore(result, receipt)
            return
        raise DeployError(f'Vault recovery set is {_text(status)}; stop and inspect it before continuing.',
                          code='vault_' + (_text(status) or 'mismatch'))

    def _restore(self, result, receipt):
        from . import vault
        names = {d['id']: d['name'] for d in receipt['documents']}
        state_ids = [doc_id for doc_id, name in names.items() if name == 'state']
        current = {d['id']: d for d in result['documents'] if not d.get('archived')}
        if len(state_ids) != 1 or state_ids[0] not in current:
            raise DeployError('Vault sync receipt does not identify the state document.', code='vault_missing_remote')
        state_doc = current[state_ids[0]]
        self.restored = True
        vault.restore(self.config, self.root, state_doc['id'], state_doc['version'], overwrite=True,
                      sync={doc_id: doc['version'] for doc_id, doc in current.items()})
        documents = []
        for doc_id, name in names.items():
            doc = current[doc_id]
            sha = None if name == 'state' else _sha256(local_path(self.root, name))
            documents.append({'id': doc_id, 'name': name, 'revision': doc['revision'], 'version': doc['version'],
                              'file_sha256_at_sync': sha})
        self._write_receipt(documents, receipt.get('deployment_id'))

    def _write_receipt(self, documents, deployment_id=None):
        _write_private_json(receipt_path(self.state_path), {
            'schema': 1, 'vault': self.vault, 'profile': self.config.get('profile'),
            'deployment_id': deployment_id or self.deployment_id, 'documents': sorted(documents, key=lambda d: d['name']),
            'synced_at': datetime.now(timezone.utc).isoformat()})

    def bind(self, state):
        """Check the open state's identity against the receipt."""
        self.deployment_id = state.deployment_id
        receipt = read_receipt(self.state_path)
        if receipt and receipt.get('deployment_id') not in (None, state.deployment_id):
            raise DeployError('Vault sync receipt belongs to a different deployment identity.', code='vault_not_dedicated')

    def _heartbeat(self):
        while not self._stop.wait(self.ttl / 3):
            started = time.monotonic()
            try:
                _call(self.config, 'lock', 'renew', self.vault, '--fence', self.fence, '--progress', (self.progress or '')[:200])
            except LockRefused:
                with self._lock:
                    self.lost = True
                    self._save_shared()
                return
            except DeployError:
                continue  # Transient: check() stops the run once the deadline margin passes.
            with self._lock:
                self.deadline = started + self.ttl
                self._save_shared()

    def check(self):
        """Raise unless the lease is provably valid for at least the safety margin."""
        if self.nested:
            self._load_shared()
        if self.lost or self.deadline is None or time.monotonic() > self.deadline - self.margin:
            self.lost = True
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
                self.lost = True
                self._save_shared()
            raise
        previous = {d['name']: d for d in (read_receipt(self.state_path) or {}).get('documents', [])}
        documents = []
        for name, ref in synced.items():
            if not isinstance(ref.get('revision'), int):
                raise DeployError('VaultContext client lacks lock support; install a release with vault locks.')
            self.revisions[ref['document']] = ref['revision']
            documents.append({'id': ref['document'], 'name': name, 'revision': ref['revision'],
                              'version': ref['version'], 'file_sha256_at_sync': ref['file_sha256']})
        documents += [doc for name, doc in previous.items() if name not in synced]
        self.deployment_id = state.deployment_id
        self._write_receipt(documents)
        self._save_shared()
        return result

    def finish(self, success, *, allow_missing=False):
        """Final fenced save (also after failure), then release. Never an unfenced save."""
        save_error = None
        if not self.nested and self.final_save and not self.lost and self.state_path.exists():
            from .state import State
            from .config import scope
            try:
                with State(self.state_path, self.config['profile'], scope(self.config)) as state:
                    self.progress = 'final-save'
                    self.checkpoint(state, allow_missing=allow_missing)
            except DeployError as exc:
                save_error = exc
        if not self.nested:
            self.release(clean=success and save_error is None and not self.lost)
        if save_error is not None and success:
            raise save_error

    def release(self, clean):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        try:
            if self.fence and not self.lost:
                _call(self.config, 'lock', 'release', self.vault, '--fence', self.fence,
                      '--clean' if clean else '--dirty', '--progress', 'released')
        finally:
            lease_path(self.state_path).unlink(missing_ok=True)


def default_label():
    import getpass
    import socket
    try:
        user = getpass.getuser()
    except Exception:
        user = 'operator'
    return _text(f'{user}@{socket.gethostname().split(".")[0]}', 100)


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
    def around(step, opts):
        try:
            lease.check()
        except DeployError as exc:
            lease.failure = exc
            return {**opts, 'blue/exit': 1, 'blue/err': str(exc)}
        before = state.db.total_changes
        result = step(opts)
        if isinstance(result, dict) and not result.get('blue/exit') and state.db.total_changes != before:
            try:
                lease.checkpoint(state)
            except DeployError as exc:
                lease.failure = exc
                return {**result, 'blue/exit': 1, 'blue/err': str(exc)}
        return result
    return around


def status(config, state_path):
    """Lock-free status: holder, label, progress and local drift. Never acquires."""
    result = {'state_lock': config.get('state-lock', 'local')}
    if not enabled(config):
        return result
    receipt = read_receipt(state_path)
    drift = local_drift(config, receipt)
    result['vault_sync'] = {'receipt': receipt is not None, 'synced_at': (receipt or {}).get('synced_at'),
                            'drifted_files': drift}
    try:
        code, value = _run(config, ['lock', 'status', config['vault-id']], timeout=60)
    except DeployError:
        code, value = 1, None
    if code != 0 or not isinstance(value, dict):
        result['vault_lock'] = {'available': False}
        result['warnings'] = ['Vault lock status unavailable; check vaultcontext login.']
        return result
    info = value.get('info') if isinstance(value.get('info'), dict) else {}
    lock = {'available': True, 'state': _text(value.get('state')), 'generation': value.get('generation'),
            'label': _text(info.get('label')), 'holder_email': _text(value.get('holder_email')),
            'progress': _text(value.get('progress')), 'expires_at': _text(value.get('expires_at')),
            'lock_policy': _text(value.get('lock_policy'))}
    result['vault_lock'] = lock
    warnings = []
    if lock['state'] == 'held' and not value.get('expired_by_client_clock'):
        warnings.append(f"Vault is locked by {lock['label'] or 'another operation'} (generation {lock['generation']}, "
                        f"progress {lock['progress'] or '-'}, expires {lock['expires_at']}).")
    if drift:
        warnings.append('Local recovery files changed since the last vault sync: ' + ', '.join(drift) + '.')
    if receipt is None:
        warnings.append('No vault sync receipt yet; the next locked run adopts the local recovery set.')
    if warnings:
        result['warnings'] = warnings
    return result


def confirm_break(config, observed_generation, reason, yes, reporter_say=None):
    if observed_generation is None or not reason:
        raise DeployError('Breaking a vault lock requires --observed-generation and --reason.', code='invalid_usage')
    current = status(config, Path('/nonexistent'))
    lock = current.get('vault_lock', {})
    summary = (f"Vault lock: state {lock.get('state')}, label {lock.get('label')}, holder {lock.get('holder_email')}, "
               f"generation {lock.get('generation')}, progress {lock.get('progress')}, expires {lock.get('expires_at')}.")
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


@contextmanager
def child_lock_fd(state_path):
    """Expose the held flock to an exec child so nested commands reuse it."""
    from .state import _private_path
    path = _private_path(str(state_path) + '.lock')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def run_child(config, state_path, lease, argv, flock_fd):
    env = dict(os.environ)
    if lease is not None:
        env[FENCE_ENV] = lease.env()
        lease.progress = 'exec'
    env[LOCK_FD_ENV] = str(flock_fd)
    try:
        completed = subprocess.run(argv, env=env, pass_fds=(flock_fd,), cwd=config['_root'])
    except OSError:
        raise DeployError('exec command could not be started.', code='command_unavailable') from None
    if lease is not None:
        lease._load_shared()
    return completed.returncode
