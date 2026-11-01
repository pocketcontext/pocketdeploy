"""Explicit OCI signing infrastructure. No application deletion owns its lifetime."""
import base64
import hashlib
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.parse import urlsplit

from .common import DeployError, local_path, run
from .oci import OCI

KINDS = frozenset({'oci-kms-vault', 'oci-kms-key', 'oci-kms-group', 'oci-kms-user',
                   'oci-kms-policy', 'oci-kms-membership', 'oci-kms-api-key'})
FILES = ['.ssh/kms-runtime.pem', '.ssh/kms-runtime.env']


def validate(config):
    value = config.get('oci-kms')
    if value is None:
        return
    if (config.get('provider-compute') != 'oci' or not isinstance(value, dict)
            or set(value) != {'name', 'tenancy-id', 'binding-prefix'}
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', str(value.get('name', '')))
            or not re.fullmatch(r'ocid1\.tenancy\.[a-z0-9.]+', str(value.get('tenancy-id', '')))
            or not re.fullmatch(r'APP_[A-Z][A-Z0-9_]{0,60}', str(value.get('binding-prefix', '')))):
        raise DeployError('oci-kms requires name, tenancy-id and APP_ binding-prefix on OCI.')
    if not config.get('oci-region'):
        raise DeployError('oci-kms requires an explicit oci-region.')
    for name in FILES:
        local_path(config['_root'], name)


