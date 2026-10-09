import asyncio
from types import SimpleNamespace
import pytest
from pocketdeploy import cli
from pocketdeploy.common import DeployError


def test_dag_stops_before_host_after_uncertain_compute():
    calls=[]
    class Host:
        def prepare_keys(self):calls.append('keys');return 'public'
        def cloud_init(self):return 'secret-init'
        def bootstrap(self,*args):calls.append('host')
    class Cloud:
        def converge(self,*args):calls.append('compute');raise DeployError('uncertain outcome')
    config={}
    with pytest.raises(DeployError,match='uncertain'):
        asyncio.run(cli.converge(config,None,Cloud(),Host(),'operation'))
    assert calls==['keys','compute']
    assert '_cloud_init' not in config


def test_dag_never_surfaces_arbitrary_exception_contents():
    class Host:
        def prepare_keys(self):raise ValueError('secret sentinel')
    with pytest.raises(DeployError) as e:
        asyncio.run(cli.converge({},None,None,Host(),'operation'))
    assert 'sentinel' not in str(e.value)


@pytest.mark.parametrize('fail_compute', [False, True])
def test_converge_never_implicitly_saves_to_vault(tmp_path, monkeypatch, fail_compute):
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG + '\nvault-id: synthetic-vault\n')
    monkeypatch.chdir(tmp_path)
    calls = []
    class Host:
        def __init__(self, *args): pass
        def prepare_keys(self): calls.append('keys'); return 'public'
        def cloud_init(self): return 'synthetic-init'
        def bootstrap(self, *args): calls.append('host'); return {}
        def converge(self, *args): calls.append('applications'); return {}
        def status(self, *args): calls.append('verify'); return {}
    class Cloud:
        def __init__(self, *args): pass
        def converge(self, *args):
            calls.append('compute')
            if fail_compute:
                raise DeployError('synthetic failure')
            return {}
    monkeypatch.setattr(cli, 'Host', Host)
    monkeypatch.setattr(cli, 'OCI', Cloud)
    monkeypatch.setattr(cli.vault, 'save', lambda *args: pytest.fail('Implicit Vault save'))
    args = cli.parser().parse_args(['converge'])
    if fail_compute:
        with pytest.raises(DeployError, match='synthetic failure'):
            cli.execute(args)
        assert calls == ['keys', 'compute']
    else:
        result = cli.execute(args)
        assert calls == ['keys', 'compute', 'host', 'applications', 'verify']
        assert 'vault' not in result
        assert result['profile'] == 'demo'


SYNTHETIC_CONFIG = '''profile: demo
oci-config-file-profile: test
oci-compartment-id: compartment
oci-subnet-id: subnet
oci-availability-domain: ad
'''


def init_offline(tmp_path, monkeypatch, extra=''):
    from pocketdeploy.config import load, scope
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG + extra)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, 'OCI', lambda *args: pytest.fail('Init constructed OCI'))
    monkeypatch.setattr(cli.vault, 'save', lambda *args: pytest.fail('Init called Vault'))
    config = load(tmp_path / 'colors.yml', env={}, resolve=False)
    return cli.parser().parse_args(['init']), scope(config)


def test_init_is_local_without_application_bindings_and_preserves_identity(tmp_path, monkeypatch):
    args, scope = init_offline(tmp_path, monkeypatch, '''once:
  applications:
    - host: synthetic.example.com
      image: example/image@sha256:abc
      env:
        TOKEN: app-synthetic-unset-token
''')
    monkeypatch.delenv('COLORS_PAR_APP_SYNTHETIC_UNSET_TOKEN', raising=False)
    first = cli.execute(args)
    paths = [tmp_path / name for name in (
        '.envrc.private', '.ssh/id_ed25519', '.ssh/id_ed25519.pub',
        '.ssh/host_ed25519', '.ssh/host_ed25519.pub', '.ssh/known_hosts',
    )]
    assert all(path.is_file() for path in paths)
    assert paths[0].read_bytes() == b''
    assert paths[-1].read_bytes() == b''
    assert all(path.stat().st_mode & 0o077 == 0 for path in (paths[0], paths[1], paths[3]))
    assert (tmp_path / '.colors.sqlite').stat().st_mode & 0o077 == 0
    paths[0].write_text('export SYNTHETIC=preserve\n')
    paths[-1].write_text('synthetic-host synthetic-public-key\n')
    before = {path: path.read_bytes() for path in paths}
    second = cli.execute(args)
    assert first['deployment_id'] == second['deployment_id']
    assert {path: path.read_bytes() for path in paths} == before
    with cli.State(tmp_path / '.colors.sqlite', 'demo', scope, read_only=True) as state:
        assert state.deployment_id == first['deployment_id']
        assert state.resources() == []


