"""Structured failure evidence. Standalone stdlib source for remote programs.

Operation names must be constants authored by the caller, never command arguments.
Remote consumers must additionally validate against their operation allowlist.
"""
import math
import re

CATEGORIES = frozenset({'unknown', 'nonzero_exit', 'signal', 'timeout', 'unavailable',
    'authentication', 'permission', 'rate_limit', 'dns', 'tls', 'connection',
    'http', 'invalid_response', 'rejected'})


def validate_diagnostic(value, allowed_operations=None, *, allowed_tasks=None, allowed_users=None):
    """Return a fresh allowlisted object, or None; never stringify hostile input."""
    if type(value) is not dict or set(value) - {
            'operation', 'category', 'exit_code', 'signal', 'elapsed_seconds', 'http_status', 'task', 'user'}:
        return None
    operation = value.get('operation')
    if type(operation) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,79}', operation):
        return None
    if allowed_operations is not None and operation not in allowed_operations:
        return None
    for key, allowed in [('task', allowed_tasks), ('user', allowed_users)]:
        if key in value:
            if type(value[key]) is not str or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.:-]{0,79}', value[key]):
                return None
            if allowed_operations is not None and (allowed is None or value[key] not in allowed):
                return None
    if type(value.get('category')) is not str or value['category'] not in CATEGORIES:
        return None
    for key, lower, upper in [('exit_code', 0, 255), ('signal', 1, 127), ('http_status', 100, 599)]:
        if key in value and (type(value[key]) is not int or not lower <= value[key] <= upper):
            return None
    if 'exit_code' in value and 'signal' in value:
        return None
    if 'elapsed_seconds' in value:
        elapsed = value['elapsed_seconds']
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 31536000:
            return None
    return dict(value)


def build_diagnostic(operation, category='unknown', *, returncode=None, elapsed_seconds=None, http_status=None, task=None, user=None):
    value = {'operation': operation, 'category': category}
    if returncode is not None:
        if type(returncode) is not int:
            raise ValueError('Invalid diagnostic return code.')
        value['signal' if returncode < 0 else 'exit_code'] = abs(returncode)
        if returncode < 0 and category == 'nonzero_exit':
            value['category'] = 'signal'
    if elapsed_seconds is not None:
        value['elapsed_seconds'] = round(elapsed_seconds, 3)
    if http_status is not None:
        value['http_status'] = http_status
    for key, item in [('task', task), ('user', user)]:
        if item is not None:
            value[key] = item
    result = validate_diagnostic(value)
    if result is None:
        raise ValueError('Invalid structured diagnostic.')
    return result


class CommandError(RuntimeError):
    def __init__(self, diagnostic):
        super().__init__('Command failed; private output suppressed.')
        self.diagnostic = validate_diagnostic(diagnostic)
