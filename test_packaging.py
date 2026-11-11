"""Entry point checks independent of GTK, administrator access, and fan writes."""

import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lemur_fan_control import launcher


class LauncherTests(unittest.TestCase):
    def test_gui_uses_system_python_with_installed_module_path(self):
        with patch.object(launcher.os, 'execv') as execute:
            launcher.gui_main(['--demo'])
        command = execute.call_args.args[1]
        self.assertEqual(command[0], '/usr/bin/python3')
        self.assertEqual(Path(command[1]), launcher.module_path('lemur_fan_gui'))
        self.assertEqual(command[2:], ['--demo'])

    def test_missing_system_python_reports_a_clear_error(self):
        with patch.object(launcher, 'SYSTEM_PYTHON', Path('/not-installed/python3')), \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(launcher.gui_main([]), 1)
            self.assertIn('requires Linux', error.getvalue())

    def test_setup_help_does_not_request_root(self):
        with patch.object(launcher.subprocess, 'call') as call, \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            launcher.setup_main(['--help'])
        self.assertEqual(result.exception.code, 0)
        call.assert_not_called()

    def test_invalid_setup_operation_does_not_request_root(self):
        with patch.object(launcher.subprocess, 'call') as call, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            launcher.setup_main(['arbitrary-command'])
        call.assert_not_called()

    def test_setup_uses_system_python_and_explicit_privilege_request(self):
        with patch.object(launcher.os, 'geteuid', return_value=1000), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(launcher.subprocess, 'call', return_value=0) as call:
            self.assertEqual(launcher.setup_main(['install', '--user', 'desktop']), 0)
        self.assertEqual(call.call_args.args[0],
                         ['/usr/bin/pkexec', '/usr/bin/python3',
                          str(launcher.module_path('setup_passwordless')), 'install', '--user', 'desktop'])

    def test_setup_returns_authentication_cancellation(self):
        with patch.object(launcher.os, 'geteuid', return_value=1000), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(launcher.subprocess, 'call', return_value=126):
            self.assertEqual(launcher.setup_main(['uninstall']), 126)

    def test_desktop_install_and_remove_are_explicit_user_actions(self):
        with tempfile.TemporaryDirectory(prefix='lemur package ') as directory:
            root = Path(directory)
            command = root / 'venv/bin/lemur-fan-gui'
            command.parent.mkdir(parents=True)
            command.write_text('#!/bin/sh\n')
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'data')}), \
                 patch.object(launcher.sys, 'executable', str(command.parent / 'python')), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.gui_main(['--install-desktop']), 0)
                desktop = root / 'data/applications/lemur-fan-control.desktop'
                text = desktop.read_text()
                self.assertIn(f'Exec="{command}"', text)
                if shutil.which('desktop-file-validate'):
                    subprocess.run(['desktop-file-validate', str(desktop)], check=True)
                self.assertEqual(launcher.gui_main(['--remove-desktop']), 0)
                self.assertFalse(desktop.exists())


if __name__ == '__main__':
    unittest.main()
