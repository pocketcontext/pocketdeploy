"""Cloudflare DNS and retained Resend sending infrastructure.

Provider responses and API keys remain private. No mail is sent by convergence.
"""
import ipaddress
import json
import os

from .common import DeployError, run
from .output import operation


def _provider_error(tool, stderr):
    """Classify structured failures without returning provider-controlled text."""
    try:
        data = json.loads(stderr)
    except (ValueError, TypeError):
        # The CLI may precede its JSON error with retry diagnostics.
        data = None
        if isinstance(stderr, str) and len(stderr) <= 65536:
            decoder = json.JSONDecoder()
            for offset, char in enumerate(stderr):
                if char != '{':
                    continue
                try:
                    candidate, _ = decoder.raw_decode(stderr[offset:])
                except ValueError:
                    continue
                if isinstance(candidate, dict) and isinstance(candidate.get('error'), dict):
                    data = candidate
                    break
        if data is None:
            text = stderr.lower() if isinstance(stderr, str) and len(stderr) <= 65536 else ''
            if 'domain' in text and any(word in text for word in ('limit', 'maximum', 'quota')):
                return DeployError(('Resend' if tool == 'resend' else 'Cloudflare') + ' domain quota prevents creation; review the account plan and existing domains.', code='provider_domain_quota')
            if 'rate limit' in text or 'rate_limit_exceeded' in text:
                return DeployError(('Resend' if tool == 'resend' else 'Cloudflare') + ' rate limit reached; wait before retrying reads and reconcile uncertain writes.', code='provider_rate_limited')
            return None
    if not isinstance(data, dict):
        return None
    error = data.get('error', data)
    if not isinstance(error, dict):
        return None
    details = error
    if isinstance(error.get('body'), str):
        try:
            body = json.loads(error['body'])
            if isinstance(body, dict):
                details = {**error, **body}
        except ValueError:
            pass
    code = details.get('name', details.get('code'))
    status = details.get('statusCode', details.get('status'))
    label = 'Resend' if tool == 'resend' else 'Cloudflare'
    message = details.get('message', '')
    # Provider messages are inspected privately only to select fixed explanations.
    message = message.lower() if isinstance(message, str) else ''
    if ('domain' in message and any(x in message for x in ('limit', 'maximum', 'quota'))):
        return DeployError(label + ' domain quota prevents creation; review the account plan and existing domains.', code='provider_domain_quota')
    if code in ('restricted_api_key', 'invalid_access', 'forbidden', 'insufficient_permissions', 'unrecognized_scope') or status == 403:
        return DeployError(label + ' denied this operation; the management credential needs the required resource permission.', code='provider_permission_denied')
    if code in ('auth_error', 'missing_api_key', 'invalid_api_key') or status == 401:
        return DeployError(label + ' authentication failed; check the configured management credential.', code='provider_authentication_failed')
    if code in ('rate_limit_exceeded', 'daily_quota_exceeded') or status == 429:
        return DeployError(label + ' rate or usage limit reached; wait before retrying reads and reconcile uncertain writes.', code='provider_rate_limited')
    if code in ('validation_error', 'missing_name') or status in (400, 422):
        return DeployError(label + ' rejected the requested settings; verify domain, region and account configuration.', code='provider_validation_failed')
    return DeployError(label + ' request failed; provider output suppressed. Reconcile uncertain writes before retrying.', code='provider_request_failed')


def _call(tool, args):
    with operation('Cloudflare request' if tool == 'cf' else 'Resend request'):
        raw = run([tool, *args], timeout=90, error_classifier=lambda stderr: _provider_error(tool, stderr))
    if not raw.strip() and 'delete' in args:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise DeployError('Provider returned an invalid response; output suppressed.') from None


def _identifier(value):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
        raise DeployError('Provider response is missing its resource identity.')
    return value['id']


class _EmptyState:
    deployment_id = "uninitialized"

    def get_resource(self, name):
        return None

    def get_meta(self, name, default=None):
        return default


