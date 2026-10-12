"""Hetzner CLI compute adapter. Raw provider output and tokens stay in memory."""
import ipaddress
import json
import os
import time

from .common import DeployError, run
from .digitalocean import DigitalOcean


class HCloud:
    roles = {'primary-ip': 'primary-ip', 'firewall': 'firewall', 'compute': 'server'}
    resource_kinds = frozenset('hcloud-' + kind for kind in roles.values())
    delete_pending_key = 'hcloud-delete-compute'

    def __init__(self, config, state=None):
        self.config, self.state = config, state

    def _call(self, args, payload=None, *, json_output=True):
        token = os.environ.get('COLORS_PAR_HCLOUD_TOKEN') or os.environ.get('HCLOUD_TOKEN')
        if not token:
            raise DeployError('Hetzner token is missing.')
        env = {k: v for k, v in os.environ.items() if not k.startswith('HCLOUD_')}
        env['HCLOUD_TOKEN'] = token
        command = ['hcloud', '--config', '/dev/null', '--endpoint', 'https://api.hetzner.cloud/v1',
                   '--http-timeout', '60s', *args]
        if json_output:
            command += ['--output', 'json']
        raw = run(command, input=payload, env=env, timeout=600)
        if not json_output:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise DeployError('Hetzner returned invalid JSON; output suppressed.') from None

    def _labels(self, role):
        return {'pocketdeploy': self.state.deployment_id, 'role': role}

    def _owned(self, item, role):
        return all(item.get('labels', {}).get(k) == v for k, v in self._labels(role).items())

    def _list(self, role):
        items = self._call([self.roles[role], 'list'])
        if not isinstance(items, list) or any(not isinstance(i, dict) or type(i.get('id')) is not int for i in items):
            raise DeployError('Hetzner returned invalid inventory.')
        return items

    def _find(self, role, allow_missing=False):
        items = self._list(role)
        record = self.state.get_resource(role)
        owned = [i for i in items if self._owned(i, role)]
        if len(owned) > 1:
            raise DeployError('Multiple Hetzner resources claim the deployment role.')
        if record:
            if not record['owned'] or record['kind'] != 'hcloud-' + self.roles[role]:
                raise DeployError('Recorded Hetzner ownership is invalid.')
            exact = next((i for i in items if str(i['id']) == record['provider_id']), None)
            if exact and not self._owned(exact, role):
                raise DeployError('Recorded Hetzner ownership changed.')
            if not exact and not (allow_missing and not owned):
                raise DeployError('Recorded Hetzner resource is missing; explicit recovery is required.')
        elif owned and not self.state.get_meta('hcloud-pending-' + role):
            raise DeployError('Unrecorded Hetzner resource requires checkpoint recovery; names never authorize adoption.')
        return owned[0] if owned else None

    @staticmethod
    def _lifecycle(item, role):
        return {'running': 'RUNNING', 'initializing': 'PROVISIONING', 'starting': 'PROVISIONING',
                'off': 'STOPPED', 'stopping': 'STOPPING', 'deleting': 'TERMINATING'}.get(item.get('status'), 'UNKNOWN') if role == 'compute' else 'RUNNING'

    def _remember(self, role, item):
        self.state.put_resource(role, 'hcloud-' + self.roles[role], str(item['id']),
                                {'lifecycle': self._lifecycle(item, role)}, owned=True)
        pending = self.state.get_meta('hcloud-pending-' + role)
        if pending:
            self.state.complete(pending['step'], {'id': str(item['id'])})
            self.state.set_meta('hcloud-pending-' + role, None)

    def _create(self, role, operation, args, payload=None):
        key = 'hcloud-pending-' + role
        if self.state.get_meta(key):
            raise DeployError('Hetzner creation outcome is uncertain; reconcile before retrying.')
        pending = {'step': self.state.intent(operation, 'create-' + role, {'role': role})}
        self.state.set_meta(key, pending)
        labels = [part for k, v in self._labels(role).items() for part in ('--label', k + '=' + v)]
        self._call([self.roles[role], 'create', '--name', self.config['profile'] + '-' + ('ipv4' if role == 'primary-ip' else self.roles[role]),
                    *labels, *args], payload)
        for _ in range(24):
            item = self._find(role)
            if item:
                self._remember(role, item)
                return item
            time.sleep(5)
        raise DeployError('Hetzner created resource is not visible; intent preserved.')

    def _rules(self):
        rules = []
        for source, ports in [('compute-ssh-sources', self.config.get('compute-ssh-ports', ['22'])),
                              ('compute-http-sources', ['80', '443'])]:
            if self.config.get(source, ['0.0.0.0/0']):
                rules.extend({'direction': 'in', 'protocol': 'tcp', 'port': port,
                              'source_ips': sorted(self.config.get(source, ['0.0.0.0/0']))} for port in ports)
        for rule in self.config.get('compute-ingress', []):
            rules.extend({'direction': 'in', 'protocol': rule['protocol'], 'port': port,
                          'source_ips': sorted(rule['sources'])} for port in rule['ports'])
        return rules

    @staticmethod
    def _canonical(rules):
        return sorted(json.dumps({k: v for k, v in r.items() if v not in (None, '', [])}, sort_keys=True)
                      for r in rules)

    def _rules_equal(self, item):
        return self._canonical(item.get('rules', [])) == self._canonical(self._rules())

    def _image(self):
        desired = self.config['hcloud-image']
        pinned = self.state.get_meta('hcloud-image') if self.state else None
        if pinned:
            if pinned['selector'] != desired:
                raise DeployError('Hetzner image selector changed; replacement is not supported.')
            return pinned['id']
        image = self._call(['image', 'describe', desired])
        if (not isinstance(image, dict) or type(image.get('id')) is not int
                or image.get('status') != 'available' or image.get('os_flavor') != 'ubuntu'
                or image.get('os_version') != '24.04'):
            raise DeployError('Hetzner requires an available Ubuntu 24.04 image.')
        return image['id']

    def _drift(self, server, firewall, primary):
        if not server:
            return []
        fields = []
        for field, actual in [('hcloud-server-type', server.get('server_type', {}).get('name')),
                              ('hcloud-location', (server.get('location') or server.get('datacenter', {}).get('location', {})).get('name'))]:
            if actual != self.config[field]:
                fields.append(field)
        if server.get('image', {}).get('id') != self._image():
            fields.append('hcloud-image')
        net = server.get('public_net', {})
        if (server.get('private_net') or server.get('volumes') or server.get('backup_window')
                or not primary or net.get('ipv4', {}).get('id') != primary['id']
                or net.get('ipv6') and net['ipv6'].get('id')
                or not firewall or {f['id'] for f in net.get('firewalls', [])} != {firewall['id']}):
            fields.append('network-or-storage')
        return fields

    def _check_primary(self, item, server):
        if item and (item.get('type') != 'ipv4' or item.get('auto_delete') is not False
                     or item.get('assignee_id') not in (None, 0, server['id'] if server else None)):
            raise DeployError('Hetzner primary IP lifecycle or assignment differs.')

    def _check_firewall(self, item, server):
        if not item:
            return
        expected = server['id'] if server else None
        for target in item.get('applied_to', []):
            if target.get('type') != 'server' or target.get('server', {}).get('id') != expected:
                raise DeployError('Hetzner firewall has external attachments.')

    def inspect(self):
        return {role: {'id': str(item['id']), 'state': self._lifecycle(item, role), 'owned': True} if item else None
                for role in self.roles for item in [self._find(role)]}

    def plan(self):
        self._image()
        if not self.state:
            return [{'resource': role, 'action': 'create'} for role in self.roles]
        if any(self.state.get_meta('hcloud-delete-' + r) for r in self.roles):
            raise DeployError('Hetzner deletion is pending; finish deletion first.')
        items = {r: self._find(r) for r in self.roles}
        self._check_primary(items['primary-ip'], items['compute'])
        self._check_firewall(items['firewall'], items['compute'])
        drift = self._drift(items['compute'], items['firewall'], items['primary-ip'])
        return [{'resource': r, 'action': 'blocked' if (r == 'compute' and drift or
                 self.state.get_meta('hcloud-pending-' + r) and not items[r]) else
                 'create' if not items[r] else 'update' if r == 'firewall' and not self._rules_equal(items[r]) else 'retain',
                 'changed_fields': drift if r == 'compute' else []} for r in self.roles]

    def converge(self, public_key, operation_id):
        plan = self.plan()
        if any(i['action'] == 'blocked' for i in plan):
            raise DeployError('Hetzner compute drift or uncertain creation blocks convergence.')
        image = self._image()
        if not self.state.get_meta('hcloud-image'):
            self.state.set_meta('hcloud-image', {'selector': self.config['hcloud-image'], 'id': image})
        primary = self._find('primary-ip') or self._create('primary-ip', operation_id,
                   ['--type', 'ipv4', '--location', self.config['hcloud-location'], '--auto-delete=false'])
        self._remember('primary-ip', primary)
        firewall = self._find('firewall') or self._create('firewall', operation_id,
                   ['--rules-file', '-'], json.dumps(self._rules()))
        self._remember('firewall', firewall)
        key = 'hcloud-pending-rules'
        pending = self.state.get_meta(key)
        if pending and (pending['id'] != firewall['id'] or pending['rules'] != self._rules()):
            raise DeployError('Pending Hetzner firewall update differs from desired configuration.')
        if not self._rules_equal(firewall):
            if pending:
                raise DeployError('Hetzner firewall update is uncertain; reconcile explicitly.')
            pending = {'id': firewall['id'], 'rules': self._rules(),
                       'step': self.state.intent(operation_id, 'firewall-rules', {'id': str(firewall['id'])})}
            self.state.set_meta(key, pending)
            self._call(['firewall', 'replace-rules', str(firewall['id']), '--rules-file', '-'],
                       json.dumps(self._rules()), json_output=False)
            if not self._rules_equal(self._find('firewall')):
                raise DeployError('Hetzner firewall update was not verified.')
        if pending:
            self.state.complete(pending['step'], {'verified': True})
            self.state.set_meta(key, None)
        server = self._find('compute') or self._create('compute', operation_id,
                 ['--type', self.config['hcloud-server-type'], '--location', self.config['hcloud-location'],
                  '--image', str(image), '--primary-ipv4', str(primary['id']), '--without-ipv6',
                  '--firewall', str(firewall['id']), '--enable-protection', 'delete,rebuild',
                  '--user-data-from-file', '-'], DigitalOcean._user_data(self, public_key))
        self._remember('compute', server)
        if self._drift(server, firewall, primary):
            raise DeployError('Created Hetzner server settings differ; state preserved.')
        for _ in range(120):
            server = self._find('compute')
            if server['status'] == 'running':
                self._protect(server, operation_id)
                return self.connection()
            if server['status'] not in ('initializing', 'starting'):
                raise DeployError('Hetzner server is not starting.')
            time.sleep(5)
        raise DeployError('Hetzner startup timed out; state preserved.')

    def _protect(self, server, operation_id):
        key = 'hcloud-pending-protection'
        pending = self.state.get_meta(key)
        if pending and pending['id'] != str(server['id']):
            raise DeployError('Hetzner pending protection identity differs.')
        if not all(server.get('protection', {}).get(k) is True for k in ('delete', 'rebuild')):
            if not pending:
                pending = {'id': str(server['id']), 'step': self.state.intent(operation_id, 'protect-compute', {'id': str(server['id'])})}
                self.state.set_meta(key, pending)
            self._call(['server', 'enable-protection', str(server['id']), 'delete', 'rebuild'], json_output=False)
            server = self._find('compute')
            if not all(server.get('protection', {}).get(k) is True for k in ('delete', 'rebuild')):
                raise DeployError('Hetzner protection was not verified.')
        if pending:
            self.state.complete(pending['step'], {'verified': True})
            self.state.set_meta(key, None)

    def connection(self):
        server = self._find('compute')
        if not server:
            raise DeployError('No owned Hetzner server.')
        address = server.get('public_net', {}).get('ipv4', {}).get('ip')
        try:
            ipaddress.IPv4Address(address)
        except (ValueError, TypeError):
            raise DeployError('Hetzner returned invalid IPv4.') from None
        return {'instance_id': str(server['id']), 'ip': address, 'user': self.config.get('ssh-user', 'ubuntu')}

    def plan_delete(self):
        if any(self.state.get_meta('hcloud-pending-' + r) for r in self.roles):
            raise DeployError('Reconcile pending Hetzner creation before deletion.')
        items = {r: self._find(r, allow_missing=bool(self.state.get_meta('hcloud-delete-' + r))) for r in self.roles}
        self._check_primary(items['primary-ip'], items['compute'])
        self._check_firewall(items['firewall'], items['compute'])
        server = items['compute']
        net = server.get('public_net', {}) if server else {}
        if server and (server.get('volumes') or server.get('backup_window') or server.get('private_net')
                       or not items['primary-ip'] or net.get('ipv4', {}).get('id') != items['primary-ip']['id']
                       or net.get('ipv6') and net['ipv6'].get('id')
                       or not items['firewall'] or {f['id'] for f in net.get('firewalls', [])} != {items['firewall']['id']}):
            raise DeployError('Hetzner server has external storage or networking; reconcile before deletion.')
        result = []
        for role, item in items.items():
            record = self.state.get_resource(role)
            pending = self.state.get_meta('hcloud-delete-' + role)
            if pending and (not record or pending['id'] != record['provider_id']):
                raise DeployError('Hetzner deletion identity differs.')
            result.append({'resource': role, 'action': 'delete' if item else 'absent',
                           'id': str(item['id']) if item else record['provider_id'] if record else None,
                           'state': self._lifecycle(item, role) if item else None})
        return result

    def delete(self, operation_id):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deployment destruction is protected.')
        self.plan_delete()
        for role in ('compute', 'firewall', 'primary-ip'):
            key = 'hcloud-delete-' + role
            pending = self.state.get_meta(key)
            item = self._find(role, allow_missing=bool(pending))
            if not item and not pending:
                continue
            if not pending:
                pending = {'id': str(item['id']), 'step': self.state.intent(operation_id, 'delete-' + role, {'id': str(item['id'])})}
                self.state.set_meta(key, pending)
            if item:
                # These setters/deletes are idempotent on the exact verified identity.
                if role == 'compute':
                    self._call(['server', 'disable-protection', pending['id'], 'delete', 'rebuild'], json_output=False)
                elif role == 'primary-ip' and item.get('protection', {}).get('delete'):
                    self._call(['primary-ip', 'disable-protection', pending['id'], 'delete'], json_output=False)
                self._call([self.roles[role], 'delete', pending['id']], json_output=False)
            if self._find(role, allow_missing=True):
                raise DeployError('Hetzner deletion is pending; retry after it settles.')
            for update in (['hcloud-pending-protection'] if role == 'compute' else
                           ['hcloud-pending-rules'] if role == 'firewall' else []):
                unfinished = self.state.get_meta(update)
                if unfinished:
                    self.state.complete(unfinished['step'], {'deleted': True})
                    self.state.set_meta(update, None)
            self.state.complete_delete(role, pending['step'], key)
        return {'deleted': True}

    def adopt(self, instance_id, operation_id):
        raise DeployError('Hetzner adoption is unsupported; restore the original controller checkpoint.')
