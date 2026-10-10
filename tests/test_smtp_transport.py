from pathlib import Path
from types import SimpleNamespace

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.host import Host
from pocketdeploy.services import Services
from pocketdeploy.smtp import smtp_transport
from pocketdeploy.smtp_test_remote import submit


@pytest.mark.parametrize('port,security,scheme', [
    (465, 'implicit-tls', 'smtps'), (2465, 'implicit-tls', 'smtps'),
    (587, 'starttls', 'smtp'), (2587, 'starttls', 'smtp'),
])
def test_transport_reaches_app_and_test_without_unsupported_once_flags(monkeypatch, port, security, scheme):
    config = {'smtp-port': port, 'smtp-security': security, 'smtp-from': 'sender@example.com'}
    state = SimpleNamespace(deployment_id='synthetic-deployment', get_resource=lambda name: {'attributes': {'token': 'synthetic_secret'}})
    settings = Services(config, state).smtp_settings()
    assert (settings['port'], settings['security']) == (port, security)
    app = Host.resolved_app(SimpleNamespace(smtp_settings=settings), {'smtp': True})
    assert app['resolved-smtp']['port'] == str(port)
    assert set(app['resolved-smtp']) == {'server', 'port', 'username', 'password', 'from'}
    paths = []

    def execute(args, **kwargs):
        path = Path(kwargs['env']['MAILRC'])
        paths.append(path)
        mailrc = path.read_text()
        assert f'set mta={scheme}://resend:synthetic_secret@smtp.resend.com:{port}\n' in mailrc
        assert ('set smtp-use-starttls\n' if security == 'starttls' else 'unset smtp-use-starttls\n') in mailrc
        assert 'set tls-verify=strict\n' in mailrc
        assert path.stat().st_mode & 0o777 == 0o600
        assert 'synthetic_secret' not in str(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('pocketdeploy.smtp_test_remote.subprocess.run', execute)
    assert submit({'smtp': settings, 'to': 'recipient@example.com'}) == {'accepted': True}
    assert not paths[0].exists()


def test_transport_legacy_default_and_derived_modes():
    assert smtp_transport({}) == (465, 'implicit-tls')
    assert smtp_transport({'smtp-port': 2587}) == (2587, 'starttls')
    assert smtp_transport({'smtp-port': 2465}) == (2465, 'implicit-tls')


@pytest.mark.parametrize('port,security', [
    (25, 'starttls'), ('2587', 'starttls'), (True, 'starttls'),
    (2587, 'implicit-tls'), (2465, 'starttls'), (2587, 'none'),
])
def test_unsafe_transport_rejected_before_submission(monkeypatch, port, security):
    monkeypatch.setattr('pocketdeploy.smtp_test_remote.subprocess.run', lambda *a, **kw: pytest.fail('submission attempted'))
    with pytest.raises(DeployError):
        smtp_transport({'smtp-port': port, 'smtp-security': security})
    with pytest.raises(ValueError):
        submit({'smtp': {'server': 'smtp.resend.com', 'port': port, 'security': security,
                         'username': 'resend', 'password': 'synthetic_secret', 'from': 'sender@example.com'},
                'to': 'recipient@example.com'})
