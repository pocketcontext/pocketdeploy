import copy
from pathlib import Path

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.kms import KMS, validate, FILES
from pocketdeploy.state import State


@pytest.fixture
def kms(tmp_path, monkeypatch):
    monkeypatch.setenv('COLORS_PAR_APP_IDENTITYCONTEXT_OCI_USER_EMAIL', 'signer@example.test')
    config = {'profile': 'test', '_root': str(tmp_path), 'provider-compute': 'oci',
              'oci-compartment-id': 'ocid1.compartment.oc1.example', 'oci-region': 'eu-frankfurt-1',
              'oci-kms': {'name': 'identitycontext', 'tenancy-id': 'ocid1.tenancy.oc1.example',
                          'binding-prefix': 'APP_IDENTITYCONTEXT'}}
    state = State(tmp_path / 'state.sqlite', 'test', {}, create=True)
    adapter = KMS(config, state)
    adapter.rows = {role: [] for role in ('vault', 'key', 'user', 'group', 'policy')}
    adapter.calls = []
    adapter.members = False
    adapter.fingerprint = None
    adapter.version = 'ocid1.keyversion.oc1.first'
    def fake(*args, body=None, **kw):
        adapter.calls.append((args, body))
        if args[:3] == ('iam', 'user', 'update-user-capabilities'):
            adapter.rows['user'][0]['capabilities'] = {
                'can-use-api-keys': True, 'can-use-auth-tokens': False, 'can-use-console-password': False,
                'can-use-customer-secret-keys': False, 'can-use-db-credentials': False,
                'can-use-o-auth2-client-credentials': False, 'can-use-smtp-credentials': False}
            return adapter.rows['user'][0]
        if args[:3] == ('iam', 'group', 'list-users'):
            return [adapter.rows['user'][0]] if adapter.members else []
        if args[:3] == ('iam', 'user', 'list-groups'):
            return [adapter.rows['group'][0]] if adapter.members else []
        if args[:3] == ('iam', 'group', 'add-user'):
            adapter.members = True
            return {}
        if args[:4] == ('iam', 'user', 'api-key', 'list'):
            return [{'fingerprint': adapter.fingerprint, 'lifecycle-state': 'ACTIVE'}] if adapter.fingerprint else []
        if args[:4] == ('iam', 'user', 'api-key', 'upload'):
            adapter.fingerprint = adapter._private_key()[2]
            return {}
        if args[:4] == ('kms', 'management', 'key-version', 'create'):
            adapter.rows['key'][0]['current-key-version'] = 'ocid1.keyversion.oc1.second'
            return {'id': 'ocid1.keyversion.oc1.second'}
        role, action = args[2:4] if args[0] == 'kms' else args[1:3]
        if action == 'get' and role == 'key':
            return copy.deepcopy(adapter.rows[role][0])
        if action == 'list':
            rows = copy.deepcopy(adapter.rows[role])
            if role == 'key':
                for row in rows:
                    row.pop('key-shape', None)
                    row.pop('current-key-version', None)
            return rows
        if action == 'create':
            row = {'id': 'ocid1.' + role + '.oc1.example', 'freeform-tags': body['freeformTags'],
                   'compartment-id': body['compartmentId'], 'lifecycle-state': 'ACTIVE'}
            if role == 'vault':
                row.update({'vault-type': body['vaultType'],
                    'management-endpoint': 'https://example-management.kms.eu-frankfurt-1.oraclecloud.com',
                    'crypto-endpoint': 'https://example-crypto.kms.eu-frankfurt-1.oraclecloud.com'})
            if role == 'key':
                row.update({'key-shape': body['keyShape'], 'protection-mode': body['protectionMode'],
                            'current-key-version': adapter.version, 'lifecycle-state': 'ENABLED'})
            if role == 'user':
                row['email'] = body['email']
                row['name'] = body['name']
            if role == 'policy':
                row['statements'] = body['statements']
            adapter.rows[role].append(row)
            return row
        raise AssertionError(args)
    monkeypatch.setattr(adapter, '_call', fake)
    yield adapter
    state.db.close()


def operation(kms):
    return kms.state.begin_operation('kms-converge', 'test')


