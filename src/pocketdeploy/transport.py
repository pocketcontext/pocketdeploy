"""Prepared pinned SSH commands, safe to use without controller state in workers."""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import selectors
import secrets
import shlex
import socket
import subprocess
import time

from .common import DeployError
from .diagnostics import build_diagnostic


def python_command(source):
    """Transport source unchanged through POSIX shells and Fish.

    Fish interprets backslash escapes even inside single quotes. Encode program
    text instead of quoting it directly; the fixed wrapper has no backslashes
    or embedded single quotes. Private payloads still travel only over stdin.
    """
    encoded = base64.b64encode(source.encode('utf-8')).decode('ascii')
    wrapper = 'import base64;exec(compile(base64.b64decode("' + encoded + '"),"<remote>","exec"))'
    return "sudo python3 -c '" + wrapper + "'"


def _stream_python(argv, payload, timeout, progress, allowed_stages):
    """Drain private output; forward only bounded, strictly allowlisted events.

    Callbacks run on the caller thread. Progress is advisory, never evidence of
    success: the final JSON response and process status remain authoritative.
    """
    data = memoryview(json.dumps(payload).encode())
    stdout = bytearray()
    pending = bytearray()
    discarding = False
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for stream, events in ((process.stdin, selectors.EVENT_WRITE),
                                       (process.stdout, selectors.EVENT_READ),
                                       (process.stderr, selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, events)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired('ssh-python', timeout)
                    for key, _ in selector.select(min(remaining, .2)):
                        stream = key.fileobj
                        if stream is process.stdin:
                            try:
                                written = os.write(stream.fileno(), data[:65536])
                                data = data[written:]
                            except BrokenPipeError:
                                data = data[len(data):]
                            if not data:
                                selector.unregister(stream)
                                stream.close()
                            continue
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        if stream is process.stdout:
                            stdout.extend(chunk)
                            if len(stdout) > 8 * 1024 * 1024:
                                raise DeployError('Host returned oversized response; output suppressed',
                                                  diagnostic=build_diagnostic('ssh-python', 'invalid_response'))
                            continue
                        # Process each byte so even a newline-free hostile stream
                        # cannot grow memory beyond one short event record.
                        for byte in chunk:
                            if byte == 10:
                                if not discarding:
                                    try:
                                        event = json.loads(pending)
                                    except (ValueError, UnicodeDecodeError):
                                        event = None
                                    if (isinstance(event, dict) and set(event) == {'stage', 'event'}
                                            and isinstance(event['stage'], str)
                                            and event['stage'] in allowed_stages
                                            and event['event'] in ('started', 'completed', 'failed')):
                                        progress(event['stage'], event['event'])
                                pending.clear()
                                discarding = False
                            elif not discarding:
                                pending.append(byte)
                                if len(pending) > 1024:
                                    pending.clear()
                                    discarding = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('ssh-python', timeout)
                process.wait(timeout=remaining)
                return subprocess.CompletedProcess(argv, process.returncode,
                                                   stdout.decode('utf-8', errors='replace'), '')
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise


@dataclass(frozen=True)
class SSHTransport:
    argv: tuple[str, ...]

    def python_process(self, source, payload, timeout=1200, *, progress=None, allowed_stages=()):
        started = time.monotonic()
        try:
            if progress is None:
                result = subprocess.run([*self.argv, python_command(source)],
                                      input=json.dumps(payload), capture_output=True, text=True, timeout=timeout)
            else:
                result = _stream_python([*self.argv, python_command(source)], payload, timeout,
                                        progress, frozenset(allowed_stages))
            result.elapsed_seconds = time.monotonic() - started
            return result
        except subprocess.TimeoutExpired:
            raise DeployError('SSH host operation timed out; remote work may still be running.', code='command_timeout', diagnostic=build_diagnostic('ssh-python', 'timeout', elapsed_seconds=time.monotonic() - started)) from None
        except OSError:
            raise DeployError('SSH could not be started; check OpenSSH installation.', code='command_unavailable', diagnostic=build_diagnostic('ssh-python', 'unavailable', elapsed_seconds=time.monotonic() - started)) from None

    def run_python(self, connection, source, payload, timeout=1200, *, progress=None, allowed_stages=()):
        # Connection is retained for the Host-compatible interface, never used to retarget trust.
        if connection.get('ip') != self.argv[-1] or connection.get('user', 'ubuntu') != self.argv[-3]:
            raise DeployError('Prepared SSH transport cannot change destination.')
        result = self.python_process(source, payload, timeout, progress=progress, allowed_stages=allowed_stages)
        if result.returncode:
            raise DeployError('Remote Python operation failed; output suppressed', diagnostic=build_diagnostic('ssh-python', 'nonzero_exit', returncode=result.returncode, elapsed_seconds=getattr(result, "elapsed_seconds", None)))
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise DeployError('Host returned invalid response; output suppressed', diagnostic=build_diagnostic('ssh-python', 'invalid_response')) from None

    def tunnel_argv(self, local_port, remote_port, *, session=False):
        for port in (local_port, remote_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise DeployError('Tunnel ports must be integers between 1 and 65535.')
        return [*self.argv[:-2], '-T' if session else '-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', *self.argv[-2:]]

    @contextmanager
    def tunnel(self, local_port, remote_port, *, timeout=20):
        marker = secrets.token_hex(32).encode('ascii')
        # OpenSSH starts the remote command only after installing forwarding;
        # ExitOnForwardFailure prevents this acknowledgement on a bind failure.
        # Keep stdin open so cat holds the authenticated session until cleanup.
        argv = [*self.tunnel_argv(local_port, remote_port, session=True),
                "printf '%s' " + marker.decode('ascii') + "; exec cat >/dev/null"]
        # Refuse an already occupied port before starting SSH; readiness must
        # never silently route application credentials to an existing listener.
        try:
            with socket.socket() as probe:
                # OpenSSH listeners use SO_REUSEADDR: completed forwards can
                # leave TIME_WAIT sockets without any live listener. Match that
                # behavior while still refusing an active listener.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1', local_port))
        except OSError:
            raise DeployError('Local tunnel port is unavailable.') from None
        try:
            process = subprocess.Popen(argv,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise DeployError('SSH tunnel could not start; output suppressed.') from None
        try:
            deadline = time.monotonic() + timeout
            received = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if not selector.select(min(remaining, .2)):
                        continue
                    chunk = os.read(process.stdout.fileno(), len(marker) - len(received))
                    if not chunk:
                        break
                    received.extend(chunk)
                    if not marker.startswith(received):
                        break
                    if received == marker:
                        if process.poll() is None:
                            yield local_port
                            return
                        break
            raise DeployError('SSH tunnel failed or timed out; output suppressed.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stdout.close()

    def rsync_shell(self):
        """Pinned SSH options; rsync supplies the destination and remote command."""
        return shlex.join(self.argv[:-2])
