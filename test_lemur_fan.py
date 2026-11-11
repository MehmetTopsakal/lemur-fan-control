import argparse
import contextlib
import io
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

import lemur_fan as fan


def args(*extra):
    return fan.parser().parse_args(list(extra) or ['fixed', '60', '--seconds', '1'])


def sample(temperature=50, rpm=2200):
    return dict(temperature_c=temperature, rpm=rpm, duty_percent=60)


class FakeEC:
    def __init__(self, mode=0, fail_entry=False):
        self.mode = mode
        self.calls = []
        self.fail_entry = fail_entry

    def get_mode(self):
        return self.mode

    def probe_fan(self):
        self.calls.append('probe')

    def set_mode(self, manual):
        self.mode = int(manual)
        self.calls.append(('mode', manual))
        if manual and self.fail_entry:
            raise fan.FanError('response lost after switching mode')

    def set_percent(self, value):
        self.calls.append(('duty', value))


class SequenceSensors:
    def __init__(self, *values):
        self.values = list(values) or [sample()]

    def sample(self):
        value = self.values[0]
        if len(self.values) > 1:
            self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.clock = 0
        self.time_patch = patch.object(fan, 'boot_time', side_effect=lambda: self.clock)
        self.time_patch.start()
        self.sleep_patch = patch.object(fan.time, 'sleep', side_effect=self.sleep)
        self.sleep_patch.start()

    def sleep(self, value):
        self.clock += value

    def tearDown(self):
        self.sleep_patch.stop()
        self.time_patch.stop()
        self.output.__exit__(None, None, None)
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)

    def test_timed_run_restores_auto(self):
        ec = FakeEC()
        fan.run_control(args(), SequenceSensors(), ec)
        self.assertEqual(ec.calls, ['probe', ('mode', True), ('duty', 60), ('mode', False)])

    def test_sensor_error_during_run_restores_auto(self):
        ec = FakeEC()
        with self.assertRaisesRegex(fan.FanError, 'sensor gone'):
            fan.run_control(args(), SequenceSensors(sample(), fan.FanError('sensor gone')), ec)
        self.assertEqual(ec.calls[-1], ('mode', False))

    def test_initial_sensor_error_never_switches_mode(self):
        ec = FakeEC()
        with self.assertRaises(fan.FanError):
            fan.run_control(args(), SequenceSensors(fan.FanError('sensor gone')), ec)
        self.assertEqual(ec.calls, [])

    def test_already_manual_never_takes_over(self):
        ec = FakeEC(mode=1)
        with self.assertRaisesRegex(fan.FanError, 'already in manual'):
            fan.run_control(args(), SequenceSensors(), ec)
        self.assertEqual(ec.calls, [])

    def test_unsupported_firmware_never_switches_mode(self):
        ec = FakeEC()
        ec.get_mode = lambda: (_ for _ in ()).throw(fan.FanError('unsupported'))
        with self.assertRaises(fan.FanError):
            fan.run_control(args(), SequenceSensors(), ec)
        self.assertEqual(ec.calls, [])

    def test_uncertain_mode_entry_still_restores_auto(self):
        ec = FakeEC(fail_entry=True)
        with self.assertRaises(fan.FanError):
            fan.run_control(args(), SequenceSensors(), ec)
        self.assertEqual(ec.calls[-1], ('mode', False))
        self.assertEqual(ec.mode, 0)

    def test_high_temperature_forces_full_duty(self):
        ec = FakeEC()
        fan.run_control(args('fixed', '40', '--seconds', '1'), SequenceSensors(sample(90)), ec)
        self.assertIn(('duty', 100), ec.calls)

    def test_zero_rpm_aborts_and_restores(self):
        ec = FakeEC()
        with self.assertRaisesRegex(fan.FanError, 'zero RPM'):
            fan.run_control(args('fixed', '60', '--seconds', '20'),
                            SequenceSensors(sample(rpm=0)), ec)
        self.assertEqual(ec.calls[-1], ('mode', False))

    def test_suspend_gap_aborts_and_restores(self):
        ec = FakeEC()
        def suspend(_):
            self.clock += 8
        with patch.object(fan.time, 'sleep', side_effect=suspend):
            with self.assertRaisesRegex(fan.FanError, 'Suspend'):
                fan.run_control(args('fixed', '60', '--seconds', '20'), SequenceSensors(), ec)
        self.assertEqual(ec.calls[-1], ('mode', False))

    def test_dry_run_performs_no_ec_commands(self):
        messages = []
        fan.run_control(args(), SequenceSensors(), notify=lambda *msg: messages.append(msg))
        self.assertTrue(messages)
        self.assertTrue(all(msg[0] == 'heartbeat' for msg in messages))

    def test_continuous_run_outlasts_default_duration_and_restores_on_stop(self):
        ec = FakeEC()
        config = args('curve', '--continuous')
        def normal_ticks(_):
            self.clock += 1
            if self.clock > config.seconds:
                raise fan.StopControl
        with patch.object(fan.time, 'sleep', side_effect=normal_ticks):
            with self.assertRaises(fan.StopControl):
                fan.run_control(config, SequenceSensors(sample(40)), ec)
        self.assertGreater(self.clock, config.seconds)
        self.assertIn(('duty', 20), ec.calls)
        self.assertEqual(ec.calls[-1], ('mode', False))

    def test_default_curve_uses_current_20_percent_floor_and_rises_with_heat(self):
        policy = fan.Policy(args('curve'))
        self.assertEqual(policy.target(25, 0), 20)
        self.assertEqual(policy.target(45, 1), 20)
        self.assertEqual(policy.target(65, 2), 30)
        self.assertEqual(policy.target(75, 3), 50)
        self.assertEqual(policy.target(80, 4), 85)
        self.assertEqual(policy.target(85, 5), 100)

    def test_thermal_override_hysteresis_and_delayed_reduction(self):
        policy = fan.Policy(args('fixed', '40'))
        self.assertEqual(policy.target(85, 0), 100)
        self.assertEqual(policy.target(81, 1), 100)
        self.assertEqual(policy.target(79, 2), 100)
        self.assertEqual(policy.target(79, 7), 95)
        self.assertEqual(policy.target(90, 8), 100)

    def test_restore_retries_failed_transactions(self):
        ec = FakeEC(mode=1)
        count = 0
        def set_mode(_):
            nonlocal count
            count += 1
            if count < 3:
                raise fan.FanError('busy')
        ec.set_mode = set_mode
        fan.restore(ec)
        self.assertEqual(count, 3)

    def test_auto_does_not_send_a_set_command_if_already_automatic(self):
        ec = FakeEC(mode=0)
        self.assertFalse(fan.restore(ec))
        self.assertEqual(ec.calls, [])

    def test_lost_auto_acknowledgement_is_not_reported_as_failed_recovery(self):
        ec = FakeEC(mode=1)
        def set_mode(_):
            ec.mode = 0
            raise fan.FanError('acknowledgement lost')
        ec.set_mode = set_mode
        self.assertFalse(fan.restore(ec))

    def test_unreadable_mode_does_not_claim_manual_control_is_active(self):
        ec = FakeEC()
        ec.get_mode = lambda: (_ for _ in ()).throw(fan.FanError('unreadable'))
        with self.assertRaisesRegex(fan.FanError, 'could not be confirmed'):
            fan.restore(ec)
        self.assertEqual(ec.calls, [])


