import json

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.services import Services, _call
from pocketdeploy.state import State


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic")
    monkeypatch.setenv("RESEND_API_KEY", "synthetic")
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


def test_delete_all_services_and_retry(service, monkeypatch):
    domain = {'id': 'd', 'name': service.c['smtp-domain']}
    key = {'id': 'k', 'name': 'test-smtp-send'}
    service.state.put_resource('smtp-domain', 'resend-domain', 'd', {'name': domain['name']})
    service.state.put_resource('smtp-key', 'resend-api-key', 'k', {'domain_id': 'd', 'token': 'SECRET'})
    service.state.put_resource('dns:TXT:domain', 'cloudflare-dns', 'txt', {'type': 'TXT', 'name': domain['name'], 'zone': 'zone'})
    inventories = {'domains': [domain], 'api-keys': [key]}
    records = [{'id': 'txt', 'type': 'TXT', 'name': domain['name'], 'comment': service.marker}]
    mutations = []
    def resend(args):
        if args[1] == 'list':
            return {'data': inventories[args[0]]}
        mutations.append(args[0])
        inventories[args[0]].clear()
        return {}
    monkeypatch.setattr(service, '_resend', resend)
    monkeypatch.setattr(service, '_records', lambda name: records)
    monkeypatch.setattr(service, '_cf', lambda args: (mutations.append('dns'), records.clear()))
    op = service.state.begin_operation('delete', 'hash')
    service.state.set_meta('pending:smtp-domain', True)
    service.state.intent(op, 'smtp-domain', {'action': 'create'})
    assert len(service.plan_delete()) == 3
    assert mutations == []
    result = service.delete(op)
    assert mutations == ['api-keys', 'domains', 'dns']
    assert 'SECRET' not in json.dumps(result)
    assert service.state.resources() == []
    assert not service.state.get_meta('pending:smtp-domain')
    assert service.state.safe_status()['pending_steps'] == 0
    assert service.delete(op)['actions'] == []


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
    domain = {'id': 'd', 'name': service.c['smtp-domain'], 'status': 'pending', 'records': [{'type': 'TXT', 'name': 'send', 'value': 'public'}]}
    monkeypatch.setattr(service, '_domain', lambda op: domain)
    monkeypatch.setattr(service, '_dns', lambda *a: {})
    calls = []
    def resend(args, **kwargs):
        calls.append(args)
        return domain
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.converge({}, 'op', smtp_verification_timeout=0)
    assert error.value.code == 'smtp_verification_pending'
    assert all(a[0] == 'domains' for a in calls)


