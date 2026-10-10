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