class InputAndProtocolTests(unittest.TestCase):
    def test_reject_invalid_curves(self):
        for value in ('45:0,85:100', '60:60,50:100', '45:80,60:50,85:100',
                      '45:40,90:100', '45:40,85:80', 'nan:40,85:100',
                      '45:40', '45:40:50,85:100'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                fan.parse_curve(value)

    def test_interpolation_and_endpoints(self):
        points = fan.parse_curve('45:40,65:60,85:100')
        self.assertEqual(fan.curve_duty(points, 20), 40)
        self.assertEqual(fan.curve_duty(points, 55), 50)
        self.assertEqual(fan.curve_duty(points, 100), 100)

    def test_reject_nonfinite_or_unbounded_duration(self):
        for text in ('0', '-1', 'nan', 'inf', '3601'):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                fan.duration(text)

    def test_continuous_and_timed_options_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            args('curve', '--continuous', '--seconds', '30')

    def test_pwm_encoding_uses_one_based_smfi_index(self):
        ec = fan.EC.__new__(fan.EC)
        calls = []
        ec.command = lambda *call: calls.append(call)
        ec.set_percent(60)
        self.assertEqual(calls, [(8, bytes((1, 153)))])
        with self.assertRaises(fan.FanError):
            ec.set_percent(0)

    def test_ec_transaction_reads_result_and_rejects_error(self):
        ec = fan.EC.__new__(fan.EC)
        ec.layout = fan.EC_RELEASES['2025-07-24_c242738']
        ec.version = '2025-07-24_c242738'
        memory = {0: 0, 1: 0, 2: 0}
        def write(offset, value):
            memory[offset] = value
            if offset == 0:
                memory[0] = 0
                memory[2] = 1
        ec._write = write
        ec._read = lambda offset: memory[offset]
        self.assertEqual(ec.get_mode(), 1)
        memory[1] = 1
        with self.assertRaisesRegex(fan.FanError, 'rejected'):
            ec.get_mode()
        with self.assertRaisesRegex(fan.FanError, 'Unsupported'):
            ec.command(6)  # EC reset is deliberately unavailable.

    def test_missing_single_cpu_sensor_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hwmon = root / 'class/hwmon'
            cpu = hwmon / 'hwmon0'
            ec = hwmon / 'hwmon1'
            cpu.mkdir(parents=True)
            ec.mkdir(parents=True)
            for directory, values in ((cpu, {'name': 'coretemp', 'temp1_input': '50000',
                                            'temp2_input': '60000'}),
                                      (ec, {'name': 'system76_acpi', 'temp1_input': '55000',
                                            'fan1_input': '2000', 'pwm1': '153'})):
                for name, value in values.items():
                    (directory / name).write_text(value)
            with patch.object(fan, 'SYS', root):
                sensors = fan.Sensors()
                self.assertEqual(sensors.sample()['temperature_c'], 60)
                (cpu / 'temp2_input').unlink()
                with self.assertRaisesRegex(fan.FanError, 'Sensor failure'):
                    sensors.sample()


class SupervisorTests(unittest.TestCase):
    def run_scenario(self, crash):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'modes.txt'
            class LoggedEC(FakeEC):
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def get_mode(self):
                    # EC state persists across handles/processes, unlike this
                    # fixture's per-instance attributes.
                    return int(log.read_text().splitlines()[-1].split()[1]) if log.exists() else 0
                def set_mode(self, manual):
                    super().set_mode(manual)
                    with log.open('a') as output:
                        output.write(f'{os.getpid()} {int(manual)}\n')
                def set_percent(self, value):
                    if crash:
                        os._exit(9)  # Bypass the worker's finally block.
                    super().set_percent(value)
            with patch.object(fan, 'EC', LoggedEC), \
                    patch.object(fan, 'Sensors', return_value=SequenceSensors()), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = fan.supervised(args())
            return result, log.read_text().splitlines()

    def test_worker_crash_triggers_parent_recovery(self):
        result, lines = self.run_scenario(crash=True)
        self.assertEqual(result, 1)
        self.assertEqual(lines[-1], f'{os.getpid()} 0')

    def test_successful_worker_exit_does_not_get_terminated_or_take_over_twice(self):
        result, lines = self.run_scenario(crash=False)
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[-1].split()[0], str(os.getpid()))
        self.assertEqual(lines[-1].split()[1], '0')