def test_plan_no_writes(kms):
    assert all(a['action'] == 'create' for a in kms.plan()['actions'])
    assert kms.state.resources() == []
    assert not (Path(kms.config['_root']) / '.ssh').exists()
    assert all('create' not in args for args, _ in kms.calls)


def test_complete_converge_private_credentials_and_least_privilege(kms):
    result = kms.converge(operation(kms))
    assert result['algorithm'] == 'RS256'
    assert len(kms.state.resources()) == 7
    statement = kms.rows['policy'][0]['statements'][0]
    assert 'KEY_SIGN' in statement and 'KEY_READ' in statement
    assert 'KEY_ENCRYPT' not in statement and 'manage' not in statement
    assert kms.rows['key'][0]['id'] in statement
    for name in FILES:
        path = Path(kms.config['_root']) / name
        assert path.stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE KEY' not in str(result)
    bindings = (Path(kms.config['_root']) / FILES[1]).read_text()
    assert 'COLORS_PAR_APP_IDENTITYCONTEXT_OCI_PRIVATE_KEY=' in bindings
    assert 'PRIVATE KEY' in bindings
    count = len([args for args, _ in kms.calls if 'create' in args or 'upload' in args])
    kms.converge(operation(kms))
    assert len([args for args, _ in kms.calls if 'create' in args or 'upload' in args]) == count


def test_uncertain_create_is_never_repeated(kms, monkeypatch):
    original = kms._call
    def fail(*args, **kwargs):
        if 'create' in args:
            raise DeployError('lost response')
        return original(*args, **kwargs)
    monkeypatch.setattr(kms, '_call', fail)
    with pytest.raises(DeployError, match='lost response'):
        kms.converge(operation(kms))
    monkeypatch.setattr(kms, '_call', original)
    with pytest.raises(DeployError, match='uncertain'):
        kms.converge(operation(kms))
    assert not any('create' in args for args, _ in kms.calls)


def test_lost_create_response_recovers_only_matching_tag(kms, monkeypatch):
    original = kms._call
    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        if 'create' in args:
            raise DeployError('lost response')
        return result
    monkeypatch.setattr(kms, '_call', lost)
    with pytest.raises(DeployError):
        kms.converge(operation(kms))
    monkeypatch.setattr(kms, '_call', original)
    kms.converge(operation(kms))
    assert len(kms.rows['vault']) == 1
    assert kms.state.get_meta('kms-pending-vault') is None


@pytest.mark.parametrize('change', ['ownership', 'vault-type', 'key-shape', 'policy', 'credential'])
def test_drift_fails_closed(kms, change):
    kms.converge(operation(kms))
    if change == 'ownership':
        kms.rows['vault'][0]['freeform-tags'] = {}
    elif change == 'vault-type':
        kms.rows['vault'][0]['vault-type'] = 'VIRTUAL_PRIVATE'
    elif change == 'key-shape':
        kms.rows['key'][0]['key-shape']['length'] = 256
    elif change == 'policy':
        kms.rows['policy'][0]['statements'] = ['Allow group everyone to manage all-resources in tenancy']
    else:
        (Path(kms.config['_root']) / FILES[0]).unlink()
    with pytest.raises(DeployError):
        kms.converge(operation(kms))


def test_rotation_explicit_and_preserves_old_key(kms):
    kms.converge(operation(kms))
    result = kms.rotate(operation(kms))
    assert result['previous_version_id'] == 'ocid1.keyversion.oc1.first'
    assert result['key_version_id'] == 'ocid1.keyversion.oc1.second'
    assert not any('delete' in args for args, _ in kms.calls)
    assert 'ocid1.keyversion.oc1.first' in (Path(kms.config['_root']) / FILES[1]).read_text()


def test_uncertain_rotation_never_repeats(kms, monkeypatch):
    kms.converge(operation(kms))
    kms.state.set_meta('kms-pending-rotation', {'previous': 'first', 'step': 'lost'})
    with pytest.raises(DeployError, match='uncertain'):
        kms.rotate(operation(kms))


def test_configuration_scope_cannot_switch(kms):
    kms.converge(operation(kms))
    config = copy.deepcopy(kms.config)
    config['oci-kms']['tenancy-id'] = 'ocid1.tenancy.oc1.other'
    with pytest.raises(DeployError, match='scope'):
        KMS(config, kms.state)


@pytest.mark.parametrize('change,value', [('name', 'x; injected'), ('binding-prefix', 'HOME'),
                                        ('tenancy-id', 'arbitrary')])
