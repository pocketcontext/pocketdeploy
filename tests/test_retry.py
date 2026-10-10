import pytest

from pocketdeploy.common import DeployError
from pocketdeploy import retry
from pocketdeploy.output import Reporter


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0
        sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
    value = Clock()
    monkeypatch.setattr(retry.time, 'monotonic', lambda: value.now)
    monkeypatch.setattr(retry.time, 'sleep', value.sleep)
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: high)
    return value


def transient(*, code='provider_request_failed', retry_after=None):
    error = DeployError('Safe failure.', code=code)
    error.retryable = True
    error.retry_after = retry_after
    return error


def test_transient_then_success_shares_budget(clock):
    calls = []
    def read(remaining):
        calls.append(remaining)
        clock.now += 3
        if len(calls) < 3:
            raise transient()
        return 'verified'
    assert retry.run_read(read, timeout=20) == 'verified'
    assert calls == [20, 16, 11]
    assert clock.sleeps == [1, 2]


def test_maximum_five_attempts(clock):
    calls = []
    def read(remaining):
        calls.append(remaining)
        raise transient()
    with pytest.raises(DeployError):
        retry.run_read(read)
    assert len(calls) == 5
    assert clock.sleeps == [1, 2, 4, 8]


@pytest.mark.parametrize('error', [DeployError('Auth', code='authentication_failed'),
                                  DeployError('Unknown', code='provider_request_failed'),
                                  RuntimeError('Unknown'), KeyboardInterrupt()])
def test_permanent_unknown_and_cancellation_fail_immediately(clock, error):
    def read(remaining):
        raise error
    with pytest.raises(type(error)) as caught:
        retry.run_read(read)
    assert caught.value is error
    assert clock.sleeps == []


def test_command_timeout_retries(clock):
    calls = []
    def read(remaining):
        calls.append(remaining)
        if len(calls) == 1:
            raise DeployError('Timeout', code='command_timeout')
        return True
    assert retry.run_read(read)
    assert len(calls) == 2


def test_retry_after_is_minimum(clock):
    calls = []
    def read(remaining):
        calls.append(remaining)
        if len(calls) == 1:
            raise transient(retry_after=9)
    retry.run_read(read, timeout=20)
    assert clock.sleeps == [9]
    assert calls == [20, 11]


@pytest.mark.parametrize('retry_after', [5, 6, float('inf'), -1])
def test_retry_after_cannot_exceed_budget_or_be_invalid(clock, retry_after):
    error = transient(retry_after=retry_after)
    def read(remaining):
        raise error
    with pytest.raises(DeployError) as caught:
        retry.run_read(read, timeout=5)
    assert caught.value is error
    assert clock.sleeps == []


def test_request_time_counts_toward_deadline(clock):
    error = transient()
    def read(remaining):
        clock.now += remaining
        raise error
    with pytest.raises(DeployError) as caught:
        retry.run_read(read, timeout=3)
    assert caught.value is error
    assert clock.sleeps == []


def test_late_success_is_rejected_without_another_request(clock):
    calls = []
    def read(remaining):
        calls.append(remaining)
        clock.now += remaining
        return 'late result'
    with pytest.raises(DeployError) as caught:
        retry.run_read(read, timeout=3)
    assert caught.value.code == 'provider_read_timeout'
    assert calls == [3]
    assert clock.sleeps == []


def test_outer_deadline_bounds_retry_and_zero_budget(clock):
    with retry.read_retry_budget(0):
        assert retry.run_read(lambda remaining: remaining, deadline=3) == 3
        assert retry.run_read(lambda remaining: remaining) is None
    assert retry.run_read(lambda remaining: remaining, deadline=7) == 7
    assert retry.run_read(lambda remaining: remaining) == 120


def test_zero_budget_one_attempt(clock):
    def read(remaining):
        assert remaining is None
        raise transient()
    with retry.read_retry_budget(0), pytest.raises(DeployError):
        retry.run_read(read)
    assert clock.sleeps == []


def test_expired_outer_deadline_does_not_call(clock):
    with pytest.raises(DeployError) as caught:
        retry.run_read(lambda remaining: pytest.fail('called'), deadline=0)
    assert caught.value.code == 'provider_read_timeout'


def test_context_restored_after_exception(clock):
    with pytest.raises(RuntimeError), retry.read_retry_budget(5):
        assert retry.run_read(lambda remaining: remaining) == 5
        raise RuntimeError()
    assert retry.run_read(lambda remaining: remaining) == 120


@pytest.mark.parametrize('budget', [-1, float('inf'), float('nan')])
def test_invalid_budget_rejected_before_request(clock, budget):
    with pytest.raises(ValueError):
        retry.run_read(lambda remaining: pytest.fail('called'), timeout=budget)
    with pytest.raises(ValueError), retry.read_retry_budget(budget):
        pytest.fail('entered invalid context')


def test_jitter_uses_full_backoff_window(clock, monkeypatch):
    windows = []
    def jitter(low, high):
        windows.append((low, high))
        return high / 2
    monkeypatch.setattr(retry.random, 'uniform', jitter)
    def read(remaining):
        raise transient()
    with pytest.raises(DeployError):
        retry.run_read(read)
    assert windows == [(0, 1), (0, 2), (0, 4), (0, 8)]
    assert clock.sleeps == [0.5, 1, 2, 4]


def test_progress_omits_provider_message(clock, capsys):
    calls = []
    def read(remaining):
        calls.append(remaining)
        if len(calls) == 1:
            error = transient()
            error.args = ('SYNTHETIC_SECRET',)
            raise error
    with Reporter().activate():
        retry.run_read(read, label='Resend read')
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Provider read: retrying' in output.err
    assert 'SYNTHETIC_SECRET' not in output.err
