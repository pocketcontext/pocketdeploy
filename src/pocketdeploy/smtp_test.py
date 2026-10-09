"""Explicit SMTP submission test. Credentials use SSH stdin and private mailrc."""
import json
from pathlib import Path
import re
import shlex

from .common import DeployError, run
from .output import operation


def smtp_test(host, connection, settings, recipient):
    if not isinstance(recipient, str) or not re.fullmatch(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', recipient):
        raise DeployError('smtp-test requires one plain recipient email address.', code='invalid_usage')
    source = Path(__file__).with_name('smtp_test_remote.py').read_text()
    payload = {'smtp': settings, 'to': recipient}
    with operation('SSH: SMTP submission test'):
        output = run(host._argv(connection) + ['sudo python3 -c ' + shlex.quote(source)],
                     input=json.dumps(payload), timeout=120)
    try:
        result = json.loads(output)
    except ValueError:
        raise DeployError('SMTP test returned an invalid response; output suppressed.') from None
    if result != {'accepted': True}:
        errors = {
            'smtp_credentials_missing': 's-nail could not resolve its SMTP credentials.',
            'smtp_tls_failed': 'SMTP TLS verification failed; check the host certificate trust and clock.',
            'smtp_authentication_failed': 'Resend rejected SMTP authentication; check the scoped sending credential.',
            'smtp_connection_failed': 'The VPS could not connect to Resend SMTP.',
            'smtp_submission_failed': 'Resend SMTP submission failed; provider output suppressed.',
            'smtp_timeout': 'SMTP submission timed out; delivery is uncertain. Check provider history before retrying.',
            'smtp_client_unavailable': 's-nail is unavailable on the VPS; converge host setup first.',
            'smtp_invalid_configuration': 'The SMTP test configuration is invalid.',
        }
        code = result.get('error') if isinstance(result, dict) else None
        if code in errors:
            raise DeployError(errors[code], code=code)
        raise DeployError('SMTP submission failed; output suppressed.', code='smtp_submission_failed')
    return {'accepted': True, 'delivery_confirmed': False}
