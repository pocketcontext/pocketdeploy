"""Deletion has its own read-only plan and ordered, resumable workflow."""
from pathlib import Path

from blue.workflow import workflow, run as run_workflow

from .common import DeployError
from .github import GitHub
from .services import Services
from .output import Reporter


class Deletion:
    def __init__(self, config, state, cloud, host, reporter=None):
        self.config, self.state, self.cloud, self.host = config, state, cloud, host
        self.reporter = reporter or Reporter()
        self.github = GitHub(config, state, Path(config['_root']), host)
        self.services = Services(config, state)
        self.connection = None
        self.prepared = None

    def plan(self):
        # Cloud authentication/ownership is checked before any external writes,
        # including DNS and GitHub retirement. No provisioning planner is used.
        supported = self.cloud.resource_kinds | {'cloudflare-dns', 'resend-domain',
                                                 'resend-api-key', 'github-environment'}
        if any(r['owned'] and r['kind'] not in supported for r in self.state.resources()):
            raise DeployError('Deployment contains an unsupported owned resource; reconcile it before deletion.')
        cloud_actions = self.cloud.plan_delete()
        github_actions = self.github.plan_delete()
        service_actions = self.services.plan_delete()
        host_key_actions = self.host.plan_key_cleanup()
        compute = next((a for a in cloud_actions if a.get('resource') == 'compute'), None)
        host_actions = []
        self.connection = None
        if compute and compute.get('state') == 'TERMINATING':
            pending = self.state.get_meta(self.cloud.delete_pending_key, {})
            checkpoint = self.state.get_meta('delete-host', {})
            if not ((isinstance(pending, dict) and pending.get('id') == compute.get('id'))
                    and (isinstance(checkpoint, dict) and checkpoint.get('quiesced')
                        and checkpoint.get('instance_id') == compute.get('id'))):
                raise DeployError('Instance is terminating without a recorded deletion; reconcile its shutdown before continuing.')
        if compute and compute.get('state') not in (None, 'TERMINATED', 'TERMINATING'):
            checkpoint = self.state.get_meta('delete-host', {})
            if compute['state'] == 'RUNNING':
                self.connection = self.cloud.connection()
                host_actions = self.host.plan_delete(self.connection)['actions']
            elif not (isinstance(checkpoint, dict) and checkpoint.get('quiesced')
                      and checkpoint.get('instance_id') == compute.get('id')):
                raise DeployError('Instance is not running and has no verified shutdown checkpoint; reconcile its application shutdown before deletion.')
        key_actions = [a for a in github_actions if a['resource'] == 'github-deployment-keys']
        actions = [*[a for a in github_actions if a['resource'] != 'github-deployment-keys'],
                   *[{'resource': a['host'], **a} for a in host_actions],
                   *service_actions, *cloud_actions, *key_actions, *host_key_actions]
        self.prepared = {'profile': self.config['profile'], 'dry_run': True,
                         'protected': self.config.get('compute-prevent-destroy', True),
                         'actions': actions,
                         'retained_local': ['configuration', 'private-bindings', 'sqlite-state'],
                         'vault': 'unchanged'}
        return self.prepared

    async def run(self, operation):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deletion protection is enabled.')
        completed = []
        results = {}
        failure = None

        def preflight():
            self.plan()

        def github():
            results['github'] = self.github.delete(operation)

        def quiesce():
            if self.connection:
                results['applications'] = self.host.quiesce(self.connection, operation)
                self.state.set_meta('delete-host', {'instance_id': self.connection['instance_id'], 'quiesced': True})
            else:
                results['applications'] = {'applications': [], 'skipped': 'already-absent-or-verified-stopped'}

        def services():
            results['services'] = self.services.delete(operation)

        def infrastructure():
            results['infrastructure'] = self.cloud.delete(operation)

        def cleanup():
            if any(resource['owned'] for resource in self.state.resources()):
                raise DeployError('Owned remote resources remain; SSH recovery keys were preserved.')
            # Validate both groups before unlinking either one.
            self.github.plan_delete()
            self.host.plan_key_cleanup()
            github_keys = self.github.cleanup_keys()
            host_keys = self.host.cleanup_keys()
            results['local_keys'] = {
                'deleted_key_files': github_keys.get('deleted_key_files', 0) + host_keys.get('deleted_key_files', 0),
                'github': github_keys, 'ssh': host_keys,
            }

        steps = {'delete-preflight': [preflight, 'delete-github'],
                 'delete-github': [github, 'delete-applications'],
                 'delete-applications': [quiesce, 'delete-services'],
                 'delete-services': [services, 'delete-infrastructure'],
                 'delete-infrastructure': [infrastructure, 'delete-local-keys'],
                 'delete-local-keys': [cleanup]}

        def wire(name, _opts):
            fn, *successors = steps[name]
            def safe(opts):
                nonlocal failure
                try:
                    with self.reporter.stage(name):
                        fn()
                    completed.append(name)
                    return dict(opts)
                except DeployError as exc:
                    failure = exc
                    if failure.stage is None:
                        failure.stage = name
                except Exception:
                    failure = DeployError('Deletion step failed; private output suppressed.', stage=name)
                return {**opts, 'blue/exit': 1, 'blue/err': str(failure)}
            return [safe, *successors]

        outcome = await run_workflow(workflow(start='delete-preflight', wire_fn=wire), {})
        if outcome.get('blue/exit'):
            error = failure or DeployError('Deletion failed.')
            if any(name != 'delete-preflight' for name in completed):
                error.args = (str(error) + ' Completed stages: ' + ', '.join(completed[1:]) + '. Rerun delete to reconcile and resume.',)
            raise error
        remaining = self.state.resources()
        remaining_names = {r['name'] for r in remaining}
        deleted = [a['resource'] for a in self.prepared['actions']
                   if a.get('action') in ('delete', 'complete-pending') and a['resource'] not in remaining_names]
        return {'profile': self.config['profile'], 'deleted': True,
                'deleted_resources': list(dict.fromkeys(deleted)),
                'retained_resources': [{'resource': r['name'], 'kind': r['kind']} for r in remaining],
                'retained_local': self.prepared['retained_local'], 'vault': 'unchanged',
                'retained_external': ['shared-network', 'vault-history'],
                'completed_stages': completed, **results}
