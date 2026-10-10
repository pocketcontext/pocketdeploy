import copy
import json

import pytest

from pocketdeploy.common import DeployError
from pocketdeploy.dns_adoption import adopt, fingerprint
from pocketdeploy.services import Services
from pocketdeploy.state import State
from pocketdeploy.cli import execute, parser


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setattr('pocketdeploy.dns_adoption._call', lambda *a: {'id': 'a' * 32, 'name': 'example.com'})
    state = State(tmp_path / 'state', 'test', 'test', create=True)
    state.put_resource('compute', 'oci', 'instance', {})
    config = {'provider-dns': 'cloudflare', 'cloudflare-zone-id': 'a' * 32,
              'once': {'applications': [{'host': 'demo.example.com'}]}}
    record = {'id': 'b' * 32, 'type': 'A', 'name': 'demo.example.com',
              'content': '192.0.2.1', 'proxied': False, 'ttl': 300, 'comment': 'previous-owner'}
    evidence = {'host': record['name'], 'zone': 'a' * 32, 'record_id': record['id'],
                'ipv4': record['content'], 'settings_sha256': fingerprint(record),
                'previous_dns_manager_disabled': True}
    calls = []
    monkeypatch.setattr(Services, '_records', lambda self, name: [copy.deepcopy(record)])
    def patch(self, args):
        calls.append(args)
        record.update(json.loads(args[-1]))
        return copy.deepcopy(record)
    monkeypatch.setattr(Services, '_cf', patch)
    connection = {'ip': record['content'], 'instance_id': 'instance'}
    def run():
        return adopt(config, state, connection, state.begin_operation('adopt-dns', 'hash'), evidence)
    yield state, config, record, evidence, calls, run
    state.db.close()


def test_adoption_changes_only_comment_and_is_idempotent(transfer):
    state, config, record, evidence, calls, run = transfer
    before = copy.deepcopy(record)
    assert run()['adopted']
    assert record == {**before, 'comment': 'pocketdeploy:' + state.deployment_id}
    assert state.get_resource('dns:A:demo.example.com')['provider_id'] == before['id']
    assert run()['adopted']
    assert len(calls) == 1


def test_lost_patch_response_recovers_without_another_mutation(transfer, monkeypatch):
    state, config, record, evidence, calls, run = transfer
    original = Services._cf
    def lost(self, args):
        original(self, args)
        raise RuntimeError('lost response')
    monkeypatch.setattr(Services, '_cf', lost)
    with pytest.raises(RuntimeError):
        run()
    assert state.get_meta('dns-adoption:dns:A:demo.example.com')
    assert run()['adopted']
    assert len(calls) == 1


def test_failure_before_patch_resumes(transfer, monkeypatch):
    *_, calls, run = transfer
    original = Services._cf
    monkeypatch.setattr(Services, '_cf', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        run()
    monkeypatch.setattr(Services, '_cf', original)
    assert run()['adopted']
    assert len(calls) == 1


def test_lost_local_identity_write_recovers(transfer, monkeypatch):
    state, _, _, _, calls, run = transfer
    original = state.put_resource
    monkeypatch.setattr(state, 'put_resource', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        run()
    monkeypatch.setattr(state, 'put_resource', original)
    assert run()['adopted']
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [('content', '192.0.2.2'), ('proxied', True),
                                          ('ttl', 1), ('comment', 'changed'), ('id', 'c' * 32)])
def test_changed_provider_evidence_refused(transfer, field, value):
    _, _, record, _, calls, run = transfer
    record[field] = value
    with pytest.raises(DeployError):
        run()
    assert not calls


def test_pending_changed_evidence_and_unrelated_ownership_refused(transfer, monkeypatch):
    state, _, _, evidence, calls, run = transfer
    monkeypatch.setattr(Services, '_cf', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        run()
    evidence['settings_sha256'] = 'c' * 64
    with pytest.raises(DeployError, match='original exact evidence'):
        run()
    assert not calls


@pytest.mark.parametrize('change', ['compute', 'zone', 'optout', 'attestation', 'saved', 'conflict'])
def test_ownership_boundaries(transfer, monkeypatch, change):
    state, config, record, evidence, calls, run = transfer
    if change == 'compute':
        state.put_resource('compute', 'oci', 'other', {})
    elif change == 'zone':
        config['cloudflare-zone-id'] = 'c' * 32
    elif change == 'optout':
        config['once']['applications'][0]['manage-dns'] = False
    elif change == 'attestation':
        evidence['previous_dns_manager_disabled'] = False
    elif change == 'saved':
        state.put_resource('dns:A:demo.example.com', 'cloudflare-dns', 'other', {})
    else:
        monkeypatch.setattr(Services, '_records', lambda *a: [record, {'type': 'AAAA'}])
    with pytest.raises(DeployError):
        run()
    assert not calls


@pytest.mark.parametrize('args', [['adopt-dns'], ['status', '--dns-host', 'demo.example.com'],
                                  ['adopt-dns', '--dns-ipv4', '::1']])
def test_cli_requires_complete_valid_evidence_before_io(args):
    with pytest.raises(DeployError, match='adopt'):
        execute(parser().parse_args(args))


def test_safe_read_only_evidence(transfer):
    from pocketdeploy.dns_adoption import inspect
    state, config, record, evidence, calls, _ = transfer
    record['secret_metadata'] = 'must not escape'
    result = inspect(config, state, {'ip': record['content'], 'instance_id': 'instance'}, record['name'])
    assert result['settings_sha256'] == evidence['settings_sha256']
    assert 'comment' not in result and 'secret_metadata' not in result
    assert not calls
    assert state.get_resource('dns:A:demo.example.com') is None


def test_zone_identity_checked(transfer, monkeypatch):
    *_, calls, run = transfer
    monkeypatch.setattr('pocketdeploy.dns_adoption._call', lambda *a: {'id': 'a' * 32, 'name': 'unrelated.example'})
    with pytest.raises(DeployError, match='zone identity'):
        run()
    assert not calls


def test_lost_verification_read_recovers(transfer, monkeypatch):
    _, _, _, _, calls, run = transfer
    original = Services._records
    reads = 0
    def flaky(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise RuntimeError('lost verification')
        return original(*args)
    monkeypatch.setattr(Services, '_records', flaky)
    with pytest.raises(RuntimeError):
        run()
    assert run()['adopted']
    assert len(calls) == 1


def test_orphan_intent_resolved(transfer, monkeypatch):
    state, _, _, _, calls, run = transfer
    original = state.set_meta
    monkeypatch.setattr(state, 'set_meta', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        run()
    monkeypatch.setattr(state, 'set_meta', original)
    assert run()['adopted']
    assert not state.db.execute("SELECT id FROM steps WHERE status='pending'").fetchall()
