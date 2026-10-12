import copy
import json
import pytest
from pocketdeploy.common import DeployError
from pocketdeploy.hcloud import HCloud
from pocketdeploy.state import State


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    config = {'profile': 'test', 'hcloud-server-type': 'cx23', 'hcloud-location': 'nbg1',
              'hcloud-image': 'ubuntu-24.04', 'compute-prevent-destroy': False,
              'compute-http-sources': ['0.0.0.0/0'], 'compute-ssh-sources': ['192.0.2.1/32'],
              'compute-ingress': [{'protocol': 'udp', 'ports': ['443'], 'sources': ['0.0.0.0/0']}]}
    with State(tmp_path / 'state.sqlite', 'test', {}, create=True) as state:
        a = HCloud(config, state)
        monkeypatch.setattr('pocketdeploy.hcloud.time.sleep', lambda _: None)
        yield a


def fake(a, monkeypatch):
    data = {'primary-ip': [], 'firewall': [], 'server': []}
    calls = []
    def call(args, payload=None, *, json_output=True):
        calls.append((args, payload))
        kind, verb = args[:2]
        if kind == 'image':
            return {'id': 1234, 'status': 'available', 'os_flavor': 'ubuntu', 'os_version': '24.04'}
        if verb == 'list':
            return copy.deepcopy(data[kind])
        if verb == 'create':
            role = next(r for r, k in a.roles.items() if k == kind)
            item = {'id': len(calls), 'name': args[args.index('--name') + 1], 'labels': a._labels(role)}
            if kind == 'primary-ip':
                item.update(type='ipv4', auto_delete=False, assignee_id=None, protection={'delete': False})
            if kind == 'firewall':
                item.update(rules=json.loads(payload), applied_to=[])
            if kind == 'server':
                item.update(status='running', protection={'delete': True, 'rebuild': True}, server_type={'name': 'cx23'}, location={'name': 'nbg1'},
                            image={'id': 1234}, private_net=[], volumes=[], backup_window=None,
                            public_net={'ipv4': {'id': data['primary-ip'][0]['id'], 'ip': '192.0.2.10'},
                                        'ipv6': None, 'firewalls': [{'id': data['firewall'][0]['id']}]})
                data['primary-ip'][0]['assignee_id'] = item['id']
                data['firewall'][0]['applied_to'] = [{'type': 'server', 'server': {'id': item['id']}}]
            data[kind].append(item)
            return item
        if verb == 'replace-rules':
            data[kind][0]['rules'] = json.loads(payload)
        elif verb == 'delete':
            data[kind].clear()
            if kind == 'server':
                data['primary-ip'][0]['assignee_id'] = None
                data['firewall'][0]['applied_to'] = []
        elif verb == 'enable-protection':
            data['server'][0]['protection'] = {'delete': True, 'rebuild': True}
        elif verb != 'disable-protection':
            raise AssertionError(args)
    monkeypatch.setattr(a, '_call', call)
    return data, calls


def converge(a):
    operation = a.state.begin_operation('converge', 'hash')
    return operation, a.converge('ssh-ed25519 synthetic', operation)


def test_lifecycle_noop_bootstrap_and_cleanup(adapter, monkeypatch):
    data, calls = fake(adapter, monkeypatch)
    operation, connection = converge(adapter)
    assert connection['ip'] == '192.0.2.10'
    args, payload = next(c for c in calls if c[0][:2] == ['server', 'create'])
    assert args[args.index('--name') + 1] == 'test-server'
    assert '--without-ipv6' in args and '--enable-protection' in args
    assert json.loads(payload.split('\n', 1)[1])['users'][0]['ssh_authorized_keys'] == ['ssh-ed25519 synthetic']
    assert data['primary-ip'][0]['name'] == 'test-ipv4'
    assert any(r['protocol'] == 'udp' and r['port'] == '443' for r in data['firewall'][0]['rules'])
    calls.clear()
    adapter.converge('ssh-ed25519 synthetic', operation)
    assert all(args[1] in ('list', 'describe') for args, _ in calls)
    adapter.delete(operation)
    assert not any(data.values()) and not adapter.state.resources()


@pytest.mark.parametrize('role', HCloud.roles)
def test_lost_create_response_recovers_one_owned_resource(adapter, monkeypatch, role):
    data, calls = fake(adapter, monkeypatch)
    real = adapter._call
    failed = False
    def lost(args, *a, **kw):
        nonlocal failed
        result = real(args, *a, **kw)
        if args[:2] == [adapter.roles[role], 'create'] and not failed:
            failed = True
            raise DeployError('synthetic lost response')
        return result
    monkeypatch.setattr(adapter, '_call', lost)
    with pytest.raises(DeployError, match='lost response'):
        converge(adapter)
    converge(adapter)
    assert len([args for args, _ in calls if args[:2] == [adapter.roles[role], 'create']]) == 1


