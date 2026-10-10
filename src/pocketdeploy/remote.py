"""Standalone root-side reconciler. Its stdout is an allowlisted JSON response."""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.request

STAGE = 'request'
BASE = Path('/var/lib/pocketdeploy')
MANIFEST = BASE / 'manifest.json'
ONCE = Path('/usr/local/bin/once')
CHECKSUMS = {
    'x86_64': ('amd64', 'aef855da263721c6c1072ff5ebc4c17a52af8c8e80c46c5a9dd458e7ca3a7f35'),
    'aarch64': ('arm64', '97e32ba0fdac0ad5e6010851b306e3cb2616285a9eeb2e869ff7e71f4b442bbb'),
}


def run(*args, timeout=None):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
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


def metadata_firewall():
    # A Docker pre-start hook recreates these rules on every daemon start and
    # reboot. DOCKER-USER is evaluated before Docker's container accept rules.
    script = Path('/usr/local/sbin/pocketdeploy-metadata-firewall')
    script.write_text("""#!/bin/sh
set -eu
iptables -N DOCKER-USER 2>/dev/null || iptables -S DOCKER-USER >/dev/null
for chain in DOCKER-USER FORWARD; do
  iptables -C "$chain" -d 169.254.169.254/32 -j DROP 2>/dev/null || iptables -I "$chain" 1 -d 169.254.169.254/32 -j DROP
done
""")
    script.chmod(0o755)
    drop = Path('/etc/systemd/system/docker.service.d')
    drop.mkdir(parents=True, exist_ok=True)
    (drop / 'pocketdeploy-metadata.conf').write_text(
           '[Service]\nExecStartPre=/usr/local/sbin/pocketdeploy-metadata-firewall\n')
    run(str(script))
    run('systemctl', 'daemon-reload')


def bootstrap(request=None):
    global STAGE
    STAGE = 'cloud-init'
    if shutil.which('cloud-init'):
        run('cloud-init', 'status', '--wait')
    STAGE = 'docker-install'
    if not shutil.which('docker'):
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', '-qq', 'docker.io', 'ca-certificates')
    if request and any(app.get('smtp') for app in request.get('applications', [])) and not shutil.which('s-nail'):
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', '-qq', 's-nail')
    STAGE = 'host-firewall'
    metadata_firewall()
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
            'smtp': app.get('resolved-smtp', {}), 'disable_tls': app.get('disable_tls', False), 'cpus': app.get('cpus', 0),
            'memory': app.get('memory', 0), 'health-path': app.get('health-path', '/'), 'strategy': app.get('deploy-strategy', 'rolling'), 'timeout': app.get('deploy-stop-timeout', 300)}


def image_repository(image):
    repository = image.split('@', 1)[0]
    # A colon in the registry authority is a port, not an image tag.
    slash = repository.rfind('/')
    colon = repository.rfind(':')
    return repository[:colon] if colon > slash else repository


def matching(app, current, previous, resolved=None):
    target = normalized(app)
    prior = dict(previous['desired']) if previous else {}
    # Old dispatchers persisted their release digest as desired configuration.
    # Migrate only after resolving the configured image to the running image.
    if (resolved and current and image_repository(prior.get('image', '')) == image_repository(target['image'])
            and '@sha256:' in prior.get('image', '') and resolved[1] == current['image_id']):
        prior['image'] = target['image']
    if not previous or {**prior, 'smtp': prior.get('smtp', {})} != target or not current or not current['running'] or current.get('restart') != 'always':
        return False
    actual = current['settings']
    if {k: v for k, v in actual.get('smtp', {}).items() if v} != target['smtp']:
        return False
    return ((actual.get('env') or {}) == target['env'] and actual.get('disableTLS', False) == target['disable_tls']
            and actual.get('autoUpdate') is False and actual.get('backup', {}).get('autoBackup', False) is False
            and actual.get('resources', {}).get('cpus', 0) == target['cpus']
            and actual.get('resources', {}).get('memoryMB', 0) == target['memory']
            and current['image_id'] == previous['image_id'])


def arguments(app):
    result = ['--auto-update=false', '--auto-backup=false', '--disable-tls=' + str(app.get('disable_tls', False)).lower(),
              '--cpus', str(app.get('cpus', 0)), '--memory', str(app.get('memory', 0))]
    for key in ('server', 'port', 'username', 'password', 'from'):
        result.extend(['--smtp-' + key, app.get('resolved-smtp', {}).get(key, '')])
    for key, value in app.get('resolved-env', {}).items():
        result.extend(['--env', key + '=' + value])
    return result


