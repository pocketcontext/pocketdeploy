import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pocketdeploy.common import DeployError, run as real_run
from pocketdeploy.github import GitHub, validate_config
from pocketdeploy.state import State


def fixture(tmp_path):
    config = {'profile': 'test-profile', 'once': {'applications': [
        {'host': 'www.example.test', 'image': 'ghcr.io/example/site:latest', 'github': 'example/site'}]}}
    state = State(tmp_path / '.state', 'test-profile', {}, create=True)
    hostpub = tmp_path / 'host.pub'
    hostpub.write_text('ssh-ed25519 c3ludGhldGlj test')
    host = SimpleNamespace(install_github=Mock(), hostpub=hostpub)
    return GitHub(config, state, tmp_path, host), state


def test_profile_and_repository_validation():
    with pytest.raises(DeployError, match='owner/repository'):
        validate_config({'profile': 'test', 'once': {'applications': [{'github': '../bad'}]}})
    with pytest.raises(DeployError, match='image'):
        validate_config({'profile': 'test', 'once': {'applications': [{'github': 'owner/repo', 'image': 'other/image:latest'}]}})
    with pytest.raises(DeployError, match='profile'):
        validate_config({'profile': 'test', 'once': {'applications': [{'github': 'owner/repo', 'image': 'ghcr.io/owner/repo:latest', 'github-environment': 'other'}]}})


def test_unmanaged_environment_never_mutated(tmp_path):
    github, state = fixture(tmp_path)
    with state, patch.object(github, '_pages', return_value=[{'name': 'test-profile', 'id': 42}]), patch.object(github, '_api') as api:
        with pytest.raises(DeployError, match='unmanaged'):
            github.converge({'ip': '192.0.2.1'}, 'operation')
        api.assert_not_called()
        github.host.install_github.assert_not_called()


