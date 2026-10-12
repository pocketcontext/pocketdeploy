"""Cloudflare A-record lifecycle for controllers independent of ONCE.

Call under the deployment lock. Hosts are explicit; removing one from desired
configuration never authorizes removing its recorded resource.
"""
import ipaddress
import os
import re

from .common import DeployError
from .services import Services, _call, _identifier


class CloudflareDNS(Services):
    def __init__(self, config, state, hosts, *, proxied=True, ttl=1):
        hosts = list(hosts)
        if (not hosts or len(set(hosts)) != len(hosts)
                or any(not isinstance(h, str) or len(h) > 253
                       or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', h)
                       or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                              for label in h.split('.')) for h in hosts)):
            raise DeployError('DNS requires unique lowercase hostnames.')
        if type(proxied) is not bool or type(ttl) is not int or (ttl != 1 and not 60 <= ttl <= 86400):
            raise DeployError('DNS proxy and TTL settings are invalid.')
        super().__init__(dict(config), state)
        self.hosts, self.proxied, self.ttl = hosts, proxied, ttl
        self._ready = False

    def _call(self, args):
        env = dict(os.environ)
        token = env.get('COLORS_PAR_CLOUDFLARE_API_TOKEN') or env.get('CLOUDFLARE_API_TOKEN')
        if not token:
            raise DeployError('Set CLOUDFLARE_API_TOKEN or COLORS_PAR_CLOUDFLARE_API_TOKEN.', code='credentials_missing')
        env['CLOUDFLARE_API_TOKEN'] = token
        return _call('cf', args, env=env)

    def _zone(self):
        explicit = self.c.get('cloudflare-zone-id')
        if explicit:
            zone = self._call(['zones', 'get', '--zone', explicit])
            if _identifier(zone) != explicit:
                raise DeployError('Cloudflare zone identity does not match.')
            zones = [zone]
        else:
            zones = []
            for page in range(1, 101):
                rows = self._call(['zones', 'list', '--page', str(page), '--per-page', '50'])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise DeployError('Cloudflare returned an invalid zone list.')
                zones.extend(rows)
                if len(rows) < 50:
                    break
            else:
                raise DeployError('Cloudflare zone pagination exceeded its safety limit.')
        selected = []
        for host in self.hosts:
            matches = [z for z in zones if isinstance(z.get('name'), str)
                       and (host == z['name'] or host.endswith('.' + z['name']))]
            if len(matches) != 1:
                raise DeployError('DNS zone is absent or ambiguous; configure an exact Cloudflare zone ID.')
            selected.append(_identifier(matches[0]))
        if len(set(selected)) != 1:
            raise DeployError('Managed hostnames must belong to one Cloudflare zone.')
        self.c['cloudflare-zone-id'] = selected[0]
        self._ready = True

    def _cf(self, args):
        if not self._ready:
            self._zone()
        return self._call(['dns', 'records', *args, '--zone', self.c['cloudflare-zone-id']])

    def _desired(self, connection):
        address = self._ip(connection) if connection else '0.0.0.0'
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            raise DeployError('DNS requires the verified compute IPv4 address.') from None
        return [{'type': 'A', 'name': host, 'content': address,
                 'proxied': self.proxied, 'ttl': self.ttl} for host in self.hosts]

    def _dns(self, desired, op=None):
        saved = self.state.get_resource('dns:A:' + desired['name'])
        if saved and (not saved['owned'] or saved['kind'] != 'cloudflare-dns'
                      or saved['attributes'].get('name') != desired['name']
                      or saved['attributes'].get('type') != 'A'):
            raise DeployError('Recorded DNS ownership differs from the explicit hostname.')
        return super()._dns(desired, op)

    def preflight(self):
        self._zone()
        self.plan()

    def plan(self, connection=None):
        if not self._ready:
            self._zone()
        actions = [self._dns(row) for row in self._desired(connection)]
        if connection is None:
            actions = [{**row, 'action': 'resolve-after-compute'} for row in actions]
        return {'actions': actions}

    def converge(self, connection, operation_id):
        if not connection:
            raise DeployError('DNS convergence requires verified compute.')
        # Check every host before the first mutation.
        self.plan(connection)
        return {'actions': [self._dns(row, operation_id) for row in self._desired(connection)]}

    def _delete_resources(self):
        names = {'dns:A:' + host for host in self.hosts}
        return [r for r in self.state.resources() if r['kind'] == 'cloudflare-dns' and r['name'] in names]

    def plan_delete(self):
        if not self._ready:
            self._zone()
        for host in self.hosts:
            name = 'dns:A:' + host
            adoption = self.state.get_meta('dns-adoption:' + name)
            if adoption and not adoption.get('completed'):
                raise DeployError('Resume explicit DNS adoption before deletion.', code='provider_recovery_required')
            if self.state.get_meta('pending:' + name) and not self.state.get_resource(name):
                raise DeployError('Unrecorded DNS creation requires reconciliation before deletion.', code='provider_recovery_required')
        return [{'resource': r['name'], 'name': r['attributes']['name'], 'type': 'A',
                 'action': 'delete' if self._delete_current(r) else 'absent'} for r in self._delete_resources()]

    def delete(self, operation_id):
        return {'actions': super().delete(operation_id)['actions']}

    def evidence(self, host):
        """Return only exact identity and routing evidence; do not claim ownership."""
        from .dns_adoption import fingerprint
        self._zone()
        if host not in self.hosts:
            raise DeployError('DNS evidence requires an explicitly managed hostname.')
        rows = [r for r in self._records(host) if r.get('type') in ('A', 'AAAA', 'CNAME')]
        if (len(rows) != 1 or rows[0].get('type') != 'A' or rows[0].get('name') != host
                or type(rows[0].get('proxied')) is not bool or type(rows[0].get('ttl')) is not int):
            raise DeployError('DNS evidence requires exactly one A record and no address conflicts.')
        row = rows[0]
        try:
            ipaddress.IPv4Address(row.get('content'))
        except (ValueError, TypeError):
            raise DeployError('DNS evidence requires an IPv4 address.') from None
        return {'host': host, 'zone': self.c['cloudflare-zone-id'], 'record_id': _identifier(row),
                'ipv4': row['content'], 'proxied': row['proxied'], 'ttl': row['ttl'],
                'settings_sha256': fingerprint(row)}

    def adopt(self, connection, operation_id, evidence):
        """Transfer the old address unchanged, then separately converge to new compute.

        The caller supplies exact prior record evidence and affirms the former
        manager is disabled. The target compute must already be owned and ready.
        """
        from .dns_adoption import adopt_record
        self._zone()
        if evidence.get('host') not in self.hosts or evidence.get('zone') != self.c['cloudflare-zone-id']:
            raise DeployError('DNS adoption requires an explicit hostname and exact zone.')
        compute = self.state.get_resource('compute')
        if not compute or not compute['owned'] or connection.get('instance_id') != compute['provider_id']:
            raise DeployError('DNS adoption requires recorded owned target compute.')
        try:
            ipaddress.IPv4Address(connection.get('ip'))
            ipaddress.IPv4Address(evidence.get('ipv4'))
        except (ValueError, TypeError):
            raise DeployError('DNS adoption requires source and target IPv4 addresses.') from None
        if not evidence.get('record_id') or not evidence.get('settings_sha256'):
            raise DeployError('DNS adoption requires exact record identity and settings evidence.')
        return adopt_record(self, self.state, operation_id, evidence)
