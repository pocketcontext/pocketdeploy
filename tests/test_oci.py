import json
import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.oci import OCI
from pocketdeploy.state import State


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.delenv("OCI_CLI_SECURITY_TOKEN_FILE", raising=False)
    monkeypatch.setenv("OCI_CLI_CONFIG_FILE", str(tmp_path / "absent-config"))
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
            return {'id': 'boot-id', 'size-in-gbs': 50, 'vpus-per-gb': 10, 'freeform-tags': adapter._tags('boot-volume')}
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
    assert adapter._list('bv', 'boot-volume', 'list', '--all') == []
    assert adapter._call('network', 'nsg', 'delete', '--nsg-id', 'synthetic') is None


def test_json_null_list_still_rejected(adapter, monkeypatch):
    monkeypatch.setattr('pocketdeploy.oci.run', lambda *a, **kw: '{"data":null}')
    with pytest.raises(DeployError, match='invalid resource list'):
        adapter._list('network', 'nsg', 'rules', 'list', '--all')


def test_legacy_retained_binding_archived_before_replacement(adapter, monkeypatch):
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'old-boot', {'instance_id': 'old-instance'})
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {'id': 'new-boot', 'size-in-gbs': 50, 'vpus-per-gb': 10, 'freeform-tags': adapter._tags('boot-volume')})
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
    assert adapter.state.get_resource('retained-boot-volume:retained-boot') is None


def test_delete_plan_ignores_provisioning_drift(adapter, monkeypatch):
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'boot', {'instance_id': 'compute-id'})
    monkeypatch.setattr(adapter, '_volumes_for_delete', lambda: [])
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {'id': 'boot'})
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter) if role == 'compute' else None)
    monkeypatch.setattr(adapter, '_subnet', lambda: pytest.fail('provisioning check'))
    monkeypatch.setattr(adapter, '_drift', lambda *a: pytest.fail('provisioning drift'))
    before = adapter.state.db.total_changes
    actions = adapter.plan_delete()
    assert actions[0]['id'] == 'compute-id'
    assert actions[0]['state'] == 'RUNNING'
    assert adapter.state.db.total_changes == before


def test_delete_validates_firewall_before_terminating(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {'id': 'boot'})
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter, role))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('mutation before firewall ownership'))
    with pytest.raises(DeployError, match='recorded ownership'):
        adapter.delete('unused')


def test_delete_keeps_state_until_cloud_absence_verified(adapter, monkeypatch):
    monkeypatch.setattr('pocketdeploy.oci.time.sleep', lambda seconds: None)
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('firewall', resource(adapter, 'firewall'))
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter, role) if role == 'firewall' else None)
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: None)
    op = adapter.state.begin_operation('delete', 'test')
    with pytest.raises(DeployError, match='not complete'):
        adapter.delete(op)
    assert adapter.state.get_resource('firewall')
    assert adapter.state.get_meta('oci-delete-firewall')


def test_delete_plan_retires_previously_retained_disk(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_volumes_for_delete', lambda: [(adapter.state.get_resource('boot-volume'), {'id': 'boot'})])
    adapter.config['compute-retain-boot-volume'] = False
    adapter._remember('compute', resource(adapter))
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'boot', {})
    op = adapter.state.begin_operation('delete', 'test')
    step = adapter.state.intent(op, 'delete-compute', {'id': 'compute-id'})
    adapter.state.set_meta('oci-delete-compute', {'id': 'compute-id', 'step': step, 'retain_boot': True})
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: None)
    assert next(a for a in adapter.plan_delete() if a['resource'] == 'boot-volume')['action'] == 'delete'


@pytest.mark.parametrize('recorded_id,instance_id,owned', [('other', 'compute-id', True), ('boot', 'other', True), ('boot', 'compute-id', False)])
def test_delete_plan_refuses_boot_ownership_drift_without_writes(adapter, monkeypatch, recorded_id, instance_id, owned):
    adapter._remember('compute', resource(adapter))
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', recorded_id, {'instance_id': instance_id}, owned=owned)
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter) if role == 'compute' else None)
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda instance: {'id': 'boot'})
    before = adapter.state.db.total_changes
    with pytest.raises(DeployError, match='boot volume differs'):
        adapter.plan_delete()
    assert adapter.state.db.total_changes == before
    assert adapter.state.get_resource('boot-volume')['provider_id'] == recorded_id


