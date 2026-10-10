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


@pytest.mark.parametrize('user', ['-oProxyCommand=bad', 'ubuntu@host', 'a b', '', 'a' * 33, 42, None])
def test_ssh_user_rejects_options_and_invalid_identity(tmp_path, user):
    import json
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + '\nssh-user: ' + json.dumps(user) + '\n')
    with pytest.raises(DeployError, match='ssh-user'):
        load(path, env={}, resolve=False)


@pytest.mark.parametrize('user', ['ubuntu', 'deploy-user', '_service', 'deploy_01'])
def test_ssh_user_accepts_conventional_accounts(tmp_path, user):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + '\nssh-user: ' + user + '\n')
    assert load(path, env={}, resolve=False)['ssh-user'] == user


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


@pytest.mark.parametrize('value', ['true', 'false'])
def test_retired_automatic_backup_option_requires_explicit_workflow(tmp_path, value):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + '\nvault-save-after-run: ' + value + '\n')
    with pytest.raises(DeployError) as error:
        load(path, env={}, resolve=False)
    assert 'vault-save-after-run' in str(error.value)
    assert 'vault-save' in str(error.value)
    assert 'remove' in str(error.value).lower()


def test_retired_automatic_backup_environment_override_is_not_silently_ignored(tmp_path):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE)
    with pytest.raises(DeployError) as error:
        load(path, env={'COLORS_PAR_VAULT_SAVE_AFTER_RUN': 'false'}, resolve=False)
    assert 'COLORS_PAR_VAULT_SAVE_AFTER_RUN' in str(error.value)
    assert 'vault-save' in str(error.value)


@pytest.mark.parametrize('changes,message', [
    ({'smtp-from': 'mail@'}, 'full address'),
    ({'smtp-from': 'mail@elsewhere.example'}, 'full address'),
    ({'cloudflare-zone-id': 'not-an-id'}, 'zone-id'),
    ({'provider-dns': 'no-infra'}, 'requires Cloudflare'),
])
def test_managed_services_reject_incomplete_configuration(tmp_path, changes, message):
    import json
    c = {'profile': 'demo', 'oci-config-file-profile': 'test',
         'oci-compartment-id': 'compartment', 'oci-subnet-id': 'subnet',
         'oci-availability-domain': 'ad', 'provider-dns': 'cloudflare',
         'provider-smtp': 'resend', 'cloudflare-zone-id': 'a' * 32,
         'smtp-domain': 'notifications.example.com',
         'smtp-from': 'mail@notifications.example.com'}
    c.update(changes)
    path = tmp_path / 'colors.yml'
    path.write_text(json.dumps(c))
    with pytest.raises(DeployError, match=message):
        load(path, env={})

@pytest.mark.parametrize('environment,extra', [({}, '\ncompute-retain-boot-volume: false\n'), ({'COLORS_PAR_COMPUTE_RETAIN_BOOT_VOLUME': 'true'}, '')])
def test_retired_boot_retention_rejected(tmp_path, environment, extra):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + extra)
    with pytest.raises(DeployError, match='retired'):
        load(path, env=environment, resolve=False)


@pytest.mark.parametrize('extra', [
    'ssh-known-hosts-file: .ssh/id_ed25519',
    'ssh-known-hosts-file: .ssh/host_ed25519.pub',
    'ssh-known-hosts-file: custom.yml',
    'ssh-known-hosts-file: .envrc',
    'ssh-known-hosts-file: .colors',
    'ssh-known-hosts-file: .colors.sqlite',
    'ssh-known-hosts-file: .colors.sqlite.lock',
    'ssh-known-hosts-file: .colors.sqlite-journal',
    'ssh-known-hosts-file: .colors.sqlite-wal',
    'ssh-known-hosts-file: .colors.sqlite-shm',
    'ssh-private-key-file: .ssh',
    'state-file: .envrc.private',
    'state-file: custom.yml',
    'state-file: .ssh/id_ed25519',
    'workdir: .envrc.private/nested',
])
def test_local_file_collisions_are_rejected_at_load(tmp_path, extra):
    path = tmp_path / 'custom.yml'
    path.write_text(BASE + '\n' + extra + '\n')
    with pytest.raises(DeployError, match='distinct paths'):
        load(path, env={}, resolve=False)


def test_ssh_paths_cannot_use_disposable_github_namespace(tmp_path):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + '\nssh-known-hosts-file: .ssh/github-' + 'a' * 20 + '.pub\n')
    with pytest.raises(DeployError, match='GitHub authority'):
        load(path, env={}, resolve=False)


@pytest.mark.parametrize('field', ['state-file', 'workdir', '_file'])
@pytest.mark.parametrize('suffix', ['', '.pub', '/nested'])
def test_deployment_paths_cannot_use_disposable_github_namespace(tmp_path, field, suffix):
    from pocketdeploy.config import validate_local_paths
    target = '.ssh/github-' + 'a' * 20 + suffix
    config = {'_root': str(tmp_path), field: str(tmp_path / target)}
    with pytest.raises(DeployError, match='GitHub authority'):
        validate_local_paths(config)
    assert not (tmp_path / '.ssh').exists()


@pytest.mark.parametrize('field', ['state-file', 'workdir'])
@pytest.mark.parametrize('suffix', ['', '.pub'])
def test_reserved_github_paths_are_rejected_when_loading_configuration(tmp_path, field, suffix):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE + '\n' + field + ': .ssh/github-' + 'a' * 20 + suffix + '\n')
    with pytest.raises(DeployError, match='GitHub authority'):
        load(path, env={}, resolve=False)
    assert not (tmp_path / '.ssh').exists()


@pytest.mark.parametrize('flag,allowed', [('', False), ('      manage-dns: false\n', True)])
def test_implicit_cloudflare_dns_requires_tls(tmp_path, flag, allowed):
    path = tmp_path / 'colors.yml'
    path.write_text(BASE.replace('      env:', '      disable_tls: true\n' + flag + '      env:') +
                    '\nprovider-dns: cloudflare\ncloudflare-zone-id: ' + 'a' * 32 + '\n')
    if allowed:
        load(path, env={}, resolve=False)
    else:
        with pytest.raises(DeployError, match='requires TLS'):
            load(path, env={}, resolve=False)
