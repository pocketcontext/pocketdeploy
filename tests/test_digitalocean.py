import json
import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.digitalocean import DigitalOcean
from pocketdeploy.state import State


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    config = {'profile': 'test', 'digitalocean-account-id': 'account', 'digitalocean-region': 'lon1',
              'digitalocean-vpc-id': 'vpc', 'digitalocean-size': 's-1vcpu-2gb',
              'digitalocean-image': 'ubuntu-24-04-x64', 'compute-prevent-destroy': False,
              'compute-ssh-sources': ['192.0.2.1/32'], 'compute-http-sources': ['0.0.0.0/0']}
    with State(tmp_path / 'state.sqlite', 'test', {}, create=True) as state:
        instance = DigitalOcean(config, state)
        monkeypatch.setattr('pocketdeploy.digitalocean.time.sleep', lambda _: None)
        yield instance


def droplet(a, **kw):
    return {'id': 123, 'status': 'active', 'tags': [a._tag()],
            'region': {'slug': 'lon1'}, 'vpc_uuid': 'vpc', 'size_slug': 's-1vcpu-2gb', 'disk': 50,
            'image': {'id': 1234, 'slug': 'ubuntu-24-04-x64'},
            'networks': {'v4': [{'type': 'public', 'ip_address': '192.0.2.10'}]}, **kw}


def firewall(a, **kw):
    return {'id': 'fw', 'name': a._tag() + '-firewall', 'tags': [a._tag()],
            'droplet_ids': [], 'status': 'succeeded', 'pending_changes': [], **a._rules(), **kw}


def fake(a, monkeypatch, droplets=None, firewalls=None):
    inventory = {'droplets': droplets or [], 'firewalls': firewalls or [], 'tags': []}
    calls = []
    def request(method, path, body=None):
        calls.append((method, path, body))
        endpoint = path.split('?')[0]
        if method == 'GET':
            if endpoint == 'account':
                return {'account': {'uuid': 'account', 'status': 'active'}}
            if endpoint == 'vpcs/vpc':
                return {'vpc': {'id': 'vpc', 'region': 'lon1'}}
            if endpoint.startswith('images/'):
                return {'image': {'id': 1234, 'distribution': 'Ubuntu', 'regions': ['lon1'], 'status': 'available'}}
            if endpoint == 'sizes':
                return {'sizes': [{'slug': 's-1vcpu-2gb', 'available': True, 'regions': ['lon1']}]}
            if endpoint == 'tags':
                for tag in inventory['tags']:
                    tag['resources'] = {'count': len(inventory['droplets']), 'droplets': {'count': len(inventory['droplets'])}}
            return {endpoint: inventory[endpoint]}
        if method == 'POST':
            if endpoint == 'tags':
                inventory['tags'].append({'name': body['name'], 'resources': {'count': 0}})
                return {'tag': inventory['tags'][0]}
            item = droplet(a) if endpoint == 'droplets' else firewall(a)
            inventory[endpoint].append(item)
            return {'droplet' if endpoint == 'droplets' else 'firewall': item}
        if method == 'DELETE':
            inventory[endpoint.split('/')[0]].clear()
            return {}
        if method == 'PUT':
            inventory['firewalls'][0].update(body)
            return {'firewall': inventory['firewalls'][0]}
        raise AssertionError(method)
    monkeypatch.setattr(a, '_call', request)
    return inventory, calls


def test_complete_lifecycle_and_noop(adapter, monkeypatch):
    inventory, calls = fake(adapter, monkeypatch)
    operation = adapter.state.begin_operation('converge', 'hash')
    assert adapter.converge('ssh-ed25519 synthetic', operation)['instance_id'] == '123'
    assert adapter.state.get_meta('digitalocean-image-id') == 1234
    body = next(body for method, path, body in calls if method == 'POST' and path == 'droplets')
    cloud = json.loads(body['user_data'].split('\n', 1)[1])
    assert cloud['users'][0]['name'] == 'ubuntu'
    assert cloud['users'][0]['ssh_authorized_keys'] == ['ssh-ed25519 synthetic']
    calls.clear()
    adapter.converge('ssh-ed25519 synthetic', operation)
    assert all(method == 'GET' for method, _, _ in calls)
    adapter.delete(operation)
    assert not inventory['droplets'] and not inventory['firewalls']
    assert not adapter.state.resources()