def test_delete_preflight_boot_permission_failure_prevents_mutation(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter) if role == 'compute' else None)
    def denied(instance):
        raise DeployError('Boot volume permission denied.')
    monkeypatch.setattr(adapter, '_read_boot_volume', denied)
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('cloud mutation'))
    before = adapter.state.db.total_changes
    with pytest.raises(DeployError, match='permission denied'):
        adapter.delete('unused')
    assert adapter.state.db.total_changes == before


def test_delete_cannot_destroy_unrecorded_boot_volume(adapter, monkeypatch):
    adapter.config['compute-retain-boot-volume'] = False
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter) if role == 'compute' else None)
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda instance: {'id': 'boot'})
    with pytest.raises(DeployError, match='requires recorded ownership'):
        adapter.plan_delete()
    assert adapter.state.get_resource('boot-volume') is None


def test_delete_rechecks_boot_identity_immediately_before_termination(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_volumes_for_delete', lambda: [])
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    adapter.state.put_resource('boot-volume', 'oci-boot-volume', 'boot', {'instance_id': 'compute-id'})
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter) if role == 'compute' else None)
    volumes = iter([{'id': 'boot'}, {'id': 'different'}])
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda instance: next(volumes))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('cloud mutation'))
    op = adapter.state.begin_operation('delete', 'test')
    with pytest.raises(DeployError, match='boot volume differs'):
        adapter.delete(op)
    assert adapter.state.get_resource('boot-volume')['provider_id'] == 'boot'


def volume_cloud(adapter, monkeypatch, *, legacy=False, foreign=False):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('compute', resource(adapter))
    volumes = {name: {'id': name, 'lifecycle-state': 'AVAILABLE',
                     'freeform-tags': {} if legacy else adapter._tags('boot-volume')}
               for name in ('current', 'historical')}
    for name, instance in [('current', 'compute-id'), ('historical', 'old-instance')]:
        adapter.state.put_resource('boot-volume' if name == 'current' else 'retained-boot-volume:' + name,
                                   'oci-boot-volume', name, {'instance_id': instance})
    cloud = {'instance': resource(adapter), 'volumes': volumes, 'calls': []}
    def call(*args, body=None, **kw):
        command = args[:3]
        cloud['calls'].append(command)
        if command == ('compute', 'instance', 'list'):
            return [cloud['instance']] if cloud['instance'] else []
        if command == ('network', 'nsg', 'list'):
            return []
        if command == ('bv', 'boot-volume', 'list'):
            return list(volumes.values())
        if command == ('compute', 'boot-volume-attachment', 'list'):
            return [{'boot-volume-id': name, 'instance-id': 'foreign' if foreign else instance,
                     'lifecycle-state': 'ATTACHED' if foreign or (name == 'current' and cloud['instance']) else 'DETACHED'}
                    for name, instance in [('current', 'compute-id'), ('historical', 'old-instance')]]
        if command == ('bv', 'boot-volume', 'update'):
            volumes[args[4]]['freeform-tags'] = body['freeformTags']
            return volumes[args[4]]
        if command == ('bv', 'boot-volume', 'get'):
            return volumes[args[4]]
        if command == ('compute', 'instance', 'terminate'):
            assert args[args.index('--preserve-boot-volume') + 1] == 'true'
            cloud['instance'] = None
            return None
        if command == ('bv', 'boot-volume', 'delete'):
            del volumes[args[4]]
            return None
        pytest.fail('Unexpected synthetic OCI request')
    monkeypatch.setattr(adapter, '_call', call)
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda instance: volumes['current'])
    return cloud


@pytest.mark.parametrize('legacy', [False, True])
def test_all_owned_volumes_deleted_and_retries_noop(adapter, monkeypatch, legacy):
    cloud = volume_cloud(adapter, monkeypatch, legacy=legacy)
    operation = adapter.state.begin_operation('delete', 'test')
    plan = adapter.plan_delete()
    assert len([a for a in plan if 'volume' in a['resource'] and a['action'] == 'delete']) == 2
    assert adapter.delete(operation) == {'deleted': True}
    assert cloud['volumes'] == {}
    assert adapter.state.resources() == []
    assert adapter.state.safe_status()['pending_steps'] == 0
    assert adapter.delete(operation) == {'deleted': True}
    if legacy:
        assert cloud['calls'].index(('bv', 'boot-volume', 'update')) < cloud['calls'].index(('compute', 'instance', 'terminate'))


