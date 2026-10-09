#!/usr/bin/env python3
"""Exercise the published portable launcher outside this checkout; no cloud calls."""
import os
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
        for command in ('oci', 'ssh', 'scp', 'ssh-keygen', 'vaultcontext'):
            stub = launcher.parent / command
            stub.write_text(guard)
            stub.chmod(0o700)
        environment['PATH'] = str(launcher.parent) + os.pathsep + environment.get('PATH', '')

        def invoke(directory, *arguments, expected=None):
            result = subprocess.run(
                [str(launcher), *arguments], cwd=directory, env=environment,
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout + result.stderr
            if expected is None:
                assert result.returncode == 0, output
                assert 'vault-restore' in output and 'converge' in output, output
            else:
                assert result.returncode != 0, 'Launcher swallowed the command failure.'
                assert expected in output, output
            assert not marker.exists(), 'Launcher test attempted an external deployment command.'
            return output

        empty = root / 'empty'
        empty.mkdir()
        invoke(empty, '--help')
        # A standalone script must ignore the caller's project dependencies.
        (empty / 'pyproject.toml').write_text(
            '[project]\nname = "unrelated-project"\nversion = "0.0.0"\n'
            'dependencies = ["this is deliberately invalid !!!"]\n'
        )
        invoke(empty, '--help')
        missing = invoke(empty, 'plan', expected='pocketdeploy:')
        assert STATE_REQUIRED not in missing, 'Missing config unexpectedly selected a deployment.'

        deployment = root / 'deployment'
        nested = deployment / 'nested/deeper'
        nested.mkdir(parents=True)
        (deployment / 'colors.yml').write_text(CONFIG)
        invoke(nested, 'plan', expected=STATE_REQUIRED)
        # Explicit -f must win over the invalid nearest discovered configuration.
        (nested / 'colors.yml').write_text('unknown-field: true\n')
        invoke(nested, 'plan', '-f', '../../colors.yml', expected=STATE_REQUIRED)
        missing = invoke(nested, 'plan', '-f', 'missing.yml', expected='pocketdeploy:')
        assert STATE_REQUIRED not in missing, 'Missing explicit config fell back to discovery.'
        assert not list(root.rglob('.colors.sqlite*')), 'Checks unexpectedly wrote deployment state.'
    print('Portable launcher: 6 checks passed (no cloud access).')


if __name__ == '__main__':
    main()
