#!/usr/bin/env python3
"""Package-owned SSH config updater; stdin contains only connection metadata."""
import fcntl
import fnmatch
import glob
import ipaddress
import json
import os
import pathlib
import pwd
import re
import shlex
import stat
import sys
import tempfile
import time


def _read(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('unsafe SSH config file')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ''
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('unsafe SSH config file')
        text = stream.read(2 * 1024 * 1024 + 1)
        if len(text.encode('utf-8')) > 2 * 1024 * 1024:
            raise ValueError('SSH config exceeds size limit')
        return text


def _check_includes(path, main, directory, destination, aliases):
    """Conservatively verify Include order without executing Match commands.

    OpenSSH uses the first obtained setting. Require the owned snippet before
    applicable options, and refuse conditional inclusion rather than guessing Match
    predicates. Still traverse later Includes to find exact alias collisions.
    """
    seen = set()
    found = False
    options_seen = False
    count = 0

    def visit(current, text, scope='global', depth=0, active=True):
        nonlocal found, options_seen, count
        count += 1
        if depth > 16 or count > 1024 or current in seen:
            raise ValueError('recursive or excessive SSH Include configuration')
        seen.add(current)
        for line in text.splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            # OpenSSH also accepts Keyword=value and Keyword = value.
            key, separator, value = words[0].partition('=')
            args = ([value] if separator and value else []) + words[1:]
            if args and args[0] == '=':
                args = args[1:]
            key = key.lower()
            if key == 'host':
                if aliases.intersection(word.lower() for word in args):
                    raise ValueError('SSH alias is outside the deployment marker')
                scope = 'global' if args == ['*'] else 'conditional'
                active = any(
                    any(fnmatch.fnmatchcase(alias, pattern.lower()) for pattern in args if not pattern.startswith('!'))
                    and not any(fnmatch.fnmatchcase(alias, pattern[1:].lower()) for pattern in args if pattern.startswith('!'))
                    for alias in aliases)
            elif key == 'match':
                scope = 'conditional'
                active = True
            elif key == 'include':
                if scope != 'global':
                    raise ValueError('conditional SSH Include cannot be verified safely')
                for pattern in args:
                    if any(character in pattern for character in ('%', '$')) or pattern.startswith('~'):
                        raise ValueError('unsupported SSH Include expansion')
                    pattern_path = pathlib.Path(pattern)
                    if not pattern_path.is_absolute():
                        pattern_path = directory / pattern_path
                    pattern = os.path.abspath(pattern_path)
                    matches = set(glob.glob(pattern))
                    # Include must cover the destination even before it exists.
                    if len(destination.parts) == len(pathlib.Path(pattern).parts) and all(
                            fnmatch.fnmatchcase(part, glob_part)
                            for part, glob_part in zip(destination.parts, pathlib.Path(pattern).parts)):
                        matches.add(str(destination))
                    for name in sorted(matches):
                        included = pathlib.Path(name)
                        if included == destination:
                            if found or options_seen:
                                raise ValueError('SSH config.d Include must load the owned alias once before applicable SSH options')
                            found = True
                        else:
                            visit(included, _read(included), scope, depth + 1, active)
            else:
                options_seen = options_seen or active
        seen.remove(current)

    visit(path, main)
    if not found:
        raise ValueError('SSH config requires an early Include config.d/* under Host * before installing aliases')


def update(payload, home=None, *, owner='POCKETDEPLOY'):
    """Install/remove an owned config.d snippet without rewriting SSH config.

    Payload: host_alias profile, block_state present/absent, ssh_hosts list of
    {name, ip, user}, absolute identity_file and known_hosts_file paths.
    Optional check_only performs read-only collision and configuration checks.
    """
    if not isinstance(owner, str) or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', owner):
        raise ValueError('invalid SSH block owner')
    profile = payload.get('host_alias')
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}', profile):
        raise ValueError('invalid SSH deployment alias')
    mode = payload.get('block_state')
    if mode not in ('present', 'absent'):
        raise ValueError('invalid SSH block request')
    check_only = payload.get('check_only', False)
    if type(check_only) is not bool:
        raise ValueError('invalid SSH check flag')
    hosts = payload.get('ssh_hosts')
    if not isinstance(hosts, list) or (mode == 'present' and not hosts) or len(hosts) > 1001:
        raise ValueError('invalid SSH host inventory')
    aliases = set()
    for host in hosts:
        if not isinstance(host, dict):
            raise ValueError('invalid SSH host')
        alias, address, user = (host.get(key) for key in ('name', 'ip', 'user'))
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', alias):
            raise ValueError('invalid SSH host alias')
        if alias != profile and not alias.startswith(profile + '-'):
            raise ValueError('SSH node alias outside deployment')
        if alias.lower() in aliases:
            raise ValueError('duplicate SSH alias')
        aliases.add(alias.lower())
        if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', user):
            raise ValueError('invalid SSH user')
        if not isinstance(address, str) or str(ipaddress.ip_address(address)) != address:
            raise ValueError('invalid SSH address')
    if mode == 'present' and profile.lower() not in aliases:
        raise ValueError('missing SSH entry alias')
    home = pathlib.Path(home or os.environ.get('HOME') or pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute() or any(p.is_symlink() for p in (home, *home.parents)) or not home.is_dir():
        raise ValueError('unsafe SSH home')
    directory = home / '.ssh'
    if not directory.exists() and not directory.is_symlink() and mode == 'absent':
        return False
    if not check_only:
        directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or directory.stat().st_uid != os.getuid())):
        raise ValueError('unsafe SSH directory')
    if not check_only:
        os.chmod(directory, 0o700)
    lock = directory / '.pocketdeploy-ssh-config.lock'
    fd = None if check_only else os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if fd is not None:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('unsafe SSH config lock')
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('SSH config lock timed out')
                    time.sleep(0.025)
        path = directory / 'config'
        main = _read(path)
        snippets = directory / 'config.d'
        if snippets.is_symlink() or (snippets.exists() and
                (not snippets.is_dir() or snippets.stat().st_uid != os.getuid())):
            raise ValueError('unsafe SSH config.d directory')
        destination = snippets / ('00-' + owner.lower() + '-' + profile + '.conf')
        begin = '# BEGIN ' + profile + ' ' + owner + ' MANAGED BLOCK'
        end = '# END ' + profile + ' ' + owner + ' MANAGED BLOCK'
        if begin in main or end in main:
            raise ValueError('legacy owned SSH block requires explicit migration from config to config.d')
        old = _read(destination)
        if old and (not old.startswith(begin + '\n') or not old.endswith(end + '\n') or
                    old.count(begin) != 1 or old.count(end) != 1):
            raise ValueError('SSH config.d destination is outside the deployment marker')
        if destination.exists() and not old:
            raise ValueError('SSH config.d destination is outside the deployment marker')
        if mode == 'absent':
            if check_only or not destination.exists():
                return False
            destination.unlink()
            return True
        _check_includes(path, main, directory, destination, aliases)
        block = [begin]
        for host in hosts:
            block.extend(['Host ' + host['name'], '    HostName ' + host['ip'], '    User ' + host['user'], '    Port 22'])
            for field, directive in [('identity_file', 'IdentityFile'), ('known_hosts_file', 'UserKnownHostsFile')]:
                value = payload.get(field, '')
                if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in '\r\n\x00"%'):
                    raise ValueError('invalid SSH file path')
                block.append('    ' + directive + ' "' + value + '"')
            block.extend(['    IdentitiesOnly yes', '    IdentityAgent none',
                          '    ForwardAgent no', '    StrictHostKeyChecking yes',
                          '    GlobalKnownHostsFile /dev/null', '    HostKeyAlgorithms ssh-ed25519',
                          '    AddKeysToAgent no', '    ControlMaster no', '    ControlPath none'])
        # Keep snippets independently scoped when inspected or concatenated.
        new = '\n'.join(block + ['Host *', end, ''])
        if check_only or new == old:
            return False
        snippets.mkdir(mode=0o700, exist_ok=True)
        temporary_fd, temporary = tempfile.mkstemp(prefix='.colors-config-', dir=snippets)
        try:
            with os.fdopen(temporary_fd, 'w', encoding='utf-8', newline='') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(new)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    try:
        print('changed' if update(json.load(sys.stdin)) else 'unchanged')
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
