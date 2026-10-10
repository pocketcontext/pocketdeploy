"""DigitalOcean HTTPS adapter; responses, tokens and bootstrap data stay private.

A firewall has no labels: its UUID-derived name and exact deployment-tag selector
establish its binding. Droplets carry that unique deployment tag; their resource kind defines the role.
"""
import http.client
import ipaddress
import json
import os
import time
from urllib.parse import quote

from .common import DeployError


class DigitalOcean:
    resource_kinds = {'digitalocean-droplet', 'digitalocean-firewall', 'digitalocean-tag'}
    delete_pending_key = 'digitalocean-delete-compute'

    def __init__(self, config, state=None):
        self.config, self.state = config, state

    def _call(self, method, path, body=None):
        token = os.environ.get(self.config.get('digitalocean-token-env', 'COLORS_PAR_DIGITALOCEAN_ACCESS_TOKEN'))
        if not token:
            raise DeployError('DigitalOcean access token is missing.')
        connection = http.client.HTTPSConnection('api.digitalocean.com', timeout=60)
        try:
            connection.request(method, '/v2/' + path, body=json.dumps(body) if body is not None else None,
                               headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise DeployError('DigitalOcean request failed (HTTP %d); response suppressed.' % response.status)
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise DeployError('DigitalOcean response exceeded the safe size limit.')
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except DeployError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise DeployError('DigitalOcean request failed; private response suppressed.') from None
        finally:
            connection.close()

    def _list(self, endpoint, key=None):
        result = []
        for page in range(1, 10001):
            data = self._call('GET', endpoint + '?per_page=200&page=' + str(page))
            items = data.get(key or endpoint)
            if not isinstance(items, list) or any(not isinstance(i, dict) or not i.get('name' if endpoint == 'tags' else 'slug' if endpoint == 'sizes' else 'id') for i in items):
                raise DeployError('DigitalOcean returned invalid resource inventory.')
            result.extend(items)
            # Never follow provider URLs with credentials. Page through the fixed endpoint.
            if not data.get('links', {}).get('pages', {}).get('next'):
                return result
        raise DeployError('DigitalOcean inventory pagination exceeded its bound.')

    def _scope(self, network=False):
        account = self._call('GET', 'account').get('account', {})
        if account.get('uuid') != self.config['digitalocean-account-id'] or account.get('status') != 'active':
            raise DeployError('DigitalOcean account identity or active status does not match configuration.')
        if network:
            vpc = self._call('GET', 'vpcs/' + quote(self.config['digitalocean-vpc-id'], safe='')).get('vpc', {})
            if vpc.get('id') != self.config['digitalocean-vpc-id'] or vpc.get('region') != self.config['digitalocean-region']:
                raise DeployError('DigitalOcean VPC does not match the configured region.')

    def _tag(self):
        if not self.state:
            raise DeployError('Deployment state is required for DigitalOcean ownership.')
        return 'pocketdeploy-' + self.state.deployment_id

    def _owned(self, item, role):
        if role == 'compute':
            return self._tag() in (item.get('tags') or [])
        return (item.get('name') == self._tag() + '-firewall'
                and set(item.get('tags') or []) == {self._tag()}
                and not item.get('droplet_ids'))

    def _tag_record(self):
        matches = [item for item in self._list('tags') if item.get('name') == self._tag()]
        if len(matches) > 1:
            raise DeployError('DigitalOcean deployment tag is ambiguous.')
        return matches[0] if matches else None

    def _check_tag_associations(self, item):
        resources = item.get('resources')
        if not isinstance(resources, dict) or not isinstance(resources.get('count'), int):
            raise DeployError('DigitalOcean tag resource associations could not be verified.')
        compute = self._find('compute', allow_missing=bool(self.state.get_meta(self.delete_pending_key)))
        expected = 1 if compute else 0
        if resources['count'] != expected or resources.get('droplets', {}).get('count', 0) != expected:
            raise DeployError('DigitalOcean deployment tag has external resource associations.')

    def _ensure_tag(self, operation_id):
        key = 'digitalocean-pending-tag'
        item = self._tag_record()
        record = self.state.get_resource('deployment-tag')
        pending = self.state.get_meta(key)
        if record and (not record['owned'] or record['provider_id'] != self._tag()):
            raise DeployError('DigitalOcean tag ownership differs.')
        if not item:
            if record:
                raise DeployError('Recorded DigitalOcean tag is missing; explicit recovery is required.')
            if pending:
                raise DeployError('DigitalOcean tag creation has an uncertain outcome; reconcile explicitly.')
            pending = {'step': self.state.intent(operation_id, 'create-deployment-tag', {'name': self._tag()})}
            self.state.set_meta(key, pending)
            self._call('POST', 'tags', {'name': self._tag()})
            item = self._tag_record()
            if not item:
                raise DeployError('DigitalOcean tag creation was not verified; intent preserved.')
        elif not record and not pending:
            raise DeployError('Existing DigitalOcean tag needs recorded ownership; recover its checkpoint.')
        self._check_tag_associations(item)
        self.state.put_resource('deployment-tag', 'digitalocean-tag', self._tag(), {}, owned=True)
        if pending:
            self.state.complete(pending['step'], {'created': True})
            self.state.set_meta(key, None)

    def _tag_deletion_target(self):
        record = self.state.get_resource('deployment-tag')
        pending_create = self.state.get_meta('digitalocean-pending-tag')
        if pending_create:
            raise DeployError('Reconcile pending DigitalOcean tag creation before deletion.')
        if not record:
            return None
        if not record['owned'] or record['provider_id'] != self._tag():
            raise DeployError('DigitalOcean tag deletion requires recorded ownership.')
        item = self._tag_record()
        pending = self.state.get_meta('digitalocean-delete-tag')
        if pending and pending.get('id') != record['provider_id']:
            raise DeployError('DigitalOcean pending tag deletion identity differs.')
        if not item and not pending:
            raise DeployError('Recorded DigitalOcean tag is missing; reconcile explicitly.')
        if item:
            self._check_tag_associations(item)
        return record

    def _find(self, role, allow_missing=False):
        items = self._list('droplets' if role == 'compute' else 'firewalls')
        if role == 'firewall' and any(i.get('name') == self._tag() + '-firewall' and not self._owned(i, role) for i in items):
            raise DeployError('DigitalOcean firewall ownership or target attachment changed.')
        owned = [i for i in items if self._owned(i, role)]
        record = self.state.get_resource(role) if self.state else None
        if record:
            item = next((i for i in items if str(i['id']) == record['provider_id']), None)
            if not record['owned'] or record['kind'] not in self.resource_kinds:
                raise DeployError('Recorded DigitalOcean ownership is invalid.')
            if item and not self._owned(item, role):
                raise DeployError('Recorded DigitalOcean resource ownership changed.')
            if item is None and not (allow_missing and not owned):
                raise DeployError('Recorded DigitalOcean resource is missing; explicit recovery is required.')
        if len(owned) > 1:
            raise DeployError('Multiple DigitalOcean resources claim one deployment role.')
        if role == 'firewall' and owned and not record and not self._rules_equal(owned[0]):
            raise DeployError('Unrecorded DigitalOcean firewall rules do not match deployment ownership.')
        return owned[0] if owned else None

    @staticmethod
    def _lifecycle(item, role):
        if role == 'firewall':
            return {'succeeded': 'RUNNING', 'waiting': 'PROVISIONING', 'failed': 'FAILED'}.get(item.get('status'), 'UNKNOWN')
        return {'active': 'RUNNING', 'new': 'PROVISIONING', 'off': 'STOPPED', 'archive': 'STOPPED'}.get(item.get('status'), 'UNKNOWN')

    def _remember(self, role, item):
        attributes = {'lifecycle': self._lifecycle(item, role)}
        if role == 'compute':
            attributes.update(image_id=item.get('image', {}).get('id'), disk=item.get('disk'),
                              desired_image=str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64')))
        self.state.put_resource(role, 'digitalocean-droplet' if role == 'compute' else 'digitalocean-firewall',
                                str(item['id']), attributes, owned=True)

    def _recover(self, role, item):
        pending = self.state.get_meta('digitalocean-pending-' + role)
        self._remember(role, item)
        if pending:
            self.state.complete(pending['step'], {'id': str(item['id']), 'recovered': True})
            self.state.set_meta('digitalocean-pending-' + role, None)

    def _create(self, role, operation_id, body):
        key = 'digitalocean-pending-' + role
        if self.state.get_meta(key):
            raise DeployError('DigitalOcean create has an uncertain outcome; reconcile before retrying.')
        step = self.state.intent(operation_id, 'create-' + role, {'role': role})
        self.state.set_meta(key, {'step': step})
        endpoint, singular = ('droplets', 'droplet') if role == 'compute' else ('firewalls', 'firewall')
        item = self._call('POST', endpoint, body).get(singular)
        if not isinstance(item, dict) or not item.get('id') or not self._owned(item, role):
            raise DeployError('DigitalOcean creation identity was not verified; intent preserved.')
        self._recover(role, item)
        # A successful create receipt can precede visibility in inventory. Only
        # this fresh receipt permits bounded missing-resource polling; existing
        # recorded resources still fail closed when absent on subsequent runs.
        for attempt in range(12):
            visible = self._find(role, allow_missing=True)
            if visible:
                return visible
            if attempt < 11:
                time.sleep(5)
        raise DeployError('DigitalOcean created resource is not yet visible; identity preserved, retry after it settles.')

    def _rules(self):
        inbound = []
        for key, ports in [('compute-ssh-sources', ('22',)), ('compute-http-sources', ('80', '443'))]:
            addresses = sorted({str(ipaddress.ip_network(cidr, strict=False)) for cidr in self.config.get(key, ['0.0.0.0/0'])})
            if addresses:
                inbound.extend({'protocol': 'tcp', 'ports': port, 'sources': {'addresses': addresses}} for port in ports)
        outbound = [{'protocol': protocol, **({'ports': 'all'} if protocol != 'icmp' else {}),
                     'destinations': {'addresses': ['0.0.0.0/0', '::/0']}} for protocol in ('tcp', 'udp', 'icmp')]
        return {'inbound_rules': inbound, 'outbound_rules': outbound}

    @staticmethod
    def _canonical(rules):
        # The API expands default allow actions, empty selectors and all-port
        # spellings. Preserve deny/unknown actions and actual port restrictions.
        def clean(value):
            if isinstance(value, dict):
                value = dict(value)
                if value.get('protocol') in ('tcp', 'udp', 'icmp'):
                    if value.get('action') == 'allow':
                        value.pop('action')
                    if value['protocol'] in ('tcp', 'udp') and value.get('ports') in ('all', '0', '1-65535'):
                        value['ports'] = '0'
                    elif value['protocol'] == 'icmp' and value.get('ports') in (None, '', '0'):
                        value.pop('ports', None)
                return {k: clean(v) for k, v in value.items() if v not in (None, [], {}, '')}
            if isinstance(value, list):
                return sorted((clean(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
            return value
        return json.dumps(clean(rules), sort_keys=True)

    def _rules_equal(self, firewall):
        return self._canonical({k: firewall.get(k, []) for k in self._rules()}) == self._canonical(self._rules())

    def _firewall_ready(self, item):
        return item.get('status') == 'succeeded' and not item.get('pending_changes')

    def _wait_firewall_rules(self, target):
        # Successful writes apply asynchronously. Observe only: never replay a
        # write while its durable outcome is still pending.
        for attempt in range(12):
            item = self._find('firewall')
            if (item and self._firewall_ready(item)
                    and self._canonical({k: item.get(k, []) for k in target}) == self._canonical(target)):
                return item
            if attempt < 11:
                time.sleep(5)
        return None

    def _reconcile_rules(self, item, operation_id):
        key = 'digitalocean-pending-firewall-rules'
        desired = self._rules()
        pending = self.state.get_meta(key)
        if pending and (pending['id'] != str(item['id']) or pending['desired'] != desired):
            raise DeployError('Pending DigitalOcean firewall update differs; restore its desired configuration.')
        def equal(target):
            return self._canonical({k: item.get(k, []) for k in desired}) == self._canonical(target)
        if equal(desired) and self._firewall_ready(item):
            if pending:
                self.state.complete(pending['step'], {'verified': True})
                self.state.set_meta(key, None)
            return
        if pending and (not equal(pending.get('target', desired)) or not self._firewall_ready(item)):
            raise DeployError('DigitalOcean firewall update has an uncertain outcome; reconcile before retrying.')
        if not self._firewall_ready(item):
            raise DeployError('DigitalOcean firewall changes are pending or failed; retry after they settle.')
        if not pending:
            step = self.state.intent(operation_id, 'firewall-rules', {'id': str(item['id'])})
            # Retain old access until additions have been observed applied.
            expanded = {}
            for direction in desired:
                unique = {self._canonical(rule): rule for rule in item.get(direction, []) + desired[direction]}
                expanded[direction] = list(unique.values())
            pending = {'id': str(item['id']), 'step': step, 'desired': desired,
                       'target': expanded, 'phase': 'expand'}
            self.state.set_meta(key, pending)
            if not equal(expanded):
                self._call('PUT', 'firewalls/' + str(item['id']), {
                    'name': item['name'], 'tags': [self._tag()], 'droplet_ids': [], **expanded})
                item = self._wait_firewall_rules(expanded)
                if not item or not equal(expanded) or not self._firewall_ready(item):
                    raise DeployError('DigitalOcean firewall additions are not verified; existing access preserved.')
        if pending.get('phase') == 'expand':
            pending = {**pending, 'phase': 'contract', 'target': desired}
            self.state.set_meta(key, pending)
            if not equal(desired):
                self._call('PUT', 'firewalls/' + str(item['id']), {
                    'name': item['name'], 'tags': [self._tag()], 'droplet_ids': [], **desired})
                item = self._wait_firewall_rules(desired)
        if not item or not equal(desired) or not self._firewall_ready(item):
            raise DeployError('DigitalOcean firewall update is not yet verified; intent preserved.')
        self.state.complete(pending['step'], {'verified': True})
        self.state.set_meta(key, None)

    def _drift(self, item):
        if not item:
            return []
        changes = [key for key, actual in (
            ('digitalocean-region', item.get('region', {}).get('slug')),
            ('digitalocean-size', item.get('size_slug')),
            ('digitalocean-vpc-id', item.get('vpc_uuid')))
            if actual != self.config.get(key, 's-1vcpu-2gb' if key == 'digitalocean-size' else None)]
        record = self.state.get_resource('compute')
        pinned = self.state.get_meta('digitalocean-image-id')
        desired = str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64'))
        if (pinned and str(item.get('image', {}).get('id')) != str(pinned)) or (record and record['attributes'].get('desired_image') != desired):
            changes.append('digitalocean-image')
        if not pinned and str(item.get('image', {}).get('id')) != desired and item.get('image', {}).get('slug') != desired:
            changes.append('digitalocean-image')
        if record and item.get('disk') != record['attributes'].get('disk'):
            changes.append('digitalocean-disk')
        if item.get('volume_ids'):
            changes.append('externally-attached-volumes')
        return changes

    def _network_drift(self, item, firewall):
        if not item:
            return []
        if not firewall:
            return ['firewall-missing']
        tags = set(item.get('tags') or [])
        for other in self._list('firewalls'):
            if str(other['id']) == str(firewall['id']):
                continue
            if (int(item['id']) in (other.get('droplet_ids') or [])
                    or tags.intersection(other.get('tags') or [])):
                return ['additional-firewall']
        return []

    def inspect(self):
        self._scope()
        result = {}
        for role in ('firewall', 'compute'):
            item = self._find(role)
            result[role] = {'id': str(item['id']), 'state': self._lifecycle(item, role), 'owned': True} if item else None
        return result

    def plan(self):
        self._scope(network=True)
        self._validate_launch()
        if self.state is None:
            return [{'resource': role, 'action': 'create'} for role in ('deployment-tag', 'firewall', 'compute')]
        if any(self.state.get_meta('digitalocean-delete-' + role) for role in ('compute', 'firewall', 'tag')):
            raise DeployError('DigitalOcean deletion is pending; finish deletion before convergence.')
        if self.state.get_meta('digitalocean-image-id'):
            self._image()
        tag = self._tag_record()
        tag_record = self.state.get_resource('deployment-tag')
        tag_pending = self.state.get_meta('digitalocean-pending-tag')
        if tag:
            self._check_tag_associations(tag)
        tag_blocked = (not tag and (tag_record or tag_pending)) or (tag and not tag_record and not tag_pending)
        if tag_record and (not tag_record['owned'] or tag_record['provider_id'] != self._tag()):
            tag_blocked = True
        result = [{'resource': 'deployment-tag', 'action': 'blocked' if tag_blocked else 'retain' if tag else 'create'}]
        firewall = self._find('firewall')
        for role in ('firewall', 'compute'):
            item = self._find(role)
            drift = self._drift(item) + self._network_drift(item, firewall) if role == 'compute' else []
            pending = self.state.get_meta('digitalocean-pending-' + role)
            action = ('blocked' if pending else 'create') if not item else ('blocked' if drift else 'retain')
            if role == 'firewall' and item and not self._rules_equal(item):
                action = 'update'
            if tag_blocked:
                action = 'blocked'
            if role == 'firewall' and self.state.get_meta('digitalocean-pending-firewall-rules'):
                action = 'blocked'
            result.append({'resource': role, 'action': action, 'changed_fields': drift})
        return result

    def _validate_launch(self):
        image = self._call('GET', 'images/' + quote(str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64')), safe='')).get('image', {})
        if (not isinstance(image.get('id'), int) or image.get('status') != 'available'
                or self.config['digitalocean-region'] not in image.get('regions', [])
                or image.get('distribution') != 'Ubuntu'):
            raise DeployError('DigitalOcean image is not an available Ubuntu image in the configured region.')
        sizes = self._list('sizes')
        size = next((entry for entry in sizes if entry.get('slug') == self.config.get('digitalocean-size', 's-1vcpu-2gb')), None)
        if not size or not size.get('available') or self.config['digitalocean-region'] not in size.get('regions', []):
            raise DeployError('DigitalOcean size is unavailable in the configured region.')
        return image

    def _image(self):
        pinned = self.state.get_meta('digitalocean-image-id')
        desired = str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64'))
        if pinned:
            if self.state.get_meta('digitalocean-image-selector') != desired:
                raise DeployError('Pinned DigitalOcean image selector differs; explicit recovery is required.')
            return int(pinned)
        image = self._call('GET', 'images/' + quote(str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64')), safe='')).get('image', {})
        if (not isinstance(image.get('id'), int) or image.get('status') != 'available'
                or self.config['digitalocean-region'] not in image.get('regions', [])
                or image.get('distribution') != 'Ubuntu'):
            raise DeployError('DigitalOcean image is not an available Ubuntu image in the configured region.')
        self.state.set_meta('digitalocean-image-selector', desired)
        self.state.set_meta('digitalocean-image-id', image['id'])
        return image['id']

    def _user_data(self, public_key):
        try:
            document = json.loads(self.config.get('_cloud_init', '#cloud-config\n{}').split('\n', 1)[1])
        except (ValueError, IndexError):
            raise DeployError('DigitalOcean requires the generated JSON cloud-init document.') from None
        document['users'] = [{'name': self.config.get('ssh-user', 'ubuntu'), 'groups': 'sudo',
                              'shell': '/bin/bash', 'sudo': 'ALL=(ALL) NOPASSWD:ALL',
                              'lock_passwd': True, 'ssh_authorized_keys': [public_key]}]
        document['ssh_pwauth'] = False
        return '#cloud-config\n' + json.dumps(document)

    def converge(self, public_key, operation_id):
        if any(self.state.get_meta('digitalocean-delete-' + role) for role in ('compute', 'firewall', 'tag')):
            raise DeployError('DigitalOcean deletion is pending; finish deletion before convergence.')
        self._scope(network=True)
        self._validate_launch()
        if self.state.get_meta('digitalocean-image-id'):
            self._image()
        item = self._find('compute')
        if self._drift(item):
            raise DeployError('DigitalOcean immutable compute settings drifted; explicit recovery is required.')
        firewall = self._find('firewall')
        if self._network_drift(item, firewall):
            raise DeployError('DigitalOcean firewall attachment drifted; reconcile explicitly.')
        self._ensure_tag(operation_id)
        if not firewall:
            firewall = self._create('firewall', operation_id, {'name': self._tag() + '-firewall',
                'tags': [self._tag()], 'droplet_ids': [], **self._rules()})
        self._recover('firewall', firewall)
        self._reconcile_rules(firewall, operation_id)
        if not item:
            item = self._create('compute', operation_id, {
                'name': self.config['profile'] + '-once-compute', 'region': self.config['digitalocean-region'],
                'size': self.config.get('digitalocean-size', 's-1vcpu-2gb'), 'image': self._image(),
                'vpc_uuid': self.config['digitalocean-vpc-id'], 'backups': False, 'ipv6': False,
                'tags': [self._tag()], 'user_data': self._user_data(public_key)})
        self._recover('compute', item)
        for _ in range(120):
            item = self._find('compute')
            firewall = self._find('firewall')
            if item.get('status') == 'active' and firewall and self._firewall_ready(firewall):
                return self.connection()
            if item.get('status') not in ('new', 'active'):
                raise DeployError('DigitalOcean Droplet is not starting; inspect its lifecycle.')
            time.sleep(5)
        raise DeployError('DigitalOcean startup timed out; state preserved.')

    def connection(self):
        self._scope()
        item = self._find('compute')
        if not item or item.get('vpc_uuid') != self.config['digitalocean-vpc-id']:
            raise DeployError('No owned DigitalOcean Droplet in the configured VPC.')
        addresses = [n.get('ip_address') for n in item.get('networks', {}).get('v4', []) if n.get('type') == 'public']
        if len(addresses) != 1:
            raise DeployError('DigitalOcean Droplet has no unique public IPv4 address.')
        try:
            ipaddress.IPv4Address(addresses[0])
        except (ValueError, TypeError):
            raise DeployError('DigitalOcean returned an invalid public address.') from None
        return {'instance_id': str(item['id']), 'ip': addresses[0], 'user': self.config.get('ssh-user', 'ubuntu')}

    def plan_delete(self):
        if any(self.state.get_meta('digitalocean-pending-' + role) for role in ('compute', 'firewall', 'tag')):
            raise DeployError('Reconcile pending DigitalOcean creation before deletion.')
        self._scope()
        result = []
        for role in ('compute', 'firewall'):
            pending = self.state.get_meta('digitalocean-delete-' + role)
            record = self.state.get_resource(role)
            if pending and (not record or pending['id'] != record['provider_id']):
                raise DeployError('DigitalOcean pending deletion identity differs.')
            item = self._find(role, allow_missing=bool(pending))
            if item and (not record or not record['owned'] or str(item['id']) != record['provider_id']):
                raise DeployError('DigitalOcean deletion requires recorded ownership.')
            if role == 'compute' and item and (item.get('volume_ids') or item.get('backup_ids') or item.get('snapshot_ids')):
                raise DeployError('Droplet has volumes, backups or snapshots; reconcile these separately before deletion.')
            result.append({'resource': role, 'action': 'delete' if item else 'absent',
                           'id': str(item['id']) if item else (record['provider_id'] if record else None),
                           'state': self._lifecycle(item, role) if item else None})
        tag = self._tag_deletion_target()
        if tag:
            result.append({'resource': 'deployment-tag', 'action': 'delete', 'id': tag['provider_id']})
        result.append({'resource': 'shared-network', 'action': 'retain'})
        return result

    def delete(self, operation_id):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deployment destruction is protected.')
        self.plan_delete()
        for role in ('compute', 'firewall'):
            key = 'digitalocean-delete-' + role
            pending = self.state.get_meta(key)
            item = self._find(role, allow_missing=bool(pending))
            if not item and not pending:
                continue
            if not pending:
                step = self.state.intent(operation_id, 'delete-' + role, {'id': str(item['id'])})
                pending = {'step': step, 'id': str(item['id'])}
                self.state.set_meta(key, pending)
                self._call('DELETE', ('droplets/' if role == 'compute' else 'firewalls/') + str(item['id']))
            for _ in range(30):
                # Teardown may remove tags before the object disappears. Once
                # deletion is authorized, observe the exact recorded identity;
                # losing its ownership marker is neither success nor authority
                # to repeat DELETE. Preflight remains strictly ownership-bound.
                inventory = self._list('droplets' if role == 'compute' else 'firewalls')
                if any(self._owned(candidate, role) and str(candidate['id']) != pending['id'] for candidate in inventory):
                    raise DeployError('Another DigitalOcean resource claims the deleting deployment role.')
                if not any(str(candidate['id']) == pending['id'] for candidate in inventory):
                    break
                time.sleep(2)
            else:
                raise DeployError('DigitalOcean deletion outcome remains pending; reconcile before retrying.', code='deletion_pending')
            if role == 'firewall':
                rules = self.state.get_meta('digitalocean-pending-firewall-rules')
                if rules:
                    self.state.complete(rules['step'], {'deleted': True})
                    self.state.set_meta('digitalocean-pending-firewall-rules', None)
            self.state.complete_delete(role, pending['step'], key)
        tag = self._tag_deletion_target()
        if tag:
            pending = self.state.get_meta('digitalocean-delete-tag')
            item = self._tag_record()
            if not pending:
                pending = {'id': tag['provider_id'], 'step': self.state.intent(operation_id, 'delete-deployment-tag', {'name': tag['provider_id']})}
                self.state.set_meta('digitalocean-delete-tag', pending)
                if item:
                    self._call('DELETE', 'tags/' + quote(tag['provider_id'], safe=''))
            if self._tag_record():
                raise DeployError('DigitalOcean tag deletion remains pending; reconcile explicitly.', code='deletion_pending')
            self.state.complete_delete('deployment-tag', pending['step'], 'digitalocean-delete-tag')
        return {'deleted': True}

    def adopt(self, instance_id, operation_id):
        if (any(self.state.get_meta('digitalocean-delete-' + role) for role in ('compute', 'firewall', 'tag'))
                or self.state.get_meta('digitalocean-pending-firewall-rules')):
            raise DeployError('DigitalOcean deletion or firewall update is pending; reconcile before adoption.')
        self._scope(network=True)
        item, firewall = self._find('compute'), self._find('firewall')
        if (not item or str(item['id']) != str(instance_id) or not firewall
                or item.get('status') != 'active' or not self._firewall_ready(firewall)
                or self._drift(item) or self._network_drift(item, firewall) or not self._rules_equal(firewall)):
            raise DeployError('DigitalOcean adoption requires matching running owned compute and firewall.')
        tag = self._tag_record()
        if not tag or tag.get('resources', {}).get('count') != 1 or tag.get('resources', {}).get('droplets', {}).get('count') != 1:
            raise DeployError('DigitalOcean adoption requires its exclusive deployment tag.')
        step = self.state.intent(operation_id, 'adopt-compute', {'id': str(instance_id)})
        self.state.put_resource('deployment-tag', 'digitalocean-tag', self._tag(), {}, owned=True)
        self._recover('firewall', firewall)
        self._recover('compute', item)
        self.state.set_meta('digitalocean-image-selector', str(self.config.get('digitalocean-image', 'ubuntu-24-04-x64')))
        self.state.set_meta('digitalocean-image-id', item['image']['id'])
        self.state.complete(step, {'id': str(instance_id)})
        return self.connection()
