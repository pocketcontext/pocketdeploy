"""Shared safe command and path primitives. Never echo subprocess failures."""
import os
import subprocess
from pathlib import Path

class DeployError(Exception):
    """Only fixed, non-secret messages should reach this exception."""

    def __init__(self, message, *, code="operation_failed", stage=None,
                 retryable=False, retry_after=None, status=None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


def run(args, *, input=None, timeout=120, env=None, error_classifier=None):
    try:
        r = subprocess.run(args, input=input, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise DeployError('Command timed out; output suppressed.', code="command_timeout") from None
    except OSError:
        raise DeployError('Command unavailable; output suppressed.', code="command_unavailable") from None
    if r.returncode:
        if error_classifier is not None:
            classified = error_classifier(r.stderr)
            if classified is not None:
                raise classified from None
        raise DeployError('Command failed; output suppressed.', code='command_failed') from None
    return r.stdout


def local_path(root, name):
    root = Path(root).resolve()
    p = Path(name)
    if not p.is_absolute():
        p = root / p
    if not p.is_relative_to(root) or '..' in p.parts:
        raise DeployError('Path must stay inside the deployment directory.')
    for parent in [*p.parents, p]:
        if parent.is_symlink():
            raise DeployError('Symlink paths are not allowed.')
    return p