class Services:
    def __init__(self, config, state):
        self.c, self.state = config, state or _EmptyState()
        self.marker = 'pocketdeploy:' + self.state.deployment_id

    def preflight(self):
        """Validate management authority before compute mutations."""
        if self.c.get('provider-dns') == 'cloudflare':
            if not os.environ.get('CLOUDFLARE_API_TOKEN'):
                raise DeployError('Set CLOUDFLARE_API_TOKEN with zone DNS edit permission.', code='credentials_missing')
            zone = _call('cf', ['zones', 'get', '--zone', self.c['cloudflare-zone-id']])
            if _identifier(zone) != self.c['cloudflare-zone-id']:
                raise DeployError('Cloudflare zone identity does not match.')
            zone_name = zone.get('name')
            self.zone_name = zone_name
            hosts = [a['host'] for a in self.c.get('once', {}).get('applications', []) if a.get('manage-dns')]
            if self.c.get('provider-smtp') == 'resend':
                hosts.append(self.c['smtp-domain'])
            if not isinstance(zone_name, str) or any(h != zone_name and not h.endswith('.' + zone_name) for h in hosts):
                raise DeployError('Managed hostnames must belong to the configured Cloudflare zone.')
        if self.c.get('provider-smtp') == 'resend':
            if not os.environ.get('RESEND_API_KEY'):
                raise DeployError('Set RESEND_API_KEY with domain and API-key management permission.', code='credentials_missing')
            domain = self._domain()
            if domain is None and self.state.get_meta('pending:smtp-domain'):
                raise DeployError('Previous SMTP domain creation requires outcome reconciliation before convergence.', code='provider_recovery_required')
        for app in self.c.get('once', {}).get('applications', []):
            if app.get('manage-dns'):
                self._dns({'type': 'A', 'name': app['host'], 'content': '0.0.0.0'})

    def delete(self, operation_id):
        """Delete only owned website A records; retain sending infrastructure."""
        actions = []
        for saved in self.state.resources():
            if saved['kind'] != 'cloudflare-dns' or saved['attributes'].get('type') != 'A':
                continue
            attrs = saved['attributes']
            if not saved['owned'] or attrs.get('zone') != self.c.get('cloudflare-zone-id'):
                raise DeployError('Website DNS ownership does not match configuration.')
            rows = self._records(attrs['name'])
            current = next((r for r in rows if r.get('id') == saved['provider_id']), None)
            if current:
                if current.get('comment') != self.marker or current.get('type') != 'A':
                    raise DeployError('Website DNS ownership changed; refusing deletion.')
                step = self.state.intent(operation_id, saved['name'], {'action': 'delete'})
                self._cf(['delete', saved['provider_id'], '--force'])
                if any(r.get('id') == saved['provider_id'] for r in self._records(attrs['name'])):
                    raise DeployError('Website DNS deletion is not verified.')
                self.state.complete(step, {'deleted': True})
            self.state.remove_resource(saved['name'])
            actions.append({'name': attrs['name'], 'type': 'A', 'action': 'delete'})
        return {'actions': actions, 'smtp': 'retained'}

    def _cf(self, args):
        return _call('cf', ['dns', 'records', *args, '--zone', self.c['cloudflare-zone-id']])

    def _resend(self, args):
        return _call('resend', [*args, '--json'])

    def _records(self, name):
        found = []
        for page in range(1, 101):
            rows = self._cf(['list', '--name', name, '--page', str(page), '--per-page', '100'])
            if not isinstance(rows, list):
                raise DeployError('Cloudflare returned an invalid record list.')
            found.extend(rows)
            if len(rows) < 100:
                return found
        raise DeployError('Cloudflare record pagination exceeded its safety limit.')

    def _dns(self, desired, op=None):
        name = 'dns:' + desired['type'] + ':' + desired['name']
        saved = self.state.get_resource(name)
        if saved and saved['attributes'].get('zone') != self.c['cloudflare-zone-id']:
            raise DeployError('Recorded DNS zone differs from configuration.')
        rows = self._records(desired['name'])
        matches = [r for r in rows if r.get('type') == desired['type']]
        # A/CNAME collisions may redirect an application to an unrelated origin.
        if desired['type'] == 'A' and any(r.get('type') in ('AAAA', 'CNAME') for r in rows):
            raise DeployError('Conflicting website DNS records require explicit ownership resolution.')
        if len(matches) > 1:
            raise DeployError('Multiple matching DNS records require explicit ownership resolution.')
        current = matches[0] if matches else None
        if current and (current.get('comment') != self.marker or
                        (saved and current.get('id') != saved['provider_id'])):
            raise DeployError('DNS record exists outside this deployment; refusing adoption.')
        if saved and not current:
            raise DeployError('Recorded DNS record disappeared; reconcile before recreating it.')
        body = {**desired, 'ttl': 300, 'proxied': False, 'comment': self.marker}
        equal = current and all(current.get(k) == v for k, v in body.items())
        action = 'noop' if equal else ('update' if current else 'create')
        result = {'name': desired['name'], 'type': desired['type'], 'action': action}
        if op is None:
            return result
        pending = 'pending:' + name
        if not current and self.state.get_meta(pending):
            raise DeployError('A previous DNS create has an uncertain outcome; reconcile before retrying.')
        if not equal:
            step = self.state.intent(op, name, {'action': action})
            self.state.set_meta(pending, True)
            args = ['edit', _identifier(current)] if current else ['create']
            response = self._cf([*args, '--body', json.dumps(body)])
            record_id = _identifier(response)
            # Persist identity before verification so a failed read never causes a second create.
            self.state.put_resource(name, 'cloudflare-dns', record_id,
                                    {'zone': self.c['cloudflare-zone-id'], **desired})
            verified = [r for r in self._records(desired['name']) if r.get('id') == record_id]
            if len(verified) != 1 or not all(verified[0].get(k) == v for k, v in body.items()):
                raise DeployError('DNS change is not verified; retry reconciliation.')
            self.state.complete(step, {'id': record_id})
        elif not saved:
            # A UUID-marked record can recover a lost create response.
            self.state.put_resource(name, 'cloudflare-dns', _identifier(current),
                                    {'zone': self.c['cloudflare-zone-id'], **desired})
        self.state.set_meta(pending, False)
        return result

    def _domain(self, op=None):
        domain = self.c['smtp-domain']
        saved = self.state.get_resource('smtp-domain')
        if saved:
            if saved['attributes'].get('name') != domain:
                raise DeployError('SMTP domain changed; explicit migration is required.')
            result = self._resend(['domains', 'get', saved['provider_id']])
            if result.get('name') != domain or _identifier(result) != saved['provider_id']:
                raise DeployError('Recorded SMTP domain identity does not match.')
            if result.get('region') and result['region'] != self.c.get('resend-region', 'eu-west-1'):
                raise DeployError('SMTP region changed; explicit migration is required.')
            return result
        # Refuse unowned domains; Resend domains have no ownership tag primitive.
        after = None
        for _ in range(100):
            args = ['domains', 'list', '--limit', '100'] + (['--after', after] if after else [])
            page = self._resend(args)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise DeployError('Resend returned an invalid domain list.')
            if any(d.get('name') == domain for d in page['data']):
                raise DeployError('SMTP domain already exists outside recorded state; explicit recovery is required.')
            if not page.get('has_more'):
                break
            if not page['data']:
                raise DeployError('Resend pagination returned no continuation.')
            after = _identifier(page['data'][-1])
        else:
            raise DeployError('Resend pagination exceeded its safety limit.')
        if op is None:
            return None
        if self.state.get_meta('pending:smtp-domain'):
            raise DeployError('Previous SMTP domain creation is uncertain; reconcile before retrying.')
        step = self.state.intent(op, 'smtp-domain', {'action': 'create'})
        self.state.set_meta('pending:smtp-domain', True)
        result = self._resend(['domains', 'create', '--name', domain, '--region', self.c.get('resend-region', 'eu-west-1')])
        self.state.put_resource('smtp-domain', 'resend-domain', _identifier(result), {'name': domain}, owned=True)
        self.state.complete(step, {'id': result['id']})
        self.state.set_meta('pending:smtp-domain', False)
        return result

    def _email_dns(self, domain):
        rows = domain.get('records', [])
        if not isinstance(rows, list) or not rows:
            raise DeployError('Resend did not return required DNS records.')
        result = []
        for row in rows:
            kind, name, value = row.get('type'), row.get('name'), row.get('value')
            if kind not in ('TXT', 'MX', 'CNAME') or not all(isinstance(x, str) and x for x in (name, value)):
                raise DeployError('Resend returned an unsupported DNS record.')
            name = name.rstrip('.')
            suffix = self.c['smtp-domain']
            if name in ('send', 'resend._domainkey'):
                name += '.' + suffix
            elif getattr(self, 'zone_name', None) and suffix.endswith('.' + self.zone_name):
                relative = suffix[:-(len(self.zone_name) + 1)]
                if name in ('send.' + relative, 'resend._domainkey.' + relative):
                    name += '.' + self.zone_name
            if name != suffix and not name.endswith('.' + suffix):
                raise DeployError('Resend requested DNS outside the configured sending domain.')
            record = {'type': kind, 'name': name, 'content': value}
            if kind == 'MX':
                if type(row.get('priority')) is not int:
                    raise DeployError('Resend MX record is missing priority.')
                record['priority'] = row['priority']
            result.append(record)
        return result

    def plan(self, connection=None):
        actions = []
        for app in self.c.get('once', {}).get('applications', []):
            if app.get('manage-dns'):
                if connection:
                    actions.append(self._dns({'type': 'A', 'name': app['host'], 'content': self._ip(connection)}))
                else:
                    self._dns({'type': 'A', 'name': app['host'], 'content': '0.0.0.0'})
                    actions.append({'name': app['host'], 'type': 'A', 'action': 'after-compute'})
        if self.c.get('provider-smtp') == 'resend':
            domain = self._domain()
            actions.append({'name': self.c['smtp-domain'], 'type': 'smtp', 'action': 'reconcile' if domain else ('recovery-required' if self.state.get_meta('pending:smtp-domain') else 'create')})
            if domain:
                actions.extend(self._dns(row) for row in self._email_dns(domain))
        return {'actions': actions}

    @staticmethod
    def _ip(connection):
        value = connection.get('public_ip') or connection.get('ip') or connection.get('host')
        try:
            return str(ipaddress.IPv4Address(value))
        except (ValueError, TypeError):
            raise DeployError('Website DNS requires a verified public IPv4 address.') from None

    def converge(self, connection, operation_id):
        actions = []
        for app in self.c.get('once', {}).get('applications', []):
            if app.get('manage-dns'):
                actions.append(self._dns({'type': 'A', 'name': app['host'], 'content': self._ip(connection)}, operation_id))
        if self.c.get('provider-smtp') == 'resend':
            domain = self._domain(operation_id)
            actions.extend(self._dns(row, operation_id) for row in self._email_dns(domain))
            if domain.get('status') != 'verified':
                self._resend(['domains', 'verify', domain['id']])
                domain = self._resend(['domains', 'get', domain['id']])
                if domain.get('status') != 'verified':
                    raise DeployError('SMTP DNS verification is pending; rerun converge after DNS propagation.', code='smtp_verification_pending')
            key = self.state.get_resource('smtp-key')
            if key:
                if key['attributes'].get('domain_id') != domain['id'] or not key['attributes'].get('token'):
                    raise DeployError('SMTP credential does not match the recorded domain.')
                after, found = None, False
                for _ in range(100):
                    args = ['api-keys', 'list', '--limit', '100'] + (['--after', after] if after else [])
                    page = self._resend(args)
                    if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                        raise DeployError('Resend returned an invalid key list.')
                    if any(k.get('id') == key['provider_id'] for k in page['data']):
                        found = True
                        break
                    if not page.get('has_more') or not page['data']:
                        break
                    after = _identifier(page['data'][-1])
                if not found:
                    raise DeployError('Recorded SMTP credential is absent; explicit rotation is required.')
            else:
                if self.state.get_meta('pending:smtp-key'):
                    raise DeployError('SMTP key creation is uncertain; revoke the orphan key and explicitly recover before retrying.')
                step = self.state.intent(operation_id, 'smtp-key', {'action': 'create'})
                self.state.set_meta('pending:smtp-key', True)
                key = self._resend(['api-keys', 'create', '--name', self.c['profile'] + '-smtp-send',
                                    '--permission', 'sending_access', '--domain-id', domain['id']])
                if not isinstance(key.get('token'), str) or not key['token']:
                    raise DeployError('SMTP key response is incomplete; explicit recovery is required.')
                self.state.put_resource('smtp-key', 'resend-api-key', _identifier(key),
                                        {'domain_id': domain['id'], 'token': key['token']})
                self.state.complete(step, {'id': key['id']})
                self.state.set_meta('pending:smtp-key', False)
            actions.append({'name': self.c['smtp-domain'], 'type': 'smtp', 'action': 'verified'})
        return {'actions': actions}

    def smtp_settings(self):
        """Private payload for SSH stdin only; never return as a CLI result."""
        key = self.state.get_resource('smtp-key')
        if not key:
            raise DeployError('SMTP credential is missing; converge first.')
        return {'server': 'smtp.resend.com', 'port': 465, 'username': 'resend',
                'password': key['attributes']['token'], 'from': self.c['smtp-from']}
