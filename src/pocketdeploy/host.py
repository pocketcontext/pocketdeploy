"""SSH transport with deployment-owned host trust and no payload in argv."""
import json
import hashlib
import stat
import os
from pathlib import Path
import shlex
import subprocess
import time
import tempfile
import ipaddress
import re

from .common import DeployError, local_path
from .output import operation
from .config import validate_local_paths


class Host:
    def __init__(self, config, state, root):
        self.config, self.state, self.root = config, state, Path(root)
        self.smtp_settings = None
        self.key = local_path(self.root, config.get('ssh-private-key-file', '.ssh/id_ed25519'))
        self.pub = local_path(self.root, config.get('ssh-public-key-file', str(self.key) + '.pub'))
        self.hostkey = local_path(self.root, config.get('ssh-host-private-key-file', '.ssh/host_ed25519'))
        self.hostpub = local_path(self.root, config.get('ssh-host-public-key-file', str(self.hostkey) + '.pub'))
        self.known = local_path(self.root, config.get('ssh-known-hosts-file', '.ssh/known_hosts'))

    def prepare_keys(self):
        validate_local_paths(self.config, self.root)
        existing = getattr(self.state, 'get_resource', lambda name: None)('compute')
        if existing and any(not path.exists() for path in (self.key, self.pub, self.hostkey, self.hostpub)):
            raise DeployError('Existing compute SSH identity is missing; restore it from Vault.')
        pairs = ((self.key, self.pub), (self.hostkey, self.hostpub))
        if any(private.exists() != public.exists() for private, public in pairs):
            raise DeployError('SSH key pair is incomplete; restore complete identity')
        for private, public in pairs:
            if private.exists():
                continue
            for target in (private, public):
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Publish to configured paths exclusively; existing custom public
            # keys are authoritative, not replaceable copies of a sidecar.
            with tempfile.TemporaryDirectory(prefix='.colors-keys-', dir=self.root) as temporary:
                generated = Path(temporary) / 'key'
                result = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(generated)], capture_output=True)
                if result.returncode:
                    raise DeployError('SSH key generation failed; output suppressed')
                for source, target in ((generated, private), (Path(str(generated) + '.pub'), public)):
                    os.chmod(source, 0o600)
                    try:
                        os.link(source, target)
                    except FileExistsError:
                        raise DeployError('SSH key destination appeared; existing files were preserved.') from None
                    self._record_key(source, target)
        # Include a safe empty trust file in the pre-provision recovery set.
        self.known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.known.exists():
            fd = os.open(self.known, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            self._record_key(self.known, self.known, mutable=True)
        return self.pub.read_text().strip()

    def _record_key(self, source, target, mutable=False):
        # Existing user-supplied files are never implicitly adopted.
        if not hasattr(self.state, 'set_meta'):
            return
        records = self.state.get_meta('ssh-generated-files', {})
        records[str(target.relative_to(self.root))] = {
            'sha256': None if mutable else hashlib.sha256(source.read_bytes()).hexdigest(),
            'mutable': mutable,
        }
        self.state.set_meta('ssh-generated-files', records)

    def _cleanup_paths(self):
        paths = [self.key, self.pub, self.hostkey, self.hostpub, self.known]
        state_path = local_path(self.root, self.config.get('state-file', '.colors.sqlite'))
        reserved = {state_path, Path(str(state_path) + '.lock'),
                    *(Path(str(state_path) + suffix) for suffix in ('-journal', '-wal', '-shm')),
                    local_path(self.root, self.config.get('_file', 'colors.yml')),
                    local_path(self.root, '.envrc'), local_path(self.root, '.envrc.private')}
        if getattr(self.state, 'path', None):
            actual = Path(self.state.path)
            reserved.update({actual, *(Path(str(actual) + suffix) for suffix in ('.lock', '-journal', '-wal', '-shm'))})
        records = self.state.get_meta('ssh-generated-files', {})
        # Include recorded paths no longer present in desired configuration.
        for name in records:
            path = local_path(self.root, name)
            if path not in paths:
                paths.append(path)
        if len(set((self.key, self.pub, self.hostkey, self.hostpub, self.known))) != 5:
            raise DeployError('SSH cleanup paths must be distinct.')
        repositories = [app['github'] for app in self.config.get('once', {}).get('applications', []) if app.get('github')]
        repositories += self.state.get_meta('github-delete-keys', [])
        for row in self.state.db.execute("SELECT key,value FROM meta WHERE key LIKE 'github-key:%'"):
            if json.loads(row['value']):
                repositories.append(row['key'][len('github-key:'):])
        for repo in repositories:
            key = self.root / '.ssh' / ('github-' + hashlib.sha256(repo.lower().encode()).hexdigest()[:20])
            reserved.update({key, Path(str(key) + '.pub')})
        for path in paths:
            local_path(self.root, path)  # Reject symlinks created since initialization.
            if path in reserved:
                raise DeployError('SSH cleanup path overlaps configuration, state or GitHub authority.')
            if not path.exists():
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise DeployError('SSH cleanup requires owned regular files without hardlinks.')
            record = records.get(str(path.relative_to(self.root)))
            if not record:
                raise DeployError('SSH key ownership is unrecorded; verify provenance before deletion.')
            if record.get('mutable'):
                if path != self.known:
                    raise DeployError('Only the recorded SSH known-hosts file may have mutable contents.')
            elif record.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
                raise DeployError('SSH key contents changed since generation; refusing deletion.')
        return paths

    def plan_key_cleanup(self):
        files = [str(path.relative_to(self.root)) for path in self._cleanup_paths() if path.exists()]
        return [{'resource': 'ssh-key-files', 'action': 'delete' if files else 'absent', 'files': files}]

    def cleanup_keys(self):
        paths = self._cleanup_paths()
        existing = sum(path.exists() for path in paths)
        for path in paths:
            path.unlink(missing_ok=True)
        self.state.set_meta('ssh-generated-files', {})
        self.state.set_meta('ssh-host-trust', {})
        return {'deleted_key_files': existing}

    def cloud_init(self):
        return '#cloud-config\n' + json.dumps({'ssh_deletekeys': True, 'ssh_keys': {
            'ed25519_private': self.hostkey.read_text(),
            'ed25519_public': self.hostpub.read_text().strip()}})

    def trusted_public(self, connection=None):
        from .host_trust import trusted_public
        public = trusted_public(self)
        if connection and connection.get('instance_id') and connection['instance_id'] != self.state.get_resource('compute')['provider_id']:
            raise DeployError('SSH connection belongs to another instance.')
        return public

    def _argv(self, connection, *, public=None):
        validate_local_paths(self.config, self.root)
        user = connection.get('user', 'ubuntu')
        if not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
            raise DeployError('SSH username is invalid.')
        try:
            ipaddress.ip_address(connection['ip'])
        except (ValueError, TypeError, KeyError):
            raise DeployError('SSH destination must be an IP address.') from None
        public = (public if public is not None else self.trusted_public(connection)).split()
        self.known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.known.write_text(f"{connection['ip']} {public[0]} {public[1]}\n")
        os.chmod(self.known, 0o600)
        return ['ssh', '-F', '/dev/null', '-i', str(self.key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'HostKeyAlgorithms=ssh-ed25519',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                '-o', 'UserKnownHostsFile=' + str(self.known),
                '-o', 'GlobalKnownHostsFile=/dev/null',
                '-l', user, '--', connection['ip']]

    def _remote(self, connection, action):
        labels = {'bootstrap': 'SSH: host setup', 'plan': 'SSH: application plan',
                  'converge': 'SSH: application convergence', 'status': 'SSH: application status',
                  'delete-plan': 'SSH: retirement readiness'}
        with operation(labels.get(action, 'SSH: host operation')):
            budget = 1200 + sum(app.get('deploy-stop-timeout', 300) + app.get('deploy-ready-timeout', 60)
                                for app in self.config.get('once', {}).get('applications', []))
            return self._remote_request(connection, action, timeout=budget)

    def _remote_request(self, connection, action, extra=None, timeout=1200):
        source = Path(__file__).with_name('remote.py').read_text()
        applications = self.config.get('once', {}).get('applications', [])
        if action == 'bootstrap':
            applications = [{'smtp': any(app.get('smtp') for app in applications)}]
        elif action in ('plan', 'converge', 'adopt-app'):
            applications = [self.resolved_app(app) for app in applications]
        request = {'action': action, 'deployment_id': self.state.deployment_id,
                   'user': connection.get('user', 'ubuntu'),
                   'applications': applications}
        if extra:
            request.update(extra)
        # Only non-secret program text is sent as the SSH command. Data uses stdin.
        command = 'sudo python3 -c ' + shlex.quote(source)
        try:
            result = subprocess.run(self._argv(connection) + [command], input=json.dumps(request),
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeployError('SSH host operation timed out; remote work may still be running.',
                              code='command_timeout') from None
        except OSError:
            raise DeployError('SSH could not be started; check OpenSSH installation.',
                              code='command_unavailable') from None
        if result.returncode:
            try:
                response = json.loads(result.stdout)
                error = response.get('error')
                stage = response.get('stage')
                error = error if isinstance(error, str) else None
                stage = stage if isinstance(stage, str) else None
            except (ValueError, AttributeError):
                error = None
                stage = None
            if stage in {'cloud-init', 'docker-install', 'docker-service', 'host-firewall', 'once-download', 'once-service'}:
                raise DeployError('Host bootstrap failed at ' + stage + '; output suppressed')
            if error not in SAFE_ERRORS and isinstance(stage, str) and stage in APPLICATION_ERRORS:
                raise DeployError(APPLICATION_ERRORS[stage], code=stage.replace('-', '_'))
            raise DeployError('Host operation failed' + (': ' + error if error in SAFE_ERRORS else '; output suppressed'))
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise DeployError('Host returned invalid response; output suppressed') from None

    def resolved_app(self, app):
        if not app.get('smtp'):
            return app
        if not self.smtp_settings:
            raise DeployError('SMTP settings are unavailable; converge sending infrastructure first.')
        settings = self.smtp_settings
        return {**app, 'resolved-smtp': {'server': settings['server'], 'port': str(settings['port']),
                'username': settings['username'], 'password': settings['password'], 'from': settings['from']}}

    def smtp_test(self, connection, settings, recipient):
        from .smtp_test import smtp_test
        return smtp_test(self, connection, settings, recipient)

    def install_github(self, connection, targets):
        source = Path(__file__).with_name('remote.py').read_text()
        with operation('SSH: GitHub deployment authority'):
            return self._remote_request(connection, 'github-install', {
                'targets': [{**target, 'app': self.resolved_app(target['app'])} for target in targets], 'user': connection.get('user', 'ubuntu'), 'source': source})

    def rotate_key(self, connection, operation_id):
        compute = self.state.get_resource('compute')
        if not compute:
            raise DeployError('Host key rotation requires recorded compute identity.')
        if connection.get('instance_id') and connection['instance_id'] != compute['provider_id']:
            raise DeployError('SSH connection belongs to another instance.')
        payload = {'instance_id': compute['provider_id']}
        step = self.state.intent(operation_id, 'host-key-rotation', payload)
        with operation('SSH: readiness'):
            deadline = time.monotonic() + 360
            while True:
                trust = self.state.get_meta('ssh-host-trust', {})
                public = trust.get('public', self.hostpub.read_text())
                try:
                    result = subprocess.run(self._argv(connection, public=public) + ['true'], capture_output=True, timeout=20)
                    if result.returncode and trust and not trust.get('verified'):
                        result = subprocess.run(self._argv(connection, public=self.hostpub.read_text()) + ['true'], capture_output=True, timeout=20)
                except subprocess.TimeoutExpired:
                    result = subprocess.CompletedProcess([], 1)
                except OSError:
                    raise DeployError('SSH could not be started; output suppressed.') from None
                if result.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise DeployError('SSH readiness timed out; check network and pinned host key', code='command_timeout')
                time.sleep(5)
        from .host_trust import rotate
        rotate(self, connection)
        result = {'verified': True}
        self.state.complete(step, result)
        for row in self.state.db.execute("SELECT id,payload FROM steps WHERE step='host-key-rotation' AND status='pending'").fetchall():
            if json.loads(row['payload']) == payload:
                self.state.complete(row['id'], result)
        return result

    def bootstrap(self, connection, operation_id):
        step = self.state.intent(operation_id, 'host-bootstrap', {})
        self.rotate_key(connection, operation_id)
        result = self._remote(connection, 'bootstrap')
        self.state.complete(step, result)
        return result

    def plan(self, connection):
        return self._remote(connection, 'plan')['actions']

    def converge(self, connection, operation_id):
        step = self.state.intent(operation_id, 'applications-converge', {})
        result = self._remote(connection, 'converge')
        self.state.complete(step, result)
        return result

    def adopt_app(self, connection, operation_id, evidence):
        step = self.state.intent(operation_id, 'application-adoption', evidence)
        result = self._remote_request(connection, 'adopt-app', {'adoption': evidence})
        self.state.complete(step, result)
        for row in self.state.db.execute("SELECT id,payload FROM steps WHERE step='application-adoption' AND status='pending'").fetchall():
            if json.loads(row['payload']) == evidence:
                self.state.complete(row['id'], result)
        return result

    def plan_delete(self, connection):
        return self._remote(connection, 'delete-plan')

    def quiesce(self, connection, operation_id):
        readiness = self.plan_delete(connection)
        timeout = 120 + sum(item['timeout'] + 60 for item in readiness['actions'])
        compute = self.state.get_resource('compute')
        payload = {'instance_id': compute['provider_id'] if compute else None}
        step = self.state.intent(operation_id, 'host-quiesce', payload)
        with operation('SSH: retire delivery and stop applications'):
            result = self._remote_request(connection, 'quiesce', timeout=timeout)
        self.state.complete(step, result)
        # A confirmed clean retry resolves earlier uncertainty for this instance.
        for row in self.state.db.execute("SELECT id,payload FROM steps WHERE step='host-quiesce' AND status='pending'").fetchall():
            if json.loads(row['payload']) == payload:
                self.state.complete(row['id'], result)
        return result

    def status(self, connection):
        return self._remote(connection, 'status')

    def ssh(self, connection, command=None):
        with operation('SSH: session'):
            return subprocess.call(self._argv(connection) + ([command] if command else []))


SAFE_ERRORS = {'unfinished deployment; operator recovery required',
               'application removal requires explicit operator action',
               'host belongs to another deployment',
               'unmanaged application requires explicit adoption',
               'cannot clear final environment binding with pinned ONCE CLI',
               'host is retired; convergence is disabled',
               'application did not stop cleanly',
               'host retirement ownership verification failed', 'application adoption evidence does not match',
               'application already has managed ownership'}


def validate_config(config):
    from .github import validate_config as validate_github
    validate_github(config)
    once = config.get('once', {})
    if once.get('namespace', 'once') != 'once':
        raise DeployError('V1 supports the once namespace only.')
    allowed = {'host', 'image', 'env', 'resolved-env', 'deploy-strategy', 'deploy-stop-timeout', 'deploy-ready-timeout',
               'auto_update', 'auto_backup', 'disable_tls', 'health-path', 'cpus', 'memory', 'smtp', 'manage-dns', 'github', 'github-environment'}
    for app in once.get('applications', []):
        health_path = app.get('health-path', '/')
        if not isinstance(health_path, str) or not health_path.startswith('/') or any(ord(c) < 32 for c in health_path):
            raise DeployError('Application health-path must be an absolute HTTP path without control characters.')
        if set(app) - allowed:
            raise DeployError('Unsupported application field.')
        if app.get('auto_update', False) or app.get('auto_backup', False):
            raise DeployError('V1 requires automatic ONCE updates and backups disabled.')
        if app.get('deploy-strategy', 'rolling') not in ('rolling', 'stop-first'):
            raise DeployError('Unknown deployment strategy.')
        for key in ('cpus', 'memory'):
            if type(app.get(key, 0)) is not int or app.get(key, 0) < 0:
                raise DeployError('Application resource limits must be nonnegative integers.')
        if type(app.get('deploy-stop-timeout', 300)) is not int or not 1 <= app.get('deploy-stop-timeout', 300) <= 3600:
            raise DeployError('Stop timeout must be between 1 and 3600 seconds.')
        if type(app.get('deploy-ready-timeout', 60)) is not int or not 1 <= app.get('deploy-ready-timeout', 60) <= 3600:
            raise DeployError('Ready timeout must be between 1 and 3600 seconds.')
        for key in ('disable_tls', 'auto_update', 'auto_backup', 'smtp', 'manage-dns'):
            if key in app and type(app[key]) is not bool:
                raise DeployError('Application flags must be booleans.')


# Fixed messages only: a remote response is never trusted as display text.
APPLICATION_ERRORS = {
    'application-adoption': 'Application adoption failed; verify ownership evidence and drain previous delivery authority.',
    'application-inventory': 'Application inventory failed; inspect Docker service health on the VPS.',
    'application-recovery': 'Unfinished application deployment; inspect host state before recovering its pending marker.',
    'application-image-pull': 'Application image pull failed; check registry access and the configured image reference.',
    'application-image-inspect': 'Application image inspection failed; check the downloaded image and registry digest.',
    'application-stop': 'Application graceful stop failed; inspect application health before retrying.',
    'application-remove': 'ONCE application removal failed; inspect host state before recovering its pending marker.',
    'application-deploy': 'ONCE application deployment failed; check hostname DNS, public reachability and container health before recovering its pending marker.',
    'application-update': 'ONCE application update failed; check hostname DNS, public reachability and container health before recovering its pending marker.',
    'application-verification': 'Application verification failed; inspect container state, image, settings and volume continuity before recovering its pending marker.',
    'application-health': 'Application HTTP health check failed; check the configured health path and container health before retrying.',
}
