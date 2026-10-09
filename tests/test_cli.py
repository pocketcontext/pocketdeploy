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


def test_vault_preflight_prevents_cloud_mutation_on_backup_failure(monkeypatch):
    calls=[]
    class Host:
        def prepare_keys(self): calls.append('keys');return 'public'
        def cloud_init(self): return 'private host key'
    class Cloud:
        def converge(self,*args):calls.append('cloud')
    def fail(*args):raise DeployError('Vault unavailable')
    monkeypatch.setattr(cli.vault,'save',fail)
    with pytest.raises(DeployError,match='Vault unavailable'):
        asyncio.run(cli.converge({'vault-save-after-run':True,'_root':'/synthetic'},None,Cloud(),Host(),'op'))
    assert calls == ['keys']


SYNTHETIC_CONFIG = '''profile: demo
oci-config-file-profile: test
oci-compartment-id: compartment
oci-subnet-id: subnet
oci-availability-domain: ad
'''


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
