import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.gcp import GCP
from pocketdeploy.state import State


@pytest.fixture
def adapter(tmp_path):
    state = State(tmp_path / 'state', 'test', {}, create=True)
    config = {'gcp-project': 'test-project', 'gcp-zone': 'us-central1-a',
              'gcp-network': 'existing', 'gcp-subnet': 'existing',
              'compute-ssh-sources': ['192.0.2.1/32'], 'compute-http-sources': ['0.0.0.0/0']}
    yield GCP(config, state)
    state.db.close()


def resource(a, role='compute', **kw):
    return {'id': '123', 'name': 'synthetic', 'description': a._description(role),
            'labels': a._labels(role), 'status': 'RUNNING', **kw}


def test_identity_not_name(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_list', lambda role: [resource(adapter, description='')])
    assert adapter._find('compute') is None


def test_duplicate_identity(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_list', lambda role: [resource(adapter), resource(adapter, id='456')])
    with pytest.raises(DeployError, match='Multiple'):
        adapter._find('compute')


def test_reused_name_rejected(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_list', lambda role: [resource(adapter, id='456')])
    with pytest.raises(DeployError, match='missing'):
        adapter._find('compute', allow_missing=True)


def test_pagination(adapter, monkeypatch):
    calls = []
    def request(path, **kw):
        calls.append(kw)
        return {'items': [resource(adapter)], 'nextPageToken': 'next'} if len(calls) == 1 else {'items': [resource(adapter, id='456')]}
    monkeypatch.setattr(adapter, '_call', request)
    assert len(adapter._list('compute')) == 2
    assert calls[1]['query'] == {'pageToken': 'next'}


def test_uncertain_create_not_replayed(adapter, monkeypatch):
    operation = adapter.state.begin_operation('test', 'hash')
    calls = []
    def fail(*a, **kw):
        if kw.get('method') == 'POST':
            calls.append(kw)
            raise DeployError('lost')
        return {'items': []}
    monkeypatch.setattr(adapter, '_call', fail)
    with pytest.raises(DeployError, match='lost'):
        adapter._mutate('compute', operation, 'POST', {'name': 'test'})
    with pytest.raises(DeployError, match='uncertain'):
        adapter._mutate('compute', operation, 'POST', {'name': 'test'})
    assert len(calls) == 1
    assert adapter.state.get_meta('gcp-pending-compute')['request_id']


def test_operation_receipt_is_resumed(adapter, monkeypatch):
    operation = adapter.state.begin_operation('test', 'hash')
    calls = []
    def request(path, **kw):
        calls.append((path, kw))
        if kw.get('method') == 'POST':
            return {'selfLink': adapter.base + adapter._zone() + '/operations/op'}
        return {'status': 'DONE'}
    monkeypatch.setattr(adapter, '_call', request)
    adapter._mutate('compute', operation, 'POST', {'name': 'test'})
    adapter._mutate('compute', operation, 'POST', {'name': 'test'})
    assert sum(kw.get('method') == 'POST' for _, kw in calls) == 1


def test_firewall_sources_are_separate(adapter):
    ssh, http = adapter._firewall('firewall'), adapter._firewall('firewall-http')
    assert ssh['sourceRanges'] == ['192.0.2.1/32']
    assert ssh['allowed'][0]['ports'] == ['22']
    assert http['allowed'][0]['ports'] == ['80', '443']
    assert ssh['targetTags'] == http['targetTags']
    assert not adapter._firewall_equal({**ssh, 'sourceTags': ['other']}, 'firewall')


def test_os_login_rejected(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: {'commonInstanceMetadata': {'items': [{'key': 'enable-oslogin', 'value': 'TRUE'}]}})
    with pytest.raises(DeployError, match='OS Login'):
        adapter._preflight()


def test_stopped_is_not_deleted(adapter):
    assert adapter._lifecycle({'status': 'TERMINATED'}) == 'STOPPED'


def test_destroy_protection_precedes_reads(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('read'))
    with pytest.raises(DeployError, match='protected'):
        adapter.delete('op')


def test_delete_requires_record(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter, role))
    with pytest.raises(DeployError, match='recorded ownership'):
        adapter.plan_delete()


def test_failed_reads_not_absence(adapter, monkeypatch):
    def fail(*a, **kw):
        raise DeployError('read failed')
    monkeypatch.setattr(adapter, '_call', fail)
    with pytest.raises(DeployError, match='read failed'):
        adapter._find('compute', allow_missing=True)


def test_complete_lifecycle_and_noop(adapter, monkeypatch):
    adapter.config['gcp-image'] = adapter.base + 'projects/images/global/images/ubuntu-exact'
    inventory = {r: [] for r in adapter.roles}
    mutations = []
    def request(path, method='GET', body=None, query=None):
        if path == adapter._root():
            return {}
        if path.endswith('/aggregated/instances'):
            return {'items': {'zones/test': {'instances': inventory['compute']}}}
        if path == adapter._subnet():
            return {'network': adapter.base + adapter._network()}
        if '/operations/' in path:
            return {'status': 'DONE'}
        # Both firewall roles share a single collection.
        if method == 'GET':
            return {'items': [i for r, items in inventory.items() if adapter._collection(r) == path for i in items]}
        mutations.append((method, body))
        if method == 'POST':
            role = body['description'].rsplit(':', 1)[1]
            item = dict(body, id=str(100 + adapter.roles.index(role)), selfLink=adapter.base + path + '/' + body['name'])
            if role == 'compute':
                item['status'] = 'RUNNING'
                item['machineType'] = adapter.base + item['machineType']
                item['networkInterfaces'][0]['accessConfigs'][0]['natIP'] = '192.0.2.5'
                inventory['boot-volume'][0]['users'] = [item['selfLink']]
            inventory[role].append(item)
        elif method == 'DELETE':
            role = next(r for r, items in inventory.items() if items and path.endswith('/' + items[0]['name']))
            inventory[role] = []
            if role == 'compute':
                inventory['boot-volume'][0]['users'] = []
        return {'selfLink': adapter.base + adapter._zone() + '/operations/test'}
    monkeypatch.setattr(adapter, '_call', request)
    op = adapter.state.begin_operation('converge', 'hash')
    assert adapter.converge('ssh-ed25519 SYNTHETIC', op)['ip'] == '192.0.2.5'
    assert len(mutations) == 4
    assert inventory['compute'][0]['serviceAccounts'] == []
    assert inventory['compute'][0]['disks'][0]['autoDelete'] is False
    adapter.converge('ssh-ed25519 SYNTHETIC', op)
    assert len(mutations) == 4
    adapter.config['compute-prevent-destroy'] = False
    assert adapter.delete(op) == {'deleted': True}
    assert adapter.state.resources() == []
    assert adapter.state.safe_status()['pending_steps'] == 0


def test_changed_pending_payload_is_not_replayed(adapter, monkeypatch):
    op = adapter.state.begin_operation('test', 'hash')
    def fail(*a, **kw):
        raise DeployError('lost')
    monkeypatch.setattr(adapter, '_call', fail)
    with pytest.raises(DeployError, match='lost'):
        adapter._mutate('firewall', op, 'POST', {'name': 'test', 'one': 1})
    with pytest.raises(DeployError, match='differs'):
        adapter._mutate('firewall', op, 'POST', {'name': 'test', 'one': 2})


def test_empty_sources_disable_rule(adapter):
    adapter.config['compute-ssh-sources'] = []
    assert adapter._firewall('firewall')['disabled'] is True


def test_google_selflink_normalization(adapter):
    link = 'https://www.googleapis.com/compute/v1/projects/test-project/zones/us-central1-a/instances/test'
    assert adapter._normalize({'nested': [{'selfLink': link}]})['nested'][0]['selfLink'] == adapter.base + link.split('/compute/v1/')[1]


def test_foreign_vm_firewall_target_blocks_delete(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: {'items': {'zones/other': {'instances': [resource(adapter, tags={'items': [adapter._tag()]})]}}})
    with pytest.raises(DeployError, match='unowned'):
        adapter._check_firewall_users()


def test_orphan_disk_drift_blocks_before_compute_create(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_check_firewall_users', lambda: None)
    disk = resource(adapter, 'boot-volume', sizeGb='999', type='wrong')
    monkeypatch.setattr(adapter, '_find', lambda role: disk if role == 'boot-volume' else None)
    monkeypatch.setattr(adapter, '_mutate', lambda *a, **kw: pytest.fail('mutation'))
    with pytest.raises(DeployError, match='drifted'):
        adapter.converge('public', 'op')


def test_operation_error_preserves_intent(adapter, monkeypatch):
    op = adapter.state.begin_operation('test', 'hash')
    monkeypatch.setattr(adapter, '_call', lambda path, **kw: {'status': 'DONE', 'error': {'errors': [{'message': 'PRIVATE'}]}} if '/operations/' in path else {'selfLink': adapter.base + adapter._zone() + '/operations/op'})
    with pytest.raises(DeployError, match='operation failed') as error:
        adapter._mutate('compute', op, 'POST', {'name': 'test'})
    assert 'PRIVATE' not in str(error.value)
    assert adapter.state.get_meta('gcp-pending-compute')['operation']


def test_http_error_suppresses_body_and_token(adapter, monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setattr('pocketdeploy.gcp.run', lambda *a, **kw: 'PRIVATE-TOKEN')
    class Opener:
        def open(self, request, **kwargs):
            assert request.headers['Authorization'] == 'Bearer PRIVATE-TOKEN'
            raise HTTPError(request.full_url, 403, 'PRIVATE-BODY', {}, None)
    monkeypatch.setattr('pocketdeploy.gcp.build_opener', lambda *a: Opener())
    with pytest.raises(DeployError) as error:
        adapter._call(adapter._root())
    assert 'PRIVATE' not in str(error.value)


def test_plan_without_state(adapter, monkeypatch):
    adapter.state = None
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_list', lambda role: [])
    assert all(a['action'] == 'create' for a in adapter.plan())


def test_unknown_lifecycle_not_running(adapter):
    assert adapter._lifecycle({}) == 'UNKNOWN'


def test_lost_operation_response_recovered_by_request_id(adapter, monkeypatch):
    op = adapter.state.begin_operation('test', 'hash')
    calls = []
    def request(path, **kw):
        calls.append(kw.get('method', 'GET'))
        if kw.get('method') == 'POST':
            raise DeployError('lost')
        pending = adapter.state.get_meta('gcp-pending-compute')
        if path.endswith('/operations'):
            return {'items': [{'clientOperationId': pending['request_id'], 'targetLink': adapter.base + pending['target'],
                               'selfLink': adapter.base + adapter._zone() + '/operations/recovered'}]}
        return {'status': 'DONE'}
    monkeypatch.setattr(adapter, '_call', request)
    with pytest.raises(DeployError, match='lost'):
        adapter._mutate('compute', op, 'POST', {'name': 'test'})
    adapter._mutate('compute', op, 'POST', {'name': 'test'})
    assert calls.count('POST') == 1
    assert adapter.state.get_meta('gcp-pending-compute')['operation'].endswith('/recovered')


def test_foreign_firewall_attachment_rejected(adapter, monkeypatch):
    item = {**adapter._firewall('firewall'), 'id': '123', 'targetTags': ['foreign']}
    monkeypatch.setattr(adapter, '_list', lambda role: [item])
    with pytest.raises(DeployError, match='target ownership'):
        adapter._find('firewall')


def test_image_selector_drift(adapter):
    adapter.state.set_meta('gcp-image-selector', adapter._image_selector())
    adapter.config['gcp-image-family'] = 'changed'
    assert 'image-selector' in adapter._disk_drift({'sizeGb': '50'})


def test_pending_adoption_rejected(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    adapter.state.set_meta('gcp-pending-firewall', {'operation': 'pending'})
    with pytest.raises(DeployError, match='pending'):
        adapter.adopt('123', 'op')


def test_operation_recovery_read_only_does_not_write(adapter, monkeypatch):
    pending = {'request_id': 'request', 'target': adapter._collection('compute') + '/test'}
    adapter.state.read_only = True
    monkeypatch.setattr(adapter, '_call', lambda path, **kw: {'items': [{'clientOperationId': 'request', 'targetLink': adapter.base + pending['target'], 'selfLink': adapter.base + adapter._zone() + '/operations/op'}]} if path.endswith('/operations') else {'status': 'DONE'})
    adapter._wait(pending, 'gcp-pending-compute')
    assert pending['operation'].endswith('/op')


@pytest.mark.parametrize('role', GCP.roles)
def test_pending_delete_blocks_converge_and_adopt(adapter, monkeypatch, role):
    adapter.state.set_meta('gcp-delete-' + role, {'id': '123'})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('cloud access'))
    with pytest.raises(DeployError, match='deletion is pending'):
        adapter.converge('public', 'op')
    with pytest.raises(DeployError, match='deletion is pending'):
        adapter.adopt('123', 'op')


def test_image_family_link_is_not_exact(adapter):
    adapter.config['gcp-image'] = adapter.base + 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64'
    with pytest.raises(DeployError, match='exact'):
        adapter._image()


def test_partial_delete_resumes_absent_recorded_compute(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    op = adapter.state.begin_operation('delete', 'hash')
    step = adapter.state.intent(op, 'delete-compute', {'id': '123'})
    adapter.state.set_meta('gcp-delete-compute', {'id': '123', 'step': step})
    monkeypatch.setattr(adapter, '_list', lambda role: [])
    monkeypatch.setattr(adapter, '_mutate', lambda *a, **kw: pytest.fail('mutation'))
    assert adapter.delete(op) == {'deleted': True}
    assert adapter.state.get_resource('compute') is None
    assert adapter.state.get_meta('gcp-delete-compute') is None
    assert adapter.state.safe_status()['pending_steps'] == 0


def test_absent_record_without_delete_intent_blocks(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_list', lambda role: [])
    with pytest.raises(DeployError, match='missing'):
        adapter.plan_delete()


def test_plan_pending_create_is_blocked_and_read_only(adapter, monkeypatch):
    adapter.state.set_meta('gcp-pending-compute', {'request_id': 'unknown'})
    adapter.state.read_only = True
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_list', lambda role: [])
    compute = next(a for a in adapter.plan() if a['resource'] == 'compute')
    assert compute['action'] == 'blocked'
    assert 'pending-mutation' in compute['changed_fields']


def test_plan_pending_delete_blocks_all_roles_even_absent_record(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    adapter.state.set_meta('gcp-delete-compute', {'id': '123'})
    adapter.state.read_only = True
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_list', lambda role: [])
    assert all(a['action'] == 'blocked' for a in adapter.plan())


def test_plan_pending_update_not_reported_as_retain(adapter, monkeypatch):
    adapter.state.set_meta('gcp-pending-firewall', {'desired': 'old'})
    item = {**adapter._firewall('firewall'), 'id': '123'}
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_list', lambda role: [item] if role == 'firewall' else [])
    firewall = next(a for a in adapter.plan() if a['resource'] == 'firewall')
    assert firewall['action'] == 'blocked'


def test_plan_orphan_disk_selector_drift_blocks_compute(adapter, monkeypatch):
    adapter.state.set_meta('gcp-image-selector', adapter._image_selector())
    adapter.config['gcp-image-family'] = 'changed'
    disk = resource(adapter, 'boot-volume', sizeGb='50')
    monkeypatch.setattr(adapter, '_preflight', lambda: None)
    monkeypatch.setattr(adapter, '_list', lambda role: [disk] if role == 'boot-volume' else [])
    actions = {a['resource']: a for a in adapter.plan()}
    assert actions['boot-volume']['action'] == actions['compute']['action'] == 'blocked'
    assert 'image-selector' in actions['boot-volume']['changed_fields']


def test_firewall_api_reordered_lists_and_omitted_defaults(adapter):
    adapter.config['compute-http-sources'] = ['192.0.2.0/24', '198.51.100.0/24']
    item = adapter._firewall('firewall-http')
    item['sourceRanges'] = list(reversed(item['sourceRanges']))
    item['allowed'][0]['ports'].reverse()
    for key in ('direction', 'priority', 'disabled'):
        item.pop(key)
    assert adapter._firewall_equal(item, 'firewall-http')


def test_disabled_empty_sources_have_stable_api_default(adapter):
    adapter.config['compute-ssh-sources'] = []
    item = adapter._firewall('firewall')
    assert item['disabled'] is True
    assert item['sourceRanges'] == ['0.0.0.0/0']
    item.pop('sourceRanges')
    assert adapter._firewall_equal(item, 'firewall')
    item.pop('disabled')
    assert not adapter._firewall_equal(item, 'firewall')


@pytest.mark.parametrize('mode,expected', [
    ('gcloud', ['gcloud', 'auth', 'print-access-token', '--quiet']),
    ('application-default', ['gcloud', 'auth', 'application-default', 'print-access-token', '--quiet']),
])
def test_explicit_auth_mode_uses_selected_credential_source(adapter, monkeypatch, mode, expected):
    import io
    calls = []
    adapter.config['gcp-auth'] = mode
    monkeypatch.setattr('pocketdeploy.gcp.run', lambda args: calls.append(args) or 'PRIVATE-TOKEN')
    class Opener:
        def open(self, request, **kwargs):
            assert request.headers['Authorization'] == 'Bearer PRIVATE-TOKEN'
            return io.StringIO('{}')
    monkeypatch.setattr('pocketdeploy.gcp.build_opener', lambda *a: Opener())
    assert adapter._call(adapter._root()) == {}
    assert calls == [expected]