def test_volume_foreign_attachment_blocks_every_mutation(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch, foreign=True)
    with pytest.raises(DeployError, match='another instance'):
        adapter.delete('unused')
    assert not any(c[-1] in ('delete', 'update', 'terminate') for c in cloud['calls'])


def test_legacy_volume_requires_live_attachment_evidence(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch, legacy=True)
    original = adapter._call
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [] if a[:3] == ('compute', 'boot-volume-attachment', 'list') else original(*a, **kw))
    with pytest.raises(DeployError, match='attachment evidence'):
        adapter.delete('unused')
    assert cloud['instance']


def test_failed_volume_delete_preserves_intent_for_retry(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch)
    original = adapter._call
    def uncertain(*a, **kw):
        result = original(*a, **kw)
        if a[:3] == ('bv', 'boot-volume', 'delete'):
            raise DeployError('Synthetic connection lost after deletion')
        return result
    monkeypatch.setattr(adapter, '_call', uncertain)
    operation = adapter.state.begin_operation('delete', 'test')
    with pytest.raises(DeployError, match='connection lost'):
        adapter.delete(operation)
    assert adapter.state.get_meta('oci-delete-volume:current')
    monkeypatch.setattr(adapter, '_call', original)
    adapter.delete(operation)
    assert not adapter.state.resources()
    assert adapter.state.safe_status()['pending_steps'] == 0


def test_inherited_compute_tags_migrate_only_with_attachment_evidence(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch)
    for volume in cloud['volumes'].values():
        volume['freeform-tags'] = adapter._tags('compute')
    op = adapter.state.begin_operation('delete', 'test')
    adapter.delete(op)
    assert cloud['volumes'] == {}
    assert ('bv', 'boot-volume', 'update') in cloud['calls']


def test_inherited_tags_without_recorded_attachment_evidence_block(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch)
    for volume in cloud['volumes'].values():
        volume['freeform-tags'] = adapter._tags('compute')
    original = adapter._call
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: [] if a[:3] == ('compute', 'boot-volume-attachment', 'list') else original(*a, **kw))
    with pytest.raises(DeployError, match='attachment evidence'):
        adapter.delete('unused')
    assert not any(c[-1] in ('update', 'terminate', 'delete') for c in cloud['calls'])


def test_inherited_compute_tags_of_other_deployment_are_not_migrated(adapter, monkeypatch):
    cloud = volume_cloud(adapter, monkeypatch)
    cloud['volumes']['historical']['freeform-tags'] = {'pocketdeploy-id': 'someone-else', 'pocketdeploy-role': 'compute'}
    with pytest.raises(DeployError, match='tags conflict'):
        adapter.delete('unused')
    assert not any(c[-1] in ('update', 'terminate', 'delete') for c in cloud['calls'])


def test_new_disk_inherited_compute_tags_are_replaced_after_owned_attachment(adapter, monkeypatch):
    volume = {'id': 'new', 'freeform-tags': adapter._tags('compute')}
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda instance: volume)
    def call(*args, body=None, **kw):
        assert args[:3] == ('bv', 'boot-volume', 'update')
        volume['freeform-tags'] = body['freeformTags']
        return volume
    monkeypatch.setattr(adapter, '_call', call)
    adapter._boot_volume(resource(adapter))
    assert adapter._owned(volume, 'boot-volume')
    assert adapter.state.get_resource('boot-volume')['provider_id'] == 'new'


