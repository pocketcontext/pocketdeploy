import json
import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.oci import OCI
from pocketdeploy.state import State


@pytest.fixture
def adapter(tmp_path):
    config = {'profile': 'test', 'oci-compartment-id': 'compartment', 'oci-subnet-id': 'subnet',
              'oci-availability-domain': 'AD1', 'oci-shape': 'VM.Standard.A1.Flex',
              'oci-ocpus': 1, 'oci-memory-in-gbs': 6, 'oci-image-id': 'image',
              'compute-ssh-sources': ['192.0.2.1/32'], 'compute-http-sources': ['0.0.0.0/0']}
    state = State(tmp_path / 'state.sqlite', 'test', {}, create=True)
    yield OCI(config, state)
    state.db.close()


def resource(adapter, role='compute', **overrides):
    return {'id': role + '-id', 'freeform-tags': adapter._tags(role), 'lifecycle-state': 'RUNNING',
            'shape': 'VM.Standard.A1.Flex', 'availability-domain': 'AD1', 'image-id': 'image',
            'shape-config': {'ocpus': 1, 'memory-in-gbs': 6}, **overrides}


def test_failed_read_never_becomes_absence(adapter, monkeypatch):
    def fail(*a, **kw):
        raise DeployError('Command failed; output suppressed.')
    monkeypatch.setattr(adapter, '_call', fail)
    with pytest.raises(DeployError):
        adapter._find('compute')


def test_duplicate_ownership_is_ambiguous(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [resource(adapter), resource(adapter, id='second')])
    with pytest.raises(DeployError, match='Multiple'):
        adapter._find('compute')


def test_name_does_not_establish_ownership(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [resource(adapter, **{'freeform-tags': {}, 'display-name': 'test-once-compute'})])
    assert adapter._find('compute') is None


def test_recorded_ownership_mismatch_blocks(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [resource(adapter, **{'freeform-tags': {}})])
    with pytest.raises(DeployError, match='ownership'):
        adapter._find('compute')


def test_missing_recorded_resource_is_not_recreated(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [])
    with pytest.raises(DeployError, match='missing'):
        adapter._find('compute')


def test_uncertain_create_is_not_retried(adapter):
    operation = adapter.state.begin_operation('create', 'hash')
    def failed_create():
        raise DeployError('Connection lost')
    with pytest.raises(DeployError):
        adapter._create('compute', operation, failed_create)
    calls = []
    with pytest.raises(DeployError, match='uncertain'):
        adapter._create('compute', operation, lambda: calls.append(True))
    assert calls == []


def test_interrupted_create_discovered_by_tag(adapter, monkeypatch):
    adapter.state.set_meta('oci-pending-compute', 'operation')
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [resource(adapter)])
    assert adapter._find('compute')['id'] == 'compute-id'


def test_compute_drift_detects_size_and_image(adapter):
    instance = resource(adapter, **{'image-id': 'other', 'shape-config': {'ocpus': 2, 'memory-in-gbs': 6}})
    assert set(adapter._drift(instance)) == {'image-id', 'oci-ocpus'}


def test_rules_compare_oci_response_keys(adapter):
    current = []
    for idx, rule in enumerate(adapter._rules()):
        r = dict(rule, id=str(idx), **{'time-created': 'now', 'is-valid': True})
        if 'tcpOptions' in r:
            opts = r.pop('tcpOptions')
            r['tcp-options'] = {'destination-port-range': opts['destinationPortRange'], 'source-port-range': None}
        for old, new in [('sourceType', 'source-type'), ('destinationType', 'destination-type'), ('isStateless', 'is-stateless')]:
            if old in r:
                r[new] = r.pop(old)
        current.append(r)
    assert adapter._rules_equal(current)
    current.pop()
    assert not adapter._rules_equal(current)


def test_destroy_protected_without_cloud_access(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('cloud called'))
    with pytest.raises(DeployError, match='protected'):
        adapter.delete('operation')


def test_delete_requires_recorded_ownership(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter, role))
    with pytest.raises(DeployError, match='recorded ownership'):
        adapter.delete('operation')


