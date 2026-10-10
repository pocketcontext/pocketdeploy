"""Bounded certificate-verified origin and public HTTPS readiness checks."""
import http.client
import ipaddress
import secrets
import socket
import ssl
import time
import urllib.request
import urllib.parse
from .common import DeployError
from .config import manages_dns
from .output import operation


class _OriginHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the recorded origin while retaining hostname/SNI validation."""
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _origin_healthy(app, address):
    connection = _OriginHTTPSConnection(app['host'], address, timeout=10)
    try:
        url = 'https://' + app['host'] + app.get('health-path', '/')
        for _ in range(6):
            parsed = urllib.parse.urlsplit(url)
            target = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            connection.request('GET', target, headers={
                'Host': app['host'], 'Cache-Control': 'no-cache, no-store'})
            response = connection.getresponse()
            if 200 <= response.status < 300:
                return True
            if response.status not in (301, 302, 303, 307, 308):
                return False
            location = response.getheader('Location')
            if not location:
                return False
            url = urllib.parse.urljoin(url, location)
            redirected = urllib.parse.urlsplit(url)
            if redirected.scheme != 'https' or redirected.netloc != app['host']:
                return False
            # Reconnect to the same origin rather than buffering an untrusted body.
            connection.close()
        return False
    finally:
        connection.close()


def _retry(probe, message, code):
    for attempt in range(6):
        try:
            if probe():
                return
        except Exception:
            pass
        if attempt == 5:
            raise DeployError(message, code=code)
        time.sleep(2)


def verify(config, connection=None):
    for app in config.get('once', {}).get('applications', []):
        if not (manages_dns(config, app) or app.get('github')):
            continue
        if app.get('disable_tls'):
            raise DeployError('Managed website DNS requires TLS enabled.')
        if connection is not None:
            try:
                address = str(ipaddress.IPv4Address(connection.get('public_ip') or connection.get('ip') or connection.get('host')))
            except (ValueError, TypeError):
                raise DeployError('Origin HTTPS requires a verified public IPv4 address.') from None
            with operation('HTTPS: direct origin application health'):
                _retry(lambda: _origin_healthy(app, address),
                       'Origin HTTPS health verification failed; check origin TLS and application readiness.',
                       'origin_https_health_failed')
        url = 'https://' + app['host'] + app.get('health-path', '/')
        # A unique query plus cache-control prevents a normal proxy cache hit from
        # standing in for current readiness. No zone settings or caches are changed.
        url += ('&' if '?' in url else '?') + '_pocketdeploy_health=' + secrets.token_hex(12)
        request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache, no-store'})
        def public_healthy():
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300 and response.url.startswith('https://' + app['host'] + '/')
        with operation('HTTPS: public application health'):
            _retry(public_healthy,
                   'Public HTTPS health verification failed; check DNS propagation and TLS.',
                   'https_health_failed')
