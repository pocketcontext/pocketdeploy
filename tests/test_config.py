import pytest
from pocketdeploy.config import load
from pocketdeploy.common import DeployError

BASE = '''profile: demo
oci-config-file-profile: test
oci-compartment-id: compartment
oci-subnet-id: subnet
oci-availability-domain: ad
once:
  applications:
    - host: test.example.com
      image: example/image@sha256:abc
      env:
        TOKEN: app-test-token
'''


def test_references_preserve_strings_and_do_not_mutate_source(tmp_path):
    p = tmp_path / 'colors.yml'; p.write_text(BASE)
    c = load(p, env={'COLORS_PAR_APP_TEST_TOKEN':'001=false=secret'})
    assert c['once']['applications'][0]['resolved-env'] == {'TOKEN':'001=false=secret'}
    assert p.read_text() == BASE
    assert '001=false=secret' not in c['_desired_hash']


def test_missing_binding_and_unknown_field_fail_closed(tmp_path):
    p = tmp_path / 'colors.yml'; p.write_text(BASE)
    with pytest.raises(DeployError, match='missing'):
        load(p, env={})
    p.write_text(BASE + '\nprovider-backend: r2\n')
    with pytest.raises(DeployError, match='Unsupported'):
        load(p, env={})


def test_literal_equals_and_yaml12(tmp_path):
    p = tmp_path / 'colors.yml'
    p.write_text(BASE.replace('        TOKEN: app-test-token','        - TOKEN=no=001'))
    c=load(p,env={})
    assert c['once']['applications'][0]['resolved-env']['TOKEN']=='no=001'


def test_symlink_private_path_rejected(tmp_path):
    (tmp_path / '.ssh').symlink_to('/tmp')
    p=tmp_path/'colors.yml';p.write_text(BASE+'\nssh-private-key-file: .ssh/key\n')
    with pytest.raises(DeployError,match='Symlink'):
        load(p,env={})


def test_only_declared_bindings_are_captured_and_env_changes_have_new_identity(tmp_path):
    p = tmp_path / 'colors.yml'; p.write_text(BASE)
    a = load(p, env={'COLORS_PAR_APP_TEST_TOKEN': 'first', 'COLORS_PAR_UNRELATED_TOKEN': 'unrelated'})
    b = load(p, env={'COLORS_PAR_APP_TEST_TOKEN': 'second'})
    assert 'unrelated-token' not in a
    assert a['_desired_hash'] != b['_desired_hash']