def test_lost_key_response_does_not_reissue(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    domain = {'id': 'd', 'name': service.c['smtp-domain'], 'status': 'verified', 'records': [{'type': 'TXT', 'name': 'send', 'value': 'public'}]}
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


def test_delete_absent_dns_completes_historical_intent(service, monkeypatch):
    name = 'dns:A:www.example.com'
    service.state.put_resource(name, 'cloudflare-dns', 'dns', {'name': 'www.example.com', 'type': 'A', 'zone': 'zone'})
    op = service.state.begin_operation('delete', 'test')
    service.state.intent(op, name, {'action': 'delete'})
    monkeypatch.setattr(service, '_records', lambda n: [])
    monkeypatch.setattr(service, '_cf', lambda a: pytest.fail('absent record mutation'))
    assert service.plan_delete()[0]['action'] == 'absent'
    assert service.state.safe_status()['pending_steps'] == 1
    service.delete(op)
    assert service.state.safe_status()['pending_steps'] == 0
    assert service.state.get_resource(name) is None


def test_delete_validates_all_dns_before_first_mutation(service, monkeypatch):
    for host in ['first.example.com', 'second.example.com']:
        service.state.put_resource('dns:A:' + host, 'cloudflare-dns', host, {'name': host, 'type': 'A', 'zone': 'zone'})
    monkeypatch.setattr(service, '_records', lambda n: [{'id': n, 'name': n, 'type': 'A', 'comment': service.marker if n.startswith('first') else 'foreign'}])
    monkeypatch.setattr(service, '_cf', lambda a: pytest.fail('must preflight all records'))
    with pytest.raises(DeployError, match='ownership changed'):
        service.delete('unused')


def test_delete_smtp_checks_key_before_any_mutation(service, monkeypatch):
    service.state.put_resource('smtp-domain', 'resend-domain', 'd', {'name': service.c['smtp-domain']})
    service.state.put_resource('smtp-key', 'resend-api-key', 'k', {'domain_id': 'd'})
    monkeypatch.setattr(service, '_resend', lambda args: {'data': [{'id': 'k', 'name': 'foreign'}]} if args[:2] == ['api-keys', 'list'] else pytest.fail('unexpected call'))
    with pytest.raises(DeployError, match='ownership changed'):
        service.delete('unused')


def test_delete_resend_lost_response_recovers_absence(service, monkeypatch):
    service.state.put_resource('smtp-domain', 'resend-domain', 'd', {'name': service.c['smtp-domain']})
    rows = [{'id': 'd', 'name': service.c['smtp-domain']}]
    def resend(args):
        if args[1] == 'list':
            return {'data': rows}
        rows.clear()
        raise DeployError('lost')
    monkeypatch.setattr(service, '_resend', resend)
    op = service.state.begin_operation('delete', 'hash')
    with pytest.raises(DeployError, match='lost'):
        service.delete(op)
    assert service.state.get_resource('smtp-domain')
    service.delete(op)
    assert not service.state.resources()
    assert service.state.safe_status()['pending_steps'] == 0


def test_delete_unknown_smtp_creation_blocks(service):
    service.state.set_meta('pending:smtp-key', True)
    with pytest.raises(DeployError, match='ownership reconciliation'):
        service.plan_delete()


def test_delete_unknown_dns_creation_blocks_before_mutations(service, monkeypatch):
    service.state.set_meta('pending:dns:TXT:send.notifications.example.com', True)
    monkeypatch.setattr(service, '_cf', lambda args: pytest.fail('unreconciled DNS mutation'))
    with pytest.raises(DeployError, match='Unrecorded DNS creation') as error:
        service.delete('unused')
    assert error.value.code == 'provider_recovery_required'
    service.state.set_meta('pending:dns:TXT:send.notifications.example.com', False)
    assert service.plan_delete() == []


@pytest.fixture
def verification(service, monkeypatch):
    service.c['provider-smtp'] = 'resend'
    domain = {'id': 'd', 'name': service.c['smtp-domain'], 'status': 'pending',
              'records': [{'type': 'TXT', 'name': 'send', 'value': 'public'}]}
    service.state.put_resource('smtp-domain', 'resend-domain', 'd', {'name': domain['name']})
    monkeypatch.setattr(service, '_domain', lambda op: domain)
    monkeypatch.setattr(service, '_dns', lambda *a: {'action': 'noop'})
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr('pocketdeploy.services.time.monotonic', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr('pocketdeploy.services.time.sleep', sleep)
    service.prepare_dns({}, 'op')
    return domain, clock, sleeps


def test_verification_waits_once_then_allows_credentials(service, verification, monkeypatch):
    domain, clock, sleeps = verification
    calls = []
    def resend(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ['api-keys', 'create']:
            return {'id': 'k', 'token': 'PRIVATE_TOKEN'}
        return {**domain, 'status': 'verified' if clock[0] >= 20 else 'pending'}
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError, match='verification must complete'):
        service.ensure_smtp_credentials('op')
    assert service.verify_smtp(25)['status'] == 'verified'
    op = service.state.begin_operation('converge', 'hash')
    result = service.ensure_smtp_credentials(op)
    assert result == {'actions': [{'name': domain['name'], 'type': 'smtp', 'action': 'verified'}]}
    assert sleeps == [10, 10]
    assert [a[1] for a, _ in calls] == ['verify', 'get', 'get', 'get', 'create']
    assert [k['timeout'] for _, k in calls[:-1]] == [25, 25, 15, 5]
    assert 'PRIVATE_TOKEN' not in json.dumps(result)


def test_verification_deadline_includes_requests_and_clamps_sleep(service, verification, monkeypatch):
    domain, clock, sleeps = verification
    calls = []
    def resend(args, **kwargs):
        calls.append(args)
        clock[0] += 3
        return domain
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.verify_smtp(8)
    assert error.value.code == 'smtp_verification_pending'
    assert clock[0] == 8
    assert sleeps == [2]
    assert len(calls) == 2
    assert service.state.get_resource('smtp-domain')['provider_id'] == 'd'
    assert service.state.get_resource('smtp-key') is None


@pytest.mark.parametrize('changed', [{'id': 'other'}, {'name': 'other.example.com'}, {'region': 'us-east-1'}])
def test_verification_rejects_identity_change(service, verification, monkeypatch, changed):
    domain, clock, sleeps = verification
    monkeypatch.setattr(service, '_resend', lambda *a, **k: {**domain, **changed, 'status': 'verified'})
    with pytest.raises(DeployError):
        service.verify_smtp()
    assert sleeps == []
    assert service._verified_domain is None


def test_verification_auth_error_is_immediate(service, verification, monkeypatch):
    def resend(*args, **kwargs):
        raise DeployError('Authentication failed.', code='provider_authentication_failed')
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.verify_smtp()
    assert error.value.code == 'provider_authentication_failed'
    assert verification[2] == []


def test_verification_deadline_request_timeout_is_pending(service, verification, monkeypatch):
    domain, clock, sleeps = verification
    def resend(*args, **kwargs):
        clock[0] += kwargs['timeout']
        raise DeployError('Command timed out.', code='command_timeout')
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.verify_smtp(5)
    assert error.value.code == 'smtp_verification_pending'
    assert clock[0] == 5


def test_verified_domain_skips_polling(service, verification, monkeypatch):
    verification[0]['status'] = 'verified'
    monkeypatch.setattr(service, '_resend', lambda *a, **k: pytest.fail('unneeded request'))
    assert service.verify_smtp()['status'] == 'verified'
    assert verification[2] == []


def test_verification_interruption_never_unlocks_credentials(service, verification, monkeypatch):
    monkeypatch.setattr(service, '_resend', lambda *a, **k: verification[0])
    def interrupt(seconds):
        raise KeyboardInterrupt()
    monkeypatch.setattr('pocketdeploy.services.time.sleep', interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.verify_smtp()
    with pytest.raises(DeployError, match='verification must complete'):
        service.ensure_smtp_credentials('op')


@pytest.mark.parametrize('status', [None, {}, 'PRIVATE_UNKNOWN_STATUS'])
def test_verification_invalid_status_is_safely_rejected(service, verification, monkeypatch, status):
    monkeypatch.setattr(service, '_resend', lambda *a, **k: {**verification[0], 'status': status})
    with pytest.raises(DeployError, match='invalid domain verification status') as error:
        service.verify_smtp()
    assert 'PRIVATE_UNKNOWN_STATUS' not in str(error.value)
    assert verification[2] == []


def test_late_verified_response_does_not_unlock_credentials(service, verification, monkeypatch):
    domain, clock, sleeps = verification
    def resend(args, **kwargs):
        if args[1] == 'get':
            clock[0] = 11
        return {**domain, 'status': 'verified'}
    monkeypatch.setattr(service, '_resend', resend)
    with pytest.raises(DeployError) as error:
        service.verify_smtp(10)
    assert error.value.code == 'smtp_verification_pending'
    assert service._verified_domain is None


@pytest.mark.parametrize('status,code,retryable', [
    (429, 'provider_rate_limited', True), (500, 'provider_unavailable', True),
    (502, 'provider_unavailable', True), (503, 'provider_unavailable', True),
    (504, 'provider_unavailable', True), (501, 'provider_request_failed', False),
    (401, 'provider_authentication_failed', False),
    (403, 'provider_permission_denied', False),
])
def test_structured_retry_classification(status, code, retryable):
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', json.dumps({'error': {
        'code': 'fetch_error', 'statusCode': status, 'message': 'PRIVATE',
        'headers': {'Retry-After': '12', 'Authorization': 'PRIVATE'}}}))
    assert (result.code, result.status, result.retryable, result.retry_after) == (code, status, retryable, 12)
    assert 'PRIVATE' not in str(result)


