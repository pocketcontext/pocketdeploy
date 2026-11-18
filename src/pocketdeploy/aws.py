"""AWS CLI compute adapter. Raw provider output and credentials stay in memory.

Authentication comes from the named profile in the AWS CLI configuration.
Ownership is established by tags plus recorded provider IDs, never by names.
"""
import ipaddress
import json
import os
import re
import time

from .common import DeployError, run
from .digitalocean import DigitalOcean
from .output import operation


class AWS:
    kinds = {'firewall': 'aws-security-group', 'compute': 'aws-instance'}
    resource_kinds = frozenset(kinds.values())
    delete_pending_key = 'aws-delete-compute'
    vocabulary = {'ec2', 'ssm', 'describe-instances', 'describe-security-groups', 'describe-subnets',
                  'describe-instance-types', 'describe-images', 'describe-volumes', 'get-parameter',
                  'run-instances', 'create-security-group', 'delete-security-group',
                  'authorize-security-group-ingress', 'revoke-security-group-ingress',
                  'modify-instance-attribute', 'terminate-instances'}
    states = {'running': 'RUNNING', 'pending': 'PROVISIONING', 'stopping': 'STOPPING',
              'stopped': 'STOPPED', 'shutting-down': 'TERMINATING'}

    def __init__(self, config, state=None):
        self.config, self.state = config, state

    @staticmethod
    def _classify(stderr):
        if len(stderr) > 1024 * 1024:
            return None
        if re.search(r'\((?:AuthFailure|UnauthorizedOperation|ExpiredToken|InvalidClientTokenId)\)', stderr) \
                or 'Unable to locate credentials' in stderr or 'The SSO session' in stderr \
                or 'Token has expired' in stderr:
            return DeployError('AWS authentication failed or was not permitted. Check the configured aws-profile; '
                               'for SSO profiles run `aws sso login` with that profile.',
                               code='provider_authentication_failed')
        if re.search(r'The config profile \(.*\) could not be found', stderr):
            return DeployError('The configured aws-profile is not in the AWS CLI configuration.', code='credentials_missing')
        if '(DependencyViolation)' in stderr:
            return DeployError('AWS resource is still in use.', code='resource_in_use')
        return None

    def _call(self, service, *args, payload=None, timeout=180):
        # The profile and region flags take precedence; ambient selectors must not
        # silently point this deployment at another account or region.
        env = {k: v for k, v in os.environ.items()
               if k not in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_REGION', 'AWS_DEFAULT_REGION',
                            'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')}
        env.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
        command = ['aws', '--profile', self.config['aws-profile'], '--region', self.config['aws-region'],
                   '--output', 'json', service, *args]
        label = [service] + [a for a in args[:1] if a in self.vocabulary]
        with operation('aws: ' + ' '.join(label)):
            raw = run(command, input=payload, env=env, timeout=timeout, error_classifier=self._classify)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise DeployError('AWS returned invalid JSON; output suppressed.') from None

    def _ec2(self, *args, payload=None, timeout=180):
        return self._call('ec2', *args, payload=payload, timeout=timeout)

    @staticmethod
    def _items(data, key):
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise DeployError('AWS returned an invalid resource list; output suppressed.')
        return items

    def _tags(self, role):
        if not self.state:
            raise DeployError('Deployment state is required.')
        return {'pocketdeploy-id': self.state.deployment_id, 'pocketdeploy-role': role}

    def _tag_specification(self, resource, role, name):
        tags = {'Name': self.config['profile'] + '-' + name, **self._tags(role)}
        return {'ResourceType': resource, 'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()]}

    def _owned(self, item, role):
        tags = {t.get('Key'): t.get('Value') for t in item.get('Tags') or [] if isinstance(t, dict)}
        return bool(self.state) and all(tags.get(k) == v for k, v in self._tags(role).items())

    @staticmethod
    def _id(item, role):
        return item['InstanceId'] if role == 'compute' else item['GroupId']

    def _describe(self, role, filters):
        if role == 'compute':
            data = self._ec2('describe-instances', '--filters', json.dumps(filters))
            items = [i for r in self._items(data, 'Reservations') for i in self._items(r, 'Instances')]
            items = [i for i in items if (i.get('State') or {}).get('Name') != 'terminated']
        else:
            items = self._items(self._ec2('describe-security-groups', '--filters', json.dumps(filters)),
                                'SecurityGroups')
        if any(not isinstance(i.get('InstanceId' if role == 'compute' else 'GroupId'), str) for i in items):
            raise DeployError('AWS returned invalid resource identities.')
        return items

    def _find(self, role, allow_missing=False):
        tag = [{'Name': 'tag:pocketdeploy-id', 'Values': [self.state.deployment_id]},
               {'Name': 'tag:pocketdeploy-role', 'Values': [role]}]
        owned = [i for i in self._describe(role, tag) if self._owned(i, role)]
        if len(owned) > 1:
            raise DeployError('Multiple AWS resources claim one deployment role; reconcile explicitly.')
        record = self.state.get_resource(role)
        if record:
            if not record['owned'] or record['kind'] != self.kinds[role]:
                raise DeployError('Recorded AWS ownership is invalid.')
            key = 'instance-id' if role == 'compute' else 'group-id'
            exact = self._describe(role, [{'Name': key, 'Values': [record['provider_id']]}])
            if exact and not self._owned(exact[0], role):
                raise DeployError('Recorded AWS resource ownership tags do not match.')
            if not exact and not (allow_missing and not owned):
                raise DeployError('Recorded AWS resource is missing; explicit recovery is required.')
            if owned and self._id(owned[0], role) != record['provider_id']:
                raise DeployError('Another AWS resource claims the recorded deployment role.')
        elif owned and not self.state.get_meta('aws-pending-' + role):
            raise DeployError('Unrecorded AWS resource requires checkpoint recovery; names never authorize adoption.')
        return owned[0] if owned else None

    def _subnet(self):
        subnets = self._items(self._ec2('describe-subnets', '--subnet-ids', self.config['aws-subnet-id']), 'Subnets')
        if len(subnets) != 1 or not isinstance(subnets[0].get('VpcId'), str):
            raise DeployError('AWS returned invalid subnet details.')
        return subnets[0]

    def _instance_type(self):
        types = self._items(self._ec2('describe-instance-types', '--instance-types', self.config['aws-instance-type']),
                            'InstanceTypes')
        if len(types) != 1:
            raise DeployError('AWS instance type is unavailable in this region.')
        info = types[0]
        architectures = (info.get('ProcessorInfo') or {}).get('SupportedArchitectures') or []
        arch = 'amd64' if 'x86_64' in architectures else 'arm64' if 'arm64' in architectures else None
        if not arch:
            raise DeployError('AWS instance type must support x86_64 or arm64.')
        features = (info.get('ProcessorInfo') or {}).get('SupportedFeatures') or []
        if self.config.get('aws-nested-virtualization') and 'nested-virtualization' not in features:
            raise DeployError('aws-nested-virtualization requires an instance type that supports it (c8i, m8i or r8i families).')
        vcpu = info.get('VCpuInfo') or {}
        wanted = self.config.get('aws-threads-per-core')
        if wanted is not None and (wanted not in (vcpu.get('ValidThreadsPerCore') or [])
                                   or type(vcpu.get('DefaultCores')) is not int):
            raise DeployError('aws-threads-per-core is not valid for the instance type.')
        return {'arch': arch, 'cores': vcpu.get('DefaultCores')}

    def _image(self, arch):
        desired = self.config.get('aws-image-id') or 'ubuntu-24.04-' + arch
        pinned = self.state.get_meta('aws-image') if self.state else None
        if pinned:
            if pinned['selector'] != desired:
                raise DeployError('AWS image selector changed; replacement is not supported.')
            image_id = pinned['id']
            if self.state.get_resource('compute'):
                # A launched host no longer depends on its AMI, which may be deprecated or deregistered.
                return {'selector': desired, 'id': image_id}
        elif self.config.get('aws-image-id'):
            image_id = self.config['aws-image-id']
        else:
            name = '/aws/service/canonical/ubuntu/server/24.04/stable/current/' + arch + '/hvm/ebs-gp3/ami-id'
            parameter = self._call('ssm', 'get-parameter', '--name', name)
            image_id = ((parameter or {}).get('Parameter') or {}).get('Value') if isinstance(parameter, dict) else None
            if not isinstance(image_id, str) or not re.fullmatch(r'ami-[0-9a-f]{8,17}', image_id):
                raise DeployError('AWS returned an invalid Ubuntu image parameter.')
        images = self._items(self._ec2('describe-images', '--image-ids', image_id), 'Images')
        expected = {'amd64': 'x86_64', 'arm64': 'arm64'}[arch]
        if (len(images) != 1 or images[0].get('ImageId') != image_id or images[0].get('State') != 'available'
                or images[0].get('Architecture') != expected or images[0].get('RootDeviceType') != 'ebs'
                or not isinstance(images[0].get('RootDeviceName'), str)):
            raise DeployError('AWS image must be an available EBS image matching the instance architecture.')
        return {'selector': desired, 'id': image_id, 'root-device': images[0]['RootDeviceName']}

    def _cpu_options(self, cores):
        nested = bool(self.config.get('aws-nested-virtualization'))
        threads = self.config.get('aws-threads-per-core')
        options = {'CoreCount': cores, 'ThreadsPerCore': threads} if threads is not None else {}
        if nested:
            options['NestedVirtualization'] = 'enabled'
        return options or None

    def _desired(self):
        return {key: self.config.get(key) for key in ('aws-subnet-id', 'aws-root-volume-size-gb',
                                                      'aws-root-volume-type', 'aws-nested-virtualization',
                                                      'aws-threads-per-core')}

    def _drift(self, instance, firewall):
        if not instance:
            return []
        changes = []
        if instance.get('InstanceType') != self.config['aws-instance-type']:
            changes.append('aws-instance-type')
        pinned = self.state.get_meta('aws-image')
        if pinned and instance.get('ImageId') != pinned['id']:
            changes.append('aws-image-id')
        if instance.get('SubnetId') != self.config['aws-subnet-id']:
            changes.append('aws-subnet-id')
        cpu = instance.get('CpuOptions') or {}
        if (cpu.get('NestedVirtualization') == 'enabled') != bool(self.config.get('aws-nested-virtualization')):
            changes.append('aws-nested-virtualization')
        if self.config.get('aws-threads-per-core') is not None and cpu.get('ThreadsPerCore') != self.config['aws-threads-per-core']:
            changes.append('aws-threads-per-core')
        groups = {g.get('GroupId') for g in instance.get('SecurityGroups') or []}
        if not firewall or groups != {firewall['GroupId']}:
            changes.append('security-group-attachment')
        record = self.state.get_resource('compute')
        prior = (record or {}).get('attributes', {}).get('desired', {})
        changes += [k for k in ('aws-root-volume-size-gb', 'aws-root-volume-type') if k in prior and prior[k] != self.config.get(k)]
        if self._extra_storage(instance):
            changes.append('extra-storage')
        return changes

    @staticmethod
    def _extra_storage(instance):
        root = instance.get('RootDeviceName')
        mappings = instance.get('BlockDeviceMappings') or []
        return any(m.get('DeviceName') != root or not (m.get('Ebs') or {}).get('DeleteOnTermination') for m in mappings)

    def _rules(self):
        desired = set()
        for key, ports in [('compute-ssh-sources', self.config.get('compute-ssh-ports', ['22'])),
                           ('compute-http-sources', ['80', '443'])]:
            for source in self.config.get(key, []):
                desired.update(('tcp', int(port), source) for port in ports)
        for rule in self.config.get('compute-ingress', []):
            desired.update((rule['protocol'], int(port), source) for port in rule['ports'] for source in rule['sources'])
        for _, _, source in desired:
            try:
                ipaddress.IPv4Network(source, strict=True)
            except ValueError:
                raise DeployError('Invalid firewall source CIDR.') from None
        return desired

    @staticmethod
    def _current(group):
        current, foreign = set(), []
        for permission in group.get('IpPermissions') or []:
            protocol, low, high = permission.get('IpProtocol'), permission.get('FromPort'), permission.get('ToPort')
            if (protocol not in ('tcp', 'udp') or low != high or permission.get('Ipv6Ranges')
                    or permission.get('PrefixListIds') or permission.get('UserIdGroupPairs')):
                foreign.append(permission)
                continue
            current.update((protocol, low, r.get('CidrIp')) for r in permission.get('IpRanges') or [])
        return current, foreign

    @staticmethod
    def _permissions(rules):
        return [{'IpProtocol': protocol, 'FromPort': port, 'ToPort': port, 'IpRanges': [{'CidrIp': source}]}
                for protocol, port, source in sorted(rules)]

    def _rules_equal(self, group):
        current, foreign = self._current(group)
        return not foreign and current == self._rules()

    def _reconcile_rules(self, group, operation_id):
        key = 'aws-pending-rules'
        desired = self._rules()
        pending = self.state.get_meta(key)
        if pending and (pending['id'] != group['GroupId'] or pending['desired'] != sorted(map(list, desired))):
            raise DeployError('Pending AWS firewall update differs from desired configuration.')
        if self._rules_equal(group):
            if pending:
                self.state.complete(pending['step'], {'verified': True})
                self.state.set_meta(key, None)
            return
        if not pending:
            pending = {'id': group['GroupId'], 'desired': sorted(map(list, desired)),
                       'step': self.state.intent(operation_id, 'firewall-rules', {'id': group['GroupId']})}
            self.state.set_meta(key, pending)
        # Re-read before each mutation so a lost response is not replayed as a duplicate.
        current, foreign = self._current(group)
        missing = desired - current
        if missing:
            self._ec2('authorize-security-group-ingress', '--group-id', group['GroupId'],
                      '--ip-permissions', json.dumps(self._permissions(missing)))
            group = self._find('firewall')
            current, foreign = self._current(group)
            if not desired <= current:
                raise DeployError('AWS firewall additions were not verified; existing rules were preserved.')
        stale = self._permissions(current - desired) + foreign
        if stale:
            self._ec2('revoke-security-group-ingress', '--group-id', group['GroupId'],
                      '--ip-permissions', json.dumps(stale))
        if not self._rules_equal(self._find('firewall')):
            raise DeployError('AWS firewall verification failed; retry to reconcile.')
        self.state.complete(pending['step'], {'verified': True})
        self.state.set_meta(key, None)

    def _remember(self, role, item):
        attributes = {'lifecycle': self.states.get((item.get('State') or {}).get('Name'), 'RUNNING')}
        if role == 'compute':
            attributes['desired'] = self._desired()
        self.state.put_resource(role, self.kinds[role], self._id(item, role), attributes, owned=True)
        pending = self.state.get_meta('aws-pending-' + role)
        if pending:
            self.state.complete(pending['step'], {'id': self._id(item, role)})
            self.state.set_meta('aws-pending-' + role, None)

    def _await_visible(self, role):
        for _ in range(24):
            item = self._find(role)
            if item:
                self._remember(role, item)
                return item
            time.sleep(5)
        raise DeployError('AWS created resource is not visible; intent preserved.')

    def _create_firewall(self, subnet, operation_id):
        if self.state.get_meta('aws-pending-firewall'):
            raise DeployError('AWS firewall creation outcome is uncertain; inspect the VPC before retrying.')
        step = self.state.intent(operation_id, 'create-firewall', {'role': 'firewall'})
        self.state.set_meta('aws-pending-firewall', {'step': step})
        self._ec2('create-security-group', '--group-name', self.config['profile'] + '-firewall',
                  '--description', 'PocketDeploy ' + self.config['profile'], '--vpc-id', subnet['VpcId'],
                  '--tag-specifications', json.dumps([self._tag_specification('security-group', 'firewall', 'firewall')]))
        return self._await_visible('firewall')

    def _launch(self, firewall, image, instance_type, public_key, operation_id):
        # A durable client token makes a retried launch return the same instance.
        pending = self.state.get_meta('aws-pending-compute')
        if not pending:
            step = self.state.intent(operation_id, 'create-compute', {'role': 'compute'})
            pending = {'step': step, 'token': self.state.deployment_id[:32] + '-' + str(step)}
            self.state.set_meta('aws-pending-compute', pending)
        args = ['run-instances', '--client-token', pending['token'], '--count', '1',
                '--image-id', image['id'], '--instance-type', self.config['aws-instance-type'],
                '--network-interfaces', json.dumps([{'DeviceIndex': 0, 'SubnetId': self.config['aws-subnet-id'],
                                                     'Groups': [firewall['GroupId']],
                                                     'AssociatePublicIpAddress': True,
                                                     'DeleteOnTermination': True}]),
                '--block-device-mappings', json.dumps([{'DeviceName': image['root-device'], 'Ebs': {
                    'VolumeSize': self.config['aws-root-volume-size-gb'],
                    'VolumeType': self.config['aws-root-volume-type'],
                    'Encrypted': True, 'DeleteOnTermination': True}}]),
                '--metadata-options', json.dumps({'HttpTokens': 'required', 'HttpEndpoint': 'enabled',
                                                  'HttpPutResponseHopLimit': 1}),
                '--tag-specifications', json.dumps([self._tag_specification('instance', 'compute', 'compute'),
                                                    self._tag_specification('volume', 'boot-volume', 'boot')]),
                '--disable-api-termination', '--user-data', 'file:///dev/stdin']
        cpu = self._cpu_options(instance_type['cores'])
        if cpu:
            args += ['--cpu-options', json.dumps(cpu)]
        # Cloud-init carries host keys, so it travels on stdin and never in argv.
        self._ec2(*args, payload=DigitalOcean._user_data(self, public_key))
        return self._await_visible('compute')

    def inspect(self):
        return {role: {'id': self._id(item, role), 'owned': True,
                       'state': self.states.get((item.get('State') or {}).get('Name'), 'UNKNOWN') if role == 'compute' else 'RUNNING'}
                if item else None for role in ('firewall', 'compute') for item in [self._find(role)]}

    def plan(self):
        subnet = self._subnet()
        self._image(self._instance_type()['arch'])
        if not self.state:
            return [{'resource': role, 'action': 'create'} for role in ('firewall', 'compute')]
        if any(self.state.get_meta('aws-delete-' + role) for role in self.kinds):
            raise DeployError('AWS deletion is pending; finish deletion first.')
        firewall, instance = self._find('firewall'), self._find('compute')
        if firewall and firewall.get('VpcId') != subnet['VpcId']:
            raise DeployError('AWS firewall belongs to an unexpected VPC.')
        drift = self._drift(instance, firewall)
        return [{'resource': 'firewall',
                 'action': 'create' if not firewall else 'retain' if self._rules_equal(firewall) else 'update'},
                {'resource': 'compute', 'changed_fields': drift,
                 'action': 'blocked' if drift else 'retain' if instance else
                 'resume' if self.state.get_meta('aws-pending-compute') else 'create'}]

    def converge(self, public_key, operation_id):
        if any(i['action'] == 'blocked' for i in self.plan()):
            raise DeployError('AWS compute settings drifted; resizing or replacement requires explicit handling.')
        subnet, instance_type = self._subnet(), self._instance_type()
        image = self._image(instance_type['arch'])
        if not self.state.get_meta('aws-image'):
            self.state.set_meta('aws-image', {'selector': image['selector'], 'id': image['id']})
        firewall = self._find('firewall') or self._create_firewall(subnet, operation_id)
        self._remember('firewall', firewall)
        self._reconcile_rules(firewall, operation_id)
        instance = self._find('compute') or self._launch(firewall, image, instance_type, public_key, operation_id)
        self._remember('compute', instance)
        for _ in range(120):
            state = (instance.get('State') or {}).get('Name')
            if state == 'running' and instance.get('PublicIpAddress'):
                if self._drift(instance, firewall):
                    raise DeployError('Created AWS instance settings differ; state preserved.')
                self._remember('compute', instance)
                return self.connection()
            if state not in ('pending', 'running'):
                raise DeployError('AWS instance is not starting; inspect its state.')
            time.sleep(5)
            instance = self._find('compute')
        raise DeployError('AWS instance startup timed out; state preserved.')

    def connection(self):
        instance = self._find('compute')
        if not instance:
            raise DeployError('No owned AWS instance.')
        address = instance.get('PublicIpAddress')
        try:
            ipaddress.IPv4Address(address)
        except (ValueError, TypeError):
            raise DeployError('AWS instance has no valid public IPv4 address.') from None
        if instance.get('SubnetId') != self.config['aws-subnet-id']:
            raise DeployError('AWS instance subnet differs from desired configuration.')
        return {'instance_id': instance['InstanceId'], 'ip': address, 'user': self.config.get('ssh-user', 'ubuntu')}

    def _owned_volumes(self):
        volumes = self._items(self._ec2('describe-volumes', '--filters', json.dumps(
            [{'Name': 'tag:pocketdeploy-id', 'Values': [self.state.deployment_id]}])), 'Volumes')
        return [v for v in volumes if v.get('State') not in ('deleted', 'deleting')]

    def plan_delete(self):
        if any(self.state.get_meta('aws-pending-' + role) for role in self.kinds):
            raise DeployError('Reconcile pending AWS creation before deletion.')
        actions = []
        for role in ('compute', 'firewall'):
            pending = self.state.get_meta('aws-delete-' + role)
            record = self.state.get_resource(role)
            item = self._find(role, allow_missing=bool(pending))
            if item and (not record or record['provider_id'] != self._id(item, role)):
                raise DeployError('Deletion requires recorded ownership and matching cloud tags.')
            if pending and record and pending['id'] != record['provider_id']:
                raise DeployError('Pending AWS deletion identity does not match recorded ownership.')
            if role == 'compute' and item and self._extra_storage(item):
                raise DeployError('AWS instance has storage that would survive termination; reconcile before deletion.')
            actions.append({'resource': role, 'action': 'delete' if item else 'absent',
                            'id': self._id(item, role) if item else record['provider_id'] if record else None,
                            'state': self.states.get((item.get('State') or {}).get('Name')) if item and role == 'compute' else None})
        actions.append({'resource': 'boot-volume', 'action': 'delete-with-compute'})
        actions.append({'resource': 'shared-network', 'action': 'retain'})
        return actions

    def delete(self, operation_id):
        if self.config.get('compute-prevent-destroy', True):
            raise DeployError('Deployment destruction is protected.')
        self.plan_delete()
        for role in ('compute', 'firewall'):
            key = 'aws-delete-' + role
            pending = self.state.get_meta(key)
            item = self._find(role, allow_missing=bool(pending))
            if not item and not pending:
                continue
            if not pending:
                pending = {'id': self._id(item, role),
                           'step': self.state.intent(operation_id, 'delete-' + role, {'id': self._id(item, role)})}
                self.state.set_meta(key, pending)
            if role == 'compute':
                if item and (item.get('State') or {}).get('Name') != 'shutting-down':
                    # Both calls are idempotent on the exact verified instance ID.
                    self._ec2('modify-instance-attribute', '--instance-id', pending['id'], '--no-disable-api-termination')
                    self._ec2('terminate-instances', '--instance-ids', pending['id'])
                attempts = 120
            else:
                attempts = 30
            for attempt in range(attempts):
                if role == 'firewall' and self._find(role, allow_missing=True):
                    try:
                        self._ec2('delete-security-group', '--group-id', pending['id'])
                    except DeployError as error:
                        # Network interfaces release the group shortly after termination.
                        if error.code != 'resource_in_use':
                            raise
                if self._find(role, allow_missing=True) is None and not (role == 'compute' and self._owned_volumes()):
                    break
                if attempt + 1 == attempts:
                    raise DeployError('AWS deletion is not complete; retry delete to reconcile.', code='deletion_pending')
                time.sleep(5)
            if role == 'firewall':
                unfinished = self.state.get_meta('aws-pending-rules')
                if unfinished:
                    self.state.complete(unfinished['step'], {'deleted': True})
                    self.state.set_meta('aws-pending-rules', None)
            self.state.complete_delete(role, pending['step'], key)
        return {'deleted': True}

    def adopt(self, instance_id, operation_id):
        raise DeployError('AWS adoption is unsupported; restore the original controller checkpoint.')