def test_invalid_config(kms, change, value):
    kms.config['oci-kms'][change] = value
    with pytest.raises(DeployError):
        validate(kms.config)


def test_checkpoint_includes_runtime_credentials(kms):
    from pocketdeploy.vault import _files
    assert set(FILES) <= set(_files(kms.config))


def test_symlink_private_key_rejected(kms, tmp_path):
    (tmp_path / '.ssh').mkdir()
    (tmp_path / '.ssh/kms-runtime.pem').symlink_to(tmp_path / 'victim')
    with pytest.raises(DeployError, match='Symlink'):
        kms._private_key()


def test_rotation_recovery_verifies_exact_version_and_key(kms, monkeypatch):
    kms.converge(operation(kms))
    step = kms.state.intent(operation(kms), 'kms-rotate', {})
    kms.state.set_meta('kms-pending-rotation', {'step': step, 'previous': 'ocid1.keyversion.oc1.first'})
    kms.rows['key'][0]['current-key-version'] = 'ocid1.keyversion.oc1.second'
    original = kms._call
    def read(*args, **kwargs):
        if args[:4] == ('kms', 'management', 'key-version', 'get'):
            return {'id': 'ocid1.keyversion.oc1.second', 'key-id': kms.rows['key'][0]['id']}
        return original(*args, **kwargs)
    monkeypatch.setattr(kms, '_call', read)
    with pytest.raises(DeployError):
        kms.resolve_rotation('ocid1.keyversion.oc1.first')
    assert kms.resolve_rotation('ocid1.keyversion.oc1.second')['recovered']
    assert kms.state.get_meta('kms-pending-rotation') is None


def test_unknown_runtime_api_key_rejected(kms):
    kms.converge(operation(kms))
    kms.fingerprint = 'unexpected'
    with pytest.raises(DeployError, match='unexpected API key'):
        kms.converge(operation(kms))


def test_runtime_user_capabilities_are_api_only(kms):
    kms.converge(operation(kms))
    capabilities = kms.rows['user'][0]['capabilities']
    assert capabilities.pop('can-use-api-keys') is True
    assert all(value is False for value in capabilities.values())


def test_kms_cli_does_not_resolve_application_secrets(tmp_path, monkeypatch):
    from pocketdeploy.cli import execute, parser
    from pocketdeploy.config import load, scope
    path = tmp_path / 'colors.yml'
    path.write_text('''profile: test
oci-config-file-profile: test
oci-region: eu-frankfurt-1
oci-compartment-id: ocid1.compartment.oc1.example
oci-subnet-id: subnet
oci-availability-domain: ad
oci-kms:
  name: identitycontext
  tenancy-id: ocid1.tenancy.oc1.example
  binding-prefix: APP_IDENTITYCONTEXT
once:
  applications:
    - host: test.example.com
      image: example/image:latest
      env:
        SECRET: app-missing
''')
    config = load(path, env={}, resolve=False)
    with State(tmp_path / '.colors.sqlite', 'test', scope(config), create=True):
        pass
    monkeypatch.setattr(KMS, 'plan', lambda self: {'actions': [], 'safe': True})
    result = execute(parser().parse_args(['kms-plan', '-f', str(path), '--quiet']))
    assert result['safe']


def test_external_rotation_is_not_silently_adopted(kms):
    kms.converge(operation(kms))
    kms.rows['key'][0]['current-key-version'] = 'ocid1.keyversion.oc1.external'
    with pytest.raises(DeployError, match='outside the recorded rotation'):
        kms.converge(operation(kms))
    assert kms.state.get_meta('kms-signing-version') == 'ocid1.keyversion.oc1.first'


def test_explicit_rotation_can_export_new_bindings(kms):
    kms.converge(operation(kms))
    kms.rotate(operation(kms))
    result = kms.converge(operation(kms))
    assert result['key_version_id'] == 'ocid1.keyversion.oc1.second'
    assert kms.state.get_meta('kms-signing-version') == 'ocid1.keyversion.oc1.second'