class KMS(OCI):
    def __init__(self, config, state):
        validate(config)
        if not config.get('oci-kms') or not state:
            raise DeployError('KMS commands require oci-kms configuration and existing deployment state.')
        super().__init__(config, state)
        self.settings = config['oci-kms']
        self.compartment = config['oci-compartment-id']
        self.tenancy = self.settings['tenancy-id']
        self.vault = None
        previous = state.get_meta('kms-scope')
        expected = {**self.settings, 'compartment': self.compartment, 'region': config['oci-region']}
        if previous is not None and previous != expected:
            raise DeployError('KMS scope changed; restore its recorded configuration before recovery.')
        self.scope = expected

    def _endpoint(self, field):
        value = self.vault.get(field) if self.vault else None
        parsed = urlsplit(value or '')
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9-]+\.kms\.[a-z0-9-]+\.oraclecloud\.com', parsed.hostname or '')):
            raise DeployError('OCI KMS returned an invalid service endpoint.')
        return value.rstrip('/')

    def _command(self, role):
        return ('kms', 'management', role) if role in ('vault', 'key') else ('iam', role)

    def _options(self, role):
        return ['--endpoint', self._endpoint('management-endpoint')] if role == 'key' else []

    def _list_role(self, role):
        return self._list(*self._command(role), 'list', '--compartment-id',
                          self.tenancy if role in ('user', 'group') else self.compartment,
                          '--all', *self._options(role))

    def _find_role(self, role):
        name = 'kms-' + role
        records = self._list_role(role)
        recorded = self.state.get_resource(name)
        matches = [r for r in records if self._owned(r, name)]
        if len(matches) > 1:
            raise DeployError('Multiple KMS resources claim a role; reconcile explicitly.')
        item = matches[0] if matches else None
        if recorded and (not recorded['owned'] or not item or item.get('id') != recorded['provider_id']):
            raise DeployError('Recorded KMS resource is missing or ownership changed; restore explicitly.')
        if item:
            self._verify(role, item)
        return item

    def _verify(self, role, item):
        if not isinstance(item.get('id'), str) or not re.fullmatch(r'ocid1\.[a-zA-Z0-9._-]+', item['id']):
            raise DeployError('Invalid OCI KMS resource identity.')
        if item.get('compartment-id') != (self.tenancy if role in ('user', 'group') else self.compartment):
            raise DeployError('KMS resource compartment changed.')
        if role == 'vault':
            if item.get('vault-type') != 'DEFAULT':
                raise DeployError('Only standard shared DEFAULT KMS vaults are supported.')
            self.vault = item
            self._endpoint('management-endpoint')
            self._endpoint('crypto-endpoint')
        if role == 'key' and (item.get('protection-mode') != 'HSM'
                or item.get('key-shape') != {'algorithm': 'RSA', 'length': 384, 'curve-id': None}
                and item.get('key-shape') != {'algorithm': 'RSA', 'length': 384}):
            raise DeployError('KMS signing key must be RSA3072 with HSM protection.')
        if item.get('lifecycle-state') != ('ENABLED' if role == 'key' else 'ACTIVE'):
            raise DeployError('KMS resource is not ready; retain intent and retry after provisioning completes.')

    def _remember_role(self, role, item):
        self.state.put_resource('kms-' + role, 'oci-kms-' + role, item['id'], {}, owned=True)

    def _ensure(self, role, operation, payload):
        item = self._find_role(role)
        pending_name = 'kms-pending-' + role
        pending = self.state.get_meta(pending_name)
        if item:
            if pending and (item.get('freeform-tags') or {}).get('pocketdeploy-operation') != pending['operation']:
                raise DeployError('KMS recovery resource does not match pending creation intent.')
            self._remember_role(role, item)
            if pending:
                self.state.complete(pending['step'], {'id': item['id'], 'recovered': True})
                self.state.set_meta(pending_name, None)
            return item
        if pending:
            raise DeployError('KMS creation outcome is uncertain; reconcile tagged resources before retrying.')
        step = self.state.intent(operation, 'kms-create-' + role, {'role': role})
        self.state.set_meta(pending_name, {'step': step, 'operation': operation})
        data = {**payload, 'freeformTags': self._tags('kms-' + role, operation)}
        # Reads below establish readiness and ownership independently of create output.
        self._call(*self._command(role), 'create', *self._options(role), body=data)
        item = self._find_role(role)
        if not item:
            raise DeployError('KMS creation not yet visible; pending intent retained.')
        self._remember_role(role, item)
        self.state.complete(step, {'id': item['id']})
        self.state.set_meta(pending_name, None)
        return item

    def _statements(self, group, key):
        return [f"Allow group id {group['id']} to use keys in compartment id {self.compartment} where all {{target.key.id = '{key['id']}', any {{request.permission = 'KEY_SIGN', request.permission = 'KEY_READ'}}}}"]

    def plan(self):
        vault = self._find_role('vault')
        actions = [{'resource': 'kms-vault', 'action': 'retain' if vault else 'create'}]
        for role in ('key', 'group', 'user', 'policy'):
            item = self._find_role(role) if role != 'key' or vault else None
            actions.append({'resource': 'kms-' + role, 'action': 'retain' if item else 'create'})
        return {'profile': self.config['profile'], 'actions': actions,
                'vault_type': 'DEFAULT', 'key_shape': 'RSA3072', 'protection': 'HSM',
                'retained_on_delete': True}

    def _membership(self, user, group, operation):
        def read():
            rows = self._list('iam', 'group', 'list-users', '--group-id', group['id'], '--compartment-id', self.tenancy, '--all')
            if any(row.get('id') != user['id'] for row in rows):
                raise DeployError('Runtime KMS group contains an unexpected user.')
            memberships = self._list('iam', 'user', 'list-groups', '--user-id', user['id'], '--compartment-id', self.tenancy, '--all')
            if any(row.get('id') != group['id'] for row in memberships):
                raise DeployError('Runtime KMS user belongs to an unexpected group.')
            return bool(rows) and bool(memberships)
        pending = self.state.get_meta('kms-pending-membership')
        if not read():
            if pending:
                raise DeployError('KMS group membership outcome uncertain; inspect before retrying.')
            step = self.state.intent(operation, 'kms-membership', {'user': user['id'], 'group': group['id']})
            self.state.set_meta('kms-pending-membership', {'step': step})
            self._call('iam', 'group', 'add-user', body={'userId': user['id'], 'groupId': group['id']})
            pending = {'step': step}
            if not read():
                raise DeployError('KMS membership not yet visible; retry after IAM propagation.')
        if pending:
            self.state.complete(pending['step'], {'verified': True})
            self.state.set_meta('kms-pending-membership', None)
        self.state.put_resource('kms-membership', 'oci-kms-membership', user['id'] + '/' + group['id'], {})

    def _capabilities(self, user, operation):
        desired = {'can-use-api-keys': True, 'can-use-auth-tokens': False,
                   'can-use-console-password': False, 'can-use-customer-secret-keys': False,
                   'can-use-db-credentials': False, 'can-use-o-auth2-client-credentials': False,
                   'can-use-smtp-credentials': False}
        def valid(item):
            return all(item.get('capabilities', {}).get(k) is v for k, v in desired.items())
        if valid(user):
            pending = self.state.get_meta('kms-pending-capabilities')
            if pending:
                self.state.complete(pending['step'], {'verified': True, 'recovered': True})
                self.state.set_meta('kms-pending-capabilities', None)
            return
        pending = self.state.get_meta('kms-pending-capabilities')
        if pending:
            raise DeployError('KMS capabilities update uncertain; verify user capabilities before retrying.')
        step = self.state.intent(operation, 'kms-capabilities', {'user': user['id']})
        self.state.set_meta('kms-pending-capabilities', {'step': step})
        payload = {'userId': user['id'], 'canUseApiKeys': True, 'canUseAuthTokens': False,
                   'canUseConsolePassword': False, 'canUseCustomerSecretKeys': False,
                   'canUseDbCredentials': False, 'canUseOAuth2ClientCredentials': False,
                   'canUseSmtpCredentials': False}
        self._call('iam', 'user', 'update-user-capabilities', body=payload)
        if not valid(self._find_role('user')):
            raise DeployError('KMS runtime capabilities not verified.')
        self.state.complete(step, {'verified': True})
        self.state.set_meta('kms-pending-capabilities', None)

    def _private_key(self):
        path = local_path(self.config['_root'], FILES[0])
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if not path.exists():
            if self.state.get_resource('kms-api-key') or self.state.get_meta('kms-pending-api-key'):
                raise DeployError('KMS API credential is missing; restore its private checkpoint.')
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            os.close(fd)
            try:
                run(['openssl', 'genrsa', '-out', temporary, '3072'])
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise DeployError('KMS API private key requires mode 0600.')
        public = run(['openssl', 'rsa', '-in', str(path), '-pubout'])
        der = base64.b64decode(''.join(line for line in public.splitlines() if not line.startswith('---')), validate=True)
        fingerprint = ':'.join(re.findall('..', hashlib.md5(der, usedforsecurity=False).hexdigest()))
        return path, public, fingerprint

    def _api_key(self, user, operation):
        path, public, fingerprint = self._private_key()
        old = self.state.get_resource('kms-api-key')
        if old and old['provider_id'] != user['id'] + '/' + fingerprint:
            raise DeployError('KMS runtime API key changed; restore the private checkpoint.')
        def present():
            rows = self._list('iam', 'user', 'api-key', 'list', '--user-id', user['id'], '--all')
            if any(row.get('fingerprint') != fingerprint for row in rows):
                raise DeployError('KMS runtime user has an unexpected API key.')
            return any(row.get('fingerprint') == fingerprint and row.get('lifecycle-state') == 'ACTIVE' for row in rows)
        pending = self.state.get_meta('kms-pending-api-key')
        if not present():
            if old or pending:
                raise DeployError('KMS runtime credential absent or pending; inspect before retrying.')
            step = self.state.intent(operation, 'kms-upload-api-key', {'user': user['id'], 'fingerprint': fingerprint})
            self.state.set_meta('kms-pending-api-key', {'step': step})
            self._call('iam', 'user', 'api-key', 'upload', body={'userId': user['id'], 'key': public})
            pending = {'step': step}
            if not present():
                raise DeployError('KMS API key not active yet; retry after IAM propagation.')
        if pending:
            self.state.complete(pending['step'], {'verified': True})
            self.state.set_meta('kms-pending-api-key', None)
        self.state.put_resource('kms-api-key', 'oci-kms-api-key', user['id'] + '/' + fingerprint, {})
        return path, fingerprint

    def converge(self, operation):
        self.state.set_meta('kms-scope', self.scope)
        name = self.settings['name']
        self.vault = self._ensure('vault', operation, {'compartmentId': self.compartment,
            'displayName': name, 'vaultType': 'DEFAULT'})
        key = self._ensure('key', operation, {'compartmentId': self.compartment,
            'displayName': name + '-signing', 'protectionMode': 'HSM',
            'keyShape': {'algorithm': 'RSA', 'length': 384}})
        group = self._ensure('group', operation, {'compartmentId': self.tenancy,
            'name': name + '-signer', 'description': 'Dedicated PocketDeploy KMS signer'})
        user = self._ensure('user', operation, {'compartmentId': self.tenancy,
            'name': name + '-signer', 'description': 'Dedicated PocketDeploy KMS signer'})
        policy = self._ensure('policy', operation, {'compartmentId': self.compartment,
            'name': name + '-signer', 'description': 'Sign and read one PocketDeploy key only',
            'statements': self._statements(group, key)})
        if policy.get('statements') != self._statements(group, key):
            raise DeployError('KMS runtime policy drifted; refusing to broaden or overwrite it.')
        self._capabilities(user, operation)
        self._membership(user, group, operation)
        path, fingerprint = self._api_key(user, operation)
        version = key.get('current-key-version')
        if not isinstance(version, str) or not re.fullmatch(r'ocid1\.keyversion\.[a-zA-Z0-9._-]+', version):
            raise DeployError('KMS key lacks a valid current version.')
        previous = self.state.get_meta('kms-signing-version')
        approved = self.state.get_meta('kms-next-version')
        if previous and version not in (previous, approved):
            raise DeployError('KMS key version changed outside the recorded rotation; review explicitly before adoption.')
        bindings = {'OCI_TENANCY_ID': self.tenancy, 'OCI_USER_ID': user['id'],
                    'OCI_FINGERPRINT': fingerprint, 'OCI_REGION': self.config['oci-region'],
                    'OCI_PRIVATE_KEY': path.read_text(), 'KMS_KEY_ID': key['id'],
                    'KMS_KEY_VERSION_ID': version, 'KMS_CRYPTO_ENDPOINT': self._endpoint('crypto-endpoint'),
                    'KMS_MANAGEMENT_ENDPOINT': self._endpoint('management-endpoint')}
        destination = local_path(self.config['_root'], FILES[1])
        fd, temporary = tempfile.mkstemp(dir=destination.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                for field, value in bindings.items():
                    output.write('export COLORS_PAR_' + self.settings['binding-prefix'] + '_' + field + '=' + shlex.quote(value) + '\n')
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.state.set_meta('kms-signing-version', version)
        self.state.set_meta('kms-next-version', None)
        return {'profile': self.config['profile'], 'vault_id': self.vault['id'], 'key_id': key['id'],
                'key_version_id': version, 'bindings_file': FILES[1], 'algorithm': 'RS256',
                'retained_on_delete': True}

    def rotate(self, operation):
        self.vault = self._find_role('vault')
        if not self.vault:
            raise DeployError('KMS vault must exist before rotation.')
        key = self._find_role('key')
        if not key or not self.state.get_resource('kms-key'):
            raise DeployError('KMS rotation requires a recorded owned key.')
        pending = self.state.get_meta('kms-pending-rotation')
        if pending:
            raise DeployError('KMS rotation has an uncertain outcome; reconcile versions explicitly, never repeat automatically.')
        before = key['current-key-version']
        step = self.state.intent(operation, 'kms-rotate', {'key': key['id'], 'previous': before})
        self.state.set_meta('kms-pending-rotation', {'step': step, 'previous': before})
        version = self._call('kms', 'management', 'key-version', 'create',
                             '--endpoint', self._endpoint('management-endpoint'), body={'keyId': key['id']})
        observed = self._find_role('key')
        if (not isinstance(version, dict) or not version.get('id')
                or version['id'] == before or observed['current-key-version'] != version['id']):
            raise DeployError('KMS rotation not verified; pending intent retained.')
        self.state.set_meta('kms-next-version', version['id'])
        self.state.complete(step, {'version': version['id'], 'previous': before})
        self.state.set_meta('kms-pending-rotation', None)
        return {'previous_version_id': before, 'key_version_id': version['id'],
                'next_step': 'Publish old and new public keys before selecting the new signing version; run kms-converge to export bindings.'}

    def resolve_rotation(self, version_id):
        if not re.fullmatch(r'ocid1\.keyversion\.[a-zA-Z0-9.]+', version_id or ''):
            raise DeployError('Rotation recovery requires an exact key-version OCID.')
        pending = self.state.get_meta('kms-pending-rotation')
        if not pending:
            raise DeployError('No pending KMS rotation exists.')
        self.vault = self._find_role('vault')
        key = self._find_role('key') if self.vault else None
        if not key or key.get('current-key-version') != version_id or version_id == pending['previous']:
            raise DeployError('Selected version is not the observed rotated key version.')
        version = self._call('kms', 'management', 'key-version', 'get',
                             '--endpoint', self._endpoint('management-endpoint'),
                             '--key-id', key['id'], '--key-version-id', version_id)
        if not isinstance(version, dict) or version.get('id') != version_id or version.get('key-id') != key['id']:
            raise DeployError('Selected key-version ownership could not be verified.')
        self.state.set_meta('kms-next-version', version_id)
        self.state.complete(pending['step'], {'version': version_id, 'previous': pending['previous'], 'recovered': True})
        self.state.set_meta('kms-pending-rotation', None)
        return {'key_version_id': version_id, 'recovered': True}
