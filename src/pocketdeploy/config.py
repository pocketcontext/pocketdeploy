"""Flat Colors configuration; secret resolution stays out of CLI output."""
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path

from blue.cli import load_yaml, read_pars, par_name
from .common import DeployError, local_path

DEFAULTS = {
    'schema-version': 1, 'provider-compute': 'oci', 'workdir': '.colors',
    'state-file': '.colors.sqlite', 'oci-auth': 'security_token',
    'oci-shape': 'VM.Standard.A1.Flex', 'oci-ocpus': 1,
    'oci-memory-in-gbs': 6, 'oci-boot-volume-size-in-gbs': 50,
    'oci-boot-volume-vpus-per-gb': 10,
    'compute-prevent-destroy': True, 'compute-require-existing-state': False,
    'compute-ssh-sources': [], 'compute-http-sources': [],
    'provider-dns': 'no-infra', 'provider-smtp': 'no-infra',
    'vault-save-after-run': False, 'once-version': 'v0.3.3',
}
ROOT_KEYS = set(DEFAULTS) | {
    'profile', 'oci-config-file-profile', 'oci-region', 'oci-compartment-id',
    'oci-subnet-id', 'oci-availability-domain', 'oci-image-id', 'ssh-user',
    'ssh-private-key-file', 'ssh-public-key-file', 'ssh-known-hosts-file',
    'vault-id', 'vault-state-document-id', 'vault-command',
    'compute-retain-boot-volume', 'once',
    'ssh-host-private-key-file', 'ssh-host-public-key-file',
}


def load(path, *, env=None, resolve=True):
    path = Path(path).absolute()
    if path.is_symlink():
        raise DeployError('Configuration symlinks are not supported.')
    try:
        if path.stat().st_size > 1024 * 1024:
            raise DeployError('Configuration exceeds size limit.')
        raw = load_yaml(path.read_text())
    except Exception as exc:
        if isinstance(exc, DeployError):
            raise
        raise DeployError('Cannot parse configuration.') from None
    if not isinstance(raw, dict):
        raise DeployError('Configuration must be a mapping.')
    if set(raw) - ROOT_KEYS:
        raise DeployError('Unsupported configuration field; see the configuration reference.')
    overlaid = read_pars({**DEFAULTS, **raw}, os.environ if env is None else env)
    # Do not copy unrelated operator credentials into this deployment's state.
    config = {key: value for key, value in overlaid.items() if key in ROOT_KEYS}
    config['_root'] = str(path.parent)
    config['_file'] = str(path)
    validate(config)
    if resolve:
        resolve_env(config, os.environ if env is None else env)
    config['_desired_hash'] = hashlib.sha256(json.dumps(
        {k: v for k, v in config.items() if not k.startswith('_')}, sort_keys=True).encode()).hexdigest()
    return config


def scope(c):
    return {'provider': 'oci', 'profile': c['oci-config-file-profile'],
            'region': c.get('oci-region', ''), 'compartment': c['oci-compartment-id'],
            'subnet': c['oci-subnet-id']}


def validate(c):
    if c['schema-version'] != 1 or c['provider-compute'] != 'oci':
        raise DeployError('Only schema 1 and OCI compute are supported.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', str(c.get('profile', ''))):
        raise DeployError('Profile must be a lowercase identifier of at most 40 characters.')
    for key in ['oci-config-file-profile', 'oci-compartment-id', 'oci-subnet-id', 'oci-availability-domain']:
        if not isinstance(c.get(key), str) or not c[key].strip() or '<' in c[key]:
            raise DeployError('OCI profile, compartment, subnet and availability domain are required.')
    for key in ['compute-prevent-destroy', 'compute-require-existing-state', 'vault-save-after-run', 'compute-retain-boot-volume']:
        if key in c and type(c[key]) is not bool:
            raise DeployError('Protection, retention and backup flags must be booleans.')
    for key in ['oci-ocpus', 'oci-memory-in-gbs', 'oci-boot-volume-size-in-gbs']:
        if type(c[key]) not in (int, float) or c[key] <= 0:
            raise DeployError('Compute sizes must be positive numbers.')
    for key in ['compute-ssh-sources', 'compute-http-sources']:
        if not isinstance(c[key], list):
            raise DeployError('Firewall sources must be CIDR lists.')
        try:
            for source in c[key]:
                if ipaddress.ip_network(source).version != 4:
                    raise ValueError()
        except (ValueError, TypeError):
            raise DeployError('Firewall sources must contain valid IPv4 CIDRs.') from None
    if c['provider-dns'] != 'no-infra' or c['provider-smtp'] != 'no-infra':
        raise DeployError('V1 requires externally managed DNS and SMTP.')
    if c['once-version'] != 'v0.3.3':
        raise DeployError('V1 supports the checksum-pinned ONCE v0.3.3 release.')
    for key in ['state-file', 'workdir', 'ssh-private-key-file', 'ssh-public-key-file', 'ssh-known-hosts-file', 'ssh-host-private-key-file', 'ssh-host-public-key-file']:
        if key in c:
            local_path(c['_root'], c[key])
    once = c.get('once', {})
    if not isinstance(once, dict) or set(once) - {'applications', 'namespace'}:
        raise DeployError('Unsupported ONCE configuration.')
    if not isinstance(once.get('applications', []), list):
        raise DeployError('ONCE applications must be a list.')
    seen = set()
    for app in once.get('applications', []):
        if not isinstance(app, dict):
            raise DeployError('Each application must be a mapping.')
        host = app.get('host', '')
        if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*[a-z0-9]', host) or host in seen:
            raise DeployError('Application hosts must be unique valid hostnames.')
        seen.add(host)
        if not isinstance(app.get('image'), str) or not app['image'] or app['image'].startswith('-'):
            raise DeployError('Each application needs an image.')
        if app.get('github') or app.get('smtp') is True or app.get('manage-dns') is True:
            raise DeployError('GitHub publication and managed DNS/SMTP are not implemented in v1.')


def resolve_env(c, env):
    for app in c.get('once', {}).get('applications', []):
        bindings = app.get('env', {})
        resolved = {}
        if isinstance(bindings, dict):
            for name, reference in bindings.items():
                if not isinstance(reference, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', reference):
                    raise DeployError('Environment references must be lowercase parameter names.')
                variable = par_name(reference)
                if variable not in env:
                    raise DeployError('Required application environment parameter is missing.')
                resolved[name] = env[variable]
        elif isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, str) or '=' not in binding:
                    raise DeployError('Literal environment entries must use KEY=value.')
                key, value = binding.split('=', 1)
                if key in resolved:
                    raise DeployError('Duplicate application environment variable.')
                resolved[key] = value
        else:
            raise DeployError('Application env must be a reference mapping or literal list.')
        for key, value in resolved.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) or not isinstance(value, str) or '\x00' in value:
                raise DeployError('Invalid application environment binding.')
        app['resolved-env'] = resolved