def test_cli_payload_uses_stdin_not_arguments(adapter, monkeypatch):
    captured = {}
    def fake(args, **kwargs):
        captured.update(args=args, **kwargs)
        return '{"data":{"id":"safe"}}'
    monkeypatch.setattr('pocketdeploy.oci.run', fake)
    assert adapter._call('compute', 'instance', 'launch', body={'secret': 'sensitive-value'}) == {'id': 'safe'}
    assert 'sensitive-value' not in ' '.join(captured['args'])
    assert json.loads(captured['input'])['secret'] == 'sensitive-value'


@pytest.mark.parametrize('bad', [None, {}, 'error', [None]])
def test_malformed_list_not_absent(adapter, monkeypatch, bad):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: bad)
    with pytest.raises(DeployError, match='invalid resource list'):
        adapter._find('compute')


def test_delete_recovery_after_cloud_deleted(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    operation = adapter.state.begin_operation('delete', 'hash')
    step = adapter.state.intent(operation, 'delete-compute', {'id': 'compute-id'})
    adapter.state.set_meta('oci-delete-compute', {'step': step, 'id': 'compute-id', 'retain_boot': False})
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'boot-id', {}, owned=True)
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [])
    assert adapter.delete(operation) == {'deleted': True}
    assert adapter.state.get_resource('compute') is None
    assert adapter.state.get_resource('boot-volume') is None
    assert adapter.state.safe_status()['pending_steps'] == 0
    assert adapter.delete(operation) == {'deleted': True}


def test_recovered_create_completes_pending_checkpoint(adapter):
    operation = adapter.state.begin_operation('create', 'hash')
    step = adapter.state.intent(operation, 'create-compute', {})
    adapter.state.set_meta('oci-pending-compute', {'step': step, 'operation': operation})
    adapter._recovered('compute', resource(adapter))
    assert adapter.state.get_resource('compute')['provider_id'] == 'compute-id'
    assert adapter.state.safe_status()['pending_steps'] == 0
    assert adapter.state.get_meta('oci-pending-compute') is None


def test_subnet_and_nsg_drift_detected_before_mutations(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [{'is-primary': True, 'subnet-id': 'wrong', 'nsg-ids': ['other']}])
    assert adapter._network_drift(resource(adapter), resource(adapter, 'firewall')) == ['oci-subnet-id', 'nsg-attachment']


def test_terminating_instance_not_absent(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [resource(adapter, **{'lifecycle-state': 'TERMINATING'})])
    assert adapter._find('compute')['lifecycle-state'] == 'TERMINATING'


def test_external_boot_disk_drift_detected(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {'size-in-gbs': 100, 'vpus-per-gb': 10})
    assert adapter._storage_drift(resource(adapter)) == ['oci-boot-volume-size-in-gbs']


def test_create_then_converge_has_no_cloud_mutations(adapter, monkeypatch):
    cloud = {'compute': [], 'firewall': [], 'rules': []}
    mutations = []
    def call(*args, body=None, **kwargs):
        command = args[:3]
        if command == ('network', 'subnet', 'get'):
            return {'vcn-id': 'vcn', 'prohibit-public-ip-on-vnic': False}
        if command == ('compute', 'instance', 'list'):
            return cloud['compute']
        if command == ('network', 'nsg', 'list'):
            return cloud['firewall']
        if command == ('network', 'nsg', 'create'):
            mutations.append('firewall')
            item = resource(adapter, 'firewall', **{'vcn-id': 'vcn'})
            cloud['firewall'] = [item]
            return item
        if args[:4] == ('network', 'nsg', 'rules', 'list'):
            return cloud['rules']
        if args[:4] == ('network', 'nsg', 'rules', 'add'):
            mutations.append('rules')
            cloud['rules'] = [dict(rule, id=str(i)) for i, rule in enumerate(body['securityRules'])]
            return {}
        if command == ('compute', 'instance', 'launch'):
            mutations.append('compute')
            assert body['metadata']['ssh_authorized_keys'] == 'ssh-ed25519 public'
            cloud['compute'] = [resource(adapter)]
            return cloud['compute'][0]
        if command == ('compute', 'instance', 'get'):
            return cloud['compute'][0]
        if command == ('compute', 'boot-volume-attachment', 'list'):
            return [{'boot-volume-id': 'boot-id', 'lifecycle-state': 'ATTACHED'}]
        if command == ('bv', 'boot-volume', 'get'):
            return {'id': 'boot-id', 'size-in-gbs': 50, 'vpus-per-gb': 10}
        if command == ('compute', 'instance', 'list-vnics'):
            return [{'is-primary': True, 'public-ip': '192.0.2.2', 'subnet-id': 'subnet', 'nsg-ids': ['firewall-id']}]
        pytest.fail('Unexpected OCI command')
    monkeypatch.setattr(adapter, '_call', call)
    operation = adapter.state.begin_operation('create', 'hash')
    assert adapter.converge('ssh-ed25519 public', operation)['ip'] == '192.0.2.2'
    assert mutations == ['firewall', 'rules', 'compute']
    mutations.clear()
    operation = adapter.state.begin_operation('converge', 'hash')
    assert adapter.converge('ssh-ed25519 public', operation)['ip'] == '192.0.2.2'
    assert mutations == []
    assert all(action['action'] == 'retain' for action in adapter.plan())