def test_converge_publishes_secret_only_on_stdin_and_reuses_authority(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, public = github._key(github.apps[0])
        first = private.read_bytes()
        with patch('pocketdeploy.github.run', side_effect=lambda args, **kwargs: real_run(args, **kwargs) if '-y' in args else (_ for _ in ()).throw(AssertionError('must reuse'))):
            assert github._key(github.apps[0])[1] == public
        assert private.read_bytes() == first
        operation = state.begin_operation('converge', 'test')
        with patch.object(github, '_pages', side_effect=[[], []]), patch.object(github, '_api', return_value={'id': 42}), patch('pocketdeploy.github.run', side_effect=lambda args, **kwargs: real_run(args, **kwargs) if args[0] == 'ssh-keygen' else '') as run:
            result = github.converge({'ip': '192.0.2.1', 'user': 'ubuntu'}, operation)
        assert result == {'environments': [{'repository': 'example/site', 'environment': 'test-profile'}]}
        calls = run.call_args_list
        secret = next(c for c in calls if c.args[0][1] == 'secret')
        assert secret.kwargs['input'] == first.decode()
        assert first.decode() not in repr(secret.args)
        assert state.get_resource('github:example/site')['provider_id'] == '42'
        private.unlink()
        recovered, recovered_public = github._key(github.apps[0])
        assert recovered.read_bytes() != first
        assert recovered_public != public


def test_paginated_environments(tmp_path):
    github, state = fixture(tmp_path)
    with state, patch.object(github, '_api', side_effect=[{'environments': [{'name': str(n)} for n in range(100)]}, {'environments': [{'name': 'last'}]}]) as api:
        assert len(github._pages('repos/example/site/environments', 'environments')) == 101
        assert 'page=2' in api.call_args.args[0]


def test_owned_environment_checks_identity_before_host_changes(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        state.put_resource('github:example/site', 'github-environment', '42', {})
        with patch.object(github, '_pages', side_effect=[[{'name': 'test-profile', 'id': 42}], [{'name': 'POCKETDEPLOY_DEPLOYMENT_ID', 'value': 'other'}]]):
            with pytest.raises(DeployError, match='ownership'):
                github.converge({'ip': '192.0.2.1'}, 'operation')
        github.host.install_github.assert_not_called()


def test_fresh_plan_preflight_has_no_state(tmp_path):
    github, state = fixture(tmp_path)
    state.db.close()
    github.state = None
    with patch.object(github, '_api', return_value={'permissions': {'admin': True}}), patch.object(github, '_pages', return_value=[]):
        github.preflight()
        assert github.plan()['environments'][0]['action'] == 'create'


def test_missing_owner_marker_refused_after_success(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        state.put_resource('github:example/site', 'github-environment', '42', {})
        with patch.object(github, '_pages', side_effect=[[{'name': 'test-profile', 'id': 42}], []]):
            with pytest.raises(DeployError, match='ownership'):
                github.converge({'ip': '192.0.2.1'}, 'operation')


def test_uncertain_create_does_not_issue_second_create(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        state.set_meta('github-pending:example/site', True)
        with patch.object(github, '_pages', return_value=[]), patch.object(github, '_api') as api:
            with pytest.raises(DeployError, match='Uncertain'):
                github.converge({'ip': '192.0.2.1'}, 'operation')
            api.assert_not_called()


def test_delete_verifies_absence(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        state.put_resource('github:example/site', 'github-environment', '42', {'repository': 'example/site', 'environment': 'test-profile'})
        operation = state.begin_operation('delete', 'test')
        with patch.object(github, '_pages', side_effect=[[{'name': 'test-profile', 'id': 42}], [{'name': 'POCKETDEPLOY_DEPLOYMENT_ID', 'value': state.deployment_id}], []]), patch.object(github, '_api') as api:
            assert github.delete(operation)['deleted_environments'][0]['environment'] == 'test-profile'
            assert api.call_args.args[1] == 'DELETE'
        assert state.get_resource('github:example/site') is None


def test_rotation_preserves_old_authority_until_secret_success_and_retries(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, _ = github._key(github.apps[0])
        original = private.read_bytes()
        state.set_meta('github-key-pending:example/site', False)
        state.put_resource('github:example/site', 'github-environment', '42', {})
        existing = {'name': 'test-profile', 'id': 42, 'deployment_branch_policy':
                    {'protected_branches': False, 'custom_branch_policies': True}}
        def pages(endpoint, field):
            return {'environments': [existing], 'variables': [
                {'name': 'POCKETDEPLOY_DEPLOYMENT_ID', 'value': state.deployment_id}],
                'branch_policies': [{'name': 'main', 'type': 'branch'}]}[field]
        from pocketdeploy.common import run as real_run
        def fail_secret(args, **kwargs):
            if args[0] == 'ssh-keygen':
                return real_run(args, **kwargs)
            if args[1] == 'secret':
                raise DeployError('synthetic uncertain secret update')
            return ''
        operation = state.begin_operation('converge', 'test')
        with patch.object(github, '_pages', side_effect=pages), patch('pocketdeploy.github.run', side_effect=fail_secret):
            with pytest.raises(DeployError, match='uncertain'):
                github.converge({'ip': '192.0.2.1'}, operation, rotate_keys=True)
        rotated = private.read_bytes()
        assert rotated != original
        assert state.get_meta('github-key-pending:example/site') is True
        assert github.host.install_github.call_count == 1
        assert github.host.install_github.call_args.args[1][0]['preserve_existing_keys'] is True
        with patch.object(github, '_pages', side_effect=pages), patch('pocketdeploy.github.run', side_effect=lambda args, **kwargs: real_run(args, **kwargs) if args[0] == 'ssh-keygen' else ''):
            github.converge({'ip': '192.0.2.1'}, operation, rotate_keys=True)
        assert private.read_bytes() == rotated
        assert github.host.install_github.call_args.args[1][0]['preserve_existing_keys'] is False
        assert state.get_meta('github-key-pending:example/site') is False


def test_missing_keys_do_not_block_read_only_preflight(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        state.put_resource('github:example/site', 'github-environment', '42', {})
        with patch.object(github, '_api', return_value={'permissions': {'admin': True}}), patch.object(github, '_pages', side_effect=[
            [{'name': 'test-profile', 'id': 42}], [{'name': 'POCKETDEPLOY_DEPLOYMENT_ID', 'value': state.deployment_id}]]):
            github.preflight()
        assert not github.key_paths(github.apps[0])[0].exists()


def test_disposable_key_recovery_refuses_symlinks(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, public = github.key_paths(github.apps[0])
        private.parent.mkdir()
        target = tmp_path / 'untouched'
        target.write_text('synthetic')
        private.symlink_to(target)
        with pytest.raises(DeployError, match='[Ss]ymlink'):
            github._key(github.apps[0])
        assert target.read_text() == 'synthetic'


def test_interrupted_rotation_preparation_replaces_old_key(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, _ = github._key(github.apps[0])
        original = private.read_bytes()
        state.set_meta('github-key-pending:example/site', 'replace')
        github._key(github.apps[0])
        assert private.read_bytes() != original
        assert state.get_meta('github-key-pending:example/site') is True


def test_failed_prune_reuses_published_candidate(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, _ = github._key(github.apps[0])
        candidate = private.read_bytes()
        operation = state.begin_operation('converge', 'test')
        github.host.install_github.side_effect = [None, DeployError('synthetic prune failure')]
        with patch.object(github, '_pages', side_effect=[[], []]), patch.object(github, '_api', return_value={'id': 42}), patch('pocketdeploy.github.run', side_effect=lambda args, **kwargs: real_run(args, **kwargs) if args[0] == 'ssh-keygen' else ''):
            with pytest.raises(DeployError, match='prune'):
                github.converge({'ip': '192.0.2.1'}, operation)
        assert state.get_meta('github-key-pending:example/site') is True
        github.host.install_github.side_effect = None
        existing = {'name': 'test-profile', 'id': 42, 'deployment_branch_policy':
                    {'protected_branches': False, 'custom_branch_policies': True}}
        with patch.object(github, '_pages', side_effect=[[existing], [
                {'name': 'POCKETDEPLOY_DEPLOYMENT_ID', 'value': state.deployment_id}], [{'name': 'main'}]]), patch('pocketdeploy.github.run', side_effect=lambda args, **kwargs: real_run(args, **kwargs) if args[0] == 'ssh-keygen' else ''):
            github.converge({'ip': '192.0.2.1'}, operation, rotate_keys=True)
        assert private.read_bytes() == candidate
        assert state.get_meta('github-key-pending:example/site') is False


def test_mismatched_key_pair_fails_before_remote_changes(tmp_path):
    github, state = fixture(tmp_path)
    with state:
        private, _ = github._key(github.apps[0])
        public = github.key_paths(github.apps[0])[1]
        public.write_text('ssh-ed25519 c3ludGhldGlj mismatched')
        operation = state.begin_operation('converge', 'test')
        with patch.object(github, '_pages', return_value=[]):
            with pytest.raises(DeployError, match='does not match'):
                github.converge({'ip': '192.0.2.1'}, operation)
        github.host.install_github.assert_not_called()