def test_uncertain_absent_create_does_not_repeat(adapter, monkeypatch):
    _, calls = fake(adapter, monkeypatch)
    operation = adapter.state.begin_operation('converge', 'hash')
    adapter.state.set_meta('hcloud-pending-primary-ip', {'step': adapter.state.intent(operation, 'create', {})})
    with pytest.raises(DeployError):
        adapter.converge('key', operation)
    assert not any(args[1] == 'create' for args, _ in calls)


@pytest.mark.parametrize('change', ['labels', 'id', 'server_type', 'image', 'private_net', 'ipv6', 'firewall', 'ip'])
def test_ownership_and_immutable_drift_fail_before_mutation(adapter, monkeypatch, change):
    data, calls = fake(adapter, monkeypatch)
    operation, _ = converge(adapter)
    server = data['server'][0]
    if change == 'labels':
        server['labels'] = {}
    elif change == 'id':
        server['id'] += 100
    elif change == 'server_type':
        server['server_type']['name'] = 'cx33'
    elif change == 'image':
        server['image']['id'] = 5678
    elif change == 'private_net':
        server['private_net'] = [{'network': 55}]
    elif change == 'ipv6':
        server['public_net']['ipv6'] = {'id': 88}
    elif change == 'firewall':
        server['public_net']['firewalls'].append({'id': 99})
    else:
        data['primary-ip'][0]['assignee_id'] = 999
    calls.clear()
    with pytest.raises(DeployError):
        adapter.converge('key', operation)
    assert all(args[1] in ('list', 'describe') for args, _ in calls)


def test_name_collision_is_not_adopted(adapter, monkeypatch):
    data, _ = fake(adapter, monkeypatch)
    data['server'].append({'id': 1, 'name': 'test-server', 'labels': {}})
    assert adapter._find('compute') is None


def test_unrecorded_uuid_is_not_adopted(adapter, monkeypatch):
    data, _ = fake(adapter, monkeypatch)
    data['server'].append({'id': 1, 'labels': adapter._labels('compute')})
    with pytest.raises(DeployError, match='checkpoint recovery'):
        adapter._find('compute')


def test_firewall_external_attachment_blocks_delete(adapter, monkeypatch):
    data, _ = fake(adapter, monkeypatch)
    operation, _ = converge(adapter)
    data['firewall'][0]['applied_to'].append({'type': 'server', 'server': {'id': 999}})
    with pytest.raises(DeployError, match='external attachments'):
        adapter.delete(operation)


def test_firewall_update_recovers_lost_response(adapter, monkeypatch):
    data, calls = fake(adapter, monkeypatch)
    operation, _ = converge(adapter)
    adapter.config['compute-http-sources'] = []
    adapter.config['compute-ingress'] = []
    real = adapter._call
    def lost(args, *a, **kw):
        result = real(args, *a, **kw)
        if args[:2] == ['firewall', 'replace-rules']:
            raise DeployError('lost response')
        return result
    monkeypatch.setattr(adapter, '_call', lost)
    with pytest.raises(DeployError):
        adapter.converge('key', operation)
    adapter.converge('key', operation)
    assert len(data['firewall'][0]['rules']) == 1
    assert len([a for a, _ in calls if a[1] == 'replace-rules']) == 1


def test_protected_delete(adapter, monkeypatch):
    data, calls = fake(adapter, monkeypatch)
    operation, _ = converge(adapter)
    adapter.config['compute-prevent-destroy'] = True
    calls.clear()
    with pytest.raises(DeployError, match='protected'):
        adapter.delete(operation)
    assert not calls


def test_cli_token_only_environment_and_safe_error(adapter, monkeypatch):
    monkeypatch.setenv('COLORS_PAR_HCLOUD_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('HCLOUD_DEBUG', 'true')
    monkeypatch.setenv('HCLOUD_ENDPOINT', 'https://untrusted.invalid')
    seen = {}
    def run(args, **kw):
        seen.update(args=args, **kw)
        return '[]'
    monkeypatch.setattr('pocketdeploy.hcloud.run', run)
    assert adapter._call(['server', 'list']) == []
    assert seen['env']['HCLOUD_TOKEN'] == 'synthetic-secret'
    assert 'HCLOUD_DEBUG' not in seen['env'] and 'HCLOUD_ENDPOINT' not in seen['env']
    assert 'synthetic-secret' not in str(seen['args'])


def test_protection_recovers_partial_server_creation(adapter, monkeypatch):
    data, calls = fake(adapter, monkeypatch)
    operation, _ = converge(adapter)
    data['server'][0]['protection'] = {'delete': False, 'rebuild': False}
    adapter.converge('key', operation)
    assert data['server'][0]['protection'] == {'delete': True, 'rebuild': True}
