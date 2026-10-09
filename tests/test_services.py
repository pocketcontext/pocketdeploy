import json

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.services import Services, _call
from pocketdeploy.state import State


@pytest.fixture
def service(tmp_path):
    state = State(tmp_path / 'state', 'test', {}, create=True)
    config = {'profile': 'test', 'cloudflare-zone-id': 'zone', 'smtp-domain': 'notifications.example.com',
              'smtp-from': 'mail@notifications.example.com', 'once': {'applications': []}}
    yield Services(config, state)
    state.db.close()


def test_disabled_services_no_calls(service, monkeypatch):
    monkeypatch.setattr(service, '_cf', lambda a: pytest.fail('cloud access'))
    monkeypatch.setattr(service, '_resend', lambda a: pytest.fail('cloud access'))
    service.preflight()
    assert service.plan() == {'actions': []}
    assert service.converge({}, 'unused') == {'actions': []}


def test_dns_refuses_unowned_and_multiple_records(service, monkeypatch):
    desired = {'name': 'www.example.com', 'type': 'A', 'content': '192.0.2.1'}
    monkeypatch.setattr(service, '_records', lambda n: [{'id': 'other', **desired}])
    with pytest.raises(DeployError, match='outside'):
        service._dns(desired)
    monkeypatch.setattr(service, '_records', lambda n: [desired, desired])
    with pytest.raises(DeployError, match='Multiple'):
        service._dns(desired)


def test_dns_create_and_noop(service, monkeypatch):
    desired = {'name': 'www.example.com', 'type': 'A', 'content': '192.0.2.1'}
    records, calls = [], []
    monkeypatch.setattr(service, '_records', lambda n: records)
    def cf(args):
        calls.append(args)
        records.append({'id': 'dns-id', **json.loads(args[args.index('--body') + 1])})
        return records[0]
    monkeypatch.setattr(service, '_cf', cf)
    op = service.state.begin_operation('converge', 'hash')
    assert service._dns(desired, op)['action'] == 'create'
    assert service._dns(desired, op)['action'] == 'noop'
    assert len(calls) == 1
    assert service.state.get_resource('dns:A:www.example.com')['provider_id'] == 'dns-id'


def test_lost_dns_response_never_recreates(service, monkeypatch):
    desired = {'name': 'www.example.com', 'type': 'A', 'content': '192.0.2.1'}
    monkeypatch.setattr(service, '_records', lambda n: [])
    def fail(args):
        raise DeployError('lost')
    monkeypatch.setattr(service, '_cf', fail)
    op = service.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='lost'):
        service._dns(desired, op)
    with pytest.raises(DeployError, match='uncertain'):
        service._dns(desired, op)


def test_domain_existing_requires_recorded_identity(service, monkeypatch):
    monkeypatch.setattr(service, '_resend', lambda a: {'data': [{'id': 'old', 'name': service.c['smtp-domain']}]})
    with pytest.raises(DeployError, match='outside'):
        service._domain()


def test_pending_domain_creation_refuses_retry(service, monkeypatch):
    service.state.set_meta('pending:smtp-domain', True)
    monkeypatch.setattr(service, '_resend', lambda a: {'data': []})
    with pytest.raises(DeployError, match='uncertain'):
        service._domain('op')


def test_resend_dns_cannot_escape_domain(service):
    with pytest.raises(DeployError, match='outside'):
        service._email_dns({'records': [{'type': 'TXT', 'name': 'example.com', 'value': 'test'}]})


