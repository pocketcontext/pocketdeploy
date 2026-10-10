from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from pocketdeploy import health
from pocketdeploy.common import DeployError


@pytest.mark.parametrize('status,url,healthy', [
    (200, 'https://demo.example.com/', True),
    (204, 'https://demo.example.com/ready', True),
    (299, 'https://demo.example.com/', True),
    (300, 'https://demo.example.com/', False),
    (200, 'https://demo.example.com.evil.test/', False),
    (200, 'https://demo.example.com@evil.test/', False),
    (200, 'http://demo.example.com/', False),
])
def test_public_health_accepts_2xx_only_on_same_https_origin(monkeypatch, status, url, healthy):
    response = MagicMock()
    response.__enter__.return_value = SimpleNamespace(status=status, url=url)
    monkeypatch.setattr(health.urllib.request, 'urlopen', lambda *a, **k: response)
    monkeypatch.setattr(health.time, 'sleep', lambda _: None)
    config = {'once': {'applications': [{'host': 'demo.example.com', 'manage-dns': True}]}}
    if healthy:
        health.verify(config)
    else:
        with pytest.raises(DeployError, match='Public HTTPS'):
            health.verify(config)


def test_origin_uses_recorded_ip_with_certificate_hostname(monkeypatch):
    context = MagicMock()
    raw = MagicMock()
    addresses = []
    monkeypatch.setattr(health.ssl, 'create_default_context', lambda: context)
    monkeypatch.setattr(health.socket, 'create_connection', lambda address, timeout: addresses.append((address, timeout)) or raw)
    connection = health._OriginHTTPSConnection('demo.example.com', '192.0.2.1', 10)
    connection.connect()
    assert addresses == [(('192.0.2.1', 443), 10)]
    context.wrap_socket.assert_called_once_with(raw, server_hostname='demo.example.com')


def test_origin_tls_failure_closes_socket(monkeypatch):
    context, raw = MagicMock(), MagicMock()
    context.wrap_socket.side_effect = health.ssl.SSLCertVerificationError('synthetic')
    monkeypatch.setattr(health.ssl, 'create_default_context', lambda: context)
    monkeypatch.setattr(health.socket, 'create_connection', lambda *args: raw)
    with pytest.raises(health.ssl.SSLCertVerificationError):
        health._OriginHTTPSConnection('demo.example.com', '192.0.2.1', 10).connect()
    raw.close.assert_called_once()


@pytest.mark.parametrize('connection', [{'public_ip': '192.0.2.1'}, {'instance_id': 'synthetic', 'ip': '192.0.2.1', 'user': 'ubuntu'}])
def test_implicit_cloudflare_origin_then_uncached_public_health(monkeypatch, connection):
    events = []
    monkeypatch.setattr(health, '_origin_healthy', lambda app, ip: events.append(('origin', ip)) or True)
    def public(request, timeout):
        events.append(('public', request.full_url))
        assert request.get_header('Cache-control') == 'no-cache, no-store'
        response = MagicMock()
        response.__enter__.return_value = SimpleNamespace(status=200, url=request.full_url)
        return response
    monkeypatch.setattr(health.urllib.request, 'urlopen', public)
    config = {'provider-dns': 'cloudflare', 'once': {'applications': [{'host': 'demo.example.com', 'health-path': '/up'}]}}
    health.verify(config, connection)
    assert events[0] == ('origin', '192.0.2.1')
    assert events[1][1].startswith('https://demo.example.com/up?_pocketdeploy_health=')


def test_origin_failure_blocks_public_verification(monkeypatch):
    monkeypatch.setattr(health, '_origin_healthy', lambda *args: False)
    monkeypatch.setattr(health.time, 'sleep', lambda _: None)
    monkeypatch.setattr(health.urllib.request, 'urlopen', lambda *args, **kwargs: pytest.fail('public before origin'))
    with pytest.raises(DeployError) as error:
        health.verify({'once': {'applications': [{'host': 'demo.example.com', 'manage-dns': True}]}}, {'host': '192.0.2.1'})
    assert error.value.code == 'origin_https_health_failed'


@pytest.mark.parametrize('location,healthy', [('/login', True), ('https://other.example.com/', False), ('http://demo.example.com/', False)])
def test_origin_redirect_stays_on_certified_origin(monkeypatch, location, healthy):
    connection = MagicMock()
    redirect = MagicMock(status=302)
    redirect.getheader.return_value = location
    connection.getresponse.side_effect = [redirect, MagicMock(status=200)]
    monkeypatch.setattr(health, '_OriginHTTPSConnection', lambda *args, **kwargs: connection)
    assert health._origin_healthy({'host': 'demo.example.com'}, '192.0.2.1') is healthy
    assert connection.request.call_count == (2 if healthy else 1)
