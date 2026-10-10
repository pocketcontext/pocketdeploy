"""Resend transport selection, retaining legacy configuration defaults."""
from .common import DeployError


def smtp_transport(config):
    """Return (port, security); omitted settings retain implicit TLS on 465."""
    port = config.get('smtp-port', 465)
    modes = {465: 'implicit-tls', 2465: 'implicit-tls', 587: 'starttls', 2587: 'starttls'}
    if type(port) is not int or port not in modes:
        raise DeployError('smtp-port must be 465, 2465, 587 or 2587.')
    security = config.get('smtp-security', modes[port])
    if security != modes[port]:
        raise DeployError('smtp-security must match the selected port: implicit-tls for 465/2465, starttls for 587/2587.')
    return port, security