class FirmwareCompatibilityTests(unittest.TestCase):
    @contextlib.contextmanager
    def firmware(self, version, mode_get, mode_set, require_supported=True, initial_mode=1):
        memory = bytearray(256)
        state = {'mode': initial_mode, 'commands': [], 'changes': []}
        def read(_, offset):
            return memory[offset]
        def write(_, offset, value):
            memory[offset] = value
            if offset != 0:
                return
            command = value
            state['commands'].append(command)
            memory[1] = 0
            if command == 1:
                memory[2:5] = bytes((0x76, 0xEC, 1))
            elif command in (2, 3):
                content = b'system76/lemp13' if command == 2 else version.encode('ascii')
                memory[2:] = content.ljust(254, b'\0')
            elif command == 7:
                memory[3] = 153
            elif command == mode_get:
                memory[2] = state['mode']
            elif command == mode_set:
                state['mode'] = memory[2]
                state['changes'].append(memory[2])
            else:
                memory[1] = 1
            memory[0] = 0
        with contextlib.ExitStack() as stack:
            for target, replacement in (
                    ('check_model', lambda: None),
                    ('EC._read', read), ('EC._write', write)):
                stack.enter_context(patch(f'lemur_fan.{target}', replacement))
            stack.enter_context(patch('lemur_fan.os.open', return_value=123))
            stack.enter_context(patch('lemur_fan.os.close'))
            stack.enter_context(patch('lemur_fan.fcntl.lockf'))
            stack.enter_context(patch('lemur_fan.Path.is_dir', return_value=True))
            with fan.EC(require_supported=require_supported) as ec:
                yield ec, state

    def test_2025_firmware_reads_mode_22_and_sets_mode_23(self):
        with self.firmware('2025-07-24_c242738', 22, 23) as (ec, state):
            self.assertEqual(ec.get_mode(), 1)
            self.assertEqual(state['changes'], [])
            ec.probe_fan()
            self.assertEqual(state['changes'], [])
            self.assertTrue(fan.restore(ec))
            self.assertEqual(state['changes'], [0])
            self.assertNotIn(24, state['commands'])

    def test_2025_auto_is_read_only_when_automatic(self):
        with self.firmware('2025-07-24_c242738', 22, 23, initial_mode=0) as (ec, state):
            self.assertFalse(fan.restore(ec))
            self.assertEqual(state['commands'], [1, 2, 3, 22])
            self.assertEqual(state['changes'], [])

    def test_2026_firmware_reads_mode_23_and_sets_mode_24(self):
        with self.firmware('2026-09-23_8faf1c2', 23, 24) as (ec, state):
            self.assertEqual(ec.get_mode(), 1)
            self.assertEqual(state['changes'], [])
            ec.set_mode(False)
            self.assertEqual(state['changes'], [0])
            self.assertNotIn(22, state['commands'])

    def test_unknown_firmware_diagnostic_never_guesses_mode_commands(self):
        with self.firmware('unknown', 22, 23, require_supported=False) as (ec, state):
            with self.assertRaisesRegex(fan.FanError, 'Unknown EC layout'):
                ec.get_mode()
            with self.assertRaisesRegex(fan.FanError, 'Unknown EC layout'):
                ec.set_mode(False)
            with self.assertRaisesRegex(fan.FanError, 'Unsupported EC command'):
                ec.command(8, bytes((1, 255)))
            self.assertEqual(state['commands'], [1, 2, 3])
            self.assertEqual(state['mode'], 1)
            self.assertEqual(state['changes'], [])

    def test_unknown_firmware_refuses_control_at_initialization(self):
        with self.assertRaisesRegex(fan.FanError, 'Unverified EC build'):
            with self.firmware('unknown', 22, 23):
                self.fail('unknown firmware was accepted')


if __name__ == '__main__':
    unittest.main()