def test_key_summary_requires_verified_detail(kms, monkeypatch):
    kms.converge(operation(kms))
    original = kms._call
    def altered(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[:4] == ('kms', 'management', 'key', 'get'):
            value['freeform-tags'] = {}
        return value
    monkeypatch.setattr(kms, '_call', altered)
    with pytest.raises(DeployError, match='ownership'):
        kms.converge(operation(kms))


def test_provisioning_key_reports_readiness_before_shape(kms):
    kms.converge(operation(kms))
    kms.rows['key'][0]['lifecycle-state'] = 'CREATING'
    kms.rows['key'][0].pop('key-shape')
    with pytest.raises(DeployError, match='not ready'):
        kms.converge(operation(kms))


def test_missing_runtime_email_blocks_before_cloud_mutations(kms, monkeypatch):
    monkeypatch.delenv('COLORS_PAR_APP_IDENTITYCONTEXT_OCI_USER_EMAIL')
    with pytest.raises(DeployError, match='runtime user email'):
        kms.converge(operation(kms))
    assert kms.calls == []


def rejected_creation(kms):
    op = operation(kms)
    step = kms.state.intent(op, 'kms-create-user', {'role': 'user'})
    kms.state.set_meta('kms-pending-user', {'step': step, 'operation': op})
    event = {'event-id': '10e57db6-1023-4233-98fb-c9c28af1be62', 'data': {
        'event-name': 'CreateUser', 'resource-name': 'identitycontext-signer', 'request': {'parameters': {'name': 'identitycontext-signer',
        'freeformTags': {'pocketdeploy-operation': op}}},
        'response': {'status': '400', 'message': 'Email is required'}}}
    return step, event


def test_audit_reconciles_only_definite_exact_user_rejection(kms, monkeypatch):
    step, event = rejected_creation(kms)
    original = kms._call
    monkeypatch.setattr(kms, '_call', lambda *a, **kw: [event] if a[:3] == ('audit', 'event', 'list') else original(*a, **kw))
    result = kms.resolve_user_create(event['event-id'], operation(kms))
    assert result['provider_rejected'] and result['user_absent']
    assert kms.state.get_meta('kms-pending-user') is None
    assert kms.state.db.execute('SELECT status FROM steps WHERE id=?', (step,)).fetchone()[0] == 'failed'
    assert not any('create' in args for args, _ in kms.calls)


@pytest.mark.parametrize('change', ['operation', 'name', 'status', 'event', 'message'])
def test_uncorrelated_audit_cannot_clear_pending(kms, monkeypatch, change):
    step, event = rejected_creation(kms)
    if change == 'operation': event['data']['request']['parameters']['freeformTags'] = {}
    elif change == 'name': event['data']['resource-name'] = 'other'
    elif change == 'status': event['data']['response']['status'] = '500'
    elif change == 'event': event['data']['event-name'] = 'UpdateUser'
    else: event['data']['response']['message'] = 'Unknown failure'
    original = kms._call
    monkeypatch.setattr(kms, '_call', lambda *a, **kw: [event] if a[:3] == ('audit', 'event', 'list') else original(*a, **kw))
    with pytest.raises(DeployError): kms.resolve_user_create(event['event-id'], operation(kms))
    assert kms.state.get_meta('kms-pending-user') is not None
    assert kms.state.db.execute('SELECT status FROM steps WHERE id=?', (step,)).fetchone()[0] == 'pending'


def test_existing_user_prevents_repeat_create_even_after_rejection(kms, monkeypatch):
    step, event = rejected_creation(kms)
    kms.rows['user'] = [{'name': 'identitycontext-signer', 'freeform-tags': {}}]
    original = kms._call
    monkeypatch.setattr(kms, '_call', lambda *a, **kw: [event] if a[:3] == ('audit', 'event', 'list') else original(*a, **kw))
    with pytest.raises(DeployError, match='matching runtime user exists'):
        kms.resolve_user_create(event['event-id'], operation(kms))
    assert kms.state.get_meta('kms-pending-user') is not None


def test_identity_domain_audit_metadata_correlates_rejection(kms, monkeypatch):
    step, event = rejected_creation(kms)
    event['data']['additional-details'] = {'adminValuesAdded': event['data'].pop('request')['parameters']}
    original = kms._call
    monkeypatch.setattr(kms, '_call', lambda *a, **kw: [event] if a[:3] == ('audit', 'event', 'list') else original(*a, **kw))
    assert kms.resolve_user_create(event['event-id'], operation(kms))['provider_rejected']
