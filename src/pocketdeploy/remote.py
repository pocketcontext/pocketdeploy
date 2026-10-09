"""Standalone root-side reconciler. Its stdout is an allowlisted JSON response."""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.request

STAGE = 'request'
BASE = Path('/var/lib/pocketdeploy')
MANIFEST = BASE / 'manifest.json'
ONCE = Path('/usr/local/bin/once')
CHECKSUMS = {
    'x86_64': ('amd64', 'aef855da263721c6c1072ff5ebc4c17a52af8c8e80c46c5a9dd458e7ca3a7f35'),
    'aarch64': ('arm64', '97e32ba0fdac0ad5e6010851b306e3cb2616285a9eeb2e869ff7e71f4b442bbb'),
}


def run(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                                 'HOME': '/root', 'DEBIAN_FRONTEND': 'noninteractive', 'ONCE_NO_SELF_UPDATE': '1'})
    if result.returncode:
        raise RuntimeError('subprocess failed; output suppressed')
    return result.stdout


def save(path, data):
    temp = path.with_suffix('.tmp')
    with open(temp, 'w') as file:
        os.chmod(temp, 0o600)
        json.dump(data, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bootstrap():
    global STAGE
    STAGE = 'cloud-init'
    if shutil.which('cloud-init'):
        run('cloud-init', 'status', '--wait')
    STAGE = 'docker-install'
    if not shutil.which('docker'):
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', '-qq', 'docker.io', 'ca-certificates')
    STAGE = 'docker-service'
    run('systemctl', 'enable', '--now', 'docker')
    STAGE = 'host-firewall'
    # OCI Ubuntu's host firewall rejects web traffic before Docker's proxy starts.
    for port in ('80', '443'):
        rule = ('INPUT', '-p', 'tcp', '--dport', port, '-m', 'comment', '--comment', 'pocketdeploy-web', '-j', 'ACCEPT')
        check = subprocess.run(['iptables', '-C', *rule], capture_output=True)
        if check.returncode:
            run('iptables', '-I', *rule)
    if shutil.which('netfilter-persistent'):
        run('netfilter-persistent', 'save')
    STAGE = 'once-download'
    arch, checksum = CHECKSUMS[platform.machine()]
    if not ONCE.exists() or hashlib.sha256(ONCE.read_bytes()).hexdigest() != checksum:
        data = urllib.request.urlopen(f'https://github.com/basecamp/once/releases/download/v0.3.3/once-linux-{arch}', timeout=120).read()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise RuntimeError('ONCE checksum mismatch')
        temporary = ONCE.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.chmod(0o755)
        os.replace(temporary, ONCE)
    STAGE = 'once-service'
    # Disable the updater before installing its unit, including first startup.
    drop = Path('/etc/systemd/system/once-background.service.d')
    drop.mkdir(parents=True, exist_ok=True)
    (drop / 'pocketdeploy.conf').write_text('[Service]\nEnvironment="ONCE_NO_SELF_UPDATE=1"\n')
    if not Path('/etc/systemd/system/once-background.service').exists():
        run(str(ONCE), 'background', 'install')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'once-background')
    return {'once_version': 'v0.3.3', 'docker_source': 'ubuntu-package'}


def containers():
    if not shutil.which('docker'):
        return {}
    ids = run('docker', 'ps', '-a', '--filter', 'label=once', '--format', '{{.ID}}').split()
    records = json.loads(run('docker', 'inspect', *ids)) if ids else []
    selected = {}
    for record in records:
        settings = json.loads(record.get('Config', {}).get('Labels', {}).get('once', '{}'))
        name, host = settings.get('name'), settings.get('host')
        if not name or not host:
            continue
        if not re.fullmatch(re.escape('/once-app-' + name + '-') + '[0-9a-f]{6}', record.get('Name', '')):
            continue
        item = {'id': record['Id'], 'image_id': record['Image'], 'settings': settings,
                'restart': record.get('HostConfig', {}).get('RestartPolicy', {}).get('Name'),
                'running': record['State']['Running'], 'exit_code': record['State']['ExitCode'],
                'oom': record['State']['OOMKilled'],
                'volumes': sorted((m['Name'], m['Destination']) for m in record.get('Mounts', []) if m['Type'] == 'volume'),
                'binds': any(m['Type'] == 'bind' for m in record.get('Mounts', []))}
        if host in selected:
            raise RuntimeError('multiple app containers require recovery')
        selected[host] = item
    return selected


