#!/usr/bin/env python3
"""Exercise the published portable launcher outside this checkout; no cloud calls."""
import os
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
            if not key.startswith('COLORS_PAR_') and key not in {
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

    print('Portable launcher: 12 checks passed (no cloud access).')


if __name__ == '__main__':
    main()