@pytest.mark.parametrize('message,code,retryable', [
    ('ECONNRESET PRIVATE', 'provider_connection_failed', True),
    ('EAI_AGAIN PRIVATE', 'provider_connection_failed', True),
    ('certificate expired ECONNRESET PRIVATE', 'provider_tls_failed', False),
    ('fetch failed PRIVATE', 'provider_request_failed', False),
    ('daily quota exceeded PRIVATE', 'provider_usage_quota', False),
    ('monthly limit exceeded PRIVATE', 'provider_usage_quota', False),
])
def test_network_and_quota_categories(message, code, retryable):
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', json.dumps({'error': {'code': 'fetch_error', 'message': message}}))
    assert (result.code, result.retryable) == (code, retryable)
    assert 'PRIVATE' not in str(result)


def test_quota_is_not_throttle():
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', json.dumps({'error': {
        'statusCode': 429, 'body': {'name': 'daily_quota_exceeded', 'message': 'PRIVATE'}}}))
    assert result.code == 'provider_usage_quota'
    assert not result.retryable


@pytest.mark.parametrize('value,expected', [('12', 12), ('NaN', None), ('inf', None), (True, None), ([], None), ('invalid', None)])
def test_retry_after_seconds_are_safe(value, expected):
    from pocketdeploy.services import _retry_after
    assert _retry_after({'rEtRy-AfTeR': value}) == expected


