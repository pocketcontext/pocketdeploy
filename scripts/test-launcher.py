#!/usr/bin/env python3
"""Exercise the published portable launcher outside this checkout; no cloud calls."""
import os
import base64
import time
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


STATE_REQUIRED = 'Existing deployment state is required; restore it before continuing.'
CONFIG = '''schema-version: 1
profile: launcher-test
oci-config-file-profile: synthetic
oci-compartment-id: synthetic-compartment
oci-subnet-id: synthetic-subnet
oci-availability-domain: synthetic-domain
compute-require-existing-state: true
'''


def main():
    source = Path(__file__).resolve().parents[1] / 'skills/pocketdeploy/pocketdeploy'
    if not source.is_file():
        raise AssertionError('Portable launcher is missing.')
    if shutil.which('uv') is None:
        raise AssertionError('Install uv before running launcher checks.')
    with tempfile.TemporaryDirectory(prefix='pocketdeploy-launcher-', dir='/tmp') as temporary:
        root = Path(temporary)
        launcher = root / 'bin/pocketdeploy'
        launcher.parent.mkdir()
        shutil.copyfile(source, launcher)
        launcher.chmod(0o700)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(('COLORS_PAR_', 'OCI_')) and key not in {
                'VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME', 'UV_PROJECT',
                'UV_PROJECT_ENVIRONMENT', 'UV_WORKING_DIRECTORY',
            }
        }
        # A regression must fail locally before it can invoke deployment tools.
        marker = root / 'unexpected-external-command'
        guard = f'#!/bin/sh\nprintf blocked > "{marker}"\nexit 99\n'
        for command in ('oci', 'ssh', 'scp', 'vaultcontext'):
            stub = launcher.parent / command
            stub.write_text(guard)
            stub.chmod(0o700)
        environment['OCI_CLI_CONFIG_FILE'] = str(root / 'oci-config')
        environment['PATH'] = str(launcher.parent) + os.pathsep + environment.get('PATH', '')

        def invoke(directory, *arguments, expected=None, successful=False):
            result = subprocess.run(
                [str(launcher), *arguments], cwd=directory, env=environment,
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout + result.stderr
            if expected is None:
                assert result.returncode == 0, output
                if not successful:
                    assert 'vault-restore' in output and 'converge' in output and 'init' in output, output
            else:
                assert result.returncode != 0, 'Launcher swallowed the command failure.'
                assert expected in output, output
            assert not marker.exists(), 'Launcher test attempted an external deployment command.'
            return result

        empty = root / 'empty'
        empty.mkdir()
        explicit_help = invoke(empty, '--help')
        bare = invoke(empty)
        assert bare.stdout == explicit_help.stdout
        assert 'usage:' in bare.stdout and bare.stderr == ''
        assert not list(empty.iterdir()), 'Help created local files.'
        for flags in ([], ['--json']):
            removed = invoke(empty, 'create', *flags, expected='')
            assert removed.returncode == 2
            if flags:
                assert json.loads(removed.stdout)['error']['code'] == 'invalid_usage'
            else:
                assert not removed.stdout and 'Invalid command arguments' in removed.stderr
            assert not list(empty.iterdir()), 'Removed command created deployment files.'
        assert 'smtp-test' in bare.stdout
        for flags in ([], ['--json']):
            missing_recipient = invoke(empty, 'smtp-test', *flags, expected='')
            assert missing_recipient.returncode == 2
            if flags:
                assert json.loads(missing_recipient.stdout)['error']['code'] == 'invalid_usage'
            else:
                assert '--to' in missing_recipient.stderr
            assert not list(empty.iterdir()), 'Invalid SMTP request created files.'
        # A standalone script must ignore the caller's project dependencies.
        (empty / 'pyproject.toml').write_text(
            '[project]\nname = "unrelated-project"\nversion = "0.0.0"\n'
            'dependencies = ["this is deliberately invalid !!!"]\n'
        )
        invoke(empty, '--help')
        missing = invoke(empty, 'plan', expected='pocketdeploy:')
        assert STATE_REQUIRED not in missing.stdout + missing.stderr, 'Missing config unexpectedly selected a deployment.'

        deployment = root / 'deployment'
        nested = deployment / 'nested/deeper'
        nested.mkdir(parents=True)
        (deployment / 'colors.yml').write_text(CONFIG)
        parent_ignored = invoke(nested, 'plan', expected='No colors.yml found in the current directory')
        assert STATE_REQUIRED not in parent_ignored.stdout + parent_ignored.stderr
        # Explicit -f must win over an invalid configuration in the current directory.
        (nested / 'colors.yml').write_text('unknown-field: true\n')
        invoke(nested, 'plan', '-f', '../../colors.yml', expected=STATE_REQUIRED)
        missing = invoke(nested, 'plan', '-f', 'missing.yml', expected='pocketdeploy:')
        assert STATE_REQUIRED not in missing.stdout + missing.stderr, 'Missing explicit config fell back to another file.'
        assert not list(root.rglob('.colors.sqlite*')), 'Checks unexpectedly wrote deployment state.'
        fresh = root / 'fresh'
        fresh.mkdir()
        (fresh / 'colors.yml').write_text(CONFIG.replace('compute-require-existing-state: true',
                                                       'compute-require-existing-state: false'))
        adoption = invoke(fresh, 'adopt', '--instance-id', 'synthetic-instance',
                          '--json', '--quiet', expected='')
        assert json.loads(adoption.stdout)['error']['message'] == STATE_REQUIRED
        assert not list(fresh.glob('.colors.sqlite*')), 'Adoption created a new deployment identity.'
        rotation = invoke(fresh, 'rotate-host-key', '--json', '--quiet', expected='')
        assert json.loads(rotation.stdout)['error']['message'] == STATE_REQUIRED
        assert not list(fresh.glob('.colors.sqlite*')), 'Rotation created a new deployment identity.'
        unsafe = root / 'unsafe'
        unsafe.mkdir()
        unsafe_config = unsafe / 'colors.yml'
        unsafe_config.write_text((fresh / 'colors.yml').read_text() + '\nssh-private-key-file: colors.yml\n')
        original = unsafe_config.read_bytes()
        invoke(unsafe, 'init', '--json', '--quiet', expected='distinct paths')
        assert unsafe_config.read_bytes() == original
        assert not list(unsafe.glob('.colors.sqlite*')), 'Unsafe paths initialized state.'
        unsafe_config.write_text((fresh / 'colors.yml').read_text() + '\nssh-user: -oProxyCommand=invalid\n')
        invoke(unsafe, 'init', '--json', '--quiet', expected='ssh-user')
        assert not list(unsafe.glob('.colors.sqlite*')), 'Unsafe SSH user initialized state.'
        conflicting = invoke(fresh, 'init', '--json', '--verbose', '--quiet', expected='')
        conflict_envelope = json.loads(conflicting.stdout)
        assert conflicting.returncode == 2 and conflict_envelope['ok'] is False
        assert not (fresh / '.colors.sqlite').exists(), 'Conflicting flags initialized state.'
        initialized_run = invoke(fresh, 'init', '--json', '--quiet', successful=True)
        initialized_envelope = json.loads(initialized_run.stdout)
        assert initialized_envelope['schema_version'] == 1
        assert initialized_envelope['command'] == 'init' and initialized_envelope['ok'] is True
        initialized = initialized_envelope['result']
        assert initialized_run.stderr == '', initialized_run.stderr
        assert (fresh / '.colors.sqlite').is_file()
        assert (fresh / '.envrc.private').read_bytes() == b''
        authority = [fresh / '.ssh' / name for name in (
            'id_ed25519', 'id_ed25519.pub', 'host_ed25519', 'host_ed25519.pub', 'known_hosts')]
        contents = [path.read_bytes() for path in authority]
        repeated = json.loads(invoke(fresh, 'init', '--json', '--quiet', successful=True).stdout)['result']
        assert initialized['deployment_id'] == repeated['deployment_id']
        assert contents == [path.read_bytes() for path in authority]
        verbose_run = invoke(fresh, 'init', '--json', '--verbose', successful=True)
        assert json.loads(verbose_run.stdout)['ok'] is True
        assert verbose_run.stderr.strip(), 'Verbose mode omitted progress.'
        text_run = invoke(fresh, 'init', '--quiet', successful=True)
        assert text_run.stdout.strip(), 'Quiet mode suppressed the text result.'
        assert text_run.stderr == '', text_run.stderr
        try:
            json.loads(text_run.stdout)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError('Default output unexpectedly remains JSON.')

        missing_json = invoke(empty, 'plan', '--json', '--quiet', expected='')
        missing_envelope = json.loads(missing_json.stdout)
        assert missing_json.returncode == 1
        assert missing_envelope['schema_version'] == 1
        assert missing_envelope['command'] == 'plan' and missing_envelope['ok'] is False
        assert missing_envelope['error']['code'] and missing_envelope['error']['message']
        assert missing_json.stderr == '', missing_json.stderr

        usage_json = invoke(empty, 'plan', '--json', '--quiet', '--unknown-option',
                            expected='')
        usage_envelope = json.loads(usage_json.stdout)
        assert usage_json.returncode == 2
        assert usage_envelope['schema_version'] == 1 and usage_envelope['ok'] is False
        assert usage_envelope['error']['code'] and usage_envelope['error']['message']
        assert usage_json.stderr == '', usage_json.stderr

        # Expired synthetic credentials must fail before the guarded OCI binary runs.
        token = root / 'synthetic-token'
        payload = base64.urlsafe_b64encode(json.dumps({'exp': int(time.time()) - 3600}).encode()).decode().rstrip('=')
        token.write_text('eyJhbGciOiJSUzI1NiJ9.' + payload + '.synthetic-signature')
        Path(environment['OCI_CLI_CONFIG_FILE']).write_text(
            '[synthetic]\nregion=eu-frankfurt-1\nsecurity_token_file=' + str(token) + '\n'
        )
        started = time.monotonic()
        expired_run = invoke(fresh, 'plan', '--json', '--verbose', expected='')
        elapsed = time.monotonic() - started
        expired = json.loads(expired_run.stdout)
        assert expired_run.returncode == 1 and expired['ok'] is False
        assert expired['error']['code'] == 'oci_token_expired'
        assert 'expired' in expired['error']['message'].lower()
        assert 'oci session refresh --profile synthetic --region eu-frankfurt-1' in expired['error']['message']
        assert 'oci session authenticate --profile-name synthetic --region eu-frankfurt-1' in expired['error']['message']
        assert elapsed < 10, f'Expired token failed slowly: {elapsed:.1f}s'
        assert token.read_text() not in expired_run.stdout + expired_run.stderr
        print(f'Expired synthetic OCI token rejected locally in {elapsed:.2f}s; OCI was not invoked.')

    print('Portable launcher: 23 checks passed (no cloud access).')


if __name__ == '__main__':
    main()
