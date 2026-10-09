"""SSH transport with deployment-owned host trust and no payload in argv."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import tempfile

from .common import DeployError, local_path
from .output import operation


class Host:
    def __init__(self, config, state, root):
        self.config, self.state, self.root = config, state, Path(root)
        self.key = local_path(self.root, config.get('ssh-private-key-file', '.ssh/id_ed25519'))
        self.pub = local_path(self.root, config.get('ssh-public-key-file', str(self.key) + '.pub'))
        self.hostkey = local_path(self.root, config.get('ssh-host-private-key-file', '.ssh/host_ed25519'))
        self.hostpub = local_path(self.root, config.get('ssh-host-public-key-file', str(self.hostkey) + '.pub'))
        self.known = local_path(self.root, config.get('ssh-known-hosts-file', '.ssh/known_hosts'))

    def prepare_keys(self):
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
        # Include a safe empty trust file in the pre-provision recovery set.
        self.known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.known.exists():
            fd = os.open(self.known, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        return self.pub.read_text().strip()

    def cloud_init(self):
        return '#cloud-config\n' + json.dumps({'ssh_deletekeys': True, 'ssh_keys': {
            'ed25519_private': self.hostkey.read_text(),
            'ed25519_public': self.hostpub.read_text().strip()}})

    def _argv(self, connection):
        public = self.hostpub.read_text().split()
        self.known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.known.write_text(f"{connection['ip']} {public[0]} {public[1]}\n")
        os.chmod(self.known, 0o600)
        return ['ssh', '-i', str(self.key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                '-o', 'UserKnownHostsFile=' + str(self.known),
                '-o', 'GlobalKnownHostsFile=/dev/null',
                connection.get('user', 'ubuntu') + '@' + connection['ip']]

    def _remote(self, connection, action):
        labels = {'bootstrap': 'SSH: host setup', 'plan': 'SSH: application plan',
                  'converge': 'SSH: application convergence', 'status': 'SSH: application status'}
        with operation(labels.get(action, 'SSH: host operation')):
            return self._remote_request(connection, action)

    def _remote_request(self, connection, action):
        source = Path(__file__).with_name('remote.py').read_text()
        request = {'action': action, 'deployment_id': self.state.deployment_id,
                   'applications': self.config.get('once', {}).get('applications', [])}
        # Only non-secret program text is sent as the SSH command. Data uses stdin.
        command = 'sudo python3 -c ' + shlex.quote(source)
        try:
            result = subprocess.run(self._argv(connection) + [command], input=json.dumps(request),
                                    capture_output=True, text=True, timeout=1200)
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
            except (ValueError, AttributeError):
                error = None
                stage = None
            if stage in {'cloud-init', 'docker-install', 'docker-service', 'host-firewall', 'once-download', 'once-service'}:
                raise DeployError('Host bootstrap failed at ' + stage + '; output suppressed')
            raise DeployError('Host operation failed' + (': ' + error if error in SAFE_ERRORS else '; output suppressed'))
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise DeployError('Host returned invalid response; output suppressed') from None

    def bootstrap(self, connection, operation_id):
        step = self.state.intent(operation_id, 'host-bootstrap', {})
        with operation('SSH: readiness'):
            deadline = time.monotonic() + 360
            while True:
                result = subprocess.run(self._argv(connection) + ['true'], capture_output=True)
                if result.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise DeployError('SSH readiness timed out; check network and pinned host key', code='command_timeout')
                time.sleep(5)
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

    def status(self, connection):
        return self._remote(connection, 'status')

    def ssh(self, connection, command=None):
        with operation('SSH: session'):
            return subprocess.call(self._argv(connection) + ([command] if command else []))


SAFE_ERRORS = {'unfinished deployment; operator recovery required',
               'application removal requires explicit operator action',
               'host belongs to another deployment',
               'unmanaged application requires explicit adoption',
               'cannot clear final environment binding with pinned ONCE CLI'}


def validate_config(config):
    once = config.get('once', {})
    if once.get('namespace', 'once') != 'once':
        raise DeployError('V1 supports the once namespace only.')
    allowed = {'host', 'image', 'env', 'resolved-env', 'deploy-strategy', 'deploy-stop-timeout',
               'auto_update', 'auto_backup', 'disable_tls', 'health-path', 'cpus', 'memory', 'smtp', 'manage-dns', 'github'}
    for app in once.get('applications', []):
        health_path = app.get('health-path', '/')
        if not isinstance(health_path, str) or not health_path.startswith('/') or any(ord(c) < 32 for c in health_path):
            raise DeployError('Application health-path must be an absolute HTTP path without control characters.')
        if set(app) - allowed:
            raise DeployError('Unsupported application field.')
        if app.get('auto_update', False) or app.get('auto_backup', False):
            raise DeployError('V1 requires automatic ONCE updates and backups disabled.')
        if app.get('github') or app.get('smtp') or app.get('manage-dns'):
            raise DeployError('V1 does not manage GitHub, SMTP or DNS.')
        if app.get('deploy-strategy', 'rolling') not in ('rolling', 'stop-first'):
            raise DeployError('Unknown deployment strategy.')
        for key in ('cpus', 'memory'):
            if type(app.get(key, 0)) is not int or app.get(key, 0) < 0:
                raise DeployError('Application resource limits must be nonnegative integers.')
        if type(app.get('deploy-stop-timeout', 300)) is not int or not 1 <= app.get('deploy-stop-timeout', 300) <= 3600:
            raise DeployError('Stop timeout must be between 1 and 3600 seconds.')
        for key in ('disable_tls', 'auto_update', 'auto_backup'):
            if key in app and type(app[key]) is not bool:
                raise DeployError('Application flags must be booleans.')