def test_uncertain_create_never_retries(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    operation = adapter.state.begin_operation('converge', 'hash')
    adapter.state.set_meta('digitalocean-pending-compute', {'step': adapter.state.intent(operation, 'create-compute', {})})
    with pytest.raises(DeployError, match='uncertain'):
        adapter._create('compute', operation, {})
    assert calls == []


@pytest.mark.parametrize('role', ['compute', 'firewall'])
def test_fresh_create_waits_for_inventory_visibility(adapter, monkeypatch, role):
    item = droplet(adapter) if role == 'compute' else firewall(adapter)
    endpoint = 'droplets' if role == 'compute' else 'firewalls'
    reads, posts = [], []
    def request(method, path, body=None):
        if method == 'POST':
            posts.append(path)
            return {'droplet' if role == 'compute' else 'firewall': item}
        reads.append(path)
        return {endpoint: [] if len(reads) < 3 else [item]}
    monkeypatch.setattr(adapter, '_call', request)
    operation = adapter.state.begin_operation('converge', 'hash')
    assert adapter._create(role, operation, {}) == item
    assert len(reads) == 3 and posts == [endpoint]
    assert adapter.state.get_resource(role)['provider_id'] == str(item['id'])


def test_fresh_create_visibility_timeout_keeps_identity_and_never_recreates(adapter, monkeypatch):
    reads, posts = [], []
    def request(method, path, body=None):
        if method == 'POST':
            posts.append(path)
            return {'droplet': droplet(adapter)}
        reads.append(path)
        return {'droplets': []}
    monkeypatch.setattr(adapter, '_call', request)
    operation = adapter.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='not yet visible'):
        adapter._create('compute', operation, {})
    assert len(reads) == 12 and posts == ['droplets']
    assert adapter.state.get_resource('compute')['provider_id'] == '123'
    with pytest.raises(DeployError, match='Recorded DigitalOcean resource is missing'):
        adapter._find('compute')


def test_lost_create_response_recovers_visible_resource(adapter, monkeypatch):
    fake(adapter, monkeypatch, [droplet(adapter)], [firewall(adapter)])
    operation = adapter.state.begin_operation('converge', 'hash')
    step = adapter.state.intent(operation, 'create-compute', {})
    adapter.state.set_meta('digitalocean-pending-compute', {'step': step})
    adapter.converge('key', operation)
    assert adapter.state.get_meta('digitalocean-pending-compute') is None


@pytest.mark.parametrize('variant', ['missing', 'ownership', 'duplicate'])
def test_recorded_identity_failures(adapter, monkeypatch, variant):
    item = droplet(adapter)
    adapter._remember('compute', item)
    items = [] if variant == 'missing' else [droplet(adapter, tags=[])] if variant == 'ownership' else [item, droplet(adapter, id=456)]
    fake(adapter, monkeypatch, items)
    with pytest.raises(DeployError):
        adapter._find('compute')


def test_name_alone_never_authorizes_firewall(adapter, monkeypatch):
    fake(adapter, monkeypatch, firewalls=[firewall(adapter, tags=[])])
    with pytest.raises(DeployError, match='ownership'):
        adapter._find('firewall')


def test_firewall_external_direct_attachment_blocks(adapter, monkeypatch):
    fake(adapter, monkeypatch, firewalls=[firewall(adapter, droplet_ids=[456])])
    with pytest.raises(DeployError, match='attachment'):
        adapter._find('firewall')


def test_deployment_tag_external_droplet_blocks(adapter, monkeypatch):
    fake(adapter, monkeypatch, [droplet(adapter), droplet(adapter, id=456)])
    with pytest.raises(DeployError, match='Multiple'):
        adapter._find('compute')


