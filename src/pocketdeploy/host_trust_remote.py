"""Standalone root-only host-key replacement. Input and failures stay private."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(*argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('host trust setup failed')
    return result.stdout


def atomic(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.pocketdeploy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(request):
    if os.geteuid() != 0:
        raise RuntimeError('host trust setup requires root')
    # Cloud-init must finish before replacing its bootstrap identity.
    run('cloud-init', 'status', '--wait')
    key = Path('/etc/ssh/pocketdeploy_ed25519')
    public = request['public'].split()
    if len(public) < 2 or public[0] != 'ssh-ed25519':
        raise RuntimeError('invalid host key')
    atomic(key, request['private'])
    if run('ssh-keygen', '-y', '-f', str(key)).split()[:2] != public[:2]:
        raise RuntimeError('host key mismatch')
    atomic(Path(str(key) + '.pub'), ' '.join(public[:2]) + '\n')
    configuration = Path('/etc/ssh/sshd_config.d/00-pocketdeploy-host-key.conf')
    atomic(configuration, 'HostKey ' + str(key) + '\n')
    run('/usr/sbin/sshd', '-t')
    active = [line.split()[1] for line in run('/usr/sbin/sshd', '-T').splitlines()
              if line.startswith('hostkey ')]
    # Additional explicit HostKey directives could leave the leaked identity active.
    if active != [str(key)]:
        raise RuntimeError('ambiguous host keys')
    run('systemctl', 'reload', 'ssh')
    return {'installed': True}


if __name__ == '__main__':
    try:
        result = install(json.load(sys.stdin))
    except Exception:
        print(json.dumps({'error': 'host trust setup failed'}))
        sys.exit(1)
    print(json.dumps(result))
