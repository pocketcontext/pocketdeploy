"""Standalone remote s-nail submission: copied over SSH, never imported remotely."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def classify_failure(stderr):
    text = stderr.lower()
    if 'a password is necessary' in text or 'a user is necessary' in text:
        return 'smtp_credentials_missing'
    if 'certificate' in text or 'tls' in text and any(word in text for word in ('fail', 'error', 'required')):
        return 'smtp_tls_failed'
    if any(word in text for word in ('535', 'authentication failed', 'authentication unsuccessful')):
        return 'smtp_authentication_failed'
    if any(word in text for word in ('connection refused', 'connection timed out', 'name or service not known', 'network is unreachable')):
        return 'smtp_connection_failed'
    return 'smtp_submission_failed'


def submit(request):
    smtp, recipient = request['smtp'], request['to']
    address = r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
    if not re.fullmatch(address, recipient) or not re.fullmatch(address, smtp['from']):
        raise ValueError('invalid address')
    if (smtp.get('server') != 'smtp.resend.com' or smtp.get('port') != 465
            or smtp.get('username') != 'resend'
            or not re.fullmatch(r'[A-Za-z0-9_-]+', smtp['password'])):
        raise ValueError('invalid SMTP settings')
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='pocketdeploy-smtp-') as directory:
        config = Path(directory) / 'mailrc'
        config.write_text('\n'.join([
            'set v15-compat', 'set sendwait', 'set tls-verify=strict',
            'set mta=smtps://resend:' + smtp['password'] + '@smtp.resend.com:465',
            'set smtp-auth=login', 'set from=' + smtp['from'],
            'unset record', 'unset save', '',
        ]))
        result = subprocess.run(['s-nail', '-n', '-s', 'PocketDeploy SMTP verification', recipient],
                                input='Explicit PocketDeploy SMTP submission test.\n',
                                env={'PATH': '/usr/bin:/bin', 'HOME': directory, 'MAILRC': str(config), 'LC_ALL': 'C'},
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            return {'accepted': False, 'error': classify_failure(result.stderr)}
    return {'accepted': True}


if __name__ == '__main__':
    try:
        print(json.dumps(submit(json.load(sys.stdin))))
    except subprocess.TimeoutExpired:
        print(json.dumps({'accepted': False, 'error': 'smtp_timeout'}))
    except OSError:
        print(json.dumps({'accepted': False, 'error': 'smtp_client_unavailable'}))
    except Exception:
        print(json.dumps({'accepted': False, 'error': 'smtp_invalid_configuration'}))