def test_init_respects_missing_existing_state_protection(tmp_path, monkeypatch):
    args, _ = init_offline(tmp_path, monkeypatch, 'compute-require-existing-state: true\n')
    with pytest.raises(DeployError, match='Existing deployment state is required'):
        cli.execute(args)
    assert not (tmp_path / '.colors.sqlite').exists()
    assert not (tmp_path / '.ssh').exists()
    assert not (tmp_path / '.envrc.private').exists()


@pytest.mark.parametrize('missing', ['id_ed25519', 'id_ed25519.pub', 'host_ed25519', 'host_ed25519.pub', 'known_hosts'])
def test_init_never_regenerates_missing_established_ssh_identity(tmp_path, monkeypatch, missing):
    args, scope = init_offline(tmp_path, monkeypatch)
    cli.execute(args)
    with cli.State(tmp_path / '.colors.sqlite', 'demo', scope) as state:
        state.put_resource('compute', 'oci-compute', 'synthetic-instance', {}, owned=True)
    missing_path = tmp_path / '.ssh' / missing
    missing_path.unlink()
    with pytest.raises(DeployError, match='SSH identity is missing'):
        cli.execute(args)
    assert not missing_path.exists()


def test_init_preserves_partial_keypair_and_requires_recovery(tmp_path, monkeypatch):
    args, _ = init_offline(tmp_path, monkeypatch)
    cli.execute(args)
    public = tmp_path / '.ssh/id_ed25519.pub'
    public.unlink()
    private = tmp_path / '.ssh/id_ed25519'
    before = private.read_bytes()
    with pytest.raises(DeployError):
        cli.execute(args)
    assert private.read_bytes() == before
    assert not public.exists()


def test_init_custom_public_paths_preserve_existing_sidecar_files(tmp_path, monkeypatch):
    args, _ = init_offline(tmp_path, monkeypatch, '''ssh-private-key-file: .ssh/client
ssh-public-key-file: .ssh/public/client
ssh-host-private-key-file: .ssh/server
ssh-host-public-key-file: .ssh/public/server
''')
    directory = tmp_path / '.ssh'
    directory.mkdir()
    sidecars = [directory / 'client.pub', directory / 'server.pub']
    for sidecar in sidecars:
        sidecar.write_bytes(b'synthetic existing sidecar')
    first = cli.execute(args)
    generated = [directory / name for name in ('client', 'server', 'public/client', 'public/server')]
    before = {path: path.read_bytes() for path in generated + sidecars}
    second = cli.execute(args)
    assert second['deployment_id'] == first['deployment_id']
    assert {path: path.read_bytes() for path in generated + sidecars} == before
    assert all(sidecar.read_bytes() == b'synthetic existing sidecar' for sidecar in sidecars)


def isolated_plan(monkeypatch):
    """Keep CLI path tests entirely offline, using real config and host paths."""
    captured = {}
    class Cloud:
        def __init__(self, config, state):
            captured['config'] = config
            assert state is None
        def plan(self):
            return []
    real_host = cli.Host
    def host(config, state, root):
        captured['host'] = real_host(config, state, root)
        return captured['host']
    monkeypatch.setattr(cli, 'OCI', Cloud)
    monkeypatch.setattr(cli, 'Host', host)
    return captured


@pytest.mark.parametrize('local_config', [False, True])
def test_default_config_never_selects_parent(tmp_path, monkeypatch, local_config):
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG.replace('demo', 'outer'))
    deployment = tmp_path / 'deployment'
    nested = deployment / 'docs' / 'examples'
    nested.mkdir(parents=True)
    (deployment / 'colors.yml').write_text(SYNTHETIC_CONFIG)
    monkeypatch.chdir(nested)
    captured = isolated_plan(monkeypatch)
    if not local_config:
        with pytest.raises(DeployError, match='No colors.yml found in the current directory'):
            cli.execute(cli.parser().parse_args(['plan']))
        assert captured == {}
        assert not list(tmp_path.rglob('.colors.sqlite*'))
        return
    (nested / 'colors.yml').write_text(SYNTHETIC_CONFIG)
    result = cli.execute(cli.parser().parse_args(['plan']))

    assert result['profile'] == 'demo'
    assert captured['config']['_root'] == str(nested)
    assert captured['host'].key == nested / '.ssh' / 'id_ed25519'
    assert (nested / '.colors.sqlite.lock').is_file()
    assert not (deployment / '.colors.sqlite.lock').exists()
    assert not (deployment / '.colors.sqlite').exists()