def resolve_image(image):
    global STAGE
    STAGE = 'application-image-pull'
    run('docker', 'pull', image)
    STAGE = 'application-image-inspect'
    data = json.loads(run('docker', 'image', 'inspect', image))[0]
    digests = data.get('RepoDigests', [])
    if not digests:
        raise RuntimeError('image has no immutable registry digest')
    return digests[0], data['Id']


class LocalTLS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def health(app, timeout=15):
    """Probe only this host's reverse proxy; never return content or follow redirects."""
    connection = None
    try:
        if app.get('disable_tls', False) or app['host'].endswith('.localhost'):
            connection = http.client.HTTPConnection('127.0.0.1', 80, timeout=timeout)
        else:
            connection = LocalTLS(app['host'], 443, timeout=timeout, context=ssl.create_default_context())
        connection.request('GET', app.get('health-path', '/'), headers={'Host': app['host']})
        response = connection.getresponse()
        return {'healthy': 200 <= response.status < 400, 'http_status': response.status}
    except Exception:
        return {'healthy': False, 'http_status': None}
    finally:
        if connection:
            connection.close()


def wait_healthy(app, budget=60):
    """Allow proxy startup and certificate issuance; never clear pending on failure."""
    deadline = time.monotonic() + budget
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if health(app, timeout=min(5, remaining))['healthy']:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(2, remaining))


def reconcile(request):
    global STAGE
    STAGE = 'application-inventory'
    deployment = request['deployment_id']
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {'deployment_id': deployment, 'apps': {}}
    if manifest['deployment_id'] != deployment:
        raise RuntimeError('host belongs to another deployment')
    if request['action'] != 'status' and (BASE / 'retirement.json').exists():
        raise RuntimeError('host is retired; convergence is disabled')
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
                       'managed': host in manifest['apps'], 'pending': pending.exists(),
                       'container_id': actual['id'] if actual else None,
                       'image_id': actual['image_id'] if actual else None}
            summary.update(health(probe) if actual and actual['running'] else {'healthy': False, 'http_status': None})
            summaries.append(summary)
        return {'applications': summaries, 'pending_operations': len(list(BASE.glob('*.pending')))}
    if desired_hosts.intersection(current) - set(manifest['apps']):
        raise RuntimeError('unmanaged application requires explicit adoption')
    actions = []
    for host in sorted(set(manifest['apps']) - desired_hosts) if not request.get('retain_other_apps') else []:
        pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
        STAGE = 'application-recovery'
        if pending.exists():
            raise RuntimeError('unfinished deployment; operator recovery required')
        STAGE = 'application-inventory'
        actions.append({'host': host, 'action': 'remove-retain-data'})
        if request['action'] != 'converge':
            continue
        old = current.get(host)
        save(pending, {'host': host, 'deployment_id': deployment, 'action': 'remove'})
        if old:
            STAGE = 'application-stop'
            run('docker', 'update', '--restart=no', old['id'])
            run('docker', 'stop', '--time', str(manifest['apps'][host]['desired'].get('timeout', 300)), old['id'])
            stopped = containers()[host]
            if stopped['running'] or stopped['exit_code'] != 0 or stopped['oom'] or stopped.get('restart') != 'no':
                raise RuntimeError('application did not stop cleanly')
            STAGE = 'application-remove'
            run(str(ONCE), '-n', 'once', 'remove', host)
            if host in containers():
                raise RuntimeError('application removal verification failed')
        manifest.setdefault('retained', {})[host] = {'volumes': old['volumes'] if old else [], **manifest['apps'].pop(host)}
        save(MANIFEST, manifest)
        pending.unlink()
    for app in apps:
        host = app['host']
        pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
        STAGE = 'application-recovery'
        if pending.exists():
            raise RuntimeError('unfinished deployment; operator recovery required')
        STAGE = 'application-inventory'
        old = current.get(host)
        resolved = resolve_image(app.get('deploy-image', app['image'])) if request['action'] == 'converge' else None
        STAGE = 'application-inventory'
        if matching(app, old, manifest['apps'].get(host), resolved):
            if resolved is not None and resolved[1] == old['image_id']:
                STAGE = 'application-health'
                if not health(app)['healthy']:
                    raise RuntimeError('application HTTP health check failed')
                completed = {'desired': normalized(app), 'image_id': resolved[1], 'image_digest': resolved[0]}
                if manifest['apps'][host] != completed:
                    manifest['apps'][host] = completed
                    save(MANIFEST, manifest)
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
            STAGE = 'application-stop'
            run('docker', 'update', '--restart=no', old['id'])
            run('docker', 'stop', '--time', str(app.get('deploy-stop-timeout', 300)), old['id'])
            stopped = containers()[host]
            if stopped['id'] != old['id'] or stopped['running'] or stopped['exit_code'] != 0 or stopped['oom'] or stopped.get('restart') != 'no':
                raise RuntimeError('application did not stop cleanly')
        if old:
            STAGE = 'application-update'
            run(str(ONCE), '-n', 'once', 'update', host, '--image', digest, *arguments(app))
        else:
            STAGE = 'application-deploy'
            run(str(ONCE), '-n', 'once', 'deploy', digest, '--host', host, *arguments(app))
        STAGE = 'application-verification'
        new = containers().get(host)
        if not new or not new['running'] or new['image_id'] != image_id or new['binds'] or new.get('restart') != 'always':
            raise RuntimeError('application verification failed')
        if old and (new['id'] == old['id'] or new['volumes'] != old['volumes']):
            raise RuntimeError('application volume continuity failed')
        completed = {'desired': normalized(app), 'image_id': image_id, 'image_digest': digest}
        if not matching(app, new, completed):
            raise RuntimeError('application settings verification failed')
        STAGE = 'application-health'
        if not wait_healthy(app):
            raise RuntimeError('application HTTP health check failed')
        manifest['apps'][host] = completed
        save(MANIFEST, manifest)
        pending.unlink()
    STAGE = 'application-inventory'
    if request['action'] == 'converge':
        current = containers()
    probes = {app['host']: health(app) for app in apps} if request['action'] != 'plan' else {}
    return {'actions': actions, 'applications': [{'host': h, 'running': c['running'], **probes.get(h, {})} for h, c in current.items()]}


