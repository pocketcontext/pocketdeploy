"""Explicit commands backed by a small Blue DAG and private SQLite checkpoints."""
import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys

from blue.workflow import workflow, run as run_workflow
from .common import DeployError, local_path
from .config import load, scope
from .state import State, deployment_lock
from .oci import OCI
from .host import Host
from . import vault


def parser():
    p = argparse.ArgumentParser(prog='pocketdeploy', description=__doc__)
    p.add_argument('command', choices=['plan', 'create', 'converge', 'status', 'describe', 'ssh', 'delete', 'adopt', 'vault-save', 'vault-restore'])
    p.add_argument('-f', '--file', default='colors.yml')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--instance-id', help='Exact tagged OCI instance identity for explicit recovery/adoption')
    p.add_argument('--document', help='Vault state document to restore')
    p.add_argument('--version', help='Exact Vault state version to restore')
    p.add_argument('--destination', help='Empty directory for restoring a recovery set')
    p.add_argument('--overwrite', action='store_true', help='Explicitly replace recovery destinations')
    p.add_argument('--ssh-command', help='Explicit remote command; otherwise open an interactive shell')
    return p


def emit(value):
    print(json.dumps(value, sort_keys=True))


async def converge(config, state, cloud, host, operation):
    """Blue schedules named steps; mutable secrets/state stay in closure objects."""
    results = {}

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
            print('pocketdeploy: ' + name, file=sys.stderr, flush=True)
            try:
                return fn(opts)
            except DeployError as exc:
                return {**opts, 'blue/exit': 1, 'blue/err': str(exc)}
            except Exception:
                return {**opts, 'blue/exit': 1, 'blue/err': 'Deployment step failed; private output suppressed.'}
        return [safe, *successors]
    result = await run_workflow(workflow(start='keys', wire_fn=wire), {})
    config.pop('_cloud_init', None)
    if result.get('blue/exit'):
        raise DeployError(result.get('blue/err', 'Deployment failed.'))
    return {'connection': results['connection'], 'applications': results['applications'], 'status': results['status']}


def execute(args):
    os.umask(0o077)
    read_only = args.command in ('plan', 'status', 'describe') or args.dry_run
    config = load(args.file, resolve=args.command not in ('ssh', 'delete', 'vault-save', 'vault-restore', 'status', 'describe'))
    # Reject unsupported application behavior before any cloud mutation.
    from .host import validate_config
    validate_config(config)
    root = Path(config['_root'])
    state_path = local_path(root, config['state-file'])
    if args.command == 'vault-restore':
        destination = Path(args.destination).absolute() if args.destination else root
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with deployment_lock(local_path(destination, config['state-file'])):
            return vault.restore(config, destination, args.document, args.version, args.overwrite)
    fresh = not state_path.exists()
    if fresh and config['compute-require-existing-state']:
        raise DeployError('Existing deployment state is required; restore it before continuing.')
    if fresh and args.command not in ('plan', 'create', 'converge', 'adopt'):
        raise DeployError('Deployment state is missing; restore or explicitly adopt it.')
    with deployment_lock(state_path):
        manager = nullcontext(None) if fresh and read_only else State(state_path, config['profile'], scope(config), create=fresh, read_only=read_only)
        with manager as state:
            cloud, host = OCI(config, state), Host(config, state, root)
            if read_only:
                if args.command in ('status', 'describe'):
                    observed = cloud.inspect()
                    result = {'profile': config['profile'], 'state': state.safe_status(), 'resources': observed}
                    if observed.get('compute'):
                        result['applications'] = host.status(cloud.connection())
                    return result
                actions = cloud.plan()
                if args.command == 'delete':
                    actions = [{'resource': r['name'], 'action': 'delete' if r['owned'] else 'retain'} for r in state.resources()] if state else []
                    return {'profile': config['profile'], 'protected': config['compute-prevent-destroy'], 'actions': actions,
                            'retain_boot_volume': config.get('compute-retain-boot-volume', True)}
                app_actions = host.plan(cloud.connection()) if state and state.get_resource('compute') else [{'host': a['host'], 'action': 'create'} for a in config.get('once', {}).get('applications', [])]
                return {'profile': config['profile'], 'actions': actions, 'applications': app_actions}
            if args.command == 'ssh':
                code = host.ssh(cloud.connection(), args.ssh_command)
                if code:
                    raise DeployError('SSH command failed.')
                return {'ssh_exit': code}
            if args.command == 'vault-save':
                return vault.save(config, state, root)
            operation = state.begin_operation(args.command, config['_desired_hash'])
            try:
                if args.command in ('create', 'converge'):
                    state.set_meta('desired-config', {k: v for k, v in config.items() if not k.startswith('_')})
                    result = asyncio.run(converge(config, state, cloud, host, operation))
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
                if config.get('vault-save-after-run'):
                    try:
                        vault.save(config, state, root)
                    except Exception:
                        print('pocketdeploy: failed-run Vault backup also failed; local state preserved.', file=sys.stderr)
                raise
            if config.get('vault-save-after-run'):
                try:
                    result['vault'] = vault.save(config, state, root)
                except Exception:
                    raise DeployError('Deployment succeeded, but Vault backup failed; local state preserved. Run vault-save.') from None
            return result


def main():
    args = parser().parse_args()
    try:
        emit(execute(args))
    except DeployError as exc:
        print('pocketdeploy: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('pocketdeploy: interrupted; reconcile recorded operations before retrying.', file=sys.stderr)
        return 130
    except Exception:
        print('pocketdeploy: operation failed; private output suppressed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
