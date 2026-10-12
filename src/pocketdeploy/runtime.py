"""Shared application deployment lifecycle without ONCE orchestration."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
import os
import stat

from .common import DeployError, local_path
from .config import scope
from .state import State, deployment_lock
from .compute import provider_class
from .host import Host


@dataclass
class Deployment:
    state: object
    cloud: object
    host: Host
    root: Path


@contextmanager
def session(config, *, read_only=False, allow_create=False, provider_factory=None):
    """Open matching state under the deployment lock; plans never create state."""
    root = Path(config['_root'])
    path = local_path(root, config['state-file'])
    with (nullcontext() if read_only and not path.exists() else deployment_lock(path)):
        fresh = not path.exists()
        if fresh and (config.get('compute-require-existing-state') or not allow_create):
            raise DeployError('Deployment state is missing; restore it before continuing.')
        manager = nullcontext(None) if fresh and read_only else State(
            path, config['profile'], scope(config), create=fresh, read_only=read_only)
        with manager as state:
            factory = provider_factory or provider_class(config['provider-compute'])
            yield Deployment(state, factory(config, state), Host(config, state, root), root)


@contextmanager
def recorded_operation(state, command, desired_hash):
    operation = state.begin_operation(command, desired_hash)
    try:
        yield operation
    except BaseException as exc:
        if getattr(exc, 'diagnostic', None) is not None:
            state.record_diagnostic(operation, exc.diagnostic)
        state.finish_operation(operation, 'failed')
        raise
    else:
        state.finish_operation(operation, 'succeeded')


def delete_compute(config, state, cloud, host, operation, *, before_cleanup=None):
    """Delete owned compute, then local authority. Caller quiesces its application."""
    if config.get('compute-prevent-destroy', True):
        raise DeployError('Deletion protection is enabled; set compute-prevent-destroy: false explicitly.')
    if any(a for a in config.get('once', {}).get('applications', [])):
        raise DeployError('Compute-only deletion cannot retire ONCE applications.')
    if any(r['kind'] not in cloud.resource_kinds for r in state.resources()):
        raise DeployError('Compute-only deletion cannot retire service or application resources.')
    cloud.plan_delete()
    host.plan_key_cleanup()
    result = cloud.delete(operation)
    if state.resources():
        raise DeployError('Compute deletion has unfinished resources; preserving SSH authority.')
    if before_cleanup:
        before_cleanup()
    key_cleanup = host.cleanup_keys()
    if key_cleanup.get('retained_key_files'):
        result = {**result, 'local_keys': key_cleanup}
    return result


def initialize(config, state, host, root):
    """Prepare local recovery authority without touching providers or old files."""
    bindings = local_path(root, '.envrc.private')
    pairs = ((host.key, host.pub), (host.hostkey, host.hostpub))
    authority = [path for pair in pairs for path in pair] + [host.known]
    paths = [*authority, bindings]
    if any(a.get('github') for a in config.get('once', {}).get('applications', [])):
        from .github import GitHub
        github = GitHub(config, state, root, host)
        paths.extend(path for app in github.apps for path in github.key_paths(app))
    state_path = local_path(root, config['state-file'])
    reserved = {Path(config['_file']).resolve(), local_path(root, '.envrc'),
                local_path(root, config['workdir']), state_path,
                *(Path(str(state_path) + suffix) for suffix in ('.lock', '-journal', '-wal', '-shm'))}
    if len(set(paths)) != len(paths) or set(paths).intersection(reserved):
        raise DeployError('Initialization files must have distinct paths.')
    for path in paths:
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise DeployError('Initialization files must be owned regular files without hardlinks.')
    if state.get_resource('compute') and any(not path.exists() for path in authority):
        raise DeployError('Existing compute SSH identity is missing; restore it from Vault.')
    for private, public in pairs:
        if private.exists() != public.exists():
            raise DeployError('SSH key pair is incomplete; restore the complete identity before initialization.')
    host.prepare_keys()
    if any(a.get('github') for a in config.get('once', {}).get('applications', [])):
        from .github import GitHub
        GitHub(config, state, root, host).prepare_keys()
    try:
        descriptor = os.open(bindings, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    return {'profile': config['profile'], 'initialized': True, 'deployment_id': state.deployment_id}

