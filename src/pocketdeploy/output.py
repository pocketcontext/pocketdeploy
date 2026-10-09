"""Small stdout result and stderr progress boundary for safe CLI payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
import time
import threading


_active_reporter = ContextVar('pocketdeploy_reporter', default=None)


@contextmanager
def operation(label):
    """Time a fixed, code-authored operation label; never pass commands or data."""
    reporter = _active_reporter.get()
    if reporter is None or not reporter.verbose:
        yield
    else:
        with reporter.stage(label):
            yield


def _actions(actions):
    return [f"  {item.get('resource', item.get('host', 'resource'))}: {item.get('action', 'unknown')}"
            + (f" ({', '.join(item['changed_fields'])})" if item.get('changed_fields') else '')
            for item in actions]


def _health(status):
    lines = []
    for app in status.get('applications', []):
        health = 'healthy' if app.get('healthy') else 'unhealthy'
        running = 'running' if app.get('running') else 'stopped'
        lines.append(f"  {app['host']}: {running}, {health}" + ('; pending operation' if app.get('pending') else ''))
    if 'pending_operations' in status:
        lines.append(f"Pending host operations: {status['pending_operations']}.")
    return lines


def text_result(command, result):
    """Render only the known safe fields supplied by command implementations."""
    profile = result.get('profile')
    suffix = f" for {profile}" if profile else ''
    if command == 'init':
        return [f"Initialized{suffix}.", f"Deployment: {result['deployment_id']}."]
    if 'actions' in result and command in ('plan', 'converge', 'delete'):
        lines = [f"{'Deletion plan' if command == 'delete' else 'Plan'}{suffix}."]
        actions = result['actions'] + result.get('applications', []) + [dict(a, resource=a.get('name', 'service')) for a in result.get('services', [])]
        github = result.get('github') or {}
        if isinstance(github, dict):
            actions += [dict(a, resource=a['repository'] + '/' + a['environment']) for a in github.get('environments', [])]
        lines.extend(_actions(actions))
        if not actions or all(a.get('action') in ('retain', 'noop', 'unchanged') for a in actions):
            lines.append('No changes planned.')
        if 'protected' in result:
            lines.append('Deletion protection: ' + ('enabled.' if result['protected'] else 'disabled.'))
        return lines
    if command == 'status':
        lines = [f"Status{suffix}."]
        for name, resource in result.get('resources', {}).items():
            lines.append(f"  {name}: {resource.get('state') or 'unknown'} ({resource['id']})" if resource else f"  {name}: absent")
        state = result.get('state', {})
        if state.get('deployment_id'):
            lines.append(f"Deployment: {state['deployment_id']}.")
        if 'pending_steps' in state:
            lines.append(f"Pending recorded steps: {state['pending_steps']}.")
        lines.extend(_health(result.get('applications', {})))
        backup = state.get('last_vault_backup')
        lines.append(f"Last acknowledged Vault backup: {backup.get('saved_at', 'time unknown')} (document {backup.get('document', 'unknown')}, version {backup.get('version', 'unknown')})." if backup else 'Last acknowledged Vault backup: none.')
        return lines
    if command == 'converge':
        lines = [f"Converged{suffix}."]
        connection = result.get('connection', {})
        if connection.get('ip'):
            lines.append(f"SSH: {connection.get('user', 'ubuntu')}@{connection['ip']}")
        actions = result.get('applications', {}).get('actions', [])
        lines.extend(_actions(actions))
        if not actions or all(a.get('action') in ('retain', 'noop', 'unchanged') for a in actions):
            lines.append('No application changes.')
        lines.extend(_health(result.get('status', {})))
        return lines
    if command == 'smtp-test':
        return ['SMTP accepted the test message; inbox delivery is not confirmed.']
    if command == 'delete':
        lines = [f'Deletion completed{suffix}.']
        lines.extend('  deleted: ' + name for name in result.get('deleted_resources', []))
        lines.extend('  retained: ' + item['resource'] for item in result.get('retained_resources', []))
        lines.append('Shared networking, local recovery files and Vault history retained.')
        return lines
    if command == 'adopt':
        return [f"Adopted instance {result['instance_id']}.", f"SSH: {result.get('user', 'ubuntu')}@{result['ip']}"]
    if command in ('vault-save', 'vault-restore'):
        lines = [f"{'Saved' if command == 'vault-save' else 'Restored'} recovery checkpoint ({result['file_count']} files and SQLite snapshot).",
                 f"Vault document: {result['state_document']}", f"Vault version: {result['state_version']}"]
        if result.get('reconciliation_required'):
            lines.append('Run plan to reconcile restored state before making changes.')
        return lines
    return [f"{command} completed."]


class Reporter:
    def __init__(self, json_mode=False, quiet=False, command='', verbose=False, heartbeat_interval=10):
        self.verbose = verbose
        self.heartbeat_interval = heartbeat_interval
        self._active_spans = []
        self._progress_lock = threading.Lock()
        self.json_mode = json_mode
        self.quiet = quiet
        self.command = command
        self.current_stage = None
        self.failed_stage = None

    @contextmanager
    def activate(self):
        token = _active_reporter.set(self)
        try:
            yield self
        finally:
            _active_reporter.reset(token)

    def progress(self, message):
        if not self.quiet:
            with self._progress_lock:
                print(message, file=sys.stderr, flush=True)

    @contextmanager
    def stage(self, name):
        previous = self.current_stage
        self.current_stage = name
        start = time.monotonic()
        self.progress(f'{name}: started')
        stopped = threading.Event()
        span = object()
        self._active_spans.append(span)
        def heartbeat():
            while not stopped.wait(self.heartbeat_interval):
                if self._active_spans and self._active_spans[-1] is span:
                    self.progress(f'{name}: still waiting ({time.monotonic() - start:.1f}s)')
        worker = None
        if self.verbose and not self.quiet:
            worker = threading.Thread(target=heartbeat, name='pocketdeploy-progress', daemon=True)
            worker.start()
        def stop_heartbeat():
            stopped.set()
            if worker is not None:
                worker.join()
        try:
            yield
        except BaseException:
            stop_heartbeat()
            if self.failed_stage is None:
                self.failed_stage = name
            self.progress(f'{name}: failed ({time.monotonic() - start:.1f}s)')
            raise
        else:
            stop_heartbeat()
            self.progress(f'{name}: completed ({time.monotonic() - start:.1f}s)')
        finally:
            stop_heartbeat()
            self._active_spans.remove(span)
            self.current_stage = previous

    def _emit(self, payload, elapsed_seconds):
        if elapsed_seconds is not None:
            payload['elapsed_seconds'] = round(elapsed_seconds, 3)
        print(json.dumps(payload, sort_keys=True), flush=True)

    def success(self, result, elapsed_seconds=None):
        if self.json_mode:
            self._emit({'schema_version': 1, 'command': self.command, 'ok': True, 'result': result}, elapsed_seconds)
        else:
            lines = text_result(self.command, result)
            if elapsed_seconds is not None:
                lines.append(f'Completed in {elapsed_seconds:.1f}s.')
            print('\n'.join(lines), flush=True)

    def failure(self, message, code='operation_failed', stage=None, elapsed_seconds=None):
        error = {'code': code, 'message': message}
        if stage:
            error['stage'] = stage
        if self.json_mode:
            self._emit({'schema_version': 1, 'command': self.command, 'ok': False, 'error': error}, elapsed_seconds)
        else:
            print('pocketdeploy: ' + (f'{stage}: ' if stage else '') + message, file=sys.stderr, flush=True)
