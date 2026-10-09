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
from .common import DeployError, local_path
from .config import load, scope
from .state import State, deployment_lock
from .oci import OCI
from .host import Host
from . import vault
from .output import Reporter, operation as output_operation


class UsageError(DeployError):
    def __init__(self):
        super().__init__('Invalid command arguments; run pocketdeploy --help.', code='invalid_usage')


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages can echo arbitrary user-supplied values.
        raise UsageError()


def parser():
    p = ArgumentParser(
        prog='pocketdeploy', description='Deploy and operate an OCI VPS from colors.yml.',
        allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Deployment commands:
  init           Prepare local state and SSH keys; no cloud or Vault calls
  plan           Read live resources and report proposed changes
  converge       Reconcile the deployment with desired configuration
  status         Show observed resources, application health and backup receipt
  ssh            Open SSH or run --ssh-command; preserve remote output and exit
  delete         Delete owned resources, subject to destruction protection
  smtp-test      Send one explicit SMTP test using --to
  adopt          Recover an existing instance with matching deployment UUID tags

Vault commands (explicit checkpoints):
  vault-save     Save private bindings, recovery keys and SQLite to Vault
  vault-restore  Restore a selected document/version; never deploys

Examples:
  pocketdeploy plan
  pocketdeploy converge
  pocketdeploy status --json --quiet
  pocketdeploy delete --dry-run
  pocketdeploy init
  pocketdeploy vault-save

Configuration defaults to colors.yml in the current working directory.
Use -f to select a deployment. Run without arguments to show this help.''')
    p.add_argument('command', metavar='COMMAND', choices=['init', 'plan', 'converge', 'status', 'ssh', 'delete', 'adopt', 'smtp-test', 'vault-save', 'vault-restore'], help='Deployment or Vault command listed below')
    p.add_argument('-f', '--file', help='Configuration file (default: ./colors.yml in the current working directory)')
    p.add_argument('--json', action='store_true', help='Emit one versioned JSON result on stdout')
    p.add_argument('--verbose', action='store_true', help='Show safe request timings and waiting progress on stderr')
    p.add_argument('--quiet', action='store_true', help='Suppress progress on stderr')
    p.add_argument('--dry-run', action='store_true', help='Plan converge/delete without applying changes')
    p.add_argument('--instance-id', help='Exact tagged OCI instance identity for explicit recovery/adoption')
    p.add_argument('--document', help='Vault state document to restore')
    p.add_argument('--version', help='Exact Vault state version to restore')
    p.add_argument('--destination', help='Recovery directory containing matching Git configuration')
    p.add_argument('--overwrite', action='store_true', help='Explicitly replace recovery destinations')
    p.add_argument('--ssh-command', help='Explicit remote command; otherwise open an interactive shell')
    p.add_argument('--rotate-github-keys', action='store_true', help='Rotate disposable GitHub deployment keys during converge')
    p.add_argument('--to', help='Recipient for the explicit smtp-test command')
    return p


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


async def converge(config, state, cloud, host, operation, reporter=None, rotate_github_keys=False):
    """Blue schedules named steps; mutable secrets/state stay in closure objects."""
    results = {}
    reporter = reporter or Reporter()
    failure = None
    services = None
    github = None
    if config.get('provider-dns') == 'cloudflare' or config.get('provider-smtp') == 'resend':
        from .services import Services
        services = Services(config, state)
    if any(a.get('github') for a in config.get('once', {}).get('applications', [])) or (state and any(r['kind'] == 'github-environment' for r in state.resources())):
        from .github import GitHub
        github = GitHub(config, state, Path(config['_root']), host)

    def preflight(opts):
        if services:
            services.preflight()
            services.plan()
        if github:
            github.preflight()
        return dict(opts)

    def infrastructure_services(opts):
        if services:
            results['services'] = services.converge(results['connection'], operation)
            if config.get('provider-smtp') == 'resend':
                host.smtp_settings = services.smtp_settings()
        return dict(opts)

    def publish_github(opts):
        if github:
            results['github'] = github.converge(results['connection'], operation, rotate_keys=rotate_github_keys)
        return dict(opts)

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
        from .health import verify as verify_https
        verify_https(config)
        return dict(opts)

    steps = {'preflight': [preflight, 'keys'], 'keys': [keys, 'compute'], 'compute': [compute, 'host'],
             'host': [bootstrap, 'services'], 'services': [infrastructure_services, 'applications'],
             'applications': [applications, 'verify'], 'verify': [verify, 'github'], 'github': [publish_github]}
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
        result = await run_workflow(workflow(start='preflight', wire_fn=wire), {})
    finally:
        config.pop('_cloud_init', None)
        config.pop('_smtp', None)
    if result.get('blue/exit'):
        raise failure or DeployError('Deployment failed.')
    return {'profile': config.get('profile'), 'connection': results['connection'],
            'applications': results['applications'], 'status': results['status'],
            'services': results.get('services'), 'github': results.get('github')}


def execute(args, reporter=None):
    reporter = reporter or Reporter(quiet=getattr(args, "quiet", False), command=args.command)
    if getattr(args, 'verbose', False) and getattr(args, 'quiet', False):
        raise DeployError('--verbose and --quiet cannot be combined.', code='invalid_usage')
    os.umask(0o077)
    if args.command == 'ssh' and getattr(args, 'json', False):
        raise UsageError()
    if args.dry_run and args.command not in ('converge', 'delete', 'plan'):
        raise DeployError('--dry-run is supported only for plan/converge/delete.', code='invalid_usage')
    if args.overwrite and args.command != 'vault-restore':
        raise DeployError('--overwrite is supported only for vault-restore.', code='invalid_usage')
    if bool(getattr(args, 'to', None)) != (args.command == 'smtp-test'):
        raise DeployError('smtp-test requires --to; --to is only supported for smtp-test.', code='invalid_usage')
    if getattr(args, 'rotate_github_keys', False) and (args.command != 'converge' or args.dry_run):
        raise DeployError('--rotate-github-keys requires converge without --dry-run.', code='invalid_usage')
    read_only = args.command in ('plan', 'status') or args.dry_run
    config_file = args.file if args.file is not None else 'colors.yml'
    if args.file is None and not Path(config_file).exists():
        raise DeployError('No colors.yml found in the current directory; use -f to select a configuration.')
    config = load(config_file, resolve=args.command not in ('init', 'ssh', 'delete', 'vault-save', 'vault-restore', 'status', 'smtp-test'))
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
    if fresh and args.command not in ('init', 'plan', 'converge', 'adopt'):
        raise DeployError('Deployment state is missing; restore or explicitly adopt it.')
    with deployment_lock(state_path):
        manager = nullcontext(None) if fresh and read_only else State(state_path, config['profile'], scope(config), create=fresh, read_only=read_only)
        with manager as state:
            host = Host(config, state, root)
            if args.command == 'init':
                with output_operation('init: local preparation'):
                    return initialize(config, state, host, root)
            if state and config.get('provider-dns') != 'cloudflare' and any(r['kind'] == 'cloudflare-dns' and r['attributes'].get('type') == 'A' for r in state.resources()):
                raise DeployError('Restore the managed DNS configuration before operating its deployment.')
            cloud = OCI(config, state)
            if read_only:
                if args.command == 'status':
                    observed = cloud.inspect()
                    result = {'profile': config['profile'], 'state': state.safe_status(), 'resources': observed}
                    if observed.get('compute'):
                        result['applications'] = host.status(cloud.connection())
                    return result
                if args.command == 'delete':
                    from .deletion import Deletion
                    with reporter.stage('delete-preflight'):
                        return Deletion(config, state, cloud, host, reporter).plan()
                actions = cloud.plan()
                if config.get('provider-smtp') == 'resend' and state and state.get_resource('smtp-key'):
                    from .services import Services
                    host.smtp_settings = Services(config, state).smtp_settings()
                needs_smtp = any(a.get('smtp') for a in config.get('once', {}).get('applications', [])) and not (state and state.get_resource('smtp-key'))
                app_actions = host.plan(cloud.connection()) if state and state.get_resource('compute') and not needs_smtp else [{'host': a['host'], 'action': 'after-smtp' if needs_smtp else 'create'} for a in config.get('once', {}).get('applications', [])]
                service_actions = []
                if config.get('provider-dns') == 'cloudflare' or config.get('provider-smtp') == 'resend':
                    from .services import Services
                    services = Services(config, state)
                    services.preflight()
                    connection = cloud.connection() if state and state.get_resource('compute') else None
                    service_actions = services.plan(connection)['actions']
                github_actions = []
                if any(a.get('github') for a in config.get('once', {}).get('applications', [])) or (state and any(r['kind'] == 'github-environment' for r in state.resources())):
                    from .github import GitHub
                    github = GitHub(config, state, root, host)
                    github.preflight()
                    github_actions = github.plan()
                return {'profile': config['profile'], 'actions': actions, 'applications': app_actions,
                        'services': service_actions, 'github': github_actions}
            if args.command == 'ssh':
                code = host.ssh(cloud.connection(), args.ssh_command)
                return {'ssh_exit': code}
            if args.command == 'vault-save':
                with reporter.stage('vault-save'):
                    return vault.save(config, state, root)
            operation = state.begin_operation(args.command, config['_desired_hash'])
            try:
                if args.command == 'converge':
                    state.set_meta('desired-config', {k: v for k, v in config.items() if not k.startswith('_')})
                    result = asyncio.run(converge(config, state, cloud, host, operation, reporter, rotate_github_keys=getattr(args, 'rotate_github_keys', False)))
                elif args.command == 'delete':
                    from .deletion import Deletion
                    result = asyncio.run(Deletion(config, state, cloud, host, reporter).run(operation))
                elif args.command == 'smtp-test':
                    from .services import Services
                    if config.get('provider-smtp') != 'resend':
                        raise DeployError('smtp-test requires provider-smtp: resend.')
                    result = host.smtp_test(cloud.connection(), Services(config, state).smtp_settings(), args.to)
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
    if not argv:
        parser().print_help()
        return 0
    reporter = Reporter(json_mode='--json' in argv, quiet='--quiet' in argv, command=None)
    started = time.monotonic()
    try:
        args, unknown = parser().parse_known_args(argv)
        reporter.command = args.command
        if unknown:
            raise UsageError()
        reporter.json_mode = args.json
        reporter.quiet = args.quiet
        reporter.verbose = args.verbose
        if args.verbose and args.quiet:
            raise DeployError('--verbose and --quiet cannot be combined.', code='invalid_usage')
        if args.command == 'ssh' and args.json:
            raise DeployError('SSH does not support --json.', code='invalid_usage')
        with reporter.activate():
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
