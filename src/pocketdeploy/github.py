"""Profile-named GitHub environments and narrowly scoped deployment authority."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote

from .common import DeployError, local_path, run
from .output import operation
from .config import validate_local_paths


def validate_config(config):
    repositories = set()
    for app in config.get('once', {}).get('applications', []):
        repo = app.get('github')
        if repo is None:
            continue
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repo):
            raise DeployError('GitHub must identify an owner/repository.')
        if repo.lower() in repositories:
            raise DeployError('One application per GitHub repository is supported per deployment.')
        repositories.add(repo.lower())
        if app.get('disable_tls'):
            raise DeployError('GitHub deployment requires HTTPS; disable_tls must be false.')
        image = app.get('image', '')
        if not image.startswith('ghcr.io/' + repo.lower() + ':') and not image.startswith('ghcr.io/' + repo.lower() + '@sha256:'):
            raise DeployError('GitHub deployment image must belong to its repository on ghcr.io.')
        if app.get('github-environment', config['profile']) != config['profile']:
            raise DeployError('GitHub environment must equal the deployment profile.')


class GitHub:
    def __init__(self, config, state, root, host):
        self.config, self.state, self.root, self.host = config, state, Path(root), host
        self.environment = config['profile']
        self.apps = [a for a in config.get('once', {}).get('applications', []) if a.get('github')]

    def _api(self, endpoint, method='GET', data=None):
        args = ['gh', 'api', '--method', method, endpoint]
        if data is not None:
            args += ['--input', '-']
        with operation('GitHub: environment request'):
            output = run(args, input=json.dumps(data) if data is not None else None)
        try:
            return json.loads(output) if output.strip() else {}
        except ValueError:
            raise DeployError('GitHub returned invalid JSON; output suppressed.') from None

    def _pages(self, endpoint, field):
        records = []
        page = 1
        while True:
            data = self._api(endpoint + ('&' if '?' in endpoint else '?') + f'per_page=100&page={page}')
            batch = data[field]
            records.extend(batch)
            if len(batch) < 100:
                return records
            page += 1

    def key_paths(self, app):
        token = hashlib.sha256(app['github'].lower().encode()).hexdigest()[:20]
        private = local_path(self.root, '.ssh/github-' + token)
        return private, Path(str(private) + '.pub')

    def _key(self, app):
        validate_local_paths(self.config, self.root, state_path=self.state.path)
        private, public = self.key_paths(app)
        # Missing or partial local credentials are disposable. Validate every
        # surviving file before replacement; never follow a symlink.
        for path in (private, public):
            if path.is_symlink():
                raise DeployError('GitHub keys must not be symlinks.')
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    raise DeployError('GitHub keys must be owned regular files without hardlinks.')
        pending = 'github-key-pending:' + app['github']
        if self.state.get_meta(pending) == 'replace' or private.exists() != public.exists():
            self.state.set_meta(pending, 'replace')
            private.unlink(missing_ok=True)
            public.unlink(missing_ok=True)
        if not private.exists():
            self.state.set_meta(pending, 'replace')
            private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.github-key-', dir=self.root) as directory:
                generated = Path(directory) / 'key'
                run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(generated)])
                for source, target in ((generated, private), (Path(str(generated) + '.pub'), public)):
                    source.chmod(0o600)
                    try:
                        os.link(source, target)
                    except FileExistsError:
                        raise DeployError('GitHub key destination appeared; existing files were preserved.') from None
            self.state.set_meta(pending, True)
        for path in (private, public):
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise DeployError('GitHub keys must be owned regular files without hardlinks.')
        private.chmod(0o600)
        public.chmod(0o600)
        derived = run(['ssh-keygen', '-y', '-P', '', '-f', str(private)]).split()
        if derived[:2] != public.read_text().split()[:2] or len(derived) < 2:
            raise DeployError('GitHub deployment key pair does not match; remove the disposable pair and retry convergence.')
        self.state.set_meta('github-key:' + app['github'], {'path': str(private.relative_to(self.root))})
        return private, public.read_text().strip()

    def prepare_keys(self):
        for app in self.apps:
            self._key(app)

    def preflight(self):
        if self.state is not None:
            desired = {'github:' + app['github'] for app in self.apps}
            if any(r['kind'] == 'github-environment' and r['name'] not in desired for r in self.state.resources()):
                raise DeployError('Removed GitHub target requires explicit environment retirement.')
        for app in self.apps:
            repo = app['github']
            repository = self._api('repos/' + repo)
            if not repository.get('permissions', {}).get('admin'):
                raise DeployError('GitHub environment management requires repository administrator access.')
            existing = next((e for e in self._pages(f'repos/{repo}/environments', 'environments')
                             if e['name'].lower() == self.environment.lower()), None)
            owned = self.state.get_resource('github:' + repo) if self.state is not None else None
            if existing and (not owned or str(existing['id']) != owned['provider_id']):
                raise DeployError('GitHub environment is unmanaged; explicit ownership recovery is required.')
            if not owned and not existing and self.state is not None and self.state.get_meta('github-pending:' + repo, False):
                raise DeployError('Uncertain GitHub environment creation; operator recovery is required.')
            if owned and not existing:
                raise DeployError('Recorded GitHub environment disappeared; operator recovery is required.')
            if owned:
                base = f'repos/{repo}/environments/{quote(self.environment, safe="")}'
                variables = {v['name']: v['value'] for v in self._pages(base + '/variables', 'variables')}
                if variables.get('POCKETDEPLOY_DEPLOYMENT_ID') != self.state.deployment_id and not (variables.get('POCKETDEPLOY_DEPLOYMENT_ID') is None and self.state.get_meta('github-pending:' + repo, False)):
                    raise DeployError('GitHub environment deployment ownership does not match.')
        return {'repositories': len(self.apps)}

    def plan(self):
        return {'environments': [{'repository': app['github'], 'environment': self.environment,
                                 'action': 'reconcile' if self.state is not None and self.state.get_resource('github:' + app['github']) else 'create'}
                                for app in self.apps]}

    def _delete_target(self, record):
        attrs = record['attributes']
        repo, environment = attrs['repository'], attrs['environment']
        if not record['owned'] or environment != self.environment:
            raise DeployError('GitHub environment ownership does not match.')
        repository = self._api('repos/' + repo)
        if not repository.get('permissions', {}).get('admin'):
            raise DeployError('GitHub environment management requires repository administrator access.')
        existing = next((e for e in self._pages(f'repos/{repo}/environments', 'environments')
                         if e['name'].lower() == environment.lower()), None)
        base = f'repos/{repo}/environments/{quote(environment, safe="")}'
        if existing:
            if str(existing['id']) != record['provider_id']:
                raise DeployError('GitHub environment ownership does not match.')
            variables = {v['name']: v['value'] for v in self._pages(base + '/variables', 'variables')}
            if variables.get('POCKETDEPLOY_DEPLOYMENT_ID') != self.state.deployment_id and not (variables.get('POCKETDEPLOY_DEPLOYMENT_ID') is None and self.state.get_meta('github-pending:' + repo, False)):
                raise DeployError('GitHub environment deployment ownership does not match.')
        return repo, environment, existing, base

    def _cleanup_repositories(self):
        repositories = [app['github'] for app in self.apps] + self.state.get_meta('github-delete-keys', []) + [
            r['attributes']['repository'] for r in self.state.resources() if r['kind'] == 'github-environment']
        for row in self.state.db.execute("SELECT key,value FROM meta WHERE key LIKE 'github-key:%'"):
            if json.loads(row['value']):
                repositories.append(row['key'][len('github-key:'):])
        return sorted(set(repositories))

    def _cleanup_paths(self):
        validate_local_paths(self.config, self.root, state_path=self.state.path)
        paths = []
        operator = local_path(self.root, self.config.get('ssh-private-key-file', '.ssh/id_ed25519'))
        server = local_path(self.root, self.config.get('ssh-host-private-key-file', '.ssh/host_ed25519'))
        reserved = {operator, server,
                    local_path(self.root, self.config.get('ssh-public-key-file', str(operator) + '.pub')),
                    local_path(self.root, self.config.get('ssh-host-public-key-file', str(server) + '.pub')),
                    local_path(self.root, self.config.get('ssh-known-hosts-file', '.ssh/known_hosts'))}
        state_path = local_path(self.root, self.config.get('state-file', '.colors.sqlite'))
        reserved.update({state_path, Path(str(state_path) + '.lock'),
                         Path(str(state_path) + '-journal'), Path(str(state_path) + '-wal'), Path(str(state_path) + '-shm'),
                         local_path(self.root, self.config.get('_file', 'colors.yml')),
                         local_path(self.root, '.envrc'), local_path(self.root, '.envrc.private')})
        for repo in self._cleanup_repositories():
            pair = self.key_paths({'github': repo})
            for path in pair:
                if path in reserved:
                    raise DeployError('GitHub key path overlaps SSH recovery authority.')
                if path.exists():
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                        raise DeployError('GitHub keys must be owned regular files without hardlinks.')
            paths.extend(pair)
        return paths

    def plan_delete(self):
        paths = self._cleanup_paths()
        actions = []
        for record in self.state.resources():
            if record['kind'] == 'github-environment':
                repo, environment, existing, _ = self._delete_target(record)
                actions.append({'resource': record['name'], 'repository': repo, 'environment': environment,
                                'action': 'delete' if existing else 'absent'})
        for repo in self._cleanup_repositories():
            if (self.state.get_meta('github-pending:' + repo, False)
                    and not self.state.get_resource('github:' + repo)):
                environments = self._pages(f'repos/{repo}/environments', 'environments')
                if any(e['name'].lower() == self.environment.lower() for e in environments):
                    raise DeployError('GitHub setup intent remains without verified environment deletion.')
        if any(path.exists() for path in paths):
            actions.append({'resource': 'github-deployment-keys', 'action': 'delete'})
        return actions

    def cleanup_keys(self):
        paths = self._cleanup_paths()
        existing = sum(path.exists() for path in paths)
        for path in paths:
            path.unlink(missing_ok=True)
        for repo in self._cleanup_repositories():
            self.state.set_meta('github-key:' + repo, None)
            self.state.set_meta('github-key-pending:' + repo, False)
        self.state.set_meta('github-delete-keys', [])
        return {'deleted_key_files': existing}

    def delete(self, operation_id):
        self.plan_delete()
        self.state.set_meta('github-delete-keys', self._cleanup_repositories())
        removed = []
        for record in self.state.resources():
            if record['kind'] != 'github-environment':
                continue
            repo, environment, existing, base = self._delete_target(record)
            pending_key = 'github-delete:' + repo
            step = self.state.get_meta(pending_key)
            if existing:
                step = step or self.state.intent(operation_id, 'github-environment-delete', {'repository': repo, 'environment': environment, 'id': record['provider_id']})
                self.state.set_meta(pending_key, step)
                self._api(base, 'DELETE')
                if any(str(e['id']) == record['provider_id'] for e in self._pages(f'repos/{repo}/environments', 'environments')):
                    raise DeployError('GitHub environment deletion could not be verified.')
            if step:
                self.state.complete(step, {'deleted': True, 'recovered': not bool(existing)})
                self.state.set_meta(pending_key, None)
            for old in self.state.db.execute("SELECT id,payload FROM steps WHERE step='github-environment-delete' AND status='pending'").fetchall():
                payload = json.loads(old['payload'])
                if payload.get('repository') == repo and payload.get('environment') == environment and payload.get('id', record['provider_id']) == record['provider_id']:
                    self.state.complete(old['id'], {'deleted': True, 'recovered': True})
            self.state.set_meta('github-pending:' + repo, False)
            self.state.remove_resource(record['name'])
            removed.append({'repository': repo, 'environment': environment})
        # Older interrupted/completed deletions could remove the resource row
        # but retain setup intent. Clear it only after another absence check.
        for repo in self._cleanup_repositories():
            if self.state.get_meta('github-pending:' + repo, False):
                environments = self._pages(f'repos/{repo}/environments', 'environments')
                if any(e['name'].lower() == self.environment.lower() for e in environments):
                    raise DeployError('GitHub setup intent remains without verified environment deletion.')
                self.state.set_meta('github-pending:' + repo, False)
        return {'deleted_environments': removed}

    def converge(self, connection, operation_id, rotate_keys=False):
        results = []
        for app in self.apps:
            repo = app['github']
            resource = 'github:' + repo
            base = f'repos/{repo}/environments/{quote(self.environment, safe="")}'
            existing = next((e for e in self._pages(f'repos/{repo}/environments', 'environments')
                             if e['name'].lower() == self.environment.lower()), None)
            owned = self.state.get_resource(resource)
            if existing and (not owned or str(existing['id']) != owned['provider_id']):
                raise DeployError('GitHub environment is unmanaged; explicit ownership recovery is required.')
            if not owned and not existing and self.state is not None and self.state.get_meta('github-pending:' + repo, False):
                raise DeployError('Uncertain GitHub environment creation; operator recovery is required.')
            if owned and not existing:
                raise DeployError('Recorded GitHub environment disappeared; operator recovery is required.')
            if existing:
                variables = {v['name']: v['value'] for v in self._pages(base + '/variables', 'variables')}
                if variables.get('POCKETDEPLOY_DEPLOYMENT_ID') != self.state.deployment_id and not (variables.get('POCKETDEPLOY_DEPLOYMENT_ID') is None and self.state.get_meta('github-pending:' + repo, False)):
                    raise DeployError('GitHub environment deployment ownership does not match.')
            pending = 'github-key-pending:' + repo
            if rotate_keys and not self.state.get_meta(pending, False):
                # Record intent before removing local authority. An interrupted
                # retry reuses the pending key instead of rotating it again.
                self._key(app)  # validate existing paths before unlinking
                self.state.set_meta(pending, 'replace')
            private, public = self._key(app)
            # Install and verify host authority before making a new CI target visible.
            self.host.install_github(connection, [{'app': app, 'public_key': public, 'repository': repo, 'preserve_existing_keys': True}])
            step = self.state.intent(operation_id, 'github-environment', {'repository': repo, 'environment': self.environment})
            self.state.set_meta('github-pending:' + repo, True)
            response = existing or self._api(base, 'PUT', {'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}})
            if existing and existing.get('deployment_branch_policy') != {'protected_branches': False, 'custom_branch_policies': True}:
                raise DeployError('GitHub environment branch protection changed; review it explicitly.')
            self.state.put_resource(resource, 'github-environment', str(response['id']), {'repository': repo, 'environment': self.environment})
            policies = self._pages(base + '/deployment-branch-policies', 'branch_policies')
            if any(p['name'] != 'main' or p.get('type', 'branch') != 'branch' for p in policies):
                raise DeployError('GitHub environment has unexpected branch policies; review them explicitly.')
            if not policies:
                self._api(base + '/deployment-branch-policies', 'POST', {'name': 'main', 'type': 'branch'})
            public_host = self.host.trusted_public(connection).split()
            values = {'POCKETDEPLOY_DEPLOYMENT_ID': self.state.deployment_id,
                      'POCKETDEPLOY_PROFILE': self.environment, 'SERVER_IP': connection['ip'],
                      'SERVER_USER': connection.get('user', 'ubuntu'),
                      'SSH_KNOWN_HOSTS': f"{connection['ip']} {public_host[0]} {public_host[1]}",
                      'SITE_URL': ('http' if app.get('disable_tls') else 'https') + '://' + app['host']}
            for name, value in values.items():
                run(['gh', 'variable', 'set', name, '--repo', repo, '--env', self.environment], input=value)
            run(['gh', 'secret', 'set', 'SSH_PRIVATE_KEY', '--repo', repo, '--env', self.environment], input=private.read_text())
            # Only retire old host authority after GitHub accepted the new
            # secret. Failure or a lost response leaves both keys usable;
            # retries republish the same pending credential.
            self.host.install_github(connection, [{'app': app, 'public_key': public,
                                                   'repository': repo, 'preserve_existing_keys': False}])
            self.state.set_meta(pending, False)
            result = {'repository': repo, 'environment': self.environment}
            self.state.complete(step, result)
            self.state.set_meta('github-pending:' + repo, False)
            results.append(result)
        return {'environments': results}