def test_retry_after_http_date():
    from datetime import datetime, timezone, timedelta
    from email.utils import format_datetime
    from pocketdeploy.services import _retry_after
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 58 <= _retry_after({'retry-after': value}) <= 60


@pytest.mark.parametrize('tool,args,read', [
    ('resend', ['domains', 'get', 'd'], True),
    ('resend', ['domains', 'list'], True),
    ('resend', ['api-keys', 'list'], True),
    ('cf', ['zones', 'get', '--zone', 'z'], True),
    ('cf', ['dns', 'records', 'list'], True),
    ('resend', ['domains', 'verify', 'd'], False),
    ('resend', ['domains', 'create'], False),
    ('resend', ['domains', 'delete', 'd'], False),
    ('resend', ['api-keys', 'create'], False),
    ('cf', ['dns', 'records', 'delete', 'd'], False),
    ('cf', ['dns', 'records', 'edit', 'd'], False),
])
def test_only_allowlisted_reads_use_retry_wrapper(monkeypatch, tool, args, read):
    wrappers, calls = [], []
    def wrapper(attempt, **kwargs):
        wrappers.append(kwargs)
        return attempt(7)
    def run(args, **kwargs):
        calls.append(kwargs)
        return '{}'
    monkeypatch.setattr('pocketdeploy.services.run_read', wrapper)
    monkeypatch.setattr('pocketdeploy.services.run', run)
    assert _call(tool, args, deadline=100) == {}
    assert bool(wrappers) == read
    assert calls[0]['timeout'] == (7 if read else 90)
    if read:
        assert wrappers[0]['deadline'] == 100


def test_smtp_passes_absolute_deadline_to_read_recovery(service, verification, monkeypatch):
    domain, clock, sleeps = verification
    calls = []
    def resend(args, **kwargs):
        calls.append(kwargs)
        return {**domain, 'status': 'verified'}
    monkeypatch.setattr(service, '_resend', resend)
    service.verify_smtp(30)
    assert [call['deadline'] for call in calls] == [30, 30]


@pytest.mark.parametrize('status,reason,retryable', [
    (503, 'Service Unavailable', True), (429, 'Too Many Requests', True),
    (401, 'Unauthorized', False), (403, 'Forbidden', False),
])
def test_pinned_cloudflare_api_error_box(status, reason, retryable):
    from pocketdeploy.services import _provider_error
    # cf@1.0.0-beta.14 handleError -> formatErrorBox; headers are not rendered.
    stderr = f'\n┌ APIError\n│ [10000] PRIVATE provider message\n│ {status} {reason} · HTTP /zones/PRIVATE/dns_records\n└\n'
    error = _provider_error('cf', stderr)
    assert error.status == status
    assert error.retryable is retryable
    assert error.retry_after is None
    assert 'PRIVATE' not in str(error)


@pytest.mark.parametrize('stderr', [
    'Provider said Status code: 503',
    '┌ Error\n│ 503 Service Unavailable\n└',
    '┌ APIError\n│ 503 Private Message\n└',
    '┌ APIError\n│ 503 Service Unavailable\n│ other footer\n└',
    '┌ APIError\n│ 503 Service Unavailable',
])
def test_cloudflare_arbitrary_text_does_not_supply_status(stderr):
    from pocketdeploy.services import _provider_error
    result = _provider_error('cf', stderr)
    assert result is None or result.status is None


def test_provider_body_cannot_replace_transport_status():
    from pocketdeploy.services import _provider_error
    result = _provider_error('resend', json.dumps({'error': {
        'status': 503, 'body': {'status': 'PRIVATE', 'statusCode': 400}}}))
    assert result.status == 503
    assert result.retryable


def test_cloudflare_colored_box_uses_final_footer_only():
    from pocketdeploy.services import _provider_error
    stderr = '\n\x1b[31m┌\x1b[0m \x1b[1mAPIError\x1b[0m\n│ 503 Service Unavailable\n│ 401 Unauthorized · HTTP /zones/private\n└\n'
    error = _provider_error('cf', stderr)
    assert error.status == 401
    assert not error.retryable
