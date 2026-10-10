"""Compute Engine adapter. Tokens and raw provider responses never leave memory."""
import json
import hashlib
import ipaddress
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .common import DeployError, run


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GCP:
    resource_kinds = {'gcp-instance', 'gcp-disk', 'gcp-firewall'}
    delete_pending_key = 'gcp-delete-compute'
    roles = ('firewall', 'firewall-http', 'boot-volume', 'compute')
    base = 'https://compute.googleapis.com/compute/v1/'

    def __init__(self, config, state=None):
        self.config, self.state = config, state

    def _root(self):
        return 'projects/' + self.config['gcp-project']

    def _zone(self):
        return self._root() + '/zones/' + self.config['gcp-zone']

    def _collection(self, role):
        if role.startswith('firewall'):
            return self._root() + '/global/firewalls'
        return self._zone() + ('/instances' if role == 'compute' else '/disks')

    def _normalize(self, value):
        if isinstance(value, str):
            return value.replace('https://www.googleapis.com/compute/v1/', self.base, 1) if value.startswith('https://www.googleapis.com/compute/v1/') else value
        if isinstance(value, list):
            return [self._normalize(v) for v in value]
        if isinstance(value, dict):
            return {k: self._normalize(v) for k, v in value.items()}
        return value

    def _link(self, value, prefix):
        value = self._normalize(value)
        if value.startswith(self.base):
            value = value[len(self.base):]
        if value.startswith('projects/'):
            if not value.startswith(self._root() + '/'):
                raise DeployError('GCP resources must belong to the configured project.')
            return value
        if '/' in value:
            raise DeployError('Invalid GCP resource reference.')
        return prefix + '/' + value

    def _network(self):
        return self._link(self.config['gcp-network'], self._root() + '/global/networks')

    def _subnet(self):
        region = self.config['gcp-zone'].rsplit('-', 1)[0]
        return self._link(self.config['gcp-subnet'], self._root() + '/regions/' + region + '/subnetworks')

    def _call(self, path, method='GET', body=None, query=None):
        if not path.startswith('projects/') or '..' in path or '?' in path or '#' in path:
            raise DeployError('Invalid GCP API path.')
        command = ['gcloud', 'auth']
        if self.config.get('gcp-auth') == 'application-default':
            command.append('application-default')
        command += ['print-access-token', '--quiet']
        if self.config.get('gcp-account'):
            command += ['--account', self.config['gcp-account']]
        token = run(command).strip()
        if not token or '\n' in token:
            raise DeployError('GCP authentication returned an invalid token.')
        request = Request(self.base + path + ('?' + urlencode(query) if query else ''),
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
                          method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=120) as response:
                data = json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise DeployError('GCP request failed; provider output suppressed.') from None
        if not isinstance(data, dict):
            raise DeployError('GCP returned an invalid response; output suppressed.')
        return self._normalize(data)

    def _list(self, role):
        result, token, seen = [], None, set()
        while True:
            data = self._call(self._collection(role), query={'pageToken': token} if token else None)
            items = data.get('items', [])
            if not isinstance(items, list) or any(not isinstance(i, dict) or not i.get('id') for i in items):
                raise DeployError('GCP returned an invalid resource list.')
            result.extend(items)
            token = data.get('nextPageToken')
            if not token:
                return result
            if not isinstance(token, str) or token in seen:
                raise DeployError('GCP returned invalid pagination.')
            seen.add(token)

    def _description(self, role):
        return 'pocketdeploy:' + self.state.deployment_id + ':' + role

    def _labels(self, role):
        return {'pocketdeploy-id': self.state.deployment_id, 'pocketdeploy-role': role}

    def _owned(self, item, role):
        return bool(self.state and item.get('description') == self._description(role)
                    and (role.startswith('firewall') or all(
                        item.get('labels', {}).get(k) == v for k, v in self._labels(role).items())))

    def _find(self, role, allow_missing=False):
        items = self._list(role)
        owned = [i for i in items if self._owned(i, role)]
        record = self.state.get_resource(role) if self.state else None
        if len(owned) > 1:
            raise DeployError('Multiple GCP resources claim one deployment role.')
        if record:
            found = next((i for i in items if str(i['id']) == record['provider_id']), None)
            if found and (not record['owned'] or not self._owned(found, role)):
                raise DeployError('Recorded GCP ownership does not match.')
            if found is None and (not allow_missing or owned):
                raise DeployError('Recorded GCP resource is missing; explicit recovery is required.')
        if owned and role.startswith('firewall'):
            item = owned[0]
            if item.get('network') != self.base + self._network() or item.get('targetTags') != [self._tag()] or item.get('targetServiceAccounts'):
                raise DeployError('GCP firewall target ownership differs.')
        return owned[0] if owned else None

    def _lifecycle(self, item):
        return {'TERMINATED': 'STOPPED', 'STOPPING': 'TERMINATING',
                'SUSPENDED': 'STOPPED'}.get(item.get('status'), item.get('status', 'UNKNOWN'))

    def _remember(self, role, item):
        kind = 'gcp-firewall' if role.startswith('firewall') else ('gcp-instance' if role == 'compute' else 'gcp-disk')
        self.state.put_resource(role, kind, str(item['id']),
                                {'name': item['name'], 'lifecycle': self._lifecycle(item)}, owned=True)

    def _wait(self, receipt, key):
        operation = receipt.get('operation')
        if not operation:
            target = receipt.get('target')
            if not target:
                raise DeployError('GCP mutation outcome is uncertain; reconcile before retrying.')
            collection = self._root() + '/global/operations' if '/global/firewalls/' in target else self._zone() + '/operations'
            candidates, token, seen = [], None, set()
            while True:
                data = self._call(collection, query={'pageToken': token} if token else None)
                items = data.get('items', [])
                if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                    raise DeployError('GCP returned invalid operation inventory.')
                candidates.extend(i for i in items if i.get('clientOperationId') == receipt.get('request_id'))
                token = data.get('nextPageToken')
                if not token:
                    break
                if not isinstance(token, str) or token in seen:
                    raise DeployError('GCP returned invalid operation pagination.')
                seen.add(token)
            if len(candidates) != 1:
                raise DeployError('GCP mutation outcome is uncertain; reconcile before retrying.')
            recovered = candidates[0]
            link = self._normalize(recovered.get('selfLink', ''))
            if (self._normalize(recovered.get('targetLink')) != self.base + target
                    or not link.startswith(self.base + collection + '/')
                    or (receipt.get('id') and str(recovered.get('targetId')) != receipt['id'])):
                raise DeployError('GCP operation recovery identity does not match.')
            operation = link[len(self.base):]
            receipt['operation'] = operation
            if not self.state.read_only:
                self.state.set_meta(key, receipt)
        for _ in range(120):
            data = self._call(operation)
            if data.get('status') == 'DONE':
                if data.get('error'):
                    raise DeployError('GCP operation failed; reconcile its preserved intent.')
                return
            time.sleep(2)
        raise DeployError('GCP operation is still pending; retry to reconcile.')

    def _mutate(self, role, operation, method, body=None, item=None):
        key = 'gcp-' + ('delete-' if method == 'DELETE' else 'pending-') + role
        pending = self.state.get_meta(key)
        if pending:
            desired = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if pending.get('desired') != desired or pending.get('method') != method:
                raise DeployError('Pending GCP mutation differs from desired configuration; reconcile first.')
            if item and pending.get('id') and pending['id'] != str(item['id']):
                raise DeployError('Pending GCP mutation identity differs.')
            self._wait(pending, key)
            return pending
        step = self.state.intent(operation, method.lower() + '-' + role,
                                 {'id': str(item['id']) if item else None})
        pending = {'step': step, 'request_id': str(uuid.uuid4()), 'method': method,
                   'desired': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                   'id': str(item['id']) if item else None,
                   'target': self._collection(role) + '/' + (item['name'] if item else body['name'])}
        self.state.set_meta(key, pending)
        path = self._collection(role) + ('/' + item['name'] if item else '')
        response = self._call(path, method=method, body=body, query={'requestId': pending['request_id']})
        link = self._normalize(response.get('selfLink', ''))
        if not link.startswith(self.base + self._root() + '/') or '/operations/' not in link:
            raise DeployError('GCP returned invalid operation details; intent preserved.')
        pending['operation'] = link[len(self.base):]
        self.state.set_meta(key, pending)
        self._wait(pending, key)
        return pending

    def _finish(self, role, item, deleting=False):
        key = 'gcp-' + ('delete-' if deleting else 'pending-') + role
        pending = self.state.get_meta(key)
        if deleting:
            creation = self.state.get_meta('gcp-pending-' + role)
            if creation:
                self.state.complete(creation['step'], {'deleted': True})
                self.state.set_meta('gcp-pending-' + role, None)
            self.state.remove_resource(role)
        else:
            self._remember(role, item)
        if pending:
            self.state.complete(pending['step'], {'deleted': True} if deleting else {'id': str(item['id'])})
            self.state.set_meta(key, None)

    def _name(self, role):
        return 'pd-' + self.state.deployment_id + '-' + role

    def _tag(self):
        return 'pd-' + self.state.deployment_id

    def _firewall(self, role):
        ssh = role == 'firewall'
        return {'name': self._name(role), 'description': self._description(role),
                'network': self.base + self._network(), 'direction': 'INGRESS', 'priority': 1000,
                'disabled': not bool(self.config['compute-ssh-sources' if ssh else 'compute-http-sources']), 'targetTags': [self._tag()],
                'sourceRanges': self.config['compute-ssh-sources' if ssh else 'compute-http-sources'] or ['0.0.0.0/0'],
                'allowed': [{'IPProtocol': 'tcp', 'ports': ['22'] if ssh else ['80', '443']}]}

    def _firewall_equal(self, item, role):
        desired = self._firewall(role)
        # Compute may reorder repeated fields and omit scalar/default values.
        defaults = {'direction': 'INGRESS', 'priority': 1000, 'disabled': False,
                    'sourceRanges': ['0.0.0.0/0']}
        def canonical(value):
            if isinstance(value, dict):
                return {k: canonical(v) for k, v in value.items()}
            if isinstance(value, list):
                return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
            return value
        return all(canonical(item.get(k, defaults.get(k))) == canonical(v)
                   for k, v in desired.items() if k != 'name') and not any(
            item.get(k) for k in ('sourceTags', 'sourceServiceAccounts', 'targetServiceAccounts', 'denied'))

    def _preflight(self):
        project = self._call(self._root())
        metadata = {i.get('key'): i.get('value') for i in project.get('commonInstanceMetadata', {}).get('items', [])}
        if str(metadata.get('enable-oslogin', '')).lower() == 'true':
            raise DeployError('GCP OS Login is incompatible with bootstrap SSH; use a compatible project.')
        subnet = self._call(self._subnet())
        if subnet.get('network') != self.base + self._network():
            raise DeployError('GCP subnet does not belong to the configured network.')

    def _drift(self, item, disk):
        if not item:
            return []
        changed = []
        if item.get('machineType') != self.base + self._zone() + '/machineTypes/' + self.config.get('gcp-machine-type', 'e2-small'):
            changed.append('machine-type')
        interfaces = item.get('networkInterfaces', [])
        if len(interfaces) != 1 or interfaces[0].get('subnetwork') != self.base + self._subnet() or interfaces[0].get('network') != self.base + self._network():
            changed.append('network')
        metadata = {i.get('key'): i.get('value') for i in item.get('metadata', {}).get('items', [])}
        if (str(metadata.get('enable-oslogin', '')).lower() != 'false'
                or str(metadata.get('block-project-ssh-keys', '')).lower() != 'true'
                or item.get('serviceAccounts') or item.get('tags', {}).get('items') != [self._tag()]):
            changed.append('identity')
        attachments = item.get('disks', [])
        if not disk or len(attachments) != 1 or attachments[0].get('source') != disk.get('selfLink') or not attachments[0].get('boot') or attachments[0].get('autoDelete'):
            changed.append('boot-disk')
        return changed + self._disk_drift(disk)

    def _disk_drift(self, disk):
        changed = []
        if disk:
            selector = self.state.get_meta('gcp-image-selector')
            if selector and selector != self._image_selector():
                changed.append('image-selector')
            if str(disk.get('sizeGb')) != str(self.config.get('gcp-boot-disk-size-gb', 50)) or disk.get('type') != self.base + self._zone() + '/diskTypes/' + self.config.get('gcp-boot-disk-type', 'pd-balanced'):
                changed.append('storage')
            image = self._normalize(self.config.get('gcp-image') or self.state.get_meta('gcp-image'))
            if image and disk.get('sourceImage') != image:
                changed.append('image')
        return changed

    def inspect(self):
        result = {}
        for role in self.roles:
            item = self._find(role)
            result[role] = {'id': str(item['id']), 'state': self._lifecycle(item), 'owned': True} if item else None
        return result

    def plan(self):
        self._preflight()
        deletes = {role: self.state.get_meta('gcp-delete-' + role) if self.state else None
                   for role in self.roles}
        disk = self._find('boot-volume', allow_missing=bool(deletes['boot-volume']))
        actions = []
        for role in self.roles:
            item = disk if role == 'boot-volume' else self._find(role, allow_missing=bool(deletes[role]))
            drift = self._drift(item, disk) if role == 'compute' else self._disk_drift(item) if role == 'boot-volume' else []
            if role == 'compute' and not item:
                drift = self._disk_drift(disk)
            if item and role.startswith('firewall') and not self._firewall_equal(item, role):
                drift = ['firewall-rules']
            pending = self.state.get_meta('gcp-pending-' + role) if self.state else None
            if any(deletes.values()):
                action, drift = 'blocked', drift + ['pending-deletion']
            elif pending:
                action, drift = 'blocked', drift + ['pending-mutation']
            elif drift:
                action = 'update' if role.startswith('firewall') else 'blocked'
            else:
                action = 'retain' if item else 'create'
            actions.append({'resource': role, 'action': action, 'changed_fields': drift})
        return actions

    def _image_selector(self):
        return [self._normalize(self.config.get('gcp-image')), self.config.get('gcp-image-project', 'ubuntu-os-cloud'), self.config.get('gcp-image-family', 'ubuntu-2404-lts-amd64')]

    def _image(self):
        selector = self._image_selector()
        recorded = self.state.get_meta('gcp-image-selector')
        if recorded and recorded != selector:
            if any(self.state.get_resource(r) or self.state.get_meta('gcp-pending-' + r) for r in ('compute', 'boot-volume')):
                raise DeployError('GCP image selector drifted; explicit reconciliation is required.')
            self.state.set_meta('gcp-image', None)
        image = self._normalize(self.config.get('gcp-image') or self.state.get_meta('gcp-image'))
        if not image:
            project = self.config.get('gcp-image-project', 'ubuntu-os-cloud')
            family = self.config.get('gcp-image-family', 'ubuntu-2404-lts-amd64')
            image = self._call('projects/' + project + '/global/images/family/' + family).get('selfLink')
        image = self._normalize(image)
        path = image[len(self.base):].split('/') if isinstance(image, str) and image.startswith(self.base) else []
        if len(path) != 5 or path[0] != 'projects' or path[2:4] != ['global', 'images'] or not path[1] or not path[4] or any(c in image for c in ('?', '#')):
            raise DeployError('GCP image must be an exact Compute API image selfLink.')
        self.state.set_meta('gcp-image', image)
        self.state.set_meta('gcp-image-selector', selector)
        return image

    def _reject_pending_delete(self):
        if any(self.state.get_meta('gcp-delete-' + role) for role in self.roles):
            raise DeployError('GCP deletion is pending; finish deletion before convergence or adoption.')

    def converge(self, public_key, operation_id):
        self._reject_pending_delete()
        self._preflight()
        self._check_firewall_users()
        disk, instance = self._find('boot-volume'), self._find('compute')
        if self._drift(instance, disk) or self._disk_drift(disk):
            raise DeployError('GCP compute settings drifted; explicit reconciliation is required.')
        for role in self.roles:
            item = self._find(role)
            if role.startswith('firewall'):
                body = self._firewall(role)
            elif role == 'boot-volume':
                body = {'name': self._name(role), 'description': self._description(role), 'labels': self._labels(role),
                        'sizeGb': str(self.config.get('gcp-boot-disk-size-gb', 50)),
                        'type': self.base + self._zone() + '/diskTypes/' + self.config.get('gcp-boot-disk-type', 'pd-balanced'), 'sourceImage': self._image()}
            else:
                disk = self._find('boot-volume')
                metadata = {'ssh-keys': self.config.get('ssh-user', 'ubuntu') + ':' + public_key.strip(),
                            'enable-oslogin': 'FALSE', 'block-project-ssh-keys': 'TRUE',
                            'user-data': self.config.get('_cloud_init', '')}
                body = {'name': self._name(role), 'description': self._description(role), 'labels': self._labels(role),
                        'machineType': self._zone() + '/machineTypes/' + self.config.get('gcp-machine-type', 'e2-small'),
                        'tags': {'items': [self._tag()]}, 'serviceAccounts': [],
                        'metadata': {'items': [{'key': k, 'value': v} for k, v in metadata.items()]},
                        'disks': [{'boot': True, 'autoDelete': False, 'source': disk['selfLink']}],
                        'networkInterfaces': [{'network': self.base + self._network(), 'subnetwork': self.base + self._subnet(),
                                               'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}]}]}
            if self.state.get_meta('gcp-pending-' + role):
                pending = self.state.get_meta('gcp-pending-' + role)
                self._mutate(role, operation_id, pending['method'], body, item if pending['method'] == 'PATCH' else None)
                item = self._find(role)
            if not item or (role.startswith('firewall') and not self._firewall_equal(item, role)):
                self._mutate(role, operation_id, 'PATCH' if item else 'POST', body, item)
                item = self._find(role)
                if not item or (role.startswith('firewall') and not self._firewall_equal(item, role)):
                    raise DeployError('GCP mutation is not yet visible; intent preserved.')
            self._finish(role, item)
        return self.connection()

    def connection(self):
        item = self._find('compute')
        if not item or item.get('status') != 'RUNNING':
            raise DeployError('No running GCP instance exists.')
        interfaces = item.get('networkInterfaces', [])
        if len(interfaces) != 1 or interfaces[0].get('network') != self.base + self._network() or interfaces[0].get('subnetwork') != self.base + self._subnet():
            raise DeployError('GCP connection requires matching network ownership scope.')
        addresses = [a.get('natIP') for n in item.get('networkInterfaces', []) for a in n.get('accessConfigs', []) if a.get('natIP')]
        if len(addresses) != 1:
            raise DeployError('GCP instance has no unique public address.')
        try:
            ipaddress.IPv4Address(addresses[0])
        except (ValueError, TypeError):
            raise DeployError('GCP returned an invalid public IPv4 address.') from None
        return {'instance_id': str(item['id']), 'ip': addresses[0], 'user': self.config.get('ssh-user', 'ubuntu')}

    def _check_firewall_users(self):
        record = self.state.get_resource('compute')
        token, seen = None, set()
        while True:
            response = self._call(self._root() + '/aggregated/instances', query={'pageToken': token} if token else None)
            scopes = response.get('items', {})
            if not isinstance(scopes, dict):
                raise DeployError('Invalid GCP instance inventory.')
            for scope in scopes.values():
                if not isinstance(scope, dict):
                    raise DeployError('Invalid GCP instance inventory.')
                for instance in scope.get('instances', []):
                    if self._tag() in instance.get('tags', {}).get('items', []):
                        if not record or str(instance.get('id')) != record['provider_id'] or not self._owned(instance, 'compute'):
                            raise DeployError('GCP firewall also targets unowned compute; deletion blocked.')
            token = response.get('nextPageToken')
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise DeployError('Invalid GCP instance inventory pagination.')
            seen.add(token)

    def plan_delete(self):
        actions = []
        for role in ('compute', 'boot-volume', 'firewall', 'firewall-http'):
            creation = self.state.get_meta('gcp-pending-' + role)
            if creation:
                self._wait(creation, 'gcp-pending-' + role)
            pending = self.state.get_meta('gcp-delete-' + role)
            record = self.state.get_resource(role)
            item = self._find(role, allow_missing=bool(pending))
            if item and (not record or not record['owned'] or record['provider_id'] != str(item['id'])):
                raise DeployError('GCP deletion requires recorded ownership.')
            if pending and record and pending.get('id') != record['provider_id']:
                raise DeployError('Pending GCP deletion identity differs.')
            if role == 'compute' and item:
                disk = self._find('boot-volume')
                attachments = item.get('disks', [])
                if not disk or len(attachments) != 1 or attachments[0].get('source') != disk.get('selfLink') or attachments[0].get('autoDelete'):
                    raise DeployError('GCP deletion requires a recorded non-auto-delete boot disk.')
            if role == 'boot-volume' and item and item.get('users'):
                compute = self._find('compute', allow_missing=bool(self.state.get_meta(self.delete_pending_key)))
                if not compute or item['users'] != [compute.get('selfLink')]:
                    raise DeployError('GCP boot disk is attached to unexpected compute.')
            actions.append({'resource': role, 'action': 'delete' if item else 'absent',
                            'id': str(item['id']) if item else record['provider_id'] if record else None,
                            'state': self._lifecycle(item) if item else None})
        if any(a['resource'].startswith('firewall') and a['action'] == 'delete' for a in actions):
            self._check_firewall_users()
        actions.append({'resource': 'shared-network', 'action': 'retain'})
        return actions

    def delete(self, operation_id):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deployment destruction is protected.')
        self.plan_delete()
        for role in ('compute', 'boot-volume', 'firewall', 'firewall-http'):
            pending = self.state.get_meta('gcp-delete-' + role)
            item = self._find(role, allow_missing=bool(pending))
            if item:
                if role == 'boot-volume' and item.get('users'):
                    raise DeployError('GCP boot disk is still attached; retry after termination.')
                self._mutate(role, operation_id, 'DELETE', item=item)
                if self._find(role, allow_missing=True):
                    raise DeployError('GCP deletion is not yet visible; retry to reconcile.')
            if item or pending:
                self._finish(role, None, deleting=True)
        return {'deleted': True}

    def adopt(self, instance_id, operation_id):
        self._reject_pending_delete()
        self._preflight()
        if any(self.state.get_meta('gcp-pending-' + role) for role in self.roles):
            raise DeployError('Settle pending GCP mutations through converge before adoption.')
        self._check_firewall_users()
        instance, disk = self._find('compute'), self._find('boot-volume')
        if not instance or str(instance['id']) != instance_id or instance.get('status') != 'RUNNING' or self._drift(instance, disk):
            raise DeployError('GCP adoption requires matching owned running compute and boot disk.')
        resources = {role: self._find(role) for role in self.roles}
        if any(not i for i in resources.values()) or any(not self._firewall_equal(resources[r], r) for r in ('firewall', 'firewall-http')):
            raise DeployError('GCP adoption requires matching owned firewall rules.')
        for role, item in resources.items():
            self._finish(role, item)
        return self.connection()