def normalized(app):
    return {'image': app['image'], 'env': app.get('resolved-env', {}),
            'disable_tls': app.get('disable_tls', False), 'cpus': app.get('cpus', 0),
            'memory': app.get('memory', 0), 'health-path': app.get('health-path', '/'), 'strategy': app.get('deploy-strategy', 'rolling'), 'timeout': app.get('deploy-stop-timeout', 300)}


def matching(app, current, previous):
    target = normalized(app)
    if not previous or previous['desired'] != target or not current or not current['running'] or current.get('restart') != 'always':
        return False
    actual = current['settings']
    return (actual.get('env', {}) == target['env'] and actual.get('disableTLS', False) == target['disable_tls']
            and actual.get('autoUpdate') is False and actual.get('backup', {}).get('autoBackup', False) is False
            and actual.get('resources', {}).get('cpus', 0) == target['cpus']
            and actual.get('resources', {}).get('memoryMB', 0) == target['memory']
            and current['image_id'] == previous['image_id'])


def arguments(app):
    result = ['--auto-update=false', '--auto-backup=false', '--disable-tls=' + str(app.get('disable_tls', False)).lower(),
              '--cpus', str(app.get('cpus', 0)), '--memory', str(app.get('memory', 0))]
    for key, value in app.get('resolved-env', {}).items():
        result.extend(['--env', key + '=' + value])
    return result


def resolve_image(image):
    run('docker', 'pull', image)
    data = json.loads(run('docker', 'image', 'inspect', image))[0]
    digests = data.get('RepoDigests', [])
    if not digests:
        raise RuntimeError('image has no immutable registry digest')
    return digests[0], data['Id']


class LocalTLS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def health(app):
    """Probe only this host's reverse proxy; never return content or follow redirects."""
    connection = None
    try:
        if app.get('disable_tls', False) or app['host'].endswith('.localhost'):
            connection = http.client.HTTPConnection('127.0.0.1', 80, timeout=15)
        else:
            connection = LocalTLS(app['host'], 443, timeout=15, context=ssl.create_default_context())
        connection.request('GET', app.get('health-path', '/'), headers={'Host': app['host']})
        response = connection.getresponse()
        return {'healthy': 200 <= response.status < 400, 'http_status': response.status}
    except Exception:
        return {'healthy': False, 'http_status': None}
    finally:
        if connection:
            connection.close()


