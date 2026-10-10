"""Bounded public HTTPS verification for managed website DNS."""
import time
import urllib.request
from .common import DeployError
from .output import operation


def verify(config):
    for app in config.get('once', {}).get('applications', []):
        if not (app.get('manage-dns') or app.get('github')):
            continue
        if app.get('disable_tls'):
            raise DeployError('Managed website DNS requires TLS enabled.')
        url = 'https://' + app['host'] + app.get('health-path', '/')
        with operation('HTTPS: public application health'):
            for attempt in range(6):
                try:
                    with urllib.request.urlopen(url, timeout=10) as response:
                        if 200 <= response.status < 300 and response.url.startswith('https://' + app['host'] + '/'):
                            break
                except Exception:
                    pass
                if attempt == 5:
                    raise DeployError('Public HTTPS health verification failed; check DNS propagation and TLS.', code='https_health_failed')
                time.sleep(2)
