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
        raise DeployError('SMTP submission failed; output suppressed.')
    return {'accepted': True, 'delivery_confirmed': False}