def reconcile(request):
    deployment = request['deployment_id']
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {'deployment_id': deployment, 'apps': {}}
    if manifest['deployment_id'] != deployment:
        raise RuntimeError('host belongs to another deployment')
    apps = request['applications']
    desired_hosts = {app['host'] for app in apps}
    current = containers()
    if request['action'] == 'status':
        by_host = {app['host']: app for app in apps}
        summaries = []
        for host in sorted(set(manifest['apps']) | set(current) | desired_hosts):
            actual = current.get(host)
            record = manifest['apps'].get(host, {})
            # Health uses desired non-secret probe options, never env resolution.
            probe = by_host.get(host, {'host': host, **record.get('desired', {})})
            pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
            summary = {'host': host, 'running': bool(actual and actual['running']),
                       'managed': host in manifest['apps'], 'pending': pending.exists()}
            summary.update(health(probe) if actual and actual['running'] else {'healthy': False, 'http_status': None})
            summaries.append(summary)
        return {'applications': summaries, 'pending_operations': len(list(BASE.glob('*.pending')))}
    if desired_hosts.intersection(current) - set(manifest['apps']):
        raise RuntimeError('unmanaged application requires explicit adoption')
    actions = []
    for host in sorted(set(manifest['apps']) - desired_hosts):
        pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
        if pending.exists():
            raise RuntimeError('unfinished deployment; operator recovery required')
        actions.append({'host': host, 'action': 'remove-retain-data'})
        if request['action'] != 'converge':
            continue
        old = current.get(host)
        save(pending, {'host': host, 'deployment_id': deployment, 'action': 'remove'})
        if old:
            run('docker', 'update', '--restart=no', old['id'])
            run('docker', 'stop', '--time', str(manifest['apps'][host]['desired'].get('timeout', 300)), old['id'])
            stopped = containers()[host]
            if stopped['running'] or stopped['exit_code'] != 0 or stopped['oom'] or stopped.get('restart') != 'no':
                raise RuntimeError('application did not stop cleanly')
            run(str(ONCE), '-n', 'once', 'remove', host)
            if host in containers():
                raise RuntimeError('application removal verification failed')
        manifest.setdefault('retained', {})[host] = {'volumes': old['volumes'] if old else [], **manifest['apps'].pop(host)}
        save(MANIFEST, manifest)
        pending.unlink()
    for app in apps:
        host = app['host']
        pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
        if pending.exists():
            raise RuntimeError('unfinished deployment; operator recovery required')
        old = current.get(host)
        resolved = resolve_image(app['image']) if request['action'] == 'converge' else None
        if matching(app, old, manifest['apps'].get(host)):
            if resolved is not None and resolved[1] == old['image_id']:
                if not health(app)['healthy']:
                    raise RuntimeError('application HTTP health check failed')
                continue
            if resolved is None:
                if '@sha256:' not in app['image']:
                    actions.append({'host': host, 'action': 'verify-image-tag-at-converge'})
                continue
        actions.append({'host': host, 'action': 'update' if old else 'create'})
        if request['action'] != 'converge':
            continue
        if old and old['settings'].get('env') and not app.get('resolved-env'):
            raise RuntimeError('cannot clear final environment binding with pinned ONCE CLI')
        digest, image_id = resolved
        if old and (not old['running'] or old['binds'] or old['settings'].get('autoUpdate') is not False):
            raise RuntimeError('unsafe existing application state')
        save(pending, {'host': host, 'deployment_id': deployment})
        if old and app.get('deploy-strategy', 'rolling') == 'stop-first':
            run('docker', 'update', '--restart=no', old['id'])
            run('docker', 'stop', '--time', str(app.get('deploy-stop-timeout', 300)), old['id'])
            stopped = containers()[host]
            if stopped['id'] != old['id'] or stopped['running'] or stopped['exit_code'] != 0 or stopped['oom'] or stopped.get('restart') != 'no':
                raise RuntimeError('application did not stop cleanly')
        if old:
            run(str(ONCE), '-n', 'once', 'update', host, '--image', digest, *arguments(app))
        else:
            run(str(ONCE), '-n', 'once', 'deploy', digest, '--host', host, *arguments(app))
        new = containers().get(host)
        if not new or not new['running'] or new['image_id'] != image_id or new['binds'] or new.get('restart') != 'always':
            raise RuntimeError('application verification failed')
        if old and (new['id'] == old['id'] or new['volumes'] != old['volumes']):
            raise RuntimeError('application volume continuity failed')
        completed = {'desired': normalized(app), 'image_id': image_id}
        if not matching(app, new, completed):
            raise RuntimeError('application settings verification failed')
        if not health(app)['healthy']:
            raise RuntimeError('application HTTP health check failed')
        manifest['apps'][host] = completed
        save(MANIFEST, manifest)
        pending.unlink()
    if request['action'] == 'converge':
        current = containers()
    probes = {app['host']: health(app) for app in apps} if request['action'] != 'plan' else {}
    return {'actions': actions, 'applications': [{'host': h, 'running': c['running'], **probes.get(h, {})} for h, c in current.items()]}


def main():
    os.umask(0o077)
    request = json.load(sys.stdin)
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    if request['action'] in ('plan', 'status'):
        return reconcile(request)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(BASE / 'lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if request['action'] == 'bootstrap':
            return bootstrap()
        if request['action'] == 'converge':
            return reconcile(request)
        raise RuntimeError('unknown action')


if __name__ == '__main__':
    try:
        print(json.dumps(main()))
    except Exception as exc:
        safe = {'unfinished deployment; operator recovery required', 'application removal requires explicit operator action',
                'host belongs to another deployment', 'unmanaged application requires explicit adoption',
                'cannot clear final environment binding with pinned ONCE CLI'}
        print(json.dumps({'error': str(exc) if str(exc) in safe else 'operation failed; output suppressed', 'stage': STAGE}))
        sys.exit(1)