def test_additional_firewall_blocks_converge(adapter, monkeypatch):
    fake(adapter, monkeypatch, [droplet(adapter)], [firewall(adapter), firewall(adapter, id='external', name='external')])
    with pytest.raises(DeployError, match='attachment'):
        adapter.converge('key', adapter.state.begin_operation('converge', 'hash'))


@pytest.mark.parametrize('field,value', [('size_slug', 'larger'), ('vpc_uuid', 'elsewhere'), ('image', {'id': 9}), ('disk', 100), ('volume_ids', ['other'])])
def test_drift_blocks_mutation(adapter, monkeypatch, field, value):
    adapter._remember('compute', droplet(adapter))
    _, calls = fake(adapter, monkeypatch, [droplet(adapter, **{field: value})], [firewall(adapter)])
    with pytest.raises(DeployError, match='drifted'):
        adapter.converge('key', adapter.state.begin_operation('converge', 'hash'))
    assert all(method == 'GET' for method, _, _ in calls)


def test_wrong_account_blocks_before_cloud_mutation(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *args: {'account': {'uuid': 'wrong', 'status': 'active'}})
    with pytest.raises(DeployError, match='account identity'):
        adapter.plan()


def test_delete_requires_recorded_ownership(adapter, monkeypatch):
    fake(adapter, monkeypatch, [droplet(adapter)], [firewall(adapter)])
    with pytest.raises(DeployError, match='recorded ownership'):
        adapter.plan_delete()


@pytest.mark.parametrize('disappears', [True, False])
def test_authorized_delete_waits_for_identity_after_tag_removal(adapter, monkeypatch, disappears):
    item = droplet(adapter)
    adapter._remember('compute', item)
    inventory, calls = fake(adapter, monkeypatch, droplets=[item])
    base_request = adapter._call
    deleting, polls = False, 0
    def request(method, path, body=None):
        nonlocal deleting, polls
        if method == 'DELETE' and path == 'droplets/123':
            calls.append((method, path, body))
            deleting = True
            item['tags'] = []
            return {}
        if deleting and path.startswith('droplets?'):
            polls += 1
            if disappears and polls == 3:
                inventory['droplets'].clear()
        return base_request(method, path, body)
    monkeypatch.setattr(adapter, '_call', request)
    operation = adapter.state.begin_operation('delete', 'hash')
    if disappears:
        adapter.delete(operation)
        assert polls == 3
        assert adapter.state.get_resource('compute') is None
    else:
        with pytest.raises(DeployError, match='outcome remains pending'):
            adapter.delete(operation)
        assert polls == 30
        assert adapter.state.get_resource('compute')['provider_id'] == '123'
        # A later invocation still refuses changed ownership during preflight.
        with pytest.raises(DeployError, match='ownership changed'):
            adapter.plan_delete()
    assert len([c for c in calls if c[0] == 'DELETE']) == 1


def test_delete_lost_response_reconciles_without_mutation(adapter, monkeypatch):
    adapter._remember('compute', droplet(adapter))
    operation = adapter.state.begin_operation('delete', 'hash')
    step = adapter.state.intent(operation, 'delete-compute', {'id': '123'})
    adapter.state.set_meta(adapter.delete_pending_key, {'id': '123', 'step': step})
    _, calls = fake(adapter, monkeypatch)
    adapter.delete(operation)
    assert adapter.state.get_resource('compute') is None
    assert all(method == 'GET' for method, _, _ in calls)


def test_uncertain_delete_does_not_repeat(adapter, monkeypatch):
    adapter._remember('compute', droplet(adapter))
    operation = adapter.state.begin_operation('delete', 'hash')
    step = adapter.state.intent(operation, 'delete-compute', {'id': '123'})
    adapter.state.set_meta(adapter.delete_pending_key, {'id': '123', 'step': step})
    _, calls = fake(adapter, monkeypatch, [droplet(adapter)])
    with pytest.raises(DeployError, match='pending'):
        adapter.delete(operation)
    assert all(method == 'GET' for method, _, _ in calls)


