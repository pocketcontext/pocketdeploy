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

    def delete(self, operation_id):
        removed = []
        for record in self.state.resources():
            if record['kind'] != 'github-environment':
                continue
            attributes = record['attributes']
            repo, environment = attributes['repository'], attributes['environment']
            if not record['owned'] or environment != self.environment:
                raise DeployError('GitHub environment ownership does not match.')
            existing = next((e for e in self._pages(f'repos/{repo}/environments', 'environments')
                             if e['name'].lower() == environment.lower()), None)
            if existing:
                if str(existing['id']) != record['provider_id']:
                    raise DeployError('GitHub environment ownership does not match.')
                base = f'repos/{repo}/environments/{quote(environment, safe="")}'
                variables = {v['name']: v['value'] for v in self._pages(base + '/variables', 'variables')}
                if variables.get('POCKETDEPLOY_DEPLOYMENT_ID') != self.state.deployment_id and not (variables.get('POCKETDEPLOY_DEPLOYMENT_ID') is None and self.state.get_meta('github-pending:' + repo, False)):
                    raise DeployError('GitHub environment deployment ownership does not match.')
                step = self.state.intent(operation_id, 'github-environment-delete', {'repository': repo, 'environment': environment})
                self._api(base, 'DELETE')
                if any(str(e['id']) == record['provider_id'] for e in self._pages(f'repos/{repo}/environments', 'environments')):
                    raise DeployError('GitHub environment deletion could not be verified.')
                self.state.complete(step, {'deleted': True})
            self.state.remove_resource(record['name'])
            removed.append({'repository': repo, 'environment': environment})
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
            public_host = self.host.hostpub.read_text().split()
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
