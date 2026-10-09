"""Explicit commands backed by a small Blue DAG and private SQLite checkpoints."""
import argparse
import asyncio
from contextlib import nullcontext
import os
from pathlib import Path
import sys
import stat
import time

from blue.workflow import workflow, run as run_workflow
from blue.cli import find_up
from .common import DeployError, local_path
from .config import load, scope
from .state import State, deployment_lock
from .oci import OCI
from .host import Host
from . import vault
from .output import Reporter


class UsageError(DeployError):
    def __init__(self):
        super().__init__('Invalid command arguments; run pocketdeploy --help.', code='invalid_usage')


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages can echo arbitrary user-supplied values.
        raise UsageError()


def parser():
    p = ArgumentParser(prog='pocketdeploy', description=__doc__, allow_abbrev=False)
    p.add_argument('command', choices=['init', 'plan', 'create', 'converge', 'status', 'describe', 'ssh', 'delete', 'adopt', 'vault-save', 'vault-restore'])
    p.add_argument('-f', '--file', help='Configuration file (default: nearest colors.yml in the current directory or its parents)')
    p.add_argument('--json', action='store_true', help='Emit one versioned JSON result on stdout')
    p.add_argument('--quiet', action='store_true', help='Suppress progress on stderr')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--instance-id', help='Exact tagged OCI instance identity for explicit recovery/adoption')
    p.add_argument('--document', help='Vault state document to restore')
    p.add_argument('--version', help='Exact Vault state version to restore')
    p.add_argument('--destination', help='Empty directory for restoring a recovery set')
    p.add_argument('--overwrite', action='store_true', help='Explicitly replace recovery destinations')
    p.add_argument('--ssh-command', help='Explicit remote command; otherwise open an interactive shell')
    return p