def test_firewall_uncertainty_keeps_intent(adapter, monkeypatch):
    item = firewall(adapter, inbound_rules=[])
    adapter._remember('firewall', item)
    operation = adapter.state.begin_operation('converge', 'hash')
    step = adapter.state.intent(operation, 'firewall-rules', {})
    adapter.state.set_meta('digitalocean-pending-firewall-rules', {'id': 'fw', 'step': step, 'desired': adapter._rules()})
    _, calls = fake(adapter, monkeypatch, firewalls=[item])
    with pytest.raises(DeployError, match='uncertain'):
        adapter._reconcile_rules(item, operation)
    assert calls == []


def test_pagination_ignores_foreign_next_url(adapter, monkeypatch):
    paths = []
    def request(method, path):
        paths.append(path)
        return {'droplets': [droplet(adapter, id=len(paths))],
                'links': {'pages': {'next': 'https://evil.invalid/'}} if len(paths) == 1 else {}}
    monkeypatch.setattr(adapter, '_call', request)
    assert len(adapter._list('droplets')) == 2
    assert paths == ['droplets?per_page=200&page=1', 'droplets?per_page=200&page=2']


def test_http_failure_suppresses_token_body_and_redirect(adapter, monkeypatch):
    monkeypatch.setenv('COLORS_PAR_DIGITALOCEAN_ACCESS_TOKEN', 'synthetic-private-token')
    class Response:
        status = 302
        def read(self, *_):
            raise AssertionError('Must not read error body')
    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr('pocketdeploy.digitalocean.http.client.HTTPSConnection', Connection)
    with pytest.raises(DeployError) as error:
        adapter._call('GET', 'account')
    assert '302' in str(error.value)
    assert 'synthetic' not in str(error.value)


def test_firewall_expands_before_removing_old_rules(adapter, monkeypatch):
    old = {'protocol': 'tcp', 'ports': '4444', 'sources': {'addresses': ['192.0.2.1/32']}}
    item = firewall(adapter, inbound_rules=[old])
    adapter._remember('firewall', item)
    _, calls = fake(adapter, monkeypatch, firewalls=[item])
    adapter._reconcile_rules(item, adapter.state.begin_operation('converge', 'hash'))
    writes = [body for method, _, body in calls if method == 'PUT']
    assert len(writes) == 2
    assert old in writes[0]['inbound_rules']
    assert old not in writes[1]['inbound_rules']
    assert adapter._rules_equal(item)


@pytest.mark.parametrize('all_ports', ['0', '1-65535'])
def test_live_firewall_defaults_do_not_trigger_updates(adapter, monkeypatch, all_ports):
    item = firewall(adapter)
    for direction in ('inbound_rules', 'outbound_rules'):
        for rule in item[direction]:
            rule['action'] = 'allow'
            selector = rule.get('sources', rule.get('destinations'))
            selector.update(tags=[], droplet_ids=[], load_balancer_uids=[])
            if direction == 'outbound_rules':
                rule['ports'] = '0' if rule['protocol'] == 'icmp' else all_ports
    adapter._remember('firewall', item)
    _, calls = fake(adapter, monkeypatch, firewalls=[item])
    assert adapter._rules_equal(item)
    adapter._reconcile_rules(item, adapter.state.begin_operation('converge', 'hash'))
    assert not calls


@pytest.mark.parametrize('change', [{'action': 'deny'}, {'action': 'unknown'}, {'ports': '443'}])
def test_firewall_normalization_preserves_semantic_differences(adapter, change):
    item = firewall(adapter)
    item['outbound_rules'][0].update(change)
    assert not adapter._rules_equal(item)


def test_firewall_expansion_deduplicates_live_default_rules(adapter, monkeypatch):
    item = firewall(adapter)
    for direction in ('inbound_rules', 'outbound_rules'):
        for rule in item[direction]:
            rule['action'] = 'allow'
            if direction == 'outbound_rules':
                rule['ports'] = '0'
    adapter._remember('firewall', item)
    _, calls = fake(adapter, monkeypatch, firewalls=[item])
    adapter.config['compute-ssh-sources'] = ['192.0.2.1/32', '192.0.2.2/32']
    adapter._reconcile_rules(item, adapter.state.begin_operation('converge', 'hash'))
    writes = [body for method, _, body in calls if method == 'PUT']
    assert len(writes) == 2
    assert len(writes[0]['outbound_rules']) == 3
    assert len(writes[0]['inbound_rules']) == 4


