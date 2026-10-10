"""Bounded recovery for explicitly read-only provider requests."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import random
import time

from .common import DeployError
from .output import retry_progress


_budget = ContextVar('pocketdeploy_read_retry_budget', default=120)


@contextmanager
def read_retry_budget(seconds):
    """Set the per-read recovery budget; zero disables additional attempts."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Read retry budget must be finite and nonnegative.')
    token = _budget.set(seconds)
    try:
        yield
    finally:
        _budget.reset(token)


def run_read(call, *, timeout=None, deadline=None, label='Provider read'):
    """Call a read with its remaining seconds, recovering only known transients.

    Callers must bound each request by the supplied remaining time. A zero
    budget permits one attempt and supplies None unless an outer deadline
    applies. Labels must be fixed code-authored strings, never provider data.
    The same deadline covers subprocesses' own retries and local backoff.
    """
    budget = _budget.get() if timeout is None else timeout
    if not math.isfinite(budget) or budget < 0:
        raise ValueError('Read retry budget must be finite and nonnegative.')
    end = time.monotonic() + budget if budget else None
    if deadline is not None:
        end = deadline if end is None else min(end, deadline)
    for attempt in range(1, 6):
        remaining = None if end is None else end - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise DeployError('Provider read deadline exhausted.', code='provider_read_timeout')
        try:
            result = call(remaining)
            if end is not None and time.monotonic() >= end:
                raise DeployError('Provider read deadline exhausted.', code='provider_read_timeout')
            return result
        except DeployError as error:
            if (not budget or attempt == 5 or
                    not (getattr(error, 'retryable', False) or error.code == 'command_timeout')):
                raise
            remaining = end - time.monotonic()
            delay = random.uniform(0, min(20, 2 ** (attempt - 1)))
            retry_after = getattr(error, 'retry_after', None)
            if retry_after is not None:
                if not math.isfinite(retry_after) or retry_after < 0:
                    raise
                delay = max(delay, retry_after)
            if remaining <= 0 or delay >= remaining:
                raise
            retry_progress(delay, attempt + 1)
            time.sleep(delay)