def authorized_keys(account, lines=None):
    """Use anchored directories and exclusive files for privileged key updates."""
    home = os.open(account.pw_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory = None
    try:
        if lines is not None:
            try:
                os.mkdir('.ssh', mode=0o700, dir_fd=home)
            except FileExistsError:
                pass
        try:
            directory = os.open('.ssh', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        except FileNotFoundError:
            return []
        if lines is not None:
            os.fchown(directory, account.pw_uid, account.pw_gid)
        try:
            fd = os.open('authorized_keys', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            existing = []
        else:
            with os.fdopen(fd) as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise RuntimeError('unsafe authorized keys file')
                existing = source.read().splitlines()
        if lines is None:
            return existing
        temporary = '.pocketdeploy-' + os.urandom(16).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write('\n'.join(lines) + ('\n' if lines else ''))
                output.flush()
                os.fsync(output.fileno())
                os.fchown(output.fileno(), account.pw_uid, account.pw_gid)
            os.replace(temporary, 'authorized_keys', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        if directory is not None:
            os.close(directory)
        os.close(home)


def retire(request, mutate=False):
    """Retirement is permanent on this host; retries only finish clean stops."""
    global STAGE
    STAGE = 'application-inventory'
    deployment = request['deployment_id']
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {'deployment_id': deployment, 'apps': {}}
    if manifest.get('deployment_id') != deployment:
        raise RuntimeError('host belongs to another deployment')
    retirement = BASE / 'retirement.json'
    record = json.loads(retirement.read_text()) if retirement.exists() else None
    if record and record.get('deployment_id') != deployment:
        raise RuntimeError('host belongs to another deployment')
    apps = manifest['apps']
    if apps and not shutil.which('docker'):
        raise RuntimeError('host retirement ownership verification failed')
    current = containers()
    if set(current) - set(apps):
        raise RuntimeError('unmanaged application requires explicit adoption')
    # Existing pending convergence is not safe to reinterpret as retirement.
    for pending in BASE.glob('*.pending'):
        entry = json.loads(pending.read_text())
        if not record or entry.get('action') != 'retire' or entry.get('deployment_id') != deployment:
            raise RuntimeError('unfinished deployment; operator recovery required')
        STAGE = 'application-inventory'
    actions = []
    for host, app in sorted(apps.items()):
        actual = current.get(host)
        if record and ((actual and actual['id'] != record['containers'].get(host))
                       or (not actual and host in record['containers'])):
            raise RuntimeError('host retirement ownership verification failed')
        timeout = app.get('desired', {}).get('timeout', 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise RuntimeError('host retirement ownership verification failed')
        if actual and (actual['binds'] or actual['image_id'] != app['image_id']
                       or actual['settings'].get('autoUpdate') is not False):
            raise RuntimeError('host retirement ownership verification failed')
        if actual and not actual['running'] and (actual['exit_code'] != 0 or actual['oom']):
            raise RuntimeError('application did not stop cleanly')
        actions.append({'host': host, 'action': 'stop-retain-data', 'timeout': timeout})
    account = pwd.getpwnam(request.get('user', 'ubuntu'))
    lines = authorized_keys(account)
    directory = BASE / 'github'
    retired_directory = BASE / 'retired-github'
    if directory.exists() and retired_directory.exists():
        raise RuntimeError('host retirement ownership verification failed')
    authority_directory = retired_directory if retired_directory.exists() else directory
    # Establish key ownership from private host-side configuration, including
    # repositories no longer present in the desired configuration.
    tokens = set()
    for config in authority_directory.glob('*.json'):
        data = json.loads(config.read_text())
        if data.get('deployment_id') != deployment or not re.fullmatch('[0-9a-f]{20}', config.stem):
            raise RuntimeError('host retirement ownership verification failed')
        tokens.add(config.stem)
    markers = {'pocketdeploy-github-' + token for token in tokens}
    for line in lines:
        marker = line.rsplit(' ', 1)[-1]
        if marker.startswith('pocketdeploy-github-') and marker not in markers:
            raise RuntimeError('host retirement ownership verification failed')
    remaining = [line for line in lines if line.rsplit(' ', 1)[-1] not in markers]
    if not mutate:
        return {'actions': actions, 'retired': bool(record), 'github_keys_to_revoke': len(lines) - len(remaining)}
    # Persist before any mutation; old dispatcher versions also check per-app
    # pending markers, so already-loaded jobs cannot restart apps after the lock.
    if not record:
        record = {'deployment_id': deployment, 'phase': 'stopping', 'containers': {h: c['id'] for h, c in current.items()}}
        save(retirement, record)
    # Legacy dispatchers import before locking, then read config after locking.
    # Moving the whole directory fences even a queued old module version.
    if directory.exists():
        os.rename(directory, retired_directory)
        fd = os.open(BASE, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for host in apps:
        pending = BASE / (hashlib.sha256(host.encode()).hexdigest() + '.pending')
        save(pending, {'deployment_id': deployment, 'host': host, 'action': 'retire'})
    authorized_keys(account, remaining)
    STAGE = 'application-stop'
    for action in actions:
        host = action['host']
        actual = current.get(host)
        if not actual:
            if host in record['containers']:
                raise RuntimeError('host retirement ownership verification failed')
            continue
        if actual['id'] != record['containers'].get(host):
            raise RuntimeError('host retirement ownership verification failed')
        run('docker', 'update', '--restart=no', actual['id'], timeout=30)
        if actual['running']:
            run('docker', 'stop', '--time', str(action['timeout']), actual['id'], timeout=action['timeout'] + 30)
        stopped = containers().get(host)
        if not stopped or stopped['id'] != actual['id'] or stopped['running'] or stopped['exit_code'] != 0 or stopped['oom'] or stopped.get('restart') != 'no':
            raise RuntimeError('application did not stop cleanly')
    record['phase'] = 'quiesced'
    save(retirement, record)
    return {'quiesced': True, 'applications': [item['host'] for item in actions],
            'revoked_github_keys': len(lines) - len(remaining)}


def install_github(request):
    if (BASE / 'retirement.json').exists():
        raise RuntimeError('host is retired; convergence is disabled')
    account = pwd.getpwnam(request['user'])
    manifest = json.loads(MANIFEST.read_text())
    if manifest['deployment_id'] != request['deployment_id']:
        raise RuntimeError('host belongs to another deployment')
    directory = BASE / 'github'
    directory.mkdir(mode=0o700, exist_ok=True)
    source = directory / 'reconciler.py'
    source_temp = source.with_suffix('.tmp')
    source_temp.write_text(request['source'])
    source_temp.chmod(0o600)
    os.replace(source_temp, source)
    lines = authorized_keys(account)
    for target in request['targets']:
        app = target['app']
        if app['host'] not in manifest['apps']:
            raise RuntimeError('unmanaged application requires explicit adoption')
        public = target['public_key'].split()
        if len(public) < 2 or public[0] != 'ssh-ed25519' or not re.fullmatch('[A-Za-z0-9+/=]+', public[1]):
            raise RuntimeError('invalid deployment public key')
        token = hashlib.sha256(target['repository'].lower().encode()).hexdigest()[:20]
        config = directory / (token + '.json')
        save(config, {'action': 'converge', 'deployment_id': request['deployment_id'],
                      'applications': [app], 'retain_other_apps': True})
        dispatcher = directory / (token + '.py')
        # No original SSH command is evaluated. A release digest is optional but
        # must name the configured image repository exactly.
        dispatcher_temp = dispatcher.with_suffix('.tmp')
        dispatcher_temp.write_text("import importlib.util,json,os,re,sys\n"
            + "try:\n"
            + " s=importlib.util.spec_from_file_location('reconciler'," + repr(str(source)) + "); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
            + " with open(m.BASE/'lock','a') as lock:\n"
            + "  m.fcntl.flock(lock,m.fcntl.LOCK_EX)\n"
            + "  request=json.load(open(" + repr(str(config)) + "))\n"
            + "  command=os.environ.get('SSH_ORIGINAL_COMMAND','')\n"
            + "  if command:\n"
            + "   image=request['applications'][0]['image']; repo=m.image_repository(image)\n"
            + "   if not re.fullmatch('deploy '+re.escape(repo)+'@sha256:[0-9a-f]{64}',command): raise RuntimeError()\n"
            + "   request['applications'][0]['deploy-image']=command[7:]\n"
            + "  result=m.reconcile(request)\n"
            + " print(json.dumps({'ok':True}))\n"
            + "except Exception:\n print('Deployment failed; output suppressed',file=sys.stderr); sys.exit(1)\n")
        dispatcher_temp.chmod(0o600)
        os.replace(dispatcher_temp, dispatcher)
        marker = 'pocketdeploy-github-' + token
        if not target.get('preserve_existing_keys', False):
            lines = [line for line in lines if not line.endswith(' ' + marker)]
        # Idempotently add the candidate while retaining prior authority until
        # the controller has updated GitHub's secret.
        lines = [line for line in lines if not (line.endswith(' ' + marker)
                 and (' ' + public[0] + ' ' + public[1] + ' ') in line)]
        lines.append('restrict,command="sudo -n --preserve-env=SSH_ORIGINAL_COMMAND /usr/bin/python3 '
                     + str(dispatcher) + '" ' + public[0] + ' ' + public[1] + ' ' + marker)
    authorized_keys(account, lines)
    return {'installed': len(request['targets'])}


def main():
    os.umask(0o077)
    request = json.load(sys.stdin)
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    if request['action'] == 'delete-plan':
        return retire(request, mutate=False)
    if request['action'] in ('plan', 'status'):
        return reconcile(request)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(BASE / 'lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if request['action'] == 'quiesce':
            return retire(request, mutate=True)
        if request['action'] == 'bootstrap':
            if (BASE / 'retirement.json').exists():
                raise RuntimeError('host is retired; convergence is disabled')
            return bootstrap(request)
        if request['action'] == 'converge':
            return reconcile(request)
        if request['action'] == 'github-install':
            return install_github(request)
        raise RuntimeError('unknown action')


if __name__ == '__main__':
    try:
        print(json.dumps(main()))
    except Exception as exc:
        safe = {'unfinished deployment; operator recovery required', 'application removal requires explicit operator action',
                'host belongs to another deployment', 'unmanaged application requires explicit adoption',
                'cannot clear final environment binding with pinned ONCE CLI',
                'host is retired; convergence is disabled', 'application did not stop cleanly',
                'host retirement ownership verification failed'}
        print(json.dumps({'error': str(exc) if str(exc) in safe else 'operation failed; output suppressed', 'stage': STAGE}))
        sys.exit(1)