def test_unverified_firewall_expansion_never_removes_old_rules(adapter, monkeypatch):
    old = {'protocol': 'tcp', 'ports': '4444', 'sources': {'addresses': ['192.0.2.1/32']}}
    item = firewall(adapter, inbound_rules=[old])
    adapter._remember('firewall', item)
    calls = []
    def request(method, path, body=None):
        calls.append((method, body))
        if method == 'GET':
            return {'firewalls': [item]}
        return {}
    monkeypatch.setattr(adapter, '_call', request)
    with pytest.raises(DeployError, match='additions'):
        adapter._reconcile_rules(item, adapter.state.begin_operation('converge', 'hash'))
    assert len([c for c in calls if c[0] == 'PUT']) == 1
    assert adapter.state.get_meta('digitalocean-pending-firewall-rules')['phase'] == 'expand'


def test_firewall_update_waits_for_async_expansion_and_contraction(adapter, monkeypatch):
    old = {'protocol': 'tcp', 'ports': '4444', 'sources': {'addresses': ['192.0.2.1/32']}}
    item = firewall(adapter, inbound_rules=[old])
    adapter._remember('firewall', item)
    writes, reads = [], []
    pending = None
    phase_reads = 0
    def request(method, path, body=None):
        nonlocal pending, phase_reads
        if method == 'PUT':
            writes.append(body)
            pending, phase_reads = body, 0
            item['status'] = 'waiting'
            return {'firewall': item}
        reads.append(path)
        phase_reads += 1
        if phase_reads == 2:
            item.update(pending)
        if phase_reads == 3:
            item['status'] = 'succeeded'
        return {'firewalls': [item]}
    monkeypatch.setattr(adapter, '_call', request)
    adapter._reconcile_rules(item, adapter.state.begin_operation('converge', 'hash'))
    assert len(writes) == 2 and len(reads) == 6
    assert old in writes[0]['inbound_rules']
    assert old not in writes[1]['inbound_rules']
    assert adapter._rules_equal(item)
    assert adapter.state.get_meta('digitalocean-pending-firewall-rules') is None


def test_bootstrap_preserves_host_keys(adapter):
    adapter.config['_cloud_init'] = '#cloud-config\n' + json.dumps({'ssh_keys': {'ed25519_private': 'synthetic-key'}})
    assert json.loads(adapter._user_data('public').split('\n', 1)[1])['ssh_keys']['ed25519_private'] == 'synthetic-key'


def test_read_permission_failure_never_becomes_absence(adapter, monkeypatch):
    def request(*args, **kwargs):
        raise DeployError('DigitalOcean request failed (HTTP 403); response suppressed.')
    monkeypatch.setattr(adapter, '_call', request)
    with pytest.raises(DeployError, match='403'):
        adapter._find('compute')


def test_adopt_checks_id_and_records_owned_resources(adapter, monkeypatch):
    inventory, _ = fake(adapter, monkeypatch, [droplet(adapter)], [firewall(adapter)])
    inventory['tags'].append({'name': adapter._tag()})
    operation = adapter.state.begin_operation('adopt', 'hash')
    with pytest.raises(DeployError, match='adoption'):
        adapter.adopt('other', operation)
    assert not adapter.state.resources()
    assert adapter.adopt('123', operation)['instance_id'] == '123'
    assert {r['name'] for r in adapter.state.resources()} == {'compute', 'firewall', 'deployment-tag'}


@pytest.mark.parametrize('field', ['volume_ids', 'snapshot_ids', 'backup_ids'])
def test_delete_blocks_unmanaged_storage(adapter, monkeypatch, field):
    item = droplet(adapter, **{field: ['external']})
    adapter._remember('compute', item)
    fake(adapter, monkeypatch, [item])
    with pytest.raises(DeployError, match='separately'):
        adapter.plan_delete()


