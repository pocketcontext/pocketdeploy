"""Prepared pinned SSH commands, safe to use without controller state in workers."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import shlex
import socket
import subprocess
import time

from .common import DeployError


@dataclass(frozen=True)
class SSHTransport:
    argv: tuple[str, ...]

    def python_process(self, source, payload, timeout=1200):
        try:
            return subprocess.run([*self.argv, 'sudo python3 -c ' + shlex.quote(source)],
                                  input=json.dumps(payload), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeployError('SSH host operation timed out; remote work may still be running.', code='command_timeout') from None
        except OSError:
            raise DeployError('SSH could not be started; check OpenSSH installation.', code='command_unavailable') from None

    def run_python(self, connection, source, payload, timeout=1200):
        # Connection is retained for the Host-compatible interface, never used to retarget trust.
        if connection.get('ip') != self.argv[-1] or connection.get('user', 'ubuntu') != self.argv[-3]:
            raise DeployError('Prepared SSH transport cannot change destination.')
        result = self.python_process(source, payload, timeout)
        if result.returncode:
            raise DeployError('Remote Python operation failed; output suppressed')
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise DeployError('Host returned invalid response; output suppressed') from None

    def tunnel_argv(self, local_port, remote_port):
        for port in (local_port, remote_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise DeployError('Tunnel ports must be integers between 1 and 65535.')
        return [*self.argv[:-2], '-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', *self.argv[-2:]]

    @contextmanager
    def tunnel(self, local_port, remote_port, *, timeout=20):
        argv = self.tunnel_argv(local_port, remote_port)
        # Refuse an already occupied port before starting SSH; readiness must
        # never silently route application credentials to an existing listener.
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', local_port))
        except OSError:
            raise DeployError('Local tunnel port is unavailable.') from None
        try:
            process = subprocess.Popen(argv,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise DeployError('SSH tunnel could not start; output suppressed.') from None
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                ready = False
                try:
                    with socket.create_connection(('127.0.0.1', local_port), timeout=.2):
                        ready = True
                except OSError:
                    pass
                if ready:
                    time.sleep(.05)
                    if process.poll() is None:
                        yield local_port
                        return
                if time.monotonic() >= deadline:
                    break
                time.sleep(.05)
            raise DeployError('SSH tunnel failed or timed out; output suppressed.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def rsync_shell(self):
        """Pinned SSH options; rsync supplies the destination and remote command."""
        return shlex.join(self.argv[:-2])
