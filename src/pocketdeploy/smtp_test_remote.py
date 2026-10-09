"""Standalone remote s-nail submission: copied over SSH, never imported remotely."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


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
            'set mta=smtps://resend@smtp.resend.com:465', 'set smtp-auth=login',
            'set smtp-auth-password=' + smtp['password'], 'set from=' + smtp['from'],
            'unset record', 'unset save', '',
        ]))
        result = subprocess.run(['s-nail', '-n', '-s', 'PocketDeploy SMTP verification', recipient],
                                input='Explicit PocketDeploy SMTP submission test.\n',
                                env={'PATH': '/usr/bin:/bin', 'HOME': directory, 'MAILRC': str(config), 'LC_ALL': 'C'},
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('SMTP submission failed')
    return {'accepted': True}


if __name__ == '__main__':
    try:
        print(json.dumps(submit(json.load(sys.stdin))))
    except Exception:
        print(json.dumps({'accepted': False}))
        sys.exit(1)
