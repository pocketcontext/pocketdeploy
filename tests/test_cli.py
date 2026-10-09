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


def test_nested_cwd_selects_nearest_config_and_keeps_private_paths_beside_it(tmp_path, monkeypatch):
    (tmp_path / 'colors.yml').write_text(SYNTHETIC_CONFIG.replace('demo', 'outer'))
    deployment = tmp_path / 'deployment'
    nested = deployment / 'docs' / 'examples'
    nested.mkdir(parents=True)
    (deployment / 'colors.yml').write_text(SYNTHETIC_CONFIG)
    monkeypatch.chdir(nested)
    captured = isolated_plan(monkeypatch)

    result = cli.execute(cli.parser().parse_args(['plan']))

    assert result['profile'] == 'demo'
    assert captured['config']['_root'] == str(deployment)
    assert captured['host'].key == deployment / '.ssh' / 'id_ed25519'
    assert (deployment / '.colors.sqlite.lock').is_file()
    assert not (nested / '.colors.sqlite.lock').exists()
    assert not (deployment / '.colors.sqlite').exists()


@pytest.mark.parametrize('flag', ['-f', '--file'])
def test_explicit_config_overrides_discovery_relative_to_caller(tmp_path, monkeypatch, flag):
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
    monkeypatch.setattr(cli, 'find_up', lambda name: None)
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
    monkeypatch.setattr(cli, 'find_up', lambda name: pytest.fail('Explicit files must not use discovery'))
    monkeypatch.setattr(cli.sys, 'argv', ['pocketdeploy', 'plan', '-f', 'missing.yml'])

    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Cannot parse configuration' in output.err
    assert not (tmp_path / '.colors.sqlite.lock').exists()
