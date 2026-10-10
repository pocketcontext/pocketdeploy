"""Explicit, interruption-safe transfer of one existing application A record."""
import hashlib
import ipaddress
import json

from .common import DeployError
from .config import manages_dns
from .services import Services, _call, _identifier


def settings(record):
    """Allowlist routing settings; never expose provider metadata."""
    return {key: record.get(key) for key in ('type', 'name', 'content', 'proxied', 'ttl', 'comment')}


def fingerprint(record):
    return hashlib.sha256(json.dumps(settings(record), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _context(config, state, connection, evidence):
    host = evidence['host']
    if (config.get('provider-dns') != 'cloudflare'
            or config.get('cloudflare-zone-id') != evidence['zone']
            or sum(a['host'] == host and manages_dns(config, a)
                   for a in config.get('once', {}).get('applications', [])) != 1):
        raise DeployError('DNS adoption requires one managed application in the exact configured Cloudflare zone.')
    compute = state.get_resource('compute')
    if (not compute or not compute['owned'] or connection.get('instance_id') != compute['provider_id']
            or connection.get('ip') != evidence['ipv4']):
        raise DeployError('DNS adoption requires the current public IPv4 of recorded owned compute.')
    try:
        ipaddress.IPv4Address(evidence['ipv4'])
    except ValueError:
        raise DeployError('DNS adoption requires an IPv4 address.') from None
    zone = _call('cf', ['zones', 'get', '--zone', evidence['zone']])
    zone_name = zone.get('name')
    if (_identifier(zone) != evidence['zone'] or not isinstance(zone_name, str)
            or (host != zone_name and not host.endswith('.' + zone_name))):
        raise DeployError('DNS zone identity or hostname membership differs.')
    return Services(config, state)


def _record(services, evidence):
    rows = services._records(evidence['host'])
    matches = [r for r in rows if r.get('type') in ('A', 'AAAA', 'CNAME')]
    if (len(matches) != 1 or matches[0].get('id') != evidence['record_id']
            or matches[0].get('type') != 'A' or matches[0].get('name') != evidence['host']
            or matches[0].get('content') != evidence['ipv4']
            or type(matches[0].get('proxied')) is not bool or type(matches[0].get('ttl')) is not int):
        raise DeployError('DNS evidence differs or conflicting address records exist; preserve recovery state.')
    return matches[0]


def adopt(config, state, connection, operation, evidence):
    """Change only the ownership comment; ordinary convergence changes routing policy."""
    if evidence.get('previous_dns_manager_disabled') is not True:
        raise DeployError('Disable and drain the previous DNS manager before adoption.')
    services = _context(config, state, connection, evidence)
    name = 'dns:A:' + evidence['host']
    key = 'dns-adoption:' + name
    pending = state.get_meta(key)
    saved = state.get_resource(name)
    expected_attrs = {'zone': evidence['zone'], 'type': 'A', 'name': evidence['host'], 'content': evidence['ipv4']}
    if saved and (not saved['owned'] or saved['kind'] != 'cloudflare-dns'
                  or saved['provider_id'] != evidence['record_id']
                  or any(saved['attributes'].get(k) != v for k, v in expected_attrs.items())):
        raise DeployError('DNS resource already has different recorded ownership.')
    if state.get_meta('pending:' + name):
        raise DeployError('Resolve the previous DNS operation before adoption.')
    current = _record(services, evidence)
    if pending:
        if pending.get('evidence') != evidence:
            raise DeployError('Retry DNS adoption with the original exact evidence.')
        body = pending['body']
        if settings(current) != body and fingerprint(current) != evidence['settings_sha256']:
            raise DeployError('DNS settings changed during adoption; preserve recovery state.')
    else:
        if saved:
            raise DeployError('DNS is already owned; use ordinary convergence.')
        if fingerprint(current) != evidence['settings_sha256']:
            raise DeployError('DNS settings fingerprint differs from explicit evidence.')
        body = {**settings(current), 'comment': services.marker}
        step = state.intent(operation, name, {'action': 'adopt', 'record_id': evidence['record_id']})
        pending = {'evidence': evidence, 'body': body, 'step': step}
        state.set_meta(key, pending)
    if settings(current) != body:
        # The durable intent precedes PATCH. A lost response is recovered by reading
        # the exact identity and complete routing settings on the next invocation.
        services._cf(['edit', evidence['record_id'], '--body', json.dumps({'comment': services.marker})])
    verified = _record(services, evidence)
    if settings(verified) != body:
        raise DeployError('DNS adoption is not verified; retry with the original evidence.')
    # Keep completed receipt for idempotent retries after a lost final response.
    state.put_resource(name, 'cloudflare-dns', evidence['record_id'], expected_attrs)
    state.complete(pending['step'], {'id': evidence['record_id'], 'adopted': True})
    for row in state.db.execute("SELECT id,payload FROM steps WHERE step=? AND status='pending'", (name,)).fetchall():
        payload = json.loads(row['payload'])
        if payload == {'action': 'adopt', 'record_id': evidence['record_id']}:
            state.complete(row['id'], {'id': evidence['record_id'], 'adopted': True, 'recovered': True})
    state.set_meta(key, {**pending, 'completed': True})
    return {'adopted': True, 'host': evidence['host'], 'zone': evidence['zone'],
            'record_id': evidence['record_id'], 'ipv4': evidence['ipv4'],
            'proxied': verified['proxied'], 'ttl': verified['ttl'],
            'settings_sha256': fingerprint(verified)}


def inspect(config, state, connection, host):
    """Read-only exact adoption evidence without provider response disclosure."""
    evidence = {'host': host, 'zone': config.get('cloudflare-zone-id'), 'ipv4': connection.get('ip')}
    services = _context(config, state, connection, evidence)
    rows = services._records(host)
    addresses = [r for r in rows if r.get('type') in ('A', 'AAAA', 'CNAME')]
    if len(addresses) != 1:
        raise DeployError('DNS evidence requires exactly one A record and no address conflicts.')
    evidence['record_id'] = _identifier(addresses[0])
    current = _record(services, evidence)
    return {**evidence, 'proxied': current['proxied'], 'ttl': current['ttl'],
            'settings_sha256': fingerprint(current)}