@pytest.mark.parametrize('flag', ['-f', '--file'])
def test_explicit_config_overrides_local_default_relative_to_caller(tmp_path, monkeypatch, flag):
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG.replace('demo', 'parent'))
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'colors.yml').write_text(SYNTHETIC_CONFIG.replace('demo', 'nearest'))
    selected = tmp_path / 'selected'
    selected.mkdir()
    (selected / 'custom.yml').write_text(SYNTHETIC_CONFIG)
    monkeypatch.chdir(nested)
    captured = isolated_plan(monkeypatch)

    result = cli.execute(cli.parser().parse_args(['plan', flag, '../selected/custom.yml']))

    assert result['profile'] == 'demo'
    assert captured['host'].key.resolve() == selected / '.ssh' / 'id_ed25519'
    assert (selected / '.colors.sqlite.lock').is_file()
    assert not (nested / '.colors.sqlite.lock').exists()


def test_missing_config_exits_nonzero_without_constructing_cloud(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', 'plan'])
    monkeypatch.setattr(cli, 'OCI', lambda *args: pytest.fail('Cloud must not be constructed'))

    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'No colors.yml found' in output.err
    assert 'use -f' in output.err


def test_missing_explicit_config_does_not_fall_back(tmp_path, monkeypatch, capsys):
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', 'plan', '-f', 'missing.yml'])

    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Cannot parse configuration' in output.err
    assert not (tmp_path / '.colors.sqlite.lock').exists()


def run_main(monkeypatch, argv, execute):
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', *argv])
    monkeypatch.setattr(cli, 'execute', execute)
    return cli.main()


@pytest.mark.parametrize('flags', [[], ['--json']])
def test_success_output_contract(monkeypatch, capsys, flags):
    import json
    def execute(args, reporter):
        with reporter.stage('compute'):
            pass
        return {'profile': 'demo', 'actions': [], 'applications': []}
    assert run_main(monkeypatch, ['plan', *flags], execute) == 0
    output = capsys.readouterr()
    assert 'compute' in output.err
    if flags:
        value = json.loads(output.out)
        assert value['schema_version'] == 1
        assert value['command'] == 'plan'
        assert value['ok'] is True
        assert value['result']['profile'] == 'demo'
        assert len(output.out.splitlines()) == 1
    else:
        assert output.out.strip()
        assert not output.out.startswith('{')


def test_quiet_suppresses_progress_but_keeps_result(monkeypatch, capsys):
    import json
    def execute(args, reporter):
        with reporter.stage('compute'):
            pass
        return {'profile': 'demo'}
    assert run_main(monkeypatch, ['status', '--json', '--quiet'], execute) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)['ok']


@pytest.mark.parametrize('failure,code,exit_code', [
    (DeployError('Safe timeout.', code='command_timeout', stage='compute'), 'command_timeout', 1),
    (ValueError('PRIVATE_SENTINEL'), 'operation_failed', 1),
    (KeyboardInterrupt(), 'interrupted', 130),
])
def test_json_failures_one_safe_document(monkeypatch, capsys, failure, code, exit_code):
    import json
    def execute(args, reporter):
        raise failure
    assert run_main(monkeypatch, ['converge', '--json', '--quiet'], execute) == exit_code
    output = capsys.readouterr()
    assert not output.err
    assert 'PRIVATE_SENTINEL' not in output.out
    value = json.loads(output.out)
    assert value['ok'] is False
    assert value['error']['code'] == code
    if isinstance(failure, DeployError):
        assert value['error']['stage'] == 'compute'


def test_json_usage_failure_never_echoes_bad_arguments(monkeypatch, capsys):
    import json
    assert run_main(monkeypatch, ['plan', '--json', '--PRIVATE_SENTINEL'], lambda *args: pytest.fail('execute called')) == 2
    output = capsys.readouterr()
    assert 'PRIVATE_SENTINEL' not in output.out + output.err
    assert not output.err
    assert json.loads(output.out)['error']['code'] == 'invalid_usage'