def test_smtp_key_scope_reuse_and_private_output(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    domain = {'id': 'domain', 'name': service.c['smtp-domain'], 'status': 'verified',
              'records': [{'type': 'TXT', 'name': service.c['smtp-domain'], 'value': 'public'}]}
    monkeypatch.setattr(service, '_domain', lambda op=None: domain)
    monkeypatch.setattr(service, '_dns', lambda row, op: {'action': 'noop'})
    calls = []
    def resend(args):
        calls.append(args)
        if args[:2] == ['api-keys', 'create']:
            assert args[-4:] == ['--permission', 'sending_access', '--domain-id', 'domain']
            return {'id': 'key', 'token': 'SYNTHETIC_SECRET'}
        return {'data': [{'id': 'key'}]}
    monkeypatch.setattr(service, '_resend', resend)
    op = service.state.begin_operation('converge', 'hash')
    result = service.converge({}, op)
    service.converge({}, op)
    assert len([a for a in calls if a[1] == 'create']) == 1
    assert 'SYNTHETIC_SECRET' not in json.dumps(result)
    assert service.smtp_settings()['password'] == 'SYNTHETIC_SECRET'


def test_delete_retains_smtp(service, monkeypatch):
    service.state.put_resource('smtp-domain', 'resend-domain', 'd', {'name': 'domain'})
    service.state.put_resource('dns:TXT:domain', 'cloudflare-dns', 'txt', {'type': 'TXT'})
    monkeypatch.setattr(service, '_cf', lambda a: pytest.fail('must retain email DNS'))
    assert service.delete('op')['smtp'] == 'retained'
    assert len(service.state.resources()) == 2


def test_preflight_missing_credentials_before_cloud(service, monkeypatch):
    service.c['provider-dns'] = 'cloudflare'
    monkeypatch.delenv('CLOUDFLARE_API_TOKEN', raising=False)
    with pytest.raises(DeployError, match='CLOUDFLARE_API_TOKEN'):
        service.preflight()


def test_invalid_provider_response_suppressed(monkeypatch):
    monkeypatch.setattr('pocketdeploy.services.run', lambda *a, **k: 'SYNTHETIC_SECRET')
    with pytest.raises(DeployError) as error:
        _call('resend', [])
    assert 'SYNTHETIC_SECRET' not in str(error.value)


def test_fresh_state_plan_conflict_check(monkeypatch):
    config = {'once': {'applications': [{'host': 'www.example.com', 'manage-dns': True}]}}
    service = Services(config, None)
    monkeypatch.setattr(service, '_records', lambda n: [{'type': 'A', 'id': 'other'}])
    with pytest.raises(DeployError, match='outside'):
        service.plan()


@pytest.mark.parametrize('name', ['send', 'send.notifications', 'send.notifications.example.com'])
def test_relative_resend_dns(service, name):
    service.zone_name = 'example.com'
    rows = service._email_dns({'records': [{'type': 'MX', 'name': name, 'value': 'smtp.example.net', 'priority': 10}]})
    assert rows[0]['name'] == 'send.notifications.example.com'


def test_smtp_test_secret_transport_and_cleanup(monkeypatch):
    from pocketdeploy.smtp_test_remote import submit
    from pathlib import Path
    from types import SimpleNamespace
    paths = []
    def execute(args, **kwargs):
        config = Path(kwargs['env']['MAILRC'])
        paths.append(config)
        assert 'SYNTHETIC_SECRET' not in str(args)
        assert 'SYNTHETIC_SECRET' in config.read_text()
        assert config.stat().st_mode & 0o777 == 0o600
        assert 'tls-verify=strict' in config.read_text()
        assert 'sendwait' in config.read_text()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('pocketdeploy.smtp_test_remote.subprocess.run', execute)
    assert submit({'smtp': {'server': 'smtp.resend.com', 'port': 465, 'username': 'resend',
                            'password': 'SYNTHETIC_SECRET', 'from': 'mail@notifications.example.com'},
                   'to': 'test@example.com'}) == {'accepted': True}
    assert not paths[0].exists()


def test_smtp_pending_verification_does_not_create_key(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    domain = {'id': 'd', 'status': 'pending', 'records': [{'type': 'TXT', 'name': 'send', 'value': 'public'}]}
    monkeypatch.setattr(service, '_domain', lambda op: domain)
    monkeypatch.setattr(service, '_dns', lambda *a: {})
    calls = []
    def resend(args):
        calls.append(args)
        return domain
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.converge({}, 'op')
    assert error.value.code == 'smtp_verification_pending'
    assert all(a[0] == 'domains' for a in calls)


def test_lost_key_response_does_not_reissue(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    domain = {'id': 'd', 'status': 'verified', 'records': [{'type': 'TXT', 'name': 'send', 'value': 'public'}]}
    monkeypatch.setattr(service, '_domain', lambda op: domain)
    monkeypatch.setattr(service, '_dns', lambda *a: {})
    def fail(args):
        raise DeployError('lost')
    monkeypatch.setattr(service, '_resend', fail)
    op = service.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='lost'):
        service.converge({}, op)
    with pytest.raises(DeployError, match='uncertain'):
        service.converge({}, op)


def test_dns_pagination_reads_later_conflict(service, monkeypatch):
    pages = []
    def cf(args):
        page = int(args[args.index('--page') + 1])
        pages.append(page)
        return [{'type': 'TXT'}] * 100 if page == 1 else [{'type': 'A', 'id': 'unowned'}]
    monkeypatch.setattr(service, '_cf', cf)
    with pytest.raises(DeployError, match='outside'):
        service._dns({'type': 'A', 'name': 'www.example.com', 'content': '192.0.2.1'})
    assert pages == [1, 2]


def test_delete_refuses_changed_uuid(service, monkeypatch):
    service.state.put_resource('dns:A:www.example.com', 'cloudflare-dns', 'dns',
                               {'type': 'A', 'name': 'www.example.com', 'zone': 'zone'})
    monkeypatch.setattr(service, '_records', lambda n: [{'id': 'dns', 'type': 'A', 'comment': 'other'}])
    with pytest.raises(DeployError, match='ownership changed'):
        service.delete('op')


@pytest.mark.parametrize('error,code', [
    ({'code': 'create_error', 'statusCode': 403, 'message': 'SECRET'}, 'provider_permission_denied'),
    ({'code': 'auth_error', 'message': 'SECRET'}, 'provider_authentication_failed'),
    ({'code': 'create_error', 'body': '{"name":"validation_error","message":"Domain limit exceeded SECRET"}'}, 'provider_domain_quota'),
    ({'statusCode': 429, 'message': 'SECRET'}, 'provider_rate_limited'),
    ({'code': 'create_error', 'message': 'SECRET'}, 'provider_request_failed'),
])
def test_provider_errors_are_fixed_and_safe(error, code):
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', json.dumps({'error': error}))
    assert result.code == code
    assert 'SECRET' not in str(result)


def test_provider_error_after_retry_chatter():
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', 'Rate limit retry SECRET\n' + json.dumps({'error': {'statusCode': 429, 'message': 'SECRET'}}))
    assert result.code == 'provider_rate_limited'
    assert 'SECRET' not in str(result)


def test_plain_quota_error_is_fixed():
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', 'SECRET domain limit reached 403 validation_error')
    assert result.code == 'provider_domain_quota'
    assert 'SECRET' not in str(result)


def test_pending_domain_plan_reports_recovery(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    service.state.set_meta('pending:smtp-domain', True)
    monkeypatch.setattr(service, '_domain', lambda: None)
    assert service.plan()['actions'][0]['action'] == 'recovery-required'


def test_pending_domain_preflight_blocks_before_compute(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    service.state.set_meta('pending:smtp-domain', True)
    monkeypatch.setenv('RESEND_API_KEY', 'SYNTHETIC_SECRET')
    monkeypatch.setattr(service, '_domain', lambda: None)
    with pytest.raises(DeployError) as error:
        service.preflight()
    assert error.value.code == 'provider_recovery_required'


def test_resend_zone_relative_cname_stays_within_sending_domain(service):
    service.c['smtp-domain'] = 'notifications.example.com'
    service.zone_name = 'example.com'
    records = service._email_dns({'records': [{'name': 'rsend.notifications', 'type': 'CNAME', 'value': 'tracking.resend.com'}]})
    assert records[0]['name'] == 'rsend.notifications.example.com'
    with pytest.raises(DeployError, match='outside'):
        service._email_dns({'records': [{'name': 'rsend.other', 'type': 'CNAME', 'value': 'tracking.resend.com'}]})


def test_smtp_v15_credentials_in_private_url(monkeypatch):
    from pocketdeploy.smtp_test_remote import submit
    from pathlib import Path
    from types import SimpleNamespace
    def execute(args, **kwargs):
        settings = Path(kwargs['env']['MAILRC']).read_text()
        assert 'set mta=smtps://resend:SYNTHETIC_SECRET@smtp.resend.com:465' in settings
        assert 'smtp-auth-password' not in settings
        assert 'SYNTHETIC_SECRET' not in str(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('pocketdeploy.smtp_test_remote.subprocess.run', execute)
    submit({'smtp': {'server': 'smtp.resend.com', 'port': 465, 'username': 'resend',
                     'password': 'SYNTHETIC_SECRET', 'from': 'mail@example.com'}, 'to': 'test@example.com'})


@pytest.mark.parametrize('diagnostic,code', [
    ('A password is necessary for SMTP authentication SECRET', 'smtp_credentials_missing'),
    ('certificate verification failed SECRET', 'smtp_tls_failed'),
    ('535 authentication failed SECRET', 'smtp_authentication_failed'),
    ('Connection refused SECRET', 'smtp_connection_failed'),
    ('SECRET', 'smtp_submission_failed'),
])
def test_smtp_failure_safe_classification(diagnostic, code):
    from pocketdeploy.smtp_test_remote import classify_failure
    assert classify_failure(diagnostic) == code


def test_smtp_test_reports_fixed_remote_error(monkeypatch):
    from pocketdeploy.smtp_test import smtp_test
    from types import SimpleNamespace
    monkeypatch.setattr('pocketdeploy.smtp_test.run', lambda *a, **k: json.dumps({'accepted': False, 'error': 'smtp_credentials_missing', 'raw': 'SECRET'}))
    with pytest.raises(DeployError) as result:
        smtp_test(SimpleNamespace(_argv=lambda connection: ['ssh']), {}, {}, 'test@example.com')
    assert result.value.code == 'smtp_credentials_missing'
    assert 'SECRET' not in str(result.value)