def initialize(config, state, host, root):
    """Prepare local recovery authority without touching providers or old files."""
    bindings = local_path(root, '.envrc.private')
    pairs = ((host.key, host.pub), (host.hostkey, host.hostpub))
    authority = [path for pair in pairs for path in pair] + [host.known]
    paths = [*authority, bindings]
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
    try:
        descriptor = os.open(bindings, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    return {'profile': config['profile'], 'initialized': True, 'deployment_id': state.deployment_id}


async def converge(config, state, cloud, host, operation, reporter=None):
    """Blue schedules named steps; mutable secrets/state stay in closure objects."""
    results = {}
    reporter = reporter or Reporter()
    failure = None

    def keys(opts):
        public = host.prepare_keys()
        config['_cloud_init'] = host.cloud_init()
        results['public_key'] = public
        return dict(opts)

    def compute(opts):
        results['connection'] = cloud.converge(results['public_key'], operation)
        return dict(opts)

    def bootstrap(opts):
        results['bootstrap'] = host.bootstrap(results['connection'], operation)
        return dict(opts)

    def applications(opts):
        results['applications'] = host.converge(results['connection'], operation)
        return dict(opts)

    def verify(opts):
        results['status'] = host.status(results['connection'])
        return dict(opts)

    steps = {'keys': [keys, 'compute'], 'compute': [compute, 'host'],
             'host': [bootstrap, 'applications'], 'applications': [applications, 'verify'], 'verify': [verify]}
    # Functions catch at the boundary so Blue never captures a secret-bearing traceback.
    def wire(name, _opts):
        fn, *successors = steps[name]
        def safe(opts):
            nonlocal failure
            try:
                with reporter.stage(name):
                    return fn(opts)
            except DeployError as exc:
                if exc.stage is None:
                    exc.stage = name
                failure = exc
            except Exception:
                failure = DeployError('Deployment step failed; private output suppressed.', stage=name)
            return {**opts, 'blue/exit': 1, 'blue/err': str(failure)}
        return [safe, *successors]
    try:
        result = await run_workflow(workflow(start='keys', wire_fn=wire), {})
    finally:
        config.pop('_cloud_init', None)
    if result.get('blue/exit'):
        raise failure or DeployError('Deployment failed.')
    return {'profile': config.get('profile'), 'connection': results['connection'],
            'applications': results['applications'], 'status': results['status']}


def execute(args, reporter=None):
    reporter = reporter or Reporter(quiet=getattr(args, "quiet", False), command=args.command)
    os.umask(0o077)
    if args.command == 'ssh' and getattr(args, 'json', False):
        raise UsageError()
    if args.dry_run and args.command not in ('create', 'converge', 'delete', 'plan'):
        raise DeployError('--dry-run is supported only for plan/create/converge/delete.', code='invalid_usage')
    if args.overwrite and args.command != 'vault-restore':
        raise DeployError('--overwrite is supported only for vault-restore.', code='invalid_usage')
    read_only = args.command in ('plan', 'status', 'describe') or args.dry_run
    config_file = args.file if args.file is not None else find_up('colors.yml')
    if config_file is None:
        raise DeployError('No colors.yml found in the current directory or its parents; use -f to select a configuration.')
    config = load(config_file, resolve=args.command not in ('init', 'ssh', 'delete', 'vault-save', 'vault-restore', 'status', 'describe'))
    # Reject unsupported application behavior before any cloud mutation.
    from .host import validate_config
    validate_config(config)
    root = Path(config['_root'])
    state_path = local_path(root, config['state-file'])
    if args.command == 'vault-restore':
        destination = Path(args.destination).absolute() if args.destination else root
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with deployment_lock(local_path(destination, config['state-file'])):
            with reporter.stage('vault-restore'):
                return vault.restore(config, destination, args.document, args.version, args.overwrite)
    fresh = not state_path.exists()
    if fresh and config['compute-require-existing-state']:
        raise DeployError('Existing deployment state is required; restore it before continuing.')
    if fresh and args.command not in ('init', 'plan', 'create', 'converge', 'adopt'):
        raise DeployError('Deployment state is missing; restore or explicitly adopt it.')
    with deployment_lock(state_path):
        manager = nullcontext(None) if fresh and read_only else State(state_path, config['profile'], scope(config), create=fresh, read_only=read_only)
        with manager as state:
            host = Host(config, state, root)
            if args.command == 'init':
                return initialize(config, state, host, root)
            cloud = OCI(config, state)
            if read_only:
                if args.command in ('status', 'describe'):
                    observed = cloud.inspect()
                    result = {'profile': config['profile'], 'state': state.safe_status(), 'resources': observed}
                    if observed.get('compute'):
                        result['applications'] = host.status(cloud.connection())
                    return result
                actions = cloud.plan()
                if args.command == 'delete':
                    actions = [{'resource': r['name'], 'action': 'retain' if not r['owned'] or r['attributes'].get('lifecycle') == 'retained-for-recovery' or (r['name'] == 'boot-volume' and config.get('compute-retain-boot-volume', True)) else 'delete'} for r in state.resources()] if state else []
                    return {'profile': config['profile'], 'protected': config['compute-prevent-destroy'], 'actions': actions,
                            'retain_boot_volume': config.get('compute-retain-boot-volume', True)}
                app_actions = host.plan(cloud.connection()) if state and state.get_resource('compute') else [{'host': a['host'], 'action': 'create'} for a in config.get('once', {}).get('applications', [])]
                return {'profile': config['profile'], 'actions': actions, 'applications': app_actions}
            if args.command == 'ssh':
                code = host.ssh(cloud.connection(), args.ssh_command)
                return {'ssh_exit': code}
            if args.command == 'vault-save':
                with reporter.stage('vault-save'):
                    return vault.save(config, state, root)
            operation = state.begin_operation(args.command, config['_desired_hash'])
            try:
                if args.command in ('create', 'converge'):
                    state.set_meta('desired-config', {k: v for k, v in config.items() if not k.startswith('_')})
                    result = asyncio.run(converge(config, state, cloud, host, operation, reporter))
                elif args.command == 'delete':
                    result = cloud.delete(operation)
                elif args.command == 'adopt':
                    if not args.instance_id:
                        raise DeployError('Adoption requires --instance-id and matching ownership tags.')
                    result = cloud.adopt(args.instance_id, operation)
                else:
                    raise DeployError('Unsupported workflow.')
                state.finish_operation(operation, 'succeeded')
            except BaseException:
                state.finish_operation(operation, 'failed')
                raise
            return result


def main():
    argv = sys.argv[1:]
    reporter = Reporter(json_mode='--json' in argv, quiet='--quiet' in argv, command=None)
    started = time.monotonic()
    try:
        args, unknown = parser().parse_known_args(argv)
        reporter.command = args.command
        if unknown:
            raise UsageError()
        reporter.json_mode = args.json
        reporter.quiet = args.quiet
        if args.command == 'ssh' and args.json:
            raise DeployError('SSH does not support --json.', code='invalid_usage')
        result = execute(args, reporter)
        if args.command == 'ssh':
            code = result['ssh_exit']
            return code if code >= 0 else 128 - code
        reporter.success(result, elapsed_seconds=time.monotonic() - started)
    except DeployError as exc:
        reporter.failure(str(exc), code=exc.code, stage=exc.stage or reporter.failed_stage,
                         elapsed_seconds=time.monotonic() - started)
        return 2 if exc.code == 'invalid_usage' else 1
    except KeyboardInterrupt:
        reporter.failure('Interrupted; reconcile recorded operations before retrying.',
                         code='interrupted', stage=reporter.failed_stage,
                         elapsed_seconds=time.monotonic() - started)
        return 130
    except Exception:
        reporter.failure('Operation failed; private output suppressed.',
                         stage=reporter.failed_stage, elapsed_seconds=time.monotonic() - started)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
