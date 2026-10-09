"""OCI CLI adapter. Cloud responses stay private; ownership is never name-based."""
import base64
import ipaddress
import json
import time
from .common import DeployError, run


class OCI:
    def __init__(self, config, state=None):
        self.config, self.state = config, state

    def _call(self, *args, body=None, timeout=180):
        command = ['oci', '--auth', self.config.get('oci-auth', 'security_token'),
                   '--profile', self.config.get('oci-config-file-profile', 'DEFAULT'), '--output', 'json']
        if self.config.get('oci-region'):
            command += ['--region', self.config['oci-region']]
        command += list(args)
        if body is not None:
            command += ['--from-json', 'file:///dev/stdin']
        output = run(command, input=json.dumps(body) if body is not None else None, timeout=timeout)
        # OCI CLI render() suppresses all output for a successful empty list.
        # Accept that exact representation only for our known list commands;
        # malformed JSON/null data and failed commands remain errors.
        if not output.strip():
            list_commands = (('compute', 'instance', 'list'),
                             ('compute', 'instance', 'list-vnics'),
                             ('compute', 'boot-volume-attachment', 'list'),
                             ('compute', 'image', 'list'),
                             ('network', 'nsg', 'list'),
                             ('network', 'nsg', 'rules', 'list'))
            return [] if any(tuple(args[:len(prefix)]) == prefix for prefix in list_commands) else None
        try:
            return json.loads(output).get('data')
        except (ValueError, AttributeError):
            raise DeployError('OCI returned an invalid response; output suppressed.') from None

    def _list(self, *args):
        data = self._call(*args)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise DeployError('OCI returned an invalid resource list; output suppressed.')
        return data

    def _tags(self, role, operation=None):
        if not self.state:
            raise DeployError('Deployment state is required.')
        tags = {'pocketdeploy-id': self.state.deployment_id, 'pocketdeploy-role': role}
        if operation:
            tags['pocketdeploy-operation'] = operation
        return tags

    def _owned(self, item, role):
        tags = item.get('freeform-tags') or {}
        return self.state and all(tags.get(k) == v for k, v in self._tags(role).items())

    def _find(self, role, allow_missing=False):
        command = ('compute', 'instance', 'list') if role == 'compute' else ('network', 'nsg', 'list')
        records = self._list(*command, '--compartment-id', self.config['oci-compartment-id'], '--all')
        records = [r for r in records if r.get('lifecycle-state') != 'TERMINATED']
        recorded = self.state.get_resource(role) if self.state else None
        candidates = [r for r in records if self._owned(r, role)]
        if recorded:
            found = next((r for r in records if r['id'] == recorded['provider_id']), None)
            if found and not self._owned(found, role):
                raise DeployError('Recorded OCI resource ownership tags do not match.')
            if found is None:
                if allow_missing and not candidates:
                    return None
                raise DeployError('Recorded OCI resource is missing; explicit recovery is required.')
        if len(candidates) > 1:
            raise DeployError('Multiple OCI resources claim one deployment role; reconcile explicitly.')
        return candidates[0] if candidates else None

    def _subnet(self):
        subnet = self._call('network', 'subnet', 'get', '--subnet-id', self.config['oci-subnet-id'])
        if not isinstance(subnet, dict) or not subnet.get('vcn-id'):
            raise DeployError('OCI returned invalid subnet details.')
        if subnet.get('prohibit-public-ip-on-vnic'):
            raise DeployError('A public subnet is required.')
        return subnet

    def _drift(self, instance):
        if not instance:
            return []
        expected = {'shape': self.config['oci-shape'], 'availability-domain': self.config['oci-availability-domain']}
        image = self.config.get('oci-image-id') or (self.state.get_meta('oci-image-id') if self.state else None)
        if image:
            expected['image-id'] = image
        changes = [k for k, v in expected.items() if instance.get(k) != v]
        shape = instance.get('shape-config') or {}
        for actual, desired in [('ocpus', 'oci-ocpus'), ('memory-in-gbs', 'oci-memory-in-gbs')]:
            if float(shape.get(actual, 0)) != float(self.config[desired]):
                changes.append(desired)
        if self.state:
            old = self.state.get_resource('compute')
            if old:
                prior = old.get('attributes', {}).get('desired', {})
                for key in ('oci-subnet-id', 'oci-boot-volume-size-in-gbs', 'oci-boot-volume-vpus-per-gb'):
                    if key in prior and prior[key] != self.config.get(key):
                        changes.append(key)
        return changes

    def inspect(self):
        result = {}
        for role in ('firewall', 'compute'):
            item = self._find(role)
            result[role] = {'id': item['id'], 'state': item.get('lifecycle-state'), 'owned': True} if item else None
        return result

    def plan(self):
        self._subnet()
        firewall = self._find('firewall')
        actions = []
        for role in ('firewall', 'compute'):
            item = self._find(role)
            if not item:
                actions.append({'resource': role, 'action': 'create'})
            elif role == 'compute':
                drift = self._drift(item) + self._network_drift(item, firewall) + self._storage_drift(item)
                actions.append({'resource': role, 'action': 'blocked' if drift else 'retain', 'changed_fields': drift})
            else:
                current = self._list('network', 'nsg', 'rules', 'list', '--nsg-id', item['id'], '--all')
                actions.append({'resource': role, 'action': 'retain' if self._rules_equal(current) else 'update'})
        return actions

    def _network_drift(self, instance, firewall):
        if not instance:
            return []
        vnics = self._list('compute', 'instance', 'list-vnics', '--instance-id', instance['id'], '--all')
        primary = next((v for v in vnics if v.get('is-primary')), None)
        if primary is None:
            if instance.get('lifecycle-state') == 'PROVISIONING':
                return []
            return ['primary-vnic']
        changed = []
        if primary.get('subnet-id') != self.config['oci-subnet-id']:
            changed.append('oci-subnet-id')
        if not firewall or set(primary.get('nsg-ids') or []) != {firewall['id']}:
            changed.append('nsg-attachment')
        return changed

    def _rules(self):
        rules = [{'direction': 'EGRESS', 'protocol': 'all', 'destination': '0.0.0.0/0', 'destinationType': 'CIDR_BLOCK', 'isStateless': False}]
        for key, ports in [('compute-ssh-sources', (22,)), ('compute-http-sources', (80, 443))]:
            for source in self.config.get(key, []):
                try:
                    ipaddress.ip_network(source, strict=True)
                except ValueError:
                    raise DeployError('Invalid firewall source CIDR.') from None
                for port in ports:
                    rules.append({'direction': 'INGRESS', 'protocol': '6', 'source': source, 'sourceType': 'CIDR_BLOCK', 'isStateless': False, 'tcpOptions': {'destinationPortRange': {'min': port, 'max': port}}})
        return rules

    @staticmethod
    def _normalized(rule):
        def convert(value):
            if isinstance(value, dict):
                return {k.replace('-', '').lower(): convert(v) for k, v in value.items() if v is not None and k not in ('id', 'time-created', 'is-valid', 'description')}
            return value
        return json.dumps(convert(rule), sort_keys=True)

    def _rules_equal(self, current):
        return {self._normalized(r) for r in current} == {self._normalized(r) for r in self._rules()}

    def _remember(self, role, item):
        attrs = {'desired': {k: v for k, v in self.config.items() if k in ('oci-subnet-id', 'oci-boot-volume-size-in-gbs', 'oci-boot-volume-vpus-per-gb')}}
        self.state.put_resource(role, 'oci-' + role, item['id'], attrs, owned=True)

    def _create(self, role, operation, function):
        pending = self.state.get_meta('oci-pending-' + role)
        if pending:
            raise DeployError('An OCI create has an uncertain outcome; inspect cloud resources before retrying.')
        step = self.state.intent(operation, 'create-' + role, {'role': role})
        self.state.set_meta('oci-pending-' + role, {'operation': operation, 'step': step})
        item = function()
        self._remember(role, item)
        self.state.complete(step, {'id': item['id']})
        self.state.set_meta('oci-pending-' + role, None)
        return item

    def _recovered(self, role, item):
        pending = self.state.get_meta('oci-pending-' + role)
        self._remember(role, item)
        if isinstance(pending, dict) and pending.get('step'):
            self.state.complete(pending['step'], {'id': item['id'], 'recovered': True})
        self.state.set_meta('oci-pending-' + role, None)

    def _read_boot_volume(self, instance):
        attachments = self._list('compute', 'boot-volume-attachment', 'list',
            '--compartment-id', self.config['oci-compartment-id'],
            '--availability-domain', instance['availability-domain'],
            '--instance-id', instance['id'], '--all')
        attachments = [a for a in attachments if a.get('lifecycle-state') == 'ATTACHED']
        if len(attachments) != 1:
            raise DeployError('Cannot identify a unique attached boot volume.')
        volume_id = attachments[0]['boot-volume-id']
        volume = self._call('bv', 'boot-volume', 'get', '--boot-volume-id', volume_id)
        if not isinstance(volume, dict) or volume.get('id') != volume_id:
            raise DeployError('OCI returned invalid boot volume details.')
        return volume

    def _retain_boot_volume(self):
        previous = self.state.get_resource('boot-volume')
        if previous is None:
            return
        attributes = {**previous['attributes'], 'lifecycle': 'retained-for-recovery'}
        # Copy before removing the active binding. A crash may leave duplicate
        # bindings, but can never discard the retained provider identity.
        self.state.put_resource('retained-boot-volume:' + previous['provider_id'],
                                previous['kind'], previous['provider_id'],
                                attributes, owned=previous['owned'])
        self.state.remove_resource('boot-volume')

    def _boot_volume(self, instance):
        volume = self._read_boot_volume(instance)
        previous = self.state.get_resource('boot-volume')
        if previous and previous['provider_id'] != volume['id']:
            self._retain_boot_volume()
        self.state.put_resource('boot-volume', 'oci-boot-volume', volume['id'],
            {'instance_id': instance['id'], 'size_gib': volume.get('size-in-gbs'),
             'vpus_per_gb': volume.get('vpus-per-gb')}, owned=True)
        return volume

    def _storage_drift(self, instance):
        if not instance or instance.get('lifecycle-state') != 'RUNNING':
            return []
        volume = self._read_boot_volume(instance)
        return [key for key, field, default in (
            ('oci-boot-volume-size-in-gbs', 'size-in-gbs', 50),
            ('oci-boot-volume-vpus-per-gb', 'vpus-per-gb', 10))
            if volume.get(field) != self.config.get(key, default)]

    def _image(self):
        image = self.config.get('oci-image-id') or self.state.get_meta('oci-image-id')
        if not image:
            images = self._list('compute', 'image', 'list', '--compartment-id', self.config['oci-compartment-id'], '--operating-system', 'Canonical Ubuntu', '--operating-system-version', '24.04', '--shape', self.config['oci-shape'], '--sort-by', 'TIMECREATED', '--sort-order', 'DESC', '--all')
            if not images:
                raise DeployError('No compatible Ubuntu 24.04 image found; specify oci-image-id.')
            image = images[0]['id']
        self.state.set_meta('oci-image-id', image)
        return image

    def converge(self, public_key, operation_id):
        subnet = self._subnet()
        instance = self._find('compute')
        if self._drift(instance) or self._storage_drift(instance):
            raise DeployError('OCI compute settings drifted; resizing or replacement requires explicit handling.')
        firewall = self._find('firewall')
        if firewall and firewall.get('vcn-id') != subnet['vcn-id']:
            raise DeployError('OCI firewall belongs to an unexpected VCN.')
        if self._network_drift(instance, firewall):
            raise DeployError('OCI instance networking drifted; reconcile explicitly before changes.')
        if not firewall:
            firewall = self._create('firewall', operation_id, lambda: self._call('network', 'nsg', 'create', body={'compartmentId': self.config['oci-compartment-id'], 'vcnId': subnet['vcn-id'], 'displayName': self.config['profile'] + '-once-firewall', 'freeformTags': self._tags('firewall', operation_id)}))
        self._recovered('firewall', firewall)
        rules = self._list('network', 'nsg', 'rules', 'list', '--nsg-id', firewall['id'], '--all')
        if not self._rules_equal(rules):
            step = self.state.intent(operation_id, 'firewall-rules', {'id': firewall['id']})
            if rules:
                self._call('network', 'nsg', 'rules', 'remove', body={'nsgId': firewall['id'], 'securityRuleIds': [r['id'] for r in rules]})
            self._call('network', 'nsg', 'rules', 'add', body={'nsgId': firewall['id'], 'securityRules': self._rules()})
            verified = self._list('network', 'nsg', 'rules', 'list', '--nsg-id', firewall['id'], '--all')
            if not self._rules_equal(verified):
                raise DeployError('OCI firewall verification failed.')
            self.state.complete(step, {'id': firewall['id']})
        if not instance:
            metadata = {'ssh_authorized_keys': public_key}
            if self.config.get('_cloud_init'):
                metadata['user_data'] = base64.b64encode(self.config['_cloud_init'].encode()).decode()
            body = {'compartmentId': self.config['oci-compartment-id'], 'availabilityDomain': self.config['oci-availability-domain'], 'displayName': self.config['profile'] + '-once-compute', 'shape': self.config['oci-shape'], 'shapeConfig': {'ocpus': float(self.config['oci-ocpus']), 'memoryInGBs': float(self.config['oci-memory-in-gbs'])}, 'subnetId': self.config['oci-subnet-id'], 'assignPublicIp': True, 'nsgIds': [firewall['id']], 'metadata': metadata, 'freeformTags': self._tags('compute', operation_id), 'sourceDetails': {'sourceType': 'image', 'imageId': self._image(), 'bootVolumeSizeInGBs': int(self.config.get('oci-boot-volume-size-in-gbs', 50)), 'bootVolumeVpusPerGB': int(self.config.get('oci-boot-volume-vpus-per-gb', 10))}}
            instance = self._create('compute', operation_id, lambda: self._call('compute', 'instance', 'launch', body=body))
        self._recovered('compute', instance)
        for _ in range(120):
            live = self._call('compute', 'instance', 'get', '--instance-id', instance['id'])
            if live.get('lifecycle-state') == 'RUNNING':
                self._boot_volume(live)
                return self.connection()
            if live.get('lifecycle-state') not in ('PROVISIONING', 'STARTING', 'RUNNING'):
                raise DeployError('OCI instance is not starting; inspect its lifecycle state.')
            time.sleep(5)
        raise DeployError('OCI instance startup timed out; state has been preserved.')

    def connection(self):
        item = self._find('compute')
        if not item:
            raise DeployError('No deployed OCI instance exists.')
        vnics = self._list('compute', 'instance', 'list-vnics', '--instance-id', item['id'], '--all')
        primary = next((v for v in vnics if v.get('is-primary')), None)
        if not primary or not primary.get('public-ip'):
            raise DeployError('OCI instance has no primary public address.')
        if primary.get('subnet-id') != self.config['oci-subnet-id']:
            raise DeployError('OCI instance subnet differs from desired configuration.')
        return {'instance_id': item['id'], 'ip': primary['public-ip'], 'user': self.config.get('ssh-user', 'ubuntu')}

    def delete(self, operation_id):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deployment destruction is protected.')
        for role in ('compute', 'firewall'):
            pending = self.state.get_meta('oci-delete-' + role)
            item = self._find(role, allow_missing=bool(pending))
            if not item:
                if pending:
                    if role == 'compute':
                        if pending.get('retain_boot', True):
                            self._retain_boot_volume()
                        else:
                            self.state.remove_resource('boot-volume')
                    self.state.remove_resource(role)
                    self.state.complete(pending['step'], {'deleted': True, 'recovered': True})
                    self.state.set_meta('oci-delete-' + role, None)
                continue
            recorded = self.state.get_resource(role)
            if not recorded or not recorded.get('owned') or recorded['provider_id'] != item['id']:
                raise DeployError('Deletion requires recorded ownership and matching cloud tags.')
            step = pending['step'] if pending else self.state.intent(operation_id, 'delete-' + role, {'id': item['id']})
            retain_boot = pending.get('retain_boot', True) if pending else self.config.get('compute-retain-boot-volume', True)
            self.state.set_meta('oci-delete-' + role, {'step': step, 'id': item['id'], 'retain_boot': retain_boot})
            if role == 'compute':
                if item.get('lifecycle-state') != 'TERMINATING':
                    self._boot_volume(item)
                self._call('compute', 'instance', 'terminate', '--instance-id', item['id'], '--force', '--preserve-boot-volume', str(retain_boot).lower(), '--wait-for-state', 'SUCCEEDED', timeout=1500)
            else:
                self._call('network', 'nsg', 'delete', '--nsg-id', item['id'], '--force')
            if role == 'compute':
                if retain_boot:
                    self._retain_boot_volume()
                else:
                    self.state.remove_resource('boot-volume')
            self.state.remove_resource(role)
            self.state.complete(step, {'deleted': True})
            self.state.set_meta('oci-delete-' + role, None)
        return {'deleted': True}

    def adopt(self, instance_id, operation_id):
        item = self._call('compute', 'instance', 'get', '--instance-id', instance_id)
        if item.get('compartment-id') != self.config['oci-compartment-id'] or not self._owned(item, 'compute'):
            raise DeployError('Adoption requires matching compartment and deployment ownership tags.')
        firewall = self._find('firewall')
        if self._drift(item) or self._storage_drift(item) or self._network_drift(item, firewall):
            raise DeployError('Existing instance does not match desired compute settings.')
        step = self.state.intent(operation_id, 'adopt-compute', {'id': instance_id})
        self._remember('compute', item)
        self.state.complete(step, {'id': instance_id})
        return self.connection()
