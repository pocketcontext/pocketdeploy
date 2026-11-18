import copy
import json
import pytest
from pocketdeploy import config as deployment
from pocketdeploy.aws import AWS
from pocketdeploy.common import DeployError
from pocketdeploy.state import State

AMI = 'ami-0123456789abcdef0'


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    config = {'profile': 'test', 'aws-profile': 'synthetic', 'aws-region': 'eu-central-1',
              'aws-subnet-id': 'subnet-0123456789abcdef0', 'aws-instance-type': 'm8i.xlarge',
              'aws-root-volume-size-gb': 100, 'aws-root-volume-type': 'gp3',
              'aws-nested-virtualization': True, 'aws-threads-per-core': 1,
              'compute-prevent-destroy': False, 'compute-ssh-ports': ['22', '2022'],
              'compute-ssh-sources': ['192.0.2.1/32'], 'compute-http-sources': [],
              'compute-ingress': [{'protocol': 'udp', 'ports': ['443'], 'sources': ['0.0.0.0/0']}]}
    with State(tmp_path / 'state.sqlite', 'test', {}, create=True) as state:
        a = AWS(config, state)
        monkeypatch.setattr('pocketdeploy.aws.time.sleep', lambda _: None)
        yield a


class Cloud:
    def __init__(self, nested=True):
        self.instances, self.groups, self.volumes, self.calls = [], [], [], []
        self.nested = nested
        self.launches = {}

    @staticmethod
    def tags(spec):
        return copy.deepcopy(spec['Tags'])

    @staticmethod
    def matches(item, filters):
        tags = {t['Key']: t['Value'] for t in item.get('Tags', [])}
        for f in filters:
            name, values = f['Name'], f['Values']
            if name.startswith('tag:') and tags.get(name[4:]) not in values:
                return False
            if name == 'instance-id' and item.get('InstanceId') not in values:
                return False
            if name == 'group-id' and item.get('GroupId') not in values:
                return False
        return True

    def option(self, args, name):
        return args[args.index(name) + 1]

    def __call__(self, service, *args, payload=None, timeout=180):
        args = list(args)
        self.calls.append((service, args, payload))
        verb = args[0]
        if service == 'ssm':
            return {'Parameter': {'Value': AMI}}
        if verb == 'describe-subnets':
            return {'Subnets': [{'SubnetId': self.option(args, '--subnet-ids'), 'VpcId': 'vpc-0abc'}]}
        if verb == 'describe-instance-types':
            features = ['nested-virtualization'] if self.nested else []
            return {'InstanceTypes': [{'ProcessorInfo': {'SupportedArchitectures': ['x86_64'], 'SupportedFeatures': features},
                                       'VCpuInfo': {'DefaultCores': 2, 'DefaultThreadsPerCore': 2, 'ValidThreadsPerCore': [1, 2]}}]}
        if verb == 'describe-images':
            return {'Images': [{'ImageId': self.option(args, '--image-ids'), 'State': 'available',
                                'Architecture': 'x86_64', 'RootDeviceType': 'ebs', 'RootDeviceName': '/dev/sda1'}]}
        if verb == 'describe-instances':
            filters = json.loads(self.option(args, '--filters'))
            return {'Reservations': [{'Instances': [copy.deepcopy(i) for i in self.instances if self.matches(i, filters)]}]}
        if verb == 'describe-security-groups':
            filters = json.loads(self.option(args, '--filters'))
            return {'SecurityGroups': [copy.deepcopy(g) for g in self.groups if self.matches(g, filters)]}
        if verb == 'describe-volumes':
            filters = json.loads(self.option(args, '--filters'))
            return {'Volumes': [copy.deepcopy(v) for v in self.volumes if self.matches(v, filters)]}
        if verb == 'create-security-group':
            specs = json.loads(self.option(args, '--tag-specifications'))
            self.groups.append({'GroupId': 'sg-%04d' % len(self.calls), 'VpcId': self.option(args, '--vpc-id'),
                                'GroupName': self.option(args, '--group-name'), 'Tags': self.tags(specs[0]),
                                'IpPermissions': []})
            return {'GroupId': self.groups[-1]['GroupId']}
        if verb == 'authorize-security-group-ingress':
            self.groups[0]['IpPermissions'] += json.loads(self.option(args, '--ip-permissions'))
            return {'Return': True}
        if verb == 'revoke-security-group-ingress':
            revoke = json.loads(self.option(args, '--ip-permissions'))
            self.groups[0]['IpPermissions'] = [p for p in self.groups[0]['IpPermissions'] if p not in revoke]
            return {'Return': True}
        if verb == 'run-instances':
            token = self.option(args, '--client-token')
            if not 1 <= len(token) <= 64:
                raise DeployError('Command failed; output suppressed.')
            if token in self.launches:
                return {'Instances': [self.launches[token]]}
            specs = {s['ResourceType']: s for s in json.loads(self.option(args, '--tag-specifications'))}
            cpu = json.loads(self.option(args, '--cpu-options')) if '--cpu-options' in args else {}
            interface = json.loads(self.option(args, '--network-interfaces'))[0]
            instance = {'InstanceId': 'i-%04d' % len(self.calls), 'State': {'Name': 'pending'},
                        'InstanceType': self.option(args, '--instance-type'), 'ImageId': self.option(args, '--image-id'),
                        'SubnetId': interface['SubnetId'], 'Tags': self.tags(specs['instance']),
                        'SecurityGroups': [{'GroupId': g} for g in interface['Groups']],
                        'CpuOptions': {'CoreCount': cpu.get('CoreCount', 2), 'ThreadsPerCore': cpu.get('ThreadsPerCore', 2),
                                       'NestedVirtualization': cpu.get('NestedVirtualization', 'disabled')},
                        'RootDeviceName': '/dev/sda1',
                        'BlockDeviceMappings': [{'DeviceName': '/dev/sda1', 'Ebs': {'VolumeId': 'vol-1', 'DeleteOnTermination': True}}]}
            self.instances.append(instance)
            self.volumes.append({'VolumeId': 'vol-1', 'State': 'in-use', 'Tags': self.tags(specs['volume'])})
            self.launches[token] = instance
            return {'Instances': [instance]}
        if verb == 'modify-instance-attribute':
            return None
        if verb == 'terminate-instances':
            self.instances[0]['State'] = {'Name': 'terminated'}
            self.volumes.clear()
            return {}
        if verb == 'delete-security-group':
            self.groups.clear()
            return None
        raise AssertionError(args)

    def start(self):
        for instance in self.instances:
            instance['State'] = {'Name': 'running'}
            instance['PublicIpAddress'] = '192.0.2.10'


