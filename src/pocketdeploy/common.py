"""Shared safe command and path primitives. Never echo subprocess failures."""
import os
import subprocess
from pathlib import Path

class DeployError(Exception):
    """Only fixed, non-secret messages should reach this exception."""


def run(args, *, input=None, timeout=120, env=None):
    try:
        r = subprocess.run(args, input=input, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError('Command unavailable or timed out; output suppressed.') from None
    if r.returncode:
        raise DeployError('Command failed; output suppressed.') from None
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
