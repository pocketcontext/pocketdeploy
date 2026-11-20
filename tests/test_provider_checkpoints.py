"""Exercise lease ordering at the actual Blue, SSH and deletion entry points."""
import asyncio
from types import SimpleNamespace

import pytest

from pocketdeploy import cli, host_trust
from pocketdeploy.checkpoints import provider_checkpoints
from pocketdeploy.common import DeployError
from pocketdeploy.deletion import Deletion
from pocketdeploy.host import Host
from pocketdeploy.output import Reporter, text_result
from pocketdeploy.state import State


class Lease:
    def __init__(self, record):
        self.record = record
        self.failure = None
        self.lost = False

    def checkpoint(self, state, **kwargs):
        self.check()
        self.record(state)

    def check(self):
        if self.lost:
            raise DeployError('Lost synthetic lease.', code='vault_lease_lost')


def test_blue_waits_for_durable_state_before_next_step(tmp_path):
    events = []
    lease = Lease(lambda s: events.append(('checkpoint', s.get_meta('step'))))
    with State(tmp_path / 'ledger', 'synthetic', {}, create=True) as state:
        class LocalHost:
            def prepare_keys(self):
                state.set_meta('step', 'keys')
                return 'synthetic'

            def cloud_init(self):
                return 'synthetic'

        class Compute:
            def converge(self, public, operation):
                events.append(('provider', state.get_meta('step')))
                raise DeployError('Synthetic stop.')

        with pytest.raises(DeployError, match='Synthetic stop'):
            asyncio.run(cli.converge({}, state, Compute(), LocalHost(), 'op', lease=lease))
    assert events == [('checkpoint', 'keys'), ('provider', 'keys')]


def test_rotation_checkpoints_recoverable_key_before_apply_and_after_verification(tmp_path, monkeypatch):
    events = []
    with State(tmp_path / 'ledger', 'synthetic', {}, create=True) as state:
        host = Host({}, state, tmp_path)
        host.prepare_keys()
        state.put_resource('compute', 'oci-compute', 'synthetic', {})

        def checkpoint(s):
            trust = s.get_meta(host_trust.META)
            assert trust['private'] and trust['public']
            events.append('verified' if trust['verified'] else 'pending')

        def request(host, connection, public, command, payload=None):
            if payload:
                events.append('apply')
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(host_trust, 'request', request)
        with provider_checkpoints(Lease(checkpoint), state, {'_root': tmp_path}):
            host_trust.rotate(host, {'instance_id': 'synthetic'})
    assert events == ['pending', 'apply', 'verified']


def test_deletion_break_blocks_next_destructive_stage(tmp_path):
    events = []
    lease = Lease(lambda state: events.append('checkpoint'))

    def retire(operation):
        events.append('retire')
        lease.lost = True

    deletion = Deletion.__new__(Deletion)
    deletion.config = {'compute-prevent-destroy': False}
    deletion.connection = {'instance_id': 'synthetic'}
    deletion.reporter = Reporter(quiet=True)
    deletion.plan = lambda: None
    deletion.github = SimpleNamespace(delete=retire)
    deletion.host = SimpleNamespace(quiesce=lambda *args: pytest.fail('continued after break'))
    with State(tmp_path / 'ledger', 'synthetic', {}, create=True) as deletion.state:
        with pytest.raises(DeployError, match='Lost synthetic lease'):
            asyncio.run(deletion.run('op', lease=lease))
    assert events == ['retire']


def test_operator_sees_generation_and_drift_without_json():
    result = {'vault_lock': {'available': True, 'state': 'released', 'generation': 17},
              'warnings': ['Remote checkpoint changed.']}
    assert 'generation 17' in ' '.join(text_result('lock-status', result))
    for cmd, fields in [('plan', {'actions': []}), ('status', {})]:
        assert 'Remote checkpoint changed.' in text_result(cmd, {**fields, **result})


def test_file_only_change_is_saved_before_the_next_provider_request(tmp_path):
    from pocketdeploy.checkpoints import before_provider_request
    bindings = tmp_path / '.envrc.private'
    bindings.write_text('synthetic-one')
    saved = []
    lease = Lease(lambda state: saved.append(bindings.read_text()))
    with State(tmp_path / 'ledger', 'synthetic', {}, create=True) as state:
        with provider_checkpoints(lease, state, {'_root': tmp_path}):
            before_provider_request()
            bindings.write_text('synthetic-two')
            before_provider_request()
            before_provider_request()
    assert saved == ['synthetic-one', 'synthetic-two']
