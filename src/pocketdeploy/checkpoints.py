"""Publish durable intent before a provider request in a vault-locked session."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .common import local_path
from .vault import _files


_before_request = ContextVar('pocketdeploy_before_provider_request', default=None)


@contextmanager
def provider_checkpoints(lease, state, config, *, deleting=False):
    if lease is None or state is None:
        yield
        return

    paths = [local_path(Path(config['_root']), name) for name in _files(config)]
    saved = None

    def fingerprint(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def check():
        nonlocal saved
        lease.check()
        files = tuple(fingerprint(path) for path in paths)
        if (state.db.total_changes, files) != saved:
            lease.checkpoint(state, allow_missing=deleting)
            saved = (state.db.total_changes, files)
        # Uploading the set may take an appreciable part of the lease.
        lease.check()

    token = _before_request.set(check)
    try:
        yield
    finally:
        _before_request.reset(token)


def before_provider_request():
    check = _before_request.get()
    if check is not None:
        check()