def test_firewall_delete_polls_eventual_absence_without_repeating_mutation(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('firewall', resource(adapter, 'firewall'))
    observed = {'deleted': False, 'polls': 0, 'mutations': 0}
    sleeps = []
    def find(role, **kw):
        if role != 'firewall':
            return None
        if observed['deleted']:
            observed['polls'] += 1
            if observed['polls'] >= 3:
                return None
        return resource(adapter, 'firewall')
    def call(*args, **kw):
        assert args[:3] == ('network', 'nsg', 'delete')
        observed['deleted'] = True
        observed['mutations'] += 1
    monkeypatch.setattr(adapter, '_find', find)
    monkeypatch.setattr(adapter, '_call', call)
    monkeypatch.setattr('pocketdeploy.oci.time.sleep', sleeps.append)
    operation = adapter.state.begin_operation('delete', 'test')
    adapter.delete(operation)
    assert observed['mutations'] == 1
    assert sleeps == [2, 2]
    assert adapter.state.get_resource('firewall') is None
    assert adapter.state.get_meta('oci-delete-firewall') is None
    assert adapter.state.safe_status()['pending_steps'] == 0

@pytest.mark.parametrize('failure', ['add-before', 'add-after', 'remove-after', None])
def test_firewall_preserves_access_and_recovers_without_duplicate_add(adapter, monkeypatch, failure):
    old = dict(adapter._rules()[1], source='198.51.100.0/24', id='old')
    rules = [old]
    calls = []
    def call(*args, body=None, **kwargs):
        if args[3] == 'list':
            return list(rules)
        calls.append(args[3])
        if args[3] == 'add':
            if failure == 'add-before':
                raise DeployError('lost response')
            rules.extend(dict(rule, id=str(i)) for i, rule in enumerate(body['securityRules']))
            if failure == 'add-after':
                raise DeployError('lost response')
        else:
            assert {adapter._normalized(r) for r in adapter._rules()} <= {adapter._normalized(r) for r in rules}
            rules[:] = [r for r in rules if r['id'] not in body['securityRuleIds']]
            if failure == 'remove-after':
                raise DeployError('lost response')
    monkeypatch.setattr(adapter, '_call', call)
    operation = adapter.state.begin_operation('converge', 'hash')
    if failure:
        with pytest.raises(DeployError):
            adapter._reconcile_rules('firewall-id', operation)
        if failure == 'add-before':
            assert rules == [old]
            with pytest.raises(DeployError, match='uncertain'):
                adapter._reconcile_rules('firewall-id', operation)
            assert calls == ['add']
            return
    adapter._reconcile_rules('firewall-id', operation)
    assert calls == ['add', 'remove']
    assert adapter._rules_equal(rules)
    assert adapter.state.get_meta('oci-pending-firewall-rules') is None
    assert adapter.state.safe_status()['pending_steps'] == 0


def test_adopt_rejects_recorded_conflict_before_provider_calls(adapter, monkeypatch):
    adapter._remember('compute', resource(adapter))
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: pytest.fail('provider call'))
    with pytest.raises(DeployError, match='conflicts'):
        adapter.adopt('another-instance', 'unused')


def test_adopt_records_boot_volume_and_firewall(adapter, monkeypatch):
    item = resource(adapter, **{'compartment-id': 'compartment'})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: item)
    monkeypatch.setattr(adapter, '_find', lambda role: resource(adapter, role))
    monkeypatch.setattr(adapter, '_storage_drift', lambda item: [])
    monkeypatch.setattr(adapter, '_network_drift', lambda *args: [])
    monkeypatch.setattr(adapter, '_read_boot_volume', lambda item: {
        'id': 'boot', 'freeform-tags': adapter._tags('boot-volume')})
    monkeypatch.setattr(adapter, 'connection', lambda: {'ip': '192.0.2.1'})
    operation = adapter.state.begin_operation('adopt', 'hash')
    adapter.adopt('compute-id', operation)
    assert adapter.state.get_resource('compute')['provider_id'] == 'compute-id'
    assert adapter.state.get_resource('firewall')['provider_id'] == 'firewall-id'
    assert adapter.state.get_resource('boot-volume')['provider_id'] == 'boot'


def test_null_creation_preserves_pending_intent(adapter):
    operation = adapter.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='invalid creation'):
        adapter._create('compute', operation, lambda: None)
    assert adapter.state.get_meta('oci-pending-compute')


def test_null_adoption_is_safe_error(adapter, monkeypatch):
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: None)
    with pytest.raises(DeployError, match='invalid instance'):
        adapter.adopt('compute-id', 'unused')


