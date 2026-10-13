from types import SimpleNamespace

import pytest

from pocketdeploy import remote


@pytest.fixture
def proxy(monkeypatch):
    replies = []
    connections = []
    clock = [0.0]

    class Connection:
        def __init__(self, host, port, timeout, **kwargs):
            self.host, self.port, self.timeout = host, port, timeout
            self.closed = False
            self.sock = SimpleNamespace(settimeout=lambda value: None)
            connections.append(self)

        def request(self, method, target, headers):
            self.target, self.headers = target, headers

        def getresponse(self):
            status, location, elapsed = replies.pop(0)
            clock[0] += elapsed
            return SimpleNamespace(status=status, getheader=lambda name: location)

        def close(self):
            self.closed = True

    monkeypatch.setattr(remote, 'LocalTLS', Connection)
    monkeypatch.setattr(remote.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(remote.time, 'monotonic', lambda: clock[0])
    return replies, connections


def test_relative_and_absolute_redirects_reconnect_to_local_proxy(proxy):
    replies, connections = proxy
    replies.extend([(302, '/ready?check=1', 1), (307, 'https://app.example.test/final', 1), (204, None, 0)])
    assert remote.health({'host': 'app.example.test'}, timeout=5) == {'healthy': True, 'http_status': 204}
    assert [c.target for c in connections] == ['/', '/ready?check=1', '/final']
    assert [c.timeout for c in connections] == [5, 4, 3]
    assert all(c.closed and c.host == 'app.example.test' and c.port == 443 for c in connections)


@pytest.mark.parametrize('location', [None, '', '/', '/#fragment', 'https://other.example.test/',
                                     'http://app.example.test/', '//other.example.test/',
                                     'https://app.example.test:444/', 'https://user@app.example.test/'])
def test_invalid_redirects_fail_without_another_connection(proxy, location):
    replies, connections = proxy
    replies.append((302, location, 0))
    assert remote.health({'host': 'app.example.test'}) == {'healthy': False, 'http_status': 302}
    assert len(connections) == 1
    assert connections[0].closed


@pytest.mark.parametrize('status', [300, 304, 400, 500])
def test_non_success_without_supported_redirect_is_unhealthy(proxy, status):
    replies, _ = proxy
    replies.append((status, '/next', 0))
    assert remote.health({'host': 'app.example.test'}) == {'healthy': False, 'http_status': status}


def test_redirect_chain_is_bounded(proxy):
    replies, connections = proxy
    replies.extend((302, '/' + str(i), 0) for i in range(7))
    assert not remote.health({'host': 'app.example.test'})['healthy']
    assert len(connections) == 6
    assert all(c.closed for c in connections)


def test_redirects_cannot_extend_probe_budget(proxy):
    replies, connections = proxy
    replies.extend([(302, '/next', 5), (200, None, 0)])
    assert not remote.health({'host': 'app.example.test'}, timeout=5)['healthy']
    assert len(connections) == 1


@pytest.mark.parametrize('app', [{'host': 'app.localhost'}, {'host': 'app.example.test', 'disable_tls': True}])
def test_plaintext_local_apps_keep_same_origin_redirects(proxy, app):
    replies, connections = proxy
    replies.extend([(301, '/ready', 0), (200, None, 0)])
    assert remote.health(app)['healthy']
    assert all(c.host == '127.0.0.1' and c.port == 80 and c.headers['Host'] == app['host'] for c in connections)
