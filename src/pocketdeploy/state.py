"""Private local deployment state. Cloud calls never run inside transactions."""
import fcntl
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .common import DeployError


def _private_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise DeployError('State paths must not contain symlinks.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    return path


@contextmanager
def deployment_lock(path):
    path = _private_path(str(path) + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise DeployError('Lock must be an owned regular file without hardlinks.')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('Another local deployment operation is running.') from None
        yield
    finally:
        os.close(fd)


class State:
    def __init__(self, path, profile, scope, create=False, read_only=False):
        self.path = Path(path).absolute()
        self.read_only = read_only
        if any(p.is_symlink() for p in [self.path, *self.path.parents]):
            raise DeployError('State paths must not contain symlinks.')
        exists = self.path.exists()
        if not exists and (not create or read_only):
            raise DeployError('Deployment state is missing; restore it before continuing.')
        if exists:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise DeployError('State must be an owned regular file without hardlinks.')
        else:
            self.path = _private_path(path)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(f'{self.path.as_uri()}?mode={"ro" if read_only else "rw"}', uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA trusted_schema=OFF')
            if exists:
                # Validate identity before DDL, pragmas that persist, or permissions changes.
                identity = self.get_meta('identity')
                if (not isinstance(identity, dict) or identity.get('schema') != 1
                        or identity.get('profile') != profile or identity.get('scope') != scope
                        or not isinstance(identity.get('deployment_id'), str)):
                    raise DeployError('Deployment state identity does not match configuration.')
            else:
                identity = {'profile': profile, 'scope': scope, 'deployment_id': str(uuid.uuid4()), 'schema': 1}
            if not read_only:
                os.chmod(self.path, 0o600)
                self.db.execute('PRAGMA journal_mode=DELETE')
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('PRAGMA foreign_keys=ON')
                self.db.execute('PRAGMA secure_delete=ON')
            if not exists:
                self.db.executescript('''
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE resources(name TEXT PRIMARY KEY,kind TEXT NOT NULL,
                        provider_id TEXT NOT NULL,attributes TEXT NOT NULL,owned INTEGER NOT NULL);
                    CREATE TABLE operations(id TEXT PRIMARY KEY,workflow TEXT NOT NULL,
                        desired_hash TEXT NOT NULL,status TEXT NOT NULL,started TEXT DEFAULT CURRENT_TIMESTAMP,
                        finished TEXT);
                    CREATE TABLE steps(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL
                        REFERENCES operations(id) ON DELETE CASCADE,step TEXT NOT NULL,payload TEXT NOT NULL,
                        status TEXT NOT NULL,result TEXT);
                ''')
                self.set_meta('identity', identity)
            for query in ('SELECT name,kind,provider_id,attributes,owned FROM resources LIMIT 0',
                          'SELECT id,workflow,desired_hash,status,started,finished FROM operations LIMIT 0',
                          'SELECT id,operation_id,step,payload,status,result FROM steps LIMIT 0'):
                self.db.execute(query)
            self.deployment_id = identity['deployment_id']
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            self.db.close()
            raise DeployError('Deployment state is invalid or incompatible.') from None
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _writable(self):
        if self.read_only:
            raise DeployError('Deployment state was opened read-only.')

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        self._writable()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    @staticmethod
    def _resource(row):
        if row is None:
            return None
        result = dict(row)
        result['attributes'] = json.loads(result['attributes'])
        result['owned'] = bool(result['owned'])
        return result

    def get_resource(self, name):
        return self._resource(self.db.execute('SELECT * FROM resources WHERE name=?', (name,)).fetchone())

    def resources(self):
        return [self._resource(r) for r in self.db.execute('SELECT * FROM resources ORDER BY name')]

    def put_resource(self, name, kind, provider_id, attributes, owned=True):
        self._writable()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO resources VALUES(?,?,?,?,?)',
                            (name, kind, provider_id, json.dumps(attributes), int(owned)))

    def remove_resource(self, name):
        self._writable()
        with self.db:
            self.db.execute('DELETE FROM resources WHERE name=?', (name,))

    def begin_operation(self, workflow, desired_hash):
        self._writable()
        operation = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO operations(id,workflow,desired_hash,status) VALUES(?,?,?,?)',
                            (operation, workflow, desired_hash, 'running'))
        return operation

    def finish_operation(self, operation, status):
        self._writable()
        if status not in {'succeeded', 'failed', 'interrupted', 'complete', 'completed'}:
            raise DeployError('Invalid operation outcome.')
        with self.db:
            self.db.execute('UPDATE operations SET status=?,finished=CURRENT_TIMESTAMP WHERE id=?', (status, operation))
            # A finished workflow can still hold unresolved provider requests.
            # Retain those alongside unfinished workflows and 100 resolved operations.
            self.db.execute('''WITH resolved AS (
                SELECT id, rowid AS sequence FROM operations
                WHERE finished IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM steps WHERE operation_id=operations.id AND status='pending'
                )
            ) DELETE FROM operations WHERE id IN (SELECT id FROM resolved)
                AND id NOT IN (SELECT id FROM resolved ORDER BY sequence DESC LIMIT 100)''')

    def intent(self, operation_id, step, payload):
        self._writable()
        step_id = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO steps VALUES(?,?,?,?,?,NULL)',
                            (step_id, operation_id, step, json.dumps(payload), 'pending'))
        return step_id

    def complete(self, step_id, result):
        self._writable()
        with self.db:
            self.db.execute('UPDATE steps SET status=?,result=? WHERE id=?', ('complete', json.dumps(result), step_id))

    def snapshot(self, path):
        path = _private_path(path)
        if path.exists():
            raise DeployError('Snapshot destination already exists.')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()

    def safe_status(self):
        receipt = self.get_meta('vault-state-document', {})
        backup = {key: receipt[key] for key in ('document', 'version', 'saved_at')
                  if isinstance(receipt, dict) and isinstance(receipt.get(key), str)}
        return {'deployment_id': self.deployment_id,
                'last_vault_backup': backup or None,
                'resource_count': self.db.execute('SELECT count(*) FROM resources').fetchone()[0],
                'pending_steps': self.db.execute("SELECT count(*) FROM steps WHERE status='pending'").fetchone()[0],
                'operations': [dict(r) for r in self.db.execute(
                    'SELECT id,workflow,status,started,finished FROM operations ORDER BY rowid DESC LIMIT 10')]}