def install(a, monkeypatch, cloud):
    def call(service, *args, payload=None, timeout=180):
        result = cloud(service, *args, payload=payload, timeout=timeout)
        if args[0] == 'run-instances' or args[0] == 'describe-instances' and cloud.instances:
            cloud.start()
        return result
    monkeypatch.setattr(a, '_call', call)


def converge(a):
    operation = a.state.begin_operation('converge', 'hash')
    return operation, a.converge('ssh-ed25519 synthetic', operation)


def test_lifecycle_creates_nested_host_and_cleans_up(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, connection = converge(adapter)
    assert connection == {'instance_id': cloud.instances[0]['InstanceId'], 'ip': '192.0.2.10', 'user': 'ubuntu'}
    service, args, payload = next(c for c in cloud.calls if c[1][0] == 'run-instances')
    assert json.loads(cloud.option(args, '--cpu-options')) == {'CoreCount': 2, 'ThreadsPerCore': 1, 'NestedVirtualization': 'enabled'}
    assert json.loads(cloud.option(args, '--metadata-options'))['HttpTokens'] == 'required'
    disk = json.loads(cloud.option(args, '--block-device-mappings'))[0]
    assert disk == {'DeviceName': '/dev/sda1', 'Ebs': {'VolumeSize': 100, 'VolumeType': 'gp3', 'Encrypted': True, 'DeleteOnTermination': True}}
    assert '--disable-api-termination' in args
    assert cloud.option(args, '--user-data') == 'file:///dev/stdin'
    assert 'ssh-ed25519 synthetic' not in ' '.join(args)
    assert json.loads(payload.split('\n', 1)[1])['users'][0]['ssh_authorized_keys'] == ['ssh-ed25519 synthetic']
    assert cloud.groups[0]['GroupName'] == 'test-firewall'
    rules = {(p['IpProtocol'], p['FromPort'], p['IpRanges'][0]['CidrIp']) for p in cloud.groups[0]['IpPermissions']}
    assert rules == {('tcp', 22, '192.0.2.1/32'), ('tcp', 2022, '192.0.2.1/32'), ('udp', 443, '0.0.0.0/0')}
    assert adapter.state.get_meta('aws-image')['id'] == AMI
    cloud.calls.clear()
    adapter.converge('ssh-ed25519 synthetic', operation)
    assert all(c[1][0].startswith('describe-') or c[0] == 'ssm' for c in cloud.calls)
    assert [a['action'] for a in adapter.plan_delete()] == ['delete', 'delete', 'delete-with-compute', 'retain']
    adapter.delete(operation)
    verbs = [c[1][0] for c in cloud.calls]
    assert verbs.index('modify-instance-attribute') < verbs.index('terminate-instances') < verbs.index('delete-security-group')
    assert adapter.state.get_resource('compute') is None and adapter.state.get_resource('firewall') is None


def test_interrupted_launch_retries_with_the_same_client_token(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    hidden = {'count': 0}
    original = cloud.matches

    def invisible(item, filters):
        if 'InstanceId' in item and hidden['count'] < 24:
            hidden['count'] += 1
            return False
        return original(item, filters)
    cloud.matches = invisible
    operation = adapter.state.begin_operation('converge', 'hash')
    with pytest.raises(DeployError, match='not visible'):
        adapter.converge('ssh-ed25519 synthetic', operation)
    token = adapter.state.get_meta('aws-pending-compute')['token']
    adapter.converge('ssh-ed25519 synthetic', operation)
    launches = [c for c in cloud.calls if c[1][0] == 'run-instances']
    assert len(cloud.instances) == 1
    assert all(cloud.option(args, '--client-token') == token for _, args, _ in launches)
    assert adapter.state.get_meta('aws-pending-compute') is None


def test_client_tokens_fit_the_ec2_limit_and_replace_overlong_legacy_tokens(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation = adapter.state.begin_operation('converge', 'hash')
    step = adapter.state.intent(operation, 'create-compute', {'role': 'compute'})
    legacy = adapter.state.deployment_id[:32] + '-' + str(step)
    assert len(legacy) > 64
    adapter.state.set_meta('aws-pending-compute', {'step': step, 'token': legacy})
    adapter.converge('ssh-ed25519 synthetic', operation)
    _, args, _ = next(c for c in cloud.calls if c[1][0] == 'run-instances')
    token = cloud.option(args, '--client-token')
    assert len(token) == 64 and token == adapter._client_token(step)
    assert adapter.state.get_resource('compute')['provider_id'] == cloud.instances[0]['InstanceId']


def test_drift_blocks_convergence(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    cloud.instances[0]['InstanceType'] = 'm8i.2xlarge'
    cloud.instances[0]['CpuOptions']['NestedVirtualization'] = 'disabled'
    plan = adapter.plan()
    assert plan[1]['action'] == 'blocked'
    assert set(plan[1]['changed_fields']) == {'aws-instance-type', 'aws-nested-virtualization'}
    with pytest.raises(DeployError, match='drifted'):
        adapter.converge('ssh-ed25519 synthetic', operation)


def test_stale_and_foreign_rules_are_revoked(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    cloud.groups[0]['IpPermissions'].append({'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]})
    cloud.groups[0]['IpPermissions'].append({'IpProtocol': 'tcp', 'FromPort': 3389, 'ToPort': 3389,
                                            'IpRanges': [{'CidrIp': '0.0.0.0/0'}]})
    assert adapter.plan()[0]['action'] == 'update'
    adapter.converge('ssh-ed25519 synthetic', operation)
    assert adapter._rules_equal(cloud.groups[0])
    assert adapter.state.get_meta('aws-pending-rules') is None


def test_unrecorded_tagged_instance_is_never_adopted(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    converge(adapter)
    adapter.state.remove_resource('compute')
    with pytest.raises(DeployError, match='checkpoint recovery'):
        adapter.plan()


def test_nested_virtualization_requires_a_supporting_type(adapter, monkeypatch):
    install(adapter, monkeypatch, Cloud(nested=False))
    with pytest.raises(DeployError, match='nested'):
        adapter.plan()


def test_extra_storage_blocks_deletion(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    converge(adapter)
    cloud.instances[0]['BlockDeviceMappings'].append({'DeviceName': '/dev/sdf', 'Ebs': {'DeleteOnTermination': False}})
    with pytest.raises(DeployError, match='survive termination'):
        adapter.plan_delete()


def test_nested_virtualization_alone_sends_only_that_cpu_option(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    del adapter.config['aws-threads-per-core']
    converge(adapter)
    _, args, _ = next(c for c in cloud.calls if c[1][0] == 'run-instances')
    assert json.loads(cloud.option(args, '--cpu-options')) == {'NestedVirtualization': 'enabled'}


def test_default_cpu_options_are_omitted(adapter, monkeypatch):
    cloud = Cloud(nested=False)
    install(adapter, monkeypatch, cloud)
    del adapter.config['aws-threads-per-core']
    adapter.config['aws-nested-virtualization'] = False
    converge(adapter)
    _, args, _ = next(c for c in cloud.calls if c[1][0] == 'run-instances')
    assert '--cpu-options' not in args


def test_threads_per_core_requires_configurable_cpu_options(adapter, monkeypatch):
    cloud = Cloud()
    original = cloud.__call__

    def call(service, *args, payload=None, timeout=180):
        result = original(service, *args, payload=payload, timeout=timeout)
        if args[0] == 'describe-instance-types':
            result['InstanceTypes'][0]['VCpuInfo'] = {'DefaultCores': 2, 'DefaultThreadsPerCore': 2}
        return result
    monkeypatch.setattr(adapter, '_call', call)
    with pytest.raises(DeployError, match='threads-per-core'):
        adapter.plan()


def test_launched_host_does_not_depend_on_a_deregistered_image(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    original = cloud.__call__

    def deregistered(service, *args, payload=None, timeout=180):
        if args[0] == 'describe-images':
            return {'Images': []}
        return original(service, *args, payload=payload, timeout=timeout)
    install(adapter, monkeypatch, type('C', (), {'__call__': staticmethod(deregistered), 'instances': cloud.instances,
                                                 'start': cloud.start})())
    assert adapter.plan()[1]['action'] == 'retain'
    adapter.converge('ssh-ed25519 synthetic', operation)


def test_security_group_deletion_waits_for_released_interfaces(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    original, busy = cloud.__call__, {'count': 2}

    def call(service, *args, payload=None, timeout=180):
        if args[0] == 'delete-security-group' and busy['count']:
            busy['count'] -= 1
            raise DeployError('AWS resource is still in use.', code='resource_in_use')
        return original(service, *args, payload=payload, timeout=timeout)
    monkeypatch.setattr(adapter, '_call', call)
    adapter.delete(operation)
    assert busy['count'] == 0 and not cloud.groups
    assert adapter.state.get_resource('firewall') is None


def test_security_group_deletion_does_not_hide_other_failures(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    original = cloud.__call__

    def call(service, *args, payload=None, timeout=180):
        if args[0] == 'delete-security-group':
            raise DeployError('Command failed; output suppressed.', code='command_failed')
        return original(service, *args, payload=payload, timeout=timeout)
    monkeypatch.setattr(adapter, '_call', call)
    with pytest.raises(DeployError, match='Command failed'):
        adapter.delete(operation)
    assert adapter.state.get_meta('aws-delete-firewall')


def test_compute_deletion_waits_for_the_boot_volume(adapter, monkeypatch):
    cloud = Cloud()
    install(adapter, monkeypatch, cloud)
    operation, _ = converge(adapter)
    original, lingering = cloud.__call__, {'count': 2}

    def call(service, *args, payload=None, timeout=180):
        if args[0] == 'terminate-instances':
            cloud.instances[0]['State'] = {'Name': 'terminated'}
            return {}
        if args[0] == 'describe-volumes' and lingering['count']:
            lingering['count'] -= 1
            if not lingering['count']:
                cloud.volumes.clear()
        return original(service, *args, payload=payload, timeout=timeout)
    monkeypatch.setattr(adapter, '_call', call)
    adapter.delete(operation)
    assert lingering['count'] == 0 and adapter.state.get_resource('compute') is None


def test_deletion_is_protected_by_default(adapter, monkeypatch):
    install(adapter, monkeypatch, Cloud())
    adapter.config['compute-prevent-destroy'] = True
    with pytest.raises(DeployError, match='protected'):
        adapter.delete('operation')


def test_ambient_credentials_and_selectors_do_not_reach_the_cli(adapter, monkeypatch):
    seen = {}

    def fake_run(command, *, input=None, timeout=120, env=None, error_classifier=None):
        seen['command'], seen['env'] = command, env
        return '{"Subnets": []}'
    monkeypatch.setattr('pocketdeploy.aws.run', fake_run)
    monkeypatch.setenv('AWS_PROFILE', 'other')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'synthetic')
    with pytest.raises(DeployError):
        adapter._subnet()
    assert seen['command'][:5] == ['aws', '--profile', 'synthetic', '--region', 'eu-central-1']
    assert 'AWS_PROFILE' not in seen['env'] and 'AWS_ACCESS_KEY_ID' not in seen['env']
    assert seen['env']['AWS_PAGER'] == '' and seen['env']['AWS_CLI_AUTO_PROMPT'] == 'off'


def test_authentication_failures_are_classified_without_output():
    error = AWS._classify('An error occurred (AuthFailure) when calling the DescribeSubnets operation: secret detail')
    assert error.code == 'provider_authentication_failed' and 'secret detail' not in str(error)
    assert AWS._classify('An error occurred (InvalidParameterValue)') is None
    assert AWS._classify('The config profile (missing) could not be found').code == 'credentials_missing'
    assert AWS._classify('An error occurred (DependencyViolation) when calling the DeleteSecurityGroup '
                         'operation: resource sg-1 has a dependent object').code == 'resource_in_use'


def write(tmp_path, body):
    path = tmp_path / 'colors.yml'
    path.write_text(body)
    return path


BASE = """profile: example-aws
provider-compute: aws
aws-profile: synthetic
aws-region: eu-central-1
aws-subnet-id: subnet-0123456789abcdef0
"""


def test_configuration_defaults_and_scope(tmp_path):
    config = deployment.load(write(tmp_path, BASE), env={}, resolve=False)
    assert config['aws-instance-type'] == 't3.small'
    assert config['aws-root-volume-size-gb'] == 50 and config['aws-nested-virtualization'] is False
    assert deployment.scope(config) == {'provider': 'aws', 'profile': 'synthetic', 'region': 'eu-central-1',
                                        'subnet': 'subnet-0123456789abcdef0'}


@pytest.mark.parametrize('extra', [
    'aws-subnet-id: sg-123\n', 'aws-region: Frankfurt\n', 'aws-image-id: image-1\n',
    'aws-root-volume-size-gb: 4\n', 'aws-root-volume-type: st1\n',
    'aws-nested-virtualization: "yes"\n', 'aws-threads-per-core: 3\n', 'aws-threads-per-core: true\n',
    'oci-shape: VM.Standard.A1.Flex\n'])
def test_invalid_configuration_is_rejected(tmp_path, extra):
    lines = [line for line in BASE.splitlines(keepends=True) if not line.startswith(extra.split(':')[0] + ':')]
    with pytest.raises(DeployError):
        deployment.load(write(tmp_path, ''.join(lines) + extra), env={}, resolve=False)


def test_example_configuration_loads():
    from pathlib import Path
    config = deployment.load(Path(__file__).parent.parent / 'examples' / 'colors-aws.yml', env={}, resolve=False)
    assert config['aws-instance-type'] == 'm8i.xlarge' and config['aws-nested-virtualization'] is True
    assert config['aws-threads-per-core'] == 1