def test_adopt_reuses_pending_step_after_volume_failure(adapter, monkeypatch):
    item = resource(adapter, **{'compartment-id': 'compartment'})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: item)
    monkeypatch.setattr(adapter, '_find', lambda role: resource(adapter, role))
    monkeypatch.setattr(adapter, '_storage_drift', lambda item: [])
    monkeypatch.setattr(adapter, '_network_drift', lambda *args: [])
    attempts = []
    def boot(item):
        attempts.append(item['id'])
        if len(attempts) == 1:
            raise DeployError('volume tagging response lost')
    monkeypatch.setattr(adapter, '_boot_volume', boot)
    monkeypatch.setattr(adapter, 'connection', lambda: {})
    operation = adapter.state.begin_operation('adopt', 'hash')
    with pytest.raises(DeployError, match='lost'):
        adapter.adopt('compute-id', operation)
    assert adapter.state.get_resource('compute') is None
    assert adapter.state.safe_status()['pending_steps'] == 1
    adapter.adopt('compute-id', adapter.state.begin_operation('adopt', 'hash'))
    assert adapter.state.safe_status()['pending_steps'] == 0
    assert adapter.state.get_meta('oci-pending-adopt') is None


@pytest.mark.parametrize('desired_present', [True, False])
def test_legacy_pending_firewall_step_requires_observed_resolution(adapter, monkeypatch, desired_present):
    operation = adapter.state.begin_operation('converge', 'hash')
    adapter.state.intent(operation, 'firewall-rules', {'id': 'firewall-id'})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: adapter._rules() if desired_present else [])
    if desired_present:
        adapter._reconcile_rules('firewall-id', operation)
        assert adapter.state.safe_status()['pending_steps'] == 0
    else:
        with pytest.raises(DeployError, match='legacy.*uncertain'):
            adapter._reconcile_rules('firewall-id', operation)
        assert adapter.state.safe_status()['pending_steps'] == 1


def test_verified_historical_firewall_deletion_resolves_only_matching_rules(adapter):
    operation = adapter.state.begin_operation('converge', 'hash')
    adapter.state.intent(operation, 'firewall-rules', {'id': 'deleted-firewall'})
    adapter.state.intent(operation, 'firewall-rules', {'id': 'unknown-firewall'})
    deletion = adapter.state.intent(operation, 'delete-firewall', {'id': 'deleted-firewall'})
    adapter.state.complete(deletion, {'deleted': True})
    adapter.state.set_meta('oci-pending-firewall-rules', {'id': 'deleted-firewall'})
    adapter._recover_deleted_rule_steps()
    assert adapter.state.safe_status()['pending_steps'] == 1
    assert adapter._pending_rule_steps('unknown-firewall')
    assert adapter.state.get_meta('oci-pending-firewall-rules') is None


def test_delete_clears_pending_firewall_update_after_verified_absence(adapter, monkeypatch):
    adapter.config['compute-prevent-destroy'] = False
    adapter._remember('firewall', resource(adapter, 'firewall'))
    operation = adapter.state.begin_operation('converge', 'hash')
    step = adapter.state.intent(operation, 'firewall-rules', {'id': 'firewall-id'})
    adapter.state.set_meta('oci-pending-firewall-rules', {'id': 'firewall-id', 'step': step})
    monkeypatch.setattr(adapter, 'plan_delete', lambda: [])
    monkeypatch.setattr(adapter, '_volumes_for_delete', lambda: [])
    deleted = []
    monkeypatch.setattr(adapter, '_find', lambda role, **kw: resource(adapter, 'firewall') if role == 'firewall' and not deleted else None)
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: deleted.append(True))
    adapter.delete(operation)
    assert adapter.state.get_meta('oci-pending-firewall-rules') is None
    assert adapter.state.safe_status()['pending_steps'] == 0


def test_adopt_missing_firewall_refused_even_during_provisioning(adapter, monkeypatch):
    item = resource(adapter, **{'compartment-id': 'compartment', 'lifecycle-state': 'PROVISIONING'})
    monkeypatch.setattr(adapter, '_call', lambda *a, **kw: item)
    monkeypatch.setattr(adapter, '_find', lambda role: None)
    with pytest.raises(DeployError, match='running compute and its owned firewall'):
        adapter.adopt('compute-id', 'unused')
    assert adapter.state.resources() == []
