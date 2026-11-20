#!/usr/bin/env python3
"""Synthetic stand-in for the VaultContext lock/save/restore CLI contract.

State lives in the JSON file named by FAKE_VAULT_DB. It mirrors the server rules
that PocketDeploy relies on: NOWAIT acquire, whole-set comparison by document ID,
lock_policy required after the first acquire, fenced saves and owner-only breaks.
"""
import fcntl
from datetime import datetime, timezone
import json
import os
import secrets
import sys
import time
from pathlib import Path

DB = Path(os.environ['FAKE_VAULT_DB'])
OWNER = os.environ.get('FAKE_VAULT_ROLE', 'owner') == 'owner'


def uid():
    return ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(15))


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def refuse(code, **details):
    print(json.dumps({'error': {'code': code, **details}}))
    print(code + ': refused', file=sys.stderr)
    raise SystemExit(5)


def view(lock):
    return {k: lock.get(k) for k in ('holder', 'info', 'progress', 'expires_at', 'generation', 'state')}


def main():
    args = sys.argv[1:]
    with open(DB.with_suffix('.lock'), 'w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        db = json.loads(DB.read_text()) if DB.exists() else {}
        db.setdefault('documents', {})
        db.setdefault('versions', {})
        db.setdefault('lock', None)
        db.setdefault('policy', 'none')
        db.setdefault('log', [])
        result = handle(db, args)
        DB.write_text(json.dumps(db))
    print(json.dumps(result))


def live(lock):
    return lock and lock['state'] == 'held' and time.time() < lock['expires']


def fence_ok(db, fence):
    lock = db['lock']
    generation, lease = fence.split(':')
    return lock and lock['state'] == 'held' and lock['generation'] == int(generation) and lock['lease_id'] == lease


def handle(db, args):
    db['log'].append(args[:2] + ([a for a in args if a == '--fence'] and ['fenced']))
    if args[0] == 'lock':
        sub, vault = args[1], args[2]
        lock = db['lock']
        if '--require-unlocked' in args and db.get('session_closed'):
            raise SystemExit(6)
        if sub == 'status':
            documents = manifest(db)
            if not lock:
                return {'vault': vault, 'state': 'free', 'generation': 0,
                        'lock_policy': db['policy'], 'documents': documents}
            return {**view(lock), 'vault': vault, 'lock_policy': db['policy'], 'label': lock['info'].get('label'),
                    'holder_email': 'operator@example.com', 'expired_by_client_clock': not live(lock), 'documents': documents}
        if sub == 'acquire':
            if '--force' in args:
                if not OWNER:
                    print('HTTP 403', file=sys.stderr)
                    raise SystemExit(1)
                if (lock['generation'] if lock else 0) != int(option(args, '--observed-generation')):
                    refuse('generation_changed', **(view(lock) if lock else {}))
                if lock and lock['state'] == 'held':
                    lock['state'] = 'broken'
                    lock['generation'] += 1
            elif live(lock):
                refuse('lock_held', retry_after=5, **view(lock))
            elif lock and lock['state'] == 'held':
                lock['state'] = 'broken'
                lock['generation'] += 1
            documents = manifest(db)
            status = 'unchecked'
            details = None
            if '--local-manifest' in args:
                local = json.loads(Path(option(args, '--local-manifest')).read_text())['documents']
                status, details = classify(local, documents)
                if option(args, '--on-mismatch', 'fail') == 'fail' and status not in ('match', 'empty_remote'):
                    refuse('local_mismatch', local_status=status, local_details=details)
            generation = (lock['generation'] if lock else 0) + 1
            ttl = int(option(args, '--ttl', '900'))
            db['lock'] = {'holder': 'acct', 'lease_id': uid(), 'generation': generation, 'state': 'held',
                          'expires': time.time() + ttl, 'expires_at': 'later', 'ttl': ttl, 'progress': '',
                          'info': {**json.loads(option(args, '--info', '{}')), 'label': option(args, '--label')},
                          'renewals': 0}
            db['policy'] = 'required'
            new = db['lock']
            return {'lease_id': new['lease_id'], 'generation': generation, 'expires_at': 'later', 'server_time': 'now',
                    'ttl_seconds': ttl, 'local_status': status, 'local_details': details, 'documents': documents,
                    'fence': f"{generation}:{new['lease_id']}", 'previous': None}
        if sub == 'renew':
            if not fence_ok(db, option(args, '--fence')):
                refuse('lock_lost')
            now = time.time()
            lock['ttl'] = int(option(args, '--ttl', lock['ttl']))
            lock['expires'] = now + lock['ttl']
            lock['progress'] = option(args, '--progress', lock['progress'])
            lock['renewals'] += 1
            return {'expires_at': datetime.fromtimestamp(lock['expires'], timezone.utc).isoformat(),
                    'server_time': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'late': False}
        if sub == 'release':
            if not fence_ok(db, option(args, '--fence')):
                refuse('lock_lost')
            lock['state'] = 'released'
            lock['clean'] = '--clean' in args
            return {'released': True}
        if sub == 'break':
            if not OWNER:
                print('HTTP 403', file=sys.stderr)
                raise SystemExit(1)
            if not lock or lock['generation'] != int(option(args, '--observed-generation')):
                refuse('generation_changed')
            if lock['state'] == 'held':
                lock['state'] = 'broken'
                lock['generation'] += 1
                return {'generation': lock['generation'], 'state': 'broken', 'broken': True}
            return {'generation': lock['generation'], 'state': lock['state'], 'broken': False}
    if args[0] == 'save':
        path, document = Path(args[2]), option(args, '--document')
        fence = option(args, '--fence')
        if db['policy'] == 'required' and not fence:
            refuse('lock_required')
        if fence and not fence_ok(db, fence):
            refuse('stale_fence')
        doc = db['documents'].get(document) if document else None
        revision = doc['revision'] if doc else 0
        expected = option(args, '--expect-revision')
        if expected is not None and int(expected) != revision:
            print('HTTP 409', file=sys.stderr)
            raise SystemExit(4)
        document = document or uid()
        version = uid()
        db['versions'][version] = path.read_bytes().hex()
        db['documents'][document] = {'revision': revision + 1, 'version': version, 'archived': False,
                                     'name': option(args, '--name')}
        db['log'].append(['saved', option(args, '--name')])
        return {'id': document, 'version': version, 'revision': revision + 1}
    if args[0] == 'restore':
        target = Path(option(args, '--to'))
        target.write_bytes(bytes.fromhex(db['versions'][option(args, '--version')]))
        os.chmod(target, 0o600)
        return {'restored': option(args, '--version')}
    raise SystemExit(2)


def manifest(db):
    return [{'id': k, 'revision': d['revision'], 'version': d['version'], 'archived': d['archived'],
             'archive_revision': 0, 'chunk_count': 1, 'chunks': []} for k, d in sorted(db['documents'].items())]


def classify(local, documents):
    remote = {d['id']: d for d in documents}
    details = {'stale': [], 'missing_remote': [], 'extra_remote': [], 'diverged': []}
    for item in local:
        r = remote.get(item['id'])
        if not r or r['archived']:
            details['missing_remote'].append(item['id'])
        elif item['revision'] > r['revision'] or (item['revision'] == r['revision'] and item['version'] != r['version']):
            details['diverged'].append(item['id'])
        elif item['revision'] < r['revision']:
            details['stale'].append(item['id'])
    listed = {item['id'] for item in local}
    details['extra_remote'] = [d['id'] for d in documents if not d['archived'] and d['id'] not in listed]
    active = [d for d in documents if not d['archived']]
    if not local and not active:
        return 'empty_remote', details
    for status in ('diverged', 'missing_remote', 'extra_remote', 'stale'):
        if details[status]:
            return status, details
    return 'match', details


if __name__ == '__main__':
    main()
