"""Exercise snippet composition using synthetic SSH homes only."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    'ssh_config_adversarial', Path(__file__).resolve().parents[1] / 'src/pocketdeploy/ssh_config.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def setup_home(tmp_path, suffix=''):
    home = tmp_path.resolve()
    ssh = home / '.ssh'
    ssh.mkdir()
    snippets = ssh / 'config.d'
    snippets.mkdir()
    config = ssh / 'config'
    config.write_text(f'Host *\n    Include "{snippets}/*"\n' + suffix)
    return home, config, snippets


def payload(home, alias):
    return dict(host_alias=alias, block_state='present',
                identity_file=str(home / 'key'), known_hosts_file=str(home / 'hosts'),
                ssh_hosts=[dict(name=alias, ip='203.0.113.10', user='ubuntu')])


def effective(config, alias):
    result = subprocess.run(['ssh', '-G', '-F', str(config), alias],
                            check=True, capture_output=True, text=True)
    return dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)


def test_disjoint_owned_snippets_compose_and_remove_independently(tmp_path):
    home, config, snippets = setup_home(tmp_path, '    ServerAliveInterval 42\n')
    original = config.read_bytes()
    for alias in ('zebra', 'alpha'):
        assert MODULE.update(payload(home, alias), home, owner='ALICE')
    for alias in ('alpha', 'zebra'):
        settings = effective(config, alias)
        assert settings['hostname'] == '203.0.113.10'
        assert settings['stricthostkeychecking'] == 'true'
        assert settings['serveraliveinterval'] == '42'
        assert not MODULE.update(payload(home, alias), home, owner='ALICE')
    assert effective(config, 'unrelated')['serveraliveinterval'] == '42'
    removal = dict(payload(home, 'alpha'), block_state='absent', ssh_hosts=[])
    assert MODULE.update(removal, home, owner='ALICE')
    assert effective(config, 'zebra')['hostname'] == '203.0.113.10'
    assert config.read_bytes() == original
    assert len(list(snippets.iterdir())) == 1


@pytest.mark.parametrize('scope', ['Host unrelated', 'Match host unrelated'])
def test_scoped_include_refusal_keeps_existing_snippet(tmp_path, scope):
    home, config, snippets = setup_home(tmp_path)
    MODULE.update(payload(home, 'probe'), home, owner='ALICE')
    before = {path.name: path.read_bytes() for path in snippets.iterdir()}
    config.write_text(f'{scope}\n    Include "{snippets}/*"\n')
    original = config.read_bytes()
    with pytest.raises(ValueError, match='conditional SSH Include'):
        MODULE.update(payload(home, 'probe'), home, owner='ALICE')
    assert config.read_bytes() == original
    assert {path.name: path.read_bytes() for path in snippets.iterdir()} == before


def test_earlier_nested_wildcard_trust_cannot_override_managed_alias(tmp_path):
    home, config, snippets = setup_home(tmp_path)
    defaults = home / 'defaults.conf'
    defaults.write_text('Host pro*\n    StrictHostKeyChecking no\nHost *\n')
    (snippets / '00-aaa.conf').write_text(f'Include "{defaults}"\n')
    with pytest.raises(ValueError, match='before .*SSH options'):
        MODULE.update(payload(home, 'probe'), home, owner='ALICE')
    assert len(list(snippets.iterdir())) == 1


def test_disjoint_user_snippet_does_not_change_parent_include_scope(tmp_path):
    home, config, snippets = setup_home(tmp_path, '    ServerAliveInterval 42\n')
    (snippets / '00-aaa.conf').write_text('Host unrelated\n    User operator\n')
    assert effective(config, 'other')['serveraliveinterval'] == '42'
    assert MODULE.update(payload(home, 'probe'), home, owner='ALICE')
    assert effective(config, 'probe')['hostname'] == '203.0.113.10'
    assert effective(config, 'other')['serveraliveinterval'] == '42'
