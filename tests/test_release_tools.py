"""Stdlib tests for release preparation; no network or real profile access."""
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


prepare_host = module('prepare_host')
audit_release = module('audit_release')


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qqbot-release-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host, self.project = self.root / 'host', self.root / 'project'
        self.host.mkdir()
        self.project.mkdir()
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'Release Fixture')
        self.git('config', 'user.email', 'fixture@users.noreply.github.com')
        self.git('config', 'commit.gpgsign', 'false')
        hooks = self.root / 'empty-hooks'
        hooks.mkdir()
        self.git('config', 'core.hooksPath', str(hooks))
        self.git('config', 'core.autocrlf', 'false')
        (self.host / 'entry.py').write_bytes(b'original = True\n')
        self.git('add', 'entry.py')
        self.git('commit', '-qm', 'fixture baseline')
        commit = self.git('rev-parse', 'HEAD').strip()
        self.patch = patch.object(prepare_host, 'BASE_COMMIT', commit)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.project / 'overlay').mkdir()
        public = b'original = False\n'
        (self.project / 'overlay/entry.py').write_bytes(public)
        diff = ''.join(difflib.unified_diff(['original = True\n'], ['original = False\n'],
                                          fromfile='a/entry.py', tofile='b/entry.py')).encode()
        (self.project / 'patches').mkdir()
        (self.project / 'patches/qqbot-suite.patch').write_bytes(diff)
        manifest = {'upstream': {'commit': commit}, 'files': [
            {'path': 'entry.py', 'sha256': hashlib.sha256(public).hexdigest()}],
            'patch_sha256': hashlib.sha256(diff).hexdigest()}
        (self.project / 'source-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        plugin = self.project / 'plugins/guest-workspace'
        plugin.mkdir(parents=True)
        (plugin / 'plugin.yaml').write_text('name: guest-workspace\n', encoding='utf-8')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.host), *args], text=True, stderr=subprocess.STDOUT)

    def test_default_is_read_only_and_apply_is_verified_idempotent(self):
        before = self.git('status', '--porcelain')
        result = prepare_host.prepare(self.project, self.host)
        self.assertEqual(result['status'], 'check_passed')
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertEqual((self.host / 'entry.py').read_bytes(), b'original = True\n')
        target = self.root / 'explicit-plugin'
        result = prepare_host.prepare(self.project, self.host, apply=True, plugin_target=target)
        self.assertTrue(result['plugin_installed'])
        self.assertEqual((self.host / 'entry.py').read_bytes(), b'original = False\n')
        self.assertEqual(prepare_host.prepare(self.project, self.host)['status'], 'already_matches')

    def test_dirty_host_is_refused_without_changes(self):
        (self.host / 'unrelated.txt').write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(prepare_host.PreparationError, 'dirty'):
            prepare_host.prepare(self.project, self.host, apply=True)
        self.assertEqual((self.host / 'entry.py').read_bytes(), b'original = True\n')

    def test_modified_overlay_and_patch_are_refused(self):
        source = self.project / 'overlay/entry.py'
        source.write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(prepare_host.PreparationError, 'manifest'):
            prepare_host.prepare(self.project, self.host, apply=True)
        source.write_bytes(b'original = False\n')
        (self.project / 'patches/qqbot-suite.patch').write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(prepare_host.PreparationError, 'Patch hash'):
            prepare_host.prepare(self.project, self.host, apply=True)

    def test_existing_plugin_and_unconfirmed_copy_are_refused(self):
        target = self.root / 'explicit-plugin'
        target.mkdir()
        (target / 'keep.txt').write_text('keep', encoding='utf-8')
        with self.assertRaises(prepare_host.PreparationError):
            prepare_host.prepare(self.project, self.host, apply=True, plugin_target=target)
        with self.assertRaises(prepare_host.PreparationError):
            prepare_host.prepare(self.project, self.host, plugin_target=self.root / 'new')
        self.assertEqual((target / 'keep.txt').read_text(), 'keep')
        self.assertEqual((self.host / 'entry.py').read_bytes(), b'original = True\n')

    def test_wrong_base_is_refused_without_changes(self):
        with patch.object(prepare_host, 'BASE_COMMIT', '0' * 40):
            manifest = self.project / 'source-manifest.json'
            data = json.loads(manifest.read_text(encoding='utf-8'))
            data['upstream']['commit'] = '0' * 40
            manifest.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(prepare_host.PreparationError, 'exact pinned'):
                prepare_host.prepare(self.project, self.host, apply=True)
        self.assertEqual((self.host / 'entry.py').read_bytes(), b'original = True\n')

    def test_reparse_points_are_rejected_without_path_is_junction(self):
        from types import SimpleNamespace
        attributes = getattr(prepare_host.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        with patch.object(prepare_host.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', attributes, create=True):
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(
                    st_mode=prepare_host.stat.S_IFDIR, st_file_attributes=attributes)):
                self.assertTrue(prepare_host.linked(self.host))
        self.assertFalse(prepare_host.linked(self.root / 'absent'))
        self.assertFalse(prepare_host.linked(self.host / 'entry.py'))

    def test_manifest_paths_cannot_escape(self):
        for value in ('../outside', '/outside', 'C:/outside', '.git/hooks/x', 'a\\b'):
            with self.subTest(value=value), self.assertRaises(prepare_host.PreparationError):
                prepare_host.relative(value)


class HygieneTests(unittest.TestCase):
    def test_findings_never_echo_secret_or_private_deny_values(self):
        with tempfile.TemporaryDirectory(prefix='qqbot-hygiene-test-') as tmp:
            root = Path(tmp)
            token = 'gho_' + 'X' * 40
            private = 'private-' + 'fixture'
            (root / 'sample.py').write_text(token + '\n' + private, encoding='utf-8')
            result = audit_release.audit(root, deny=[private])
            self.assertFalse(result['passed'])
            encoded = json.dumps(result)
            self.assertNotIn(token, encoded)
            self.assertNotIn(private, encoded)
            self.assertEqual({item['kind'] for item in result['findings']}, {'github-token', 'private-deny-match'})


if __name__ == '__main__':
    unittest.main()
