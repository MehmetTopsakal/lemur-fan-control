"""Installer and authorization checks using temporary paths, with no root or EC."""

from contextlib import ExitStack
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import lemur_fan_gui as gui
import setup_passwordless as setup


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.lib = self.root / 'lib'
        self.helper = self.root / 'bin/helper'
        self.policy = self.root / 'actions/fan.policy'
        self.rule = self.root / 'rules/fan.rules'
        for name, value in (('LIB', self.lib), ('HELPER', self.helper),
                            ('POLICY', self.policy), ('RULE', self.rule)):
            self.stack.enter_context(patch.object(setup, name, value))
        self.user = SimpleNamespace(pw_name='desktop', pw_uid=1234)
        self.stack.enter_context(patch.object(setup, 'account', return_value=self.user))
        # File layout tests use a temporary user directory. The real ownership
        # guard is checked independently below, with links/writable ancestors.
        self.stack.enter_context(patch.object(setup, 'trusted'))
        self.stack.enter_context(patch.object(setup.os, 'chown'))
        self.stack.enter_context(patch.object(setup.os, 'fchown'))
        self.stack.enter_context(patch('sys.stdout', new=io.StringIO()))

    def test_install_copies_only_protected_backend_modules(self):
        setup.install('desktop')
        manifest = json.loads((self.lib / 'installed.json').read_text())
        self.assertEqual(manifest['user'], 'desktop')
        self.assertEqual(manifest['uid'], 1234)
        for name in setup.MODULES:
            data = (self.lib / name).read_bytes()
            self.assertEqual(data, (setup.SOURCE / name).read_bytes())
            self.assertEqual(manifest['sha256'][name], hashlib.sha256(data).hexdigest())
            self.assertEqual(stat.S_IMODE((self.lib / name).stat().st_mode), 0o644)
        self.assertFalse((self.lib / 'lemur_fan_gui.py').exists())
        self.assertIn('/usr/bin/python3 -I -B', self.helper.read_text())
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o755)

    def test_policy_matches_only_helper_and_denies_by_default(self):
        setup.install('desktop')
        action = ET.fromstring(self.policy.read_text()).find('action')
        self.assertEqual(action.attrib['id'], setup.ACTION)
        for name in ('allow_any', 'allow_inactive', 'allow_active'):
            self.assertEqual(action.find(f'defaults/{name}').text, 'no')
        self.assertEqual(action.find('annotate').text, str(self.helper))
        self.assertNotEqual(action.find('annotate').text, '/usr/bin/python3')

    def test_uninstall_revokes_policy_and_keeps_unrelated_files(self):
        setup.install('desktop')
        extra = self.lib / 'unrelated.txt'
        extra.write_text('keep')
        setup.uninstall()
        self.assertEqual(extra.read_text(), 'keep')
        for path in (self.rule, self.policy, self.helper, self.lib / 'installed.json'):
            self.assertFalse(path.exists())

    def test_install_and_uninstall_are_repeatable(self):
        setup.install('desktop')
        setup.install('desktop')
        setup.uninstall()
        setup.uninstall()
        self.assertFalse(self.lib.exists())

    def test_failed_update_does_not_keep_password_free_grant(self):
        setup.install('desktop')
        with patch.object(setup, 'write_file', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                setup.install('desktop')
        self.assertFalse(self.rule.exists())

    def test_invalid_source_is_rejected_before_modifying_installation(self):
        setup.install('desktop')
        original_rule = self.rule.read_bytes()
        source = self.root / 'source'
        source.mkdir()
        for name in setup.MODULES:
            (source / name).write_text('this is not python!')
        with patch.object(setup, 'SOURCE', source), self.assertRaises(SyntaxError):
            setup.install('desktop')
        self.assertEqual(self.rule.read_bytes(), original_rule)


class AccessTests(unittest.TestCase):
    def test_root_and_system_accounts_are_refused(self):
        for uid, name in ((0, 'root'), (998, 'daemon'), (65534, 'nobody')):
            user = SimpleNamespace(pw_name=name, pw_uid=uid)
            with patch.object(setup.pwd, 'getpwnam', return_value=user), self.assertRaises(ValueError):
                setup.account(name)

    def test_account_is_inferred_from_sudo_or_pkexec(self):
        user = SimpleNamespace(pw_name='desktop', pw_uid=1234)
        for environment in ({'SUDO_USER': 'desktop'}, {'PKEXEC_UID': '1234'}):
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(setup.pwd, 'getpwnam', return_value=user), \
                 patch.object(setup.pwd, 'getpwuid', return_value=user):
                self.assertEqual(setup.account(), user)

    def test_installer_requires_root(self):
        with patch.object(setup.os, 'geteuid', return_value=1000), \
             patch('sys.stderr', new=io.StringIO()), patch.object(setup, 'install') as install:
            self.assertEqual(setup.main(['install', '--user', 'desktop']), 1)
            install.assert_not_called()

    def test_symlink_and_writable_ancestors_are_refused(self):
        for unsafe in (stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o777):
            def fake_stat(path):
                mode = unsafe if str(path) == '/trusted' else stat.S_IFDIR | 0o755
                if str(path) == '/trusted/helper':
                    mode = stat.S_IFREG | 0o755
                return SimpleNamespace(st_uid=0, st_mode=mode)
            with self.subTest(mode=unsafe), patch.object(Path, 'lstat', fake_stat):
                self.assertFalse(gui.protected_file(Path('/trusted/helper')))
                with self.assertRaises(ValueError):
                    setup.trusted(Path('/trusted/helper'))

    def test_non_root_owned_helper_is_refused(self):
        def fake_stat(path):
            return SimpleNamespace(st_uid=1000 if str(path).endswith('helper') else 0,
                                   st_mode=(stat.S_IFREG if str(path).endswith('helper')
                                            else stat.S_IFDIR) | 0o755)
        with patch.object(Path, 'lstat', fake_stat):
            self.assertFalse(gui.protected_file(Path('/trusted/helper')))
            with self.assertRaises(ValueError):
                setup.trusted(Path('/trusted/helper'))

    def test_gui_uses_only_installed_helper_after_setup(self):
        with patch.object(gui.os, 'geteuid', return_value=1000), \
             patch.object(gui.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(gui, 'installed_access', return_value=(True, 'installed')), \
             patch.object(gui.threading, 'Thread'), \
             patch.object(gui.subprocess, 'Popen') as popen:
            controller = gui.Controller(queue.Queue())
            controller.start(['curve', '--continuous'])
            self.assertEqual(popen.call_args.args[0],
                             ['/usr/bin/pkexec', str(gui.INSTALLED_HELPER), 'curve', '--continuous'])
            self.assertTrue(controller.passwordless)

    def test_stale_installation_requires_setup_instead_of_running_editable_code(self):
        with patch.object(gui.os, 'geteuid', return_value=1000), \
             patch.object(gui.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(gui, 'installed_access', return_value=(False, 'Update helper')), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(gui.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'Update helper'):
                gui.Controller(queue.Queue()).start(['auto'])
            popen.assert_not_called()

    def test_dry_run_always_uses_unprivileged_local_backend(self):
        with patch.object(gui.os, 'geteuid', return_value=1000), \
             patch.object(gui, 'installed_access', return_value=(True, 'installed')), \
             patch.object(gui.threading, 'Thread'), \
             patch.object(gui.subprocess, 'Popen') as popen:
            gui.Controller(queue.Queue()).start(['curve', '--continuous', '--dry-run'], preview=True)
            self.assertEqual(popen.call_args.args[0][0], sys.executable)
            self.assertNotIn(str(gui.INSTALLED_HELPER), popen.call_args.args[0])

    def test_setup_itself_still_requests_administrator_authentication(self):
        with patch.object(gui.os, 'geteuid', return_value=1000), \
             patch.object(gui.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(gui.threading, 'Thread'), \
             patch.object(gui.subprocess, 'Popen') as popen:
            controller = gui.Controller(queue.Queue())
            controller.start_setup()
            command = popen.call_args.args[0]
            self.assertEqual(command[:2], ['/usr/bin/pkexec', '/usr/bin/python3'])
            self.assertIn(str(gui.HERE / 'setup_passwordless.py'), command)
            self.assertEqual(controller.command, 'setup')
            controller.stop()
            self.assertFalse(controller.stopping)

    def test_privileged_entry_has_no_user_controlled_code_path_option(self):
        source = (gui.HERE / 'lemur_fan_privileged.py').read_text()
        self.assertIn('run_backend(sys.argv[1:])', source)
        self.assertNotIn('cli=', source)
        self.assertNotIn('PYTHONPATH', setup.rule_text('desktop'))


class ManifestTests(unittest.TestCase):
    def test_manifest_account_and_source_hashes_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {'uid': os.getuid(), 'protocol': 1,
                        'sha256': {name: hashlib.sha256((gui.HERE / name).read_bytes()).hexdigest()
                                   for name in setup.MODULES}}
            path = root / 'installed.json'
            with patch.object(gui, 'INSTALLED_LIB', root), \
                 patch.object(gui, 'protected_file', return_value=True):
                path.write_text(json.dumps(manifest))
                self.assertTrue(gui.installed_access()[0])
                manifest['sha256']['lemur_fan.py'] = 'changed'
                path.write_text(json.dumps(manifest))
                self.assertFalse(gui.installed_access()[0])
                manifest['uid'] = -1
                path.write_text(json.dumps(manifest))
                self.assertFalse(gui.installed_access()[0])


class PolkitRuleTests(unittest.TestCase):
    def test_rule_executes_with_only_active_local_user_and_exact_helper_allowed(self):
        library = ctypes.util.find_library('duktape')
        if library is None:
            self.skipTest('Duktape JavaScript runtime not installed')
        engine = ctypes.CDLL(library)
        engine.duk_create_heap.argtypes = [ctypes.c_void_p] * 5
        engine.duk_create_heap.restype = ctypes.c_void_p
        engine.duk_destroy_heap.argtypes = [ctypes.c_void_p]
        engine.duk_get_global_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        engine.duk_push_lstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        engine.duk_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int]
        engine.duk_safe_to_lstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        engine.duk_safe_to_lstring.restype = ctypes.c_char_p
        cases = [({}, 'yes'), ({'username': 'other'}, 'no'),
                 ({'local': False}, 'no'), ({'active': False}, 'no'),
                 ({'program': '/usr/bin/python3'}, 'no'),
                 ({'program': '/tmp/helper'}, 'no'), ({'target': 'other'}, 'no'),
                 ({'action': 'org.freedesktop.policykit.exec'}, 'unhandled')]
        code = '''var callback;
var polkit = {Result: {YES: 'yes', NO: 'no'}, addRule: function(fn) { callback = fn; }};
'''+ setup.rule_text('desktop') + '''
var cases = ''' + json.dumps([case for case, _ in cases]) + ''';
JSON.stringify(cases.map(function(c) {
    var result = callback({id: c.action || ''' + json.dumps(setup.ACTION) + ''',
        lookup: function(key) { return key === 'program' ? (c.program || ''' + json.dumps(str(setup.HELPER)) + ''') :
            (c.target || 'root'); }},
        {user: c.username || 'desktop', local: c.local === undefined ? true : c.local,
         active: c.active === undefined ? true : c.active});
    return result === undefined ? 'unhandled' : result;
}));'''
        context = engine.duk_create_heap(None, None, None, None, None)
        self.assertTrue(context)
        try:
            engine.duk_get_global_string(context, b'eval')
            data = code.encode()
            engine.duk_push_lstring(context, data, len(data))
            status = engine.duk_pcall(context, 1)
            output = engine.duk_safe_to_lstring(context, -1, None).decode()
            self.assertEqual(status, 0, output)
            self.assertEqual(json.loads(output), [expected for _, expected in cases])
        finally:
            engine.duk_destroy_heap(context)


if __name__ == '__main__':
    unittest.main()