def test_plan_before_init_only_reads(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    adapter.state = None
    assert [item['action'] for item in adapter.plan()] == ['create', 'create', 'create']
    assert all(method == 'GET' for method, _, _ in calls)


def test_pinned_image_selector_change_blocks(adapter, monkeypatch):
    fake(adapter, monkeypatch)
    assert adapter._image() == 1234
    adapter.config['digitalocean-image'] = 'different-image'
    with pytest.raises(DeployError, match='selector differs'):
        adapter._image()


def test_uncertain_tag_create_is_not_repeated(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    operation = adapter.state.begin_operation('converge', 'hash')
    step = adapter.state.intent(operation, 'create-deployment-tag', {})
    adapter.state.set_meta('digitalocean-pending-tag', {'step': step})
    with pytest.raises(DeployError, match='uncertain'):
        adapter._ensure_tag(operation)
    assert all(method == 'GET' for method, _, _ in calls)


def test_tag_external_associations_block_deletion(adapter, monkeypatch):
    adapter.state.put_resource('deployment-tag', 'digitalocean-tag', adapter._tag(), {}, owned=True)
    monkeypatch.setattr(adapter, '_tag_record', lambda: {'name': adapter._tag(), 'resources': {'count': 1, 'droplets': {'count': 0}}})
    monkeypatch.setattr(adapter, '_find', lambda *a, **kw: None)
    with pytest.raises(DeployError, match='external'):
        adapter._tag_deletion_target()


def test_pending_create_blocks_deletion_even_when_not_visible(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    adapter.state.set_meta('digitalocean-pending-compute', {'step': 'pending'})
    with pytest.raises(DeployError, match='creation'):
        adapter.plan_delete()
    assert calls == []


def test_pending_delete_blocks_convergence(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    adapter.state.set_meta('digitalocean-delete-compute', {'id': '123', 'step': 'pending'})
    with pytest.raises(DeployError, match='deletion is pending'):
        adapter.converge('public', 'operation')
    assert calls == []


@pytest.mark.parametrize('key', ['digitalocean-delete-compute', 'digitalocean-delete-firewall', 'digitalocean-delete-tag', 'digitalocean-pending-firewall-rules'])
def test_adopt_preserves_pending_mutations(adapter, monkeypatch, key):
    _, calls = fake(adapter, monkeypatch)
    adapter.state.set_meta(key, {'step': 'pending'})
    with pytest.raises(DeployError, match='pending'):
        adapter.adopt('123', 'operation')
    assert adapter.state.get_meta(key) == {'step': 'pending'}
    assert calls == []


def test_converge_checks_tag_associations_before_mutation(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    adapter.state.put_resource('deployment-tag', 'digitalocean-tag', adapter._tag(), {}, owned=True)
    monkeypatch.setattr(adapter, '_tag_record', lambda: {'name': adapter._tag(), 'resources': {'count': 1, 'droplets': {'count': 0}}})
    with pytest.raises(DeployError, match='external'):
        adapter.converge('key', adapter.state.begin_operation('converge', 'hash'))
    assert all(method == 'GET' for method, _, _ in calls)


def test_initialized_plan_includes_tag_and_blocks_unknown_tag_create(adapter, monkeypatch):
    fake(adapter, monkeypatch)
    assert adapter.plan()[0] == {'resource': 'deployment-tag', 'action': 'create'}
    adapter.state.set_meta('digitalocean-pending-tag', {'step': 'pending'})
    assert all(row['action'] == 'blocked' for row in adapter.plan())


def test_plan_rejects_changed_pinned_selector(adapter, monkeypatch):
    fake(adapter, monkeypatch)
    adapter._image()
    adapter.config['digitalocean-image'] = 'different'
    with pytest.raises(DeployError, match='selector differs'):
        adapter.plan()


def test_create_uses_only_tracked_deployment_tag(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    adapter.converge('key', adapter.state.begin_operation('converge', 'hash'))
    body = next(body for method, path, body in calls if method == 'POST' and path == 'droplets')
    assert body['tags'] == [adapter._tag()]