@pytest.mark.parametrize('code,expected', [(0, 0), (7, 7), (255, 255), (-2, 130)])
def test_ssh_preserves_streams_and_exit_without_footer(monkeypatch, capsys, code, expected):
    import sys
    def execute(args, reporter):
        print('remote stdout')
        print('remote stderr', file=sys.stderr)
        return {'ssh_exit': code}
    assert run_main(monkeypatch, ['ssh'], execute) == expected
    output = capsys.readouterr()
    assert output.out == 'remote stdout\n'
    assert output.err == 'remote stderr\n'


def test_ssh_json_rejected_before_execution(monkeypatch, capsys):
    import json
    assert run_main(monkeypatch, ['ssh', '--json'], lambda *args: pytest.fail('execute called')) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)['error']['code'] == 'invalid_usage'


def test_dag_preserves_failure_code_stage_and_progress(monkeypatch, capsys):
    class Host:
        def prepare_keys(self): return 'synthetic-public'
        def cloud_init(self): return 'synthetic-private'
    class Cloud:
        def converge(self, *args):
            raise DeployError('Safe timeout.', code='command_timeout')
    with pytest.raises(DeployError) as exc:
        asyncio.run(cli.converge({}, None, Cloud(), Host(), 'operation'))
    assert exc.value.code == 'command_timeout'
    assert exc.value.stage == 'compute'
    output = capsys.readouterr()
    assert output.out == ''
    assert 'keys' in output.err and 'compute' in output.err
    assert 'synthetic-private' not in output.err


@pytest.mark.parametrize('argv', [[], ['--help'], ['--help', '--json']])
def test_help_is_stdout_success_without_deployment_access(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, 'load', lambda *a, **kw: pytest.fail('Help read configuration'))
    monkeypatch.setattr(cli, 'execute', lambda *a, **kw: pytest.fail('Help executed a workflow'))
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', *argv])
    if argv:
        with pytest.raises(SystemExit) as exit:
            cli.main()
        assert exit.value.code == 0
    else:
        assert cli.main() == 0
    output = capsys.readouterr()
    assert not output.err
    assert 'usage:' in output.out
    assert 'vault-restore' in output.out and 'converge' in output.out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('argv', [['unknown'], ['--json'], ['create'], ['create', '--json']])
def test_missing_or_invalid_command_still_fails(monkeypatch, capsys, argv):
    import json
    assert run_main(monkeypatch, argv, lambda *a: pytest.fail('execute called')) == 2
    output = capsys.readouterr()
    if '--json' in argv:
        assert not output.err
        result = json.loads(output.out)
        assert result['ok'] is False and result['command'] is None
        assert result['error']['code'] == 'invalid_usage'
    else:
        assert not output.out and 'Invalid command arguments' in output.err


@pytest.mark.parametrize('json_mode', [False, True])
def test_verbose_quiet_conflict_before_execution(monkeypatch, capsys, json_mode):
    import json
    argv = ['plan', '--verbose', '--quiet'] + (['--json'] if json_mode else [])
    assert run_main(monkeypatch, argv, lambda *a: pytest.fail('execute called')) == 2
    output = capsys.readouterr()
    if json_mode:
        assert not output.err
        assert json.loads(output.out)['error']['code'] == 'invalid_usage'
        assert '--verbose and --quiet' in json.loads(output.out)['error']['message']
    else:
        assert not output.out
        assert '--verbose and --quiet' in output.err


def test_verbose_operation_uses_stderr_with_json(monkeypatch, capsys):
    import json
    from pocketdeploy.output import operation
    def execute(args, reporter):
        with operation('oci: inspect'):
            pass
        return {'actions': []}
    assert run_main(monkeypatch, ['plan', '--verbose', '--json'], execute) == 0
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    assert json.loads(output.out)['ok']
    assert 'oci: inspect: started' in output.err
    assert 'oci: inspect: completed' in output.err


def test_verbose_quiet_direct_execute_rejected_before_config(monkeypatch):
    monkeypatch.setattr(cli, 'load', lambda *a, **kw: pytest.fail('Configuration read'))
    with pytest.raises(DeployError) as error:
        cli.execute(cli.parser().parse_args(['plan', '--verbose', '--quiet']))
    assert error.value.code == 'invalid_usage'


def test_real_verbose_init_reports_only_safe_local_progress(tmp_path, monkeypatch, capsys):
    import json
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', 'init', '--json', '--verbose'])
    monkeypatch.setattr(cli, 'OCI', lambda *a: pytest.fail('Init contacted OCI'))
    assert cli.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)['ok'] is True
    assert 'init: local preparation: started' in output.err
    assert 'init: local preparation: completed' in output.err
    assert len(output.out.splitlines()) == 1