def test_oci_cli_successful_empty_list_output(adapter, monkeypatch):
    monkeypatch.setattr('pocketdeploy.oci.run', lambda *a, **kw: '')
    assert adapter._list('network', 'nsg', 'rules', 'list', '--nsg-id', 'synthetic', '--all') == []
    assert adapter._list('compute', 'instance', 'list', '--all') == []
    assert adapter._call('network', 'nsg', 'delete', '--nsg-id', 'synthetic') is None


def test_json_null_list_still_rejected(adapter, monkeypatch):
    monkeypatch.setattr('pocketdeploy.oci.run', lambda *a, **kw: '{"data":null}')
    with pytest.raises(DeployError, match='invalid resource list'):
        adapter._list('network', 'nsg', 'rules', 'list', '--all')


def test_retained_boot_volume_survives_delete_and_recreate(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter.config['compute-retain-boot-volume'] = True
    adapter._remember('compute', resource(adapter))
    cloud = {'instances': [resource(adapter)], 'volume': {'id': 'original-boot', 'size-in-gbs': 50, 'vpus-per-gb': 10}}
    def call(*args, **kwargs):
        if args[:3] == ('compute', 'instance', 'list'):
            return cloud['instances']
        if args[:3] == ('network', 'nsg', 'list'):
            return []
        if args[:3] == ('compute', 'instance', 'terminate'):
            assert args[args.index('--preserve-boot-volume') + 1] == 'true'
            cloud['instances'] = []
            return None
        pytest.fail('Unexpected OCI command')
    monkeypatch.setattr(adapter, '_call', call)
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: cloud['volume'])
    operation = adapter.state.begin_operation('delete', 'hash')
    adapter.delete(operation)
    retained = adapter.state.get_resource('retained-boot-volume:original-boot')
    assert retained['attributes']['lifecycle'] == 'retained-for-recovery'
    assert adapter.state.get_resource('boot-volume') is None
    # Simulate the next create binding a new instance and its new boot volume.
    cloud['instances'] = [resource(adapter, id='new-instance')]
    cloud['volume'] = {'id': 'new-boot', 'size-in-gbs': 50, 'vpus-per-gb': 10}
    adapter._remember('compute', cloud['instances'][0])
    adapter._boot_volume(cloud['instances'][0])
    assert adapter.state.get_resource('boot-volume')['provider_id'] == 'new-boot'
    assert adapter.state.get_resource('retained-boot-volume:original-boot')['provider_id'] == 'original-boot'


def test_legacy_retained_binding_archived_before_replacement(adapter, monkeypatch):
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'old-boot', {'instance_id': 'old-instance'})
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {'id': 'new-boot', 'size-in-gbs': 50, 'vpus-per-gb': 10})
    adapter._boot_volume(resource(adapter))
    assert adapter.state.get_resource('retained-boot-volume:old-boot')['attributes']['lifecycle'] == 'retained-for-recovery'
    assert adapter.state.get_resource('boot-volume')['provider_id'] == 'new-boot'


def test_interrupted_retaining_delete_archives_disk(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'retained-boot', {'instance_id': 'compute-id'})
    operation = adapter.state.begin_operation('delete', 'hash')
    step = adapter.state.intent(operation, 'delete-compute', {})
    adapter.state.set_meta('oci-delete-compute', {'step': step, 'id': 'compute-id', 'retain_boot': True})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [])
    adapter.delete(operation)
    assert adapter.state.get_resource('boot-volume') is None
    assert adapter.state.get_resource('retained-boot-volume:retained-boot')['provider_id'] == 'retained-boot'
