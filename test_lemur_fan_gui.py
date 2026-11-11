"""GUI transport tests; no display, sudo, or real EC access required."""

import io
import os
from pathlib import Path
import queue
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import lemur_fan as fan
import lemur_fan_gui as gui


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cli = Path(self.directory.name) / 'fake_cli.py'
        self.cli.write_text('''import signal, sys, time
def stop(*_):
    print('FAKE automatic restored', flush=True)
    sys.exit(130)
signal.signal(signal.SIGTERM, stop)
print('FAKE started', flush=True)
while True:
    time.sleep(0.05)
''')
        self.process = None

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                # EOF exercises the same cleanup as a disappearing window.
                if self.process.stdin is not None:
                    self.process.stdin.close()
                    self.process.stdin = None
                self.process.communicate(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        self.directory.cleanup()

    def launch(self, timeout=2):
        code = ('import lemur_fan_gui as gui; '
                f'raise SystemExit(gui.run_backend(["curve", "--continuous", "--dry-run"], '
                f'cli={str(self.cli)!r}, timeout={timeout!r}))')
        self.process = subprocess.Popen([sys.executable, '-u', '-c', code],
                                        cwd=gui.HERE, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0)
        self.assertEqual(self.line(), gui.READY)
        return self.process

    def line(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), 'Backend did not produce output')
            return self.process.stdout.readline().decode().strip()

    def start(self, timeout=2):
        process = self.launch(timeout)
        process.stdin.write(b'start\nping\n')
        self.assertEqual(self.line(), 'FAKE started')
        return process

    def assert_restored(self):
        output, error = self.process.communicate(timeout=5)
        self.assertIn(b'FAKE automatic restored', output)
        self.assertEqual(self.process.returncode, 130, error)

    def test_explicit_stop_waits_for_cli_restoration(self):
        process = self.start()
        process.stdin.write(b'stop\n')
        self.assert_restored()

    def test_window_pipe_eof_stops_controller(self):
        process = self.start()
        process.stdin.close()
        process.stdin = None
        self.assert_restored()

    def test_missing_heartbeat_stops_controller(self):
        self.start(timeout=0.5)
        self.assert_restored()

    def test_helper_termination_still_stops_controller(self):
        process = self.start()
        process.send_signal(signal.SIGTERM)
        self.assert_restored()

    def test_disappearing_output_reader_still_restores(self):
        restored = Path(self.directory.name) / 'restored'
        code = self.cli.read_text().replace(
            "print('FAKE automatic restored', flush=True)",
            f"open({str(restored)!r}, 'w').write('automatic'); "
            "print('FAKE automatic restored', flush=True)")
        self.cli.write_text(code)
        process = self.start()
        process.stdout.close()
        process.stdout = None
        process.stdin.close()
        process.stdin = None
        process.wait(timeout=5)
        self.assertEqual(restored.read_text(), 'automatic')

    def test_no_handshake_never_launches_cli(self):
        process = self.launch(timeout=0.3)
        output, error = process.communicate(timeout=5)
        self.assertNotIn(b'FAKE started', output)
        self.assertEqual(process.returncode, 0, error)

    def test_delayed_authorization_after_window_closes_never_starts_cli(self):
        process = self.launch()
        process.stdin.write(b'ping\nstop\n')
        output, error = process.communicate(timeout=5)
        self.assertNotIn(b'FAKE started', output)
        self.assertEqual(process.returncode, 0, error)

    def test_natural_exit_preserves_error_and_return_code(self):
        self.cli.write_text('import sys; print("Error: fake sensor failure", flush=True); sys.exit(3)')
        process = self.launch()
        process.stdin.write(b'start\nping\n')
        self.assertEqual(self.line(), 'Error: fake sensor failure')
        process.wait(timeout=5)
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 3, error)

    def test_real_cli_dry_run_can_be_stopped_without_root(self):
        # Real controller policy and restore path with fake sensors; no EC is
        # constructed, just as in the CLI's ordinary --dry-run path.
        self.cli.write_text(f'import sys; sys.path.insert(0, {str(gui.HERE)!r})\n' + '''import lemur_fan as fan
class Sensors:
    def sample(self):
        return dict(temperature_c=50, rpm=2200, duty_percent=25)
fan.Sensors = Sensors
raise SystemExit(fan.main(['curve', '--continuous', '--dry-run']))
''')
        env = dict(os.environ, PYTHONPATH=str(gui.HERE))
        with patch.dict(os.environ, env):
            process = self.launch()
        process.stdin.write(b'start\nping\n')
        self.assertIn('DRY RUN 50.0 C', self.line())
        process.stdin.write(b'stop\n')
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, error)


class TransportTests(unittest.TestCase):
    def test_validates_with_current_cli_limits(self):
        args = gui.backend_arguments(['curve', '--continuous', '--points', fan.DEFAULT_CURVE])
        self.assertEqual(args.points, fan.parse_curve(fan.DEFAULT_CURVE))
        self.assertEqual(args.points[0][1], fan.MIN_PERCENT)
        for argv in (['monitor'], ['status'], ['curve'], ['fixed', '60']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                gui.backend_arguments(argv)

    def test_invalid_curve_rejected_before_process_launch(self):
        with patch.object(gui.subprocess, 'Popen') as popen:
            with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
                gui.Controller(queue.Queue()).start(['curve', '--continuous', '--points', '45:0,85:100'])
            popen.assert_not_called()

    def test_only_actual_control_requests_privilege(self):
        for preview in (True, False):
            with self.subTest(preview=preview):
                with patch.object(gui.os, 'geteuid', return_value=1000), \
                     patch.object(gui.shutil, 'which', return_value='/usr/bin/pkexec'), \
                     patch.object(gui.threading, 'Thread'), \
                     patch.object(gui.subprocess, 'Popen') as popen:
                    controller = gui.Controller(queue.Queue())
                    argv = ['curve', '--continuous'] + (['--dry-run'] if preview else [])
                    controller.start(argv, preview)
                    command = popen.call_args.args[0]
                    self.assertEqual(command[0] == '/usr/bin/pkexec', not preview)
                    self.assertIn('--backend', command)
                    self.assertFalse(popen.call_args.kwargs.get('shell', False))

    def test_stop_closes_pipe_even_during_authorization(self):
        with patch.object(gui.threading, 'Thread'), \
             patch.object(gui.os, 'geteuid', return_value=0), \
             patch.object(gui.subprocess, 'Popen') as popen:
            controller = gui.Controller(queue.Queue())
            controller.start(['auto'])
            stream = popen.return_value.stdin
            controller.stop()
            stream.write.assert_called_once_with(b'stop\n')
            stream.close.assert_called_once()
            self.assertIsNone(controller.process.stdin)
            controller.tick()  # Must not write to a closed pipe.

    def test_restoration_is_not_assumed_at_start(self):
        controller = gui.Controller(queue.Queue())
        self.assertFalse(controller.restored)
        self.assertFalse(controller.active)


if __name__ == '__main__':
    unittest.main()
