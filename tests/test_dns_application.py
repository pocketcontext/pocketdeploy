import json
import os

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.dns import CloudflareDNS
from pocketdeploy.state import State


@pytest.fixture
def dns(tmp_path, monkeypatch):
    monkeypatch.setenv('COLORS_PAR_CLOUDFLARE_API_TOKEN', 'synthetic-token')
    state = State(tmp_path / 'state', 'test', {}, create=True)
    service = CloudflareDNS({'profile': 'test'}, state, ['stage.example.com'])
    records, calls = [], []
    def call(tool, args, **kwargs):
        calls.append(args)
        assert kwargs['env']['CLOUDFLARE_API_TOKEN'] == 'synthetic-token'
        if args[:2] == ['zones', 'list']:
            return [{'id': 'zone', 'name': 'example.com'}]
        if args[:2] == ['zones', 'get']:
            return {'id': 'zone', 'name': 'example.com'}
        if args[2] == 'list':
            return list(records)
        if args[2] == 'delete':
            records[:] = [r for r in records if r['id'] != args[3]]
            return {}
        body = json.loads(args[args.index('--body') + 1])
        if args[2] == 'edit':
            record = next(r for r in records if r['id'] == args[3])
            record.update(body)
        else:
            record = {'id': 'record', **body}
            records.append(record)
        return dict(record)
    monkeypatch.setattr('pocketdeploy.dns._call', call)
    yield service, records, calls
    state.db.close()


def test_independent_dns_lifecycle(dns):
    service, records, calls = dns
    before = dict(os.environ)
    assert service.plan()['actions'][0]['action'] == 'resolve-after-compute'
    op = service.state.begin_operation('converge', 'hash')
    connection = {'ip': '192.0.2.1'}
    assert service.converge(connection, op)['actions'][0]['action'] == 'create'
    assert service.converge(connection, op)['actions'][0]['action'] == 'noop'
    assert service.plan_delete()[0]['action'] == 'delete'
    assert service.delete(op)['actions'][0]['action'] == 'delete'
    assert not records
    assert dict(os.environ) == before
    assert 'once' not in service.c


def test_refuse_unowned_and_identity_change(dns):
    service, records, _ = dns
    records.append({'id': 'old', 'type': 'A', 'name': 'stage.example.com', 'content': '192.0.2.1'})
    with pytest.raises(DeployError, match='outside'):
        service.plan({'ip': '192.0.2.1'})
    records[0]['comment'] = service.marker
    service.state.put_resource('dns:A:stage.example.com', 'cloudflare-dns', 'different',
                               {'zone': 'zone', 'name': 'stage.example.com', 'type': 'A'})
    with pytest.raises(DeployError, match='outside'):
        service.plan({'ip': '192.0.2.1'})


def test_zone_ambiguity_and_pagination(dns, monkeypatch):
    service, _, _ = dns
    pages = []
    def call(args):
        pages.append(args)
        if '--page' in args and args[args.index('--page') + 1] == '1':
            return [{'id': str(i), 'name': f'z{i}.invalid'} for i in range(50)]
        return [{'id': 'zone', 'name': 'example.com'}]
    monkeypatch.setattr(service, '_call', call)
    service._zone()
    assert len(pages) == 2
    assert service.c['cloudflare-zone-id'] == 'zone'
    service.c.pop('cloudflare-zone-id')
    monkeypatch.setattr(service, '_call', lambda args: [
        {'id': 'parent', 'name': 'example.com'}, {'id': 'child', 'name': 'stage.example.com'}])
    with pytest.raises(DeployError, match='ambiguous'):
        service._zone()


def test_adopt_old_origin_preserves_settings_then_converges(dns):
    service, records, _ = dns
    records.append({'id': 'old', 'name': 'stage.example.com', 'type': 'A', 'content': '192.0.2.2',
                    'proxied': False, 'ttl': 300, 'comment': 'old-manager'})
    service.state.put_resource('compute', 'hcloud-server', 'server', {})
    target = {'instance_id': 'server', 'ip': '192.0.2.3'}
    evidence = service.evidence('stage.example.com')
    op = service.state.begin_operation('adopt', 'hash')
    with pytest.raises(DeployError, match='Disable'):
        service.adopt(target, op, evidence)
    evidence['previous_dns_manager_disabled'] = True
    result = service.adopt(target, op, evidence)
    assert result['record_id'] == 'old'
    assert records[0]['content'] == '192.0.2.2'
    assert records[0]['ttl'] == 300 and records[0]['proxied'] is False
    assert service.adopt(target, op, evidence)['adopted']
    service.converge(target, op)
    assert records[0]['id'] == 'old' and records[0]['content'] == '192.0.2.3'


def test_pending_and_deletion_are_scoped(dns):
    service, _, _ = dns
    service.state.put_resource('dns:A:other.example.com', 'cloudflare-dns', 'other',
                               {'zone': 'zone', 'type': 'A', 'name': 'other.example.com'})
    assert service.plan_delete() == []
    service.state.set_meta('pending:dns:A:stage.example.com', True)
    with pytest.raises(DeployError, match='reconciliation'):
        service.plan_delete()
    op = service.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='uncertain'):
        service.converge({'ip': '192.0.2.1'}, op)


def test_adoption_lost_response_reconciles_without_changing_address(dns, monkeypatch):
    service, records, _ = dns
    records.append({'id': 'old', 'name': 'stage.example.com', 'type': 'A', 'content': '192.0.2.2',
                    'proxied': False, 'ttl': 300, 'comment': 'old-manager'})
    service.state.put_resource('compute', 'hcloud-server', 'server', {})
    connection = {'instance_id': 'server', 'ip': '192.0.2.3'}
    evidence = {**service.evidence('stage.example.com'), 'previous_dns_manager_disabled': True}
    op = service.state.begin_operation('adopt', 'hash')
    original = service._cf
    def lose_response(args):
        result = original(args)
        if args[0] == 'edit':
            raise DeployError('Synthetic lost response')
        return result
    monkeypatch.setattr(service, '_cf', lose_response)
    with pytest.raises(DeployError, match='Synthetic'):
        service.adopt(connection, op, evidence)
    with pytest.raises(DeployError, match='Resume'):
        service.converge(connection, op)
    assert service.adopt(connection, op, evidence)['adopted']
    assert records[0]['content'] == '192.0.2.2'


def test_cloudflare_child_environment_reaches_runner(monkeypatch):
    from pocketdeploy.services import _call
    seen = []
    monkeypatch.setattr('pocketdeploy.services.run', lambda args, **kwargs: seen.append(kwargs) or '[]')
    env = {'CLOUDFLARE_API_TOKEN': 'synthetic'}
    assert _call('cf', ['zones', 'list'], env=env) == []
    assert seen[0]['env'] == env
