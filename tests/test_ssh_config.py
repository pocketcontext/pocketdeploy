"""Exercise SSH config ownership without touching operator files or the network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ssh_config', ROOT / 'src/pocketdeploy/ssh_config.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SSHConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.directory = self.home / '.ssh'
        self.path = self.directory / 'config'
        self.payload = dict(host_alias='probe', block_state='present', known_hosts_file=str(self.home / 'known_hosts'),
                            identity_file=str(self.home / 'id_ed25519'),
                            ssh_hosts=[
                                dict(name='probe', ip='203.0.113.10', user='ubuntu'),
                                dict(name='probe-seat', ip='203.0.113.10', user='seat')])

    def write(self, text):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(text)

    def update(self, **overrides):
        return MODULE.update({**self.payload, **overrides}, self.home)

    def test_preflight_is_read_only_and_detects_collision(self):
        with self.assertRaisesRegex(ValueError, 'requires an early Include'):
            self.update(check_only=True)
        self.assertFalse(self.directory.exists())
        self.write('Host probe\n    User operator\n')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outside the deployment marker'):
            self.update(check_only=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(['config'], [p.name for p in self.directory.iterdir()])

    def test_normal_ssh_effective_settings_and_user_options(self):
        original = (f'Include {self.directory}/config.d/*\nHost *\n    IdentityAgent /ambient/agent\n    AddKeysToAgent yes\n'
                    '    ServerAliveInterval 42\n    ControlMaster auto\n'
                    '    RemoteForward 7890 localhost:7890\n')
        self.write(original)
        self.assertTrue(self.update())
        self.assertFalse(self.update())
        self.assertEqual(self.path.read_text(), original)
        command = ['ssh', '-G', '-F', str(self.path), 'probe']
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        effective = result.stdout.lower()
        for line in ['hostname 203.0.113.10', 'user ubuntu', 'identityagent none',
                     'identitiesonly yes', 'forwardagent no', 'serveraliveinterval 42',
                     'controlmaster false', 'stricthostkeychecking true', 'addkeystoagent false']:
            self.assertIn(line, effective)
        self.assertIn('remoteforward', effective)
        self.assertEqual(0o600, (self.directory / 'config.d/00-pocketdeploy-probe.conf').stat().st_mode & 0o777)

    def test_offline_remove_and_retry_preserves_unrelated(self):
        original = f'Include {self.directory}/config.d/*\nHost unrelated\n    User operator\n'
        self.write(original)
        self.update()
        self.assertTrue(self.update(block_state='absent', ssh_hosts=[]))
        self.assertEqual(original, self.path.read_text())
        self.assertFalse(self.update(block_state='absent', ssh_hosts=[]))

    def test_refuses_unsafe_files(self):
        target = self.home / 'outside'
        target.write_text('unchanged')
        self.directory.mkdir()
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'unsafe SSH config'):
            self.update()
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaisesRegex(ValueError, 'unsafe SSH config'):
            self.update()
        self.assertEqual('unchanged', target.read_text())

    def test_refuses_directory_symlink(self):
        target = self.home / 'outside'
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe SSH directory'):
            self.update()
        self.assertEqual([], list(target.iterdir()))

    def test_refuses_malformed_marker_without_rewriting(self):
        original = '# BEGIN probe POCKETDEPLOY MANAGED BLOCK\nHost probe\n'
        self.write(original)
        with self.assertRaisesRegex(ValueError, 'explicit migration'):
            self.update()
        self.assertEqual(original, self.path.read_text())

    def test_dropin_refuses_unowned_empty_symlink_and_hardlink(self):
        self.write('Include config.d/*\n')
        snippets = self.directory / 'config.d'
        snippets.mkdir()
        destination = snippets / '00-pocketdeploy-probe.conf'
        outside = self.home / 'outside'
        outside.write_text('unowned')
        for kind in ('empty', 'unowned', 'symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    destination.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, destination)
                else:
                    destination.write_text('' if kind == 'empty' else 'Host other\n')
                before = destination.read_bytes()
                for mode in ('present', 'absent'):
                    with self.assertRaises(ValueError):
                        self.update(block_state=mode)
                    self.assertEqual(destination.read_bytes(), before)
                destination.unlink()
        self.assertEqual(outside.read_text(), 'unowned')

    def test_missing_include_does_not_publish_snippet(self):
        self.write('Host unrelated\n    User operator\n')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'requires an early Include'):
            self.update()
        self.assertFalse((self.directory / 'config.d').exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_valid_preflight_is_read_only(self):
        self.write('Include config.d/*\n')
        self.path.chmod(0o640)
        self.directory.chmod(0o750)
        before = self.path.read_bytes()
        self.assertFalse(self.update(check_only=True))
        self.assertEqual([p.name for p in self.directory.iterdir()], ['config'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o750)



if __name__ == '__main__':
    unittest.main()
