"""Durable rotation away from the cloud metadata bootstrap SSH identity."""
from pathlib import Path
import json
import shlex
import subprocess
import tempfile

from .common import DeployError


META = 'ssh-host-trust'


def instance_id(host):
    compute = host.state.get_resource('compute')
    if not compute:
        raise DeployError('Host trust requires recorded compute identity; converge first.')
    return compute['provider_id']


def trusted_public(host):
    trust = host.state.get_meta(META, {})
    if not trust.get('verified') or trust.get('instance_id') != instance_id(host):
        raise DeployError('Host key rotation is incomplete; run rotate-host-key or converge before SSH or application operations.')
    return trust['public']


def request(host, connection, public, command, payload=None):
    try:
        return subprocess.run(host._argv(connection, public=public) + [command],
                              input=payload, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError('SSH host key rotation was interrupted; rerun rotate-host-key to recover; output suppressed.') from None


def rotate(host, connection):
    identity = instance_id(host)
    if connection.get('instance_id') and connection['instance_id'] != identity:
        raise DeployError('SSH connection belongs to another instance.')
    trust = host.state.get_meta(META, {})
    if trust and trust.get('instance_id') != identity:
        raise DeployError('Host trust belongs to another instance; restore matching state.')
    if trust.get('verified'):
        return
    if not trust:
        with tempfile.TemporaryDirectory(prefix='.host-rotation-', dir=host.root) as directory:
            key = Path(directory) / 'key'
            result = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], capture_output=True)
            if result.returncode:
                raise DeployError('Host key rotation generation failed; output suppressed.')
            trust = {'instance_id': identity, 'private': key.read_text(),
                     'public': Path(str(key) + '.pub').read_text().strip(), 'verified': False}
        # Commit recovery material before any remote mutation. The same pending
        # key survives lost responses, daemon reloads and controller restarts.
        host.state.set_meta(META, trust)
    public = trust['public']
    if request(host, connection, public, 'true').returncode:
        public = host.hostpub.read_text()
    # Even when the replacement key already answers after interruption, rerun
    # effective-config validation: possession alone does not prove the leaked
    # bootstrap key is no longer accepted by sshd.
    source = Path(__file__).with_name('host_trust_remote.py').read_text()
    result = request(host, connection, public,
                     'sudo python3 -c ' + shlex.quote(source),
                     json.dumps({'private': trust['private'], 'public': trust['public']}))
    if result.returncode:
        raise DeployError('SSH host key rotation failed; rerun rotate-host-key to recover; output suppressed.')
    # A successful installer response alone never authorizes app secrets.
    if request(host, connection, trust['public'], 'true').returncode:
        raise DeployError('Rotated SSH host identity could not be verified; rerun rotate-host-key; output suppressed.')
    host.state.set_meta(META, {**trust, 'verified': True})
