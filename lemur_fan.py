#!/usr/bin/env python3
"""Foreground fan controller for the System76 Lemur Pro lemp13 (Linux).

Python standard library only. Status and dry-run never open /dev/port.
Installation and operating limits are documented in README.md.
"""

import argparse
import fcntl
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time


SYS = Path('/sys')
DEFAULT_CURVE = '45:20,65:30,75:50,80:85,85:100'
EMERGENCY_C = 85.0
MIN_PERCENT = 20
INTERVAL = 2.0
# Protocol version 1 alone is insufficient: commands were renumbered without
# changing it. Only select a layout from an exactly audited EC build string.
EC_RELEASES = {
    '2025-07-24_c242738': ('2025-layout', 22, 23),
    '2025-07-02_a832be5': ('2025-layout', 22, 23),
    '2026-09-23_8faf1c2': ('2026-layout', 23, 24),
}


class FanError(Exception):
    pass


class StopControl(Exception):
    pass


def read_text(path):
    return path.read_text().strip()


def identity():
    base = SYS / 'class/dmi/id'
    return {key: read_text(base / filename) for key, filename in (
        ('vendor', 'sys_vendor'), ('product', 'product_name'),
        ('model', 'product_version'), ('firmware', 'bios_version'))}


def check_model():
    info = identity()
    if info['vendor'] != 'System76' or info['model'] != 'lemp13':
        raise FanError('This controller is tailored to System76 lemp13; '
                       f"detected {info['vendor']} {info['model']}. No control attempted.")


class Sensors:
    def __init__(self):
        self.temperatures = []
        self.fan = None
        self.pwm = None
        found_cpu = False
        for hwmon in sorted((SYS / 'class/hwmon').glob('hwmon*')):
            try:
                name = read_text(hwmon / 'name')
            except OSError:
                continue
            if name == 'coretemp':
                paths = sorted(hwmon.glob('temp*_input'))
                found_cpu |= bool(paths)
                self.temperatures.extend(paths)
            elif name == 'system76_acpi':
                self.temperatures.extend(sorted(hwmon.glob('temp*_input')))
                self.fan = hwmon / 'fan1_input'
                self.pwm = hwmon / 'pwm1'
        if not found_cpu or self.fan is None:
            raise FanError('Required coretemp and system76_acpi sensors were not found.')

    def sample(self):
        # Every discovered CPU/EC sensor must remain readable. Never silently
        # continue with a subset after a sensor failure.
        values = []
        try:
            for path in self.temperatures:
                value = int(read_text(path)) / 1000
                if not 0 < value <= 125:
                    raise ValueError(f'implausible temperature {value} C from {path}')
                values.append(value)
            rpm = int(read_text(self.fan))
            pwm = int(read_text(self.pwm))
            if not 0 <= rpm <= 20000 or not 0 <= pwm <= 255:
                raise ValueError('implausible fan telemetry')
        except (OSError, ValueError) as exc:
            raise FanError(f'Sensor failure: {exc}') from exc
        return {'temperature_c': max(values), 'rpm': rpm,
                'duty_percent': round(pwm * 100 / 255, 1)}


class EC:
    """Small, restricted implementation of the official System76 SMFI protocol.

    Lock the command and debug port ranges used by system76_ectool. No firmware
    flashing, arbitrary register writes, security changes, or EC reset commands.
    """

    BASE = 0xE00

    def __init__(self, require_supported=True):
        self.fd = None
        self.layout = None
        self.version = None
        check_model()
        if not (SYS / 'bus/acpi/devices/17761776:00').is_dir():
            raise FanError('System76 Open EC ACPI device was not found.')
        try:
            self.fd = os.open('/dev/port', os.O_RDWR | os.O_CLOEXEC)
            fcntl.lockf(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 0x200, self.BASE)
            if self.command(1, bytes(3)) != bytes((0x76, 0xEC, 1)):
                raise FanError('Unsupported EC protocol signature/version.')
            board = self.command(2, bytes(254)).split(b'\0')[0]
            if board != b'system76/lemp13':
                raise FanError(f'Unexpected EC board: {board!r}')
            self.version = self.command(3, bytes(254)).split(b'\0')[0].decode('ascii')
            self.layout = EC_RELEASES.get(self.version)
            if require_supported and self.layout is None:
                raise FanError(f'Unverified EC build {self.version!r}. No fan-mode '
                               'commands sent. Run diagnose and share its output.')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, offset):
        os.lseek(self.fd, self.BASE + offset, os.SEEK_SET)
        data = os.read(self.fd, 1)
        if len(data) != 1:
            raise FanError('Short EC port read.')
        return data[0]

    def _write(self, offset, value):
        os.lseek(self.fd, self.BASE + offset, os.SEEK_SET)
        if os.write(self.fd, bytes((value,))) != 1:
            raise FanError('Short EC port write.')

    def _wait(self):
        deadline = time.monotonic() + 1.0
        while self._read(0) != 0:
            if time.monotonic() >= deadline:
                raise FanError('EC command timed out.')
            time.sleep(0.001)

    def command(self, command, data=b''):
        # Identity and fan-PWM reads have the same meaning in both audited
        # revisions. Never probe mode numbers to discover which layout works:
        # a read command in one layout is a write command in the other.
        allowed = {1: 3, 2: 254, 3: 254, 7: 2}
        if self.layout is not None:
            _, mode_get, mode_set = self.layout
            allowed.update({8: 2, mode_get: 1, mode_set: 1})
        if command not in allowed or len(data) != allowed[command]:
            raise FanError('Unsupported EC command.')
        self._wait()
        for offset, value in enumerate(data, 2):
            self._write(offset, value)
        self._write(0, command)
        self._wait()
        result = bytes(self._read(offset) for offset in range(2, 2 + len(data)))
        response = self._read(1)
        if response != 0:
            raise FanError(f'EC rejected command {command} (response {response}) '
                           f'on EC build {self.version!r}')
        return result

    def get_mode(self):
        if self.layout is None:
            raise FanError('Unknown EC layout; fan-mode access is disabled.')
        mode = self.command(self.layout[1], b'\0')[0]
        if mode not in (0, 1):
            raise FanError(f'Unexpected fan mode {mode}.')
        return mode

    def set_mode(self, manual):
        if self.layout is None:
            raise FanError('Unknown EC layout; fan-mode access is disabled.')
        self.command(self.layout[2], bytes((int(manual),)))
        if self.get_mode() != int(manual):
            raise FanError('Fan mode did not change as requested.')

    def set_percent(self, percent):
        if not MIN_PERCENT <= percent <= 100:
            raise FanError(f'Fan duty must be between {MIN_PERCENT} and 100 percent.')
        # SMFI uses fan index 1; ACPI channel zero is a different interface.
        self.command(8, bytes((1, round(percent * 255 / 100))))

    def probe_fan(self):
        self.command(7, bytes((1, 0)))


def restore(ec):
    """Return whether a mode change was needed; never write if already auto."""
    last = None
    for _ in range(3):
        try:
            if ec.get_mode() == 0:
                return False
            ec.set_mode(False)
            return True
        except (FanError, OSError) as exc:
            last = exc
            time.sleep(0.1)
    # A response can be lost after the command succeeded. Check before
    # reporting failed restoration or recommending a reboot.
    try:
        if ec.get_mode() == 0:
            return True
    except (FanError, OSError):
        raise FanError(f'Automatic cooling could not be confirmed: {last}. '
                       'If manual control was active, reboot to reinitialize the EC.') from last
    raise FanError(f'EC still reports manual fan control after restoration attempts: {last}. '
                   'Reboot to reinitialize the EC.') from last


def parse_curve(text):
    try:
        points = [tuple(map(float, item.split(':'))) for item in text.split(',')]
        if len(points) < 2 or any(len(p) != 2 for p in points):
            raise ValueError
        if any(not math.isfinite(x) for p in points for x in p):
            raise ValueError
        if any(not 20 <= t <= 85 or not MIN_PERCENT <= duty <= 100 for t, duty in points):
            raise ValueError
        if any(a[0] >= b[0] or a[1] > b[1] for a, b in zip(points, points[1:])):
            raise ValueError
        if points[-1][1] != 100:
            raise ValueError
        return points
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            'Use increasing TEMP:PERCENT pairs, temperatures 20..85 C, '
            f'nondecreasing duties {MIN_PERCENT}..100%, ending at 100%.') from exc


def curve_duty(points, temperature):
    if temperature <= points[0][0]:
        return points[0][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if temperature <= t1:
            return p0 + (p1 - p0) * (temperature - t0) / (t1 - t0)
    return points[-1][1]


class Policy:
    def __init__(self, args):
        self.args = args
        self.hot = False
        self.previous = None
        self.fall_since = None

    def target(self, temperature, now):
        if temperature >= EMERGENCY_C:
            self.hot = True
        elif temperature < EMERGENCY_C - 5:
            self.hot = False
        wanted = (self.args.percent if self.args.command == 'fixed' else
                  curve_duty(self.args.points, temperature))
        wanted = 100 if self.hot else math.ceil(wanted)
        # Increase immediately; wait five seconds before reducing speed.
        if self.previous is not None and wanted < self.previous:
            if self.fall_since is None:
                self.fall_since = now
            wanted = (self.previous if now - self.fall_since < 5 else
                      max(wanted, self.previous - 5))
        else:
            self.fall_since = None
        self.previous = wanted
        return wanted


def boot_time():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def stop_signal(*_):
    raise StopControl


def run_control(args, sensors, ec=None, notify=lambda *_: None, parent_pid=None):
    manual_attempted = False
    policy = Policy(args)
    started = boot_time()
    previous = started
    stall_since = None
    try:
        # Read all sensors before making any change.
        sample = sensors.sample()
        if ec is not None:
            if ec.get_mode() != 0:
                raise FanError('EC is already in manual mode. Stop the other controller '
                               'or use the auto command first.')
            ec.probe_fan()
            # Tell the supervisor before the first potentially state-changing
            # transaction, including a change whose response might be lost.
            notify('armed', None)
            manual_attempted = True
            ec.set_mode(True)
        while True:
            now = boot_time()
            if parent_pid is not None and os.getppid() != parent_pid:
                raise FanError('Supervisor exited; returning to automatic cooling.')
            if now - previous > 5:
                raise FanError('Suspend or controller delay detected; returning to automatic cooling.')
            if not getattr(args, 'continuous', False) and now - started >= args.seconds:
                break
            previous = now
            sample = sensors.sample()
            target = policy.target(sample['temperature_c'], now)
            if ec is not None:
                if ec.get_mode() != 1:
                    raise FanError('Fan mode changed externally; stopping.')
                ec.set_percent(target)
                if sample['rpm'] == 0:
                    stall_since = now if stall_since is None else stall_since
                    if now - stall_since >= 15:
                        raise FanError('Fan still reports zero RPM after 15 seconds.')
                else:
                    stall_since = None
            prefix = 'DRY RUN ' if ec is None else ''
            print(f"{prefix}{sample['temperature_c']:.1f} C | {sample['rpm']} RPM | "
                  f"measured {sample['duty_percent']:.1f}% | target {target}%", flush=True)
            notify('heartbeat', None)
            remaining = (INTERVAL if getattr(args, 'continuous', False) else
                         max(0, args.seconds - (boot_time() - started)))
            time.sleep(min(INTERVAL, remaining))
    finally:
        if manual_attempted:
            # Ignore further terminal signals while restoring. The supervisor
            # can still kill a stuck worker and retry using a new port handle.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            restore(ec)
            notify('restored', None)
            print('Firmware automatic cooling restored.', flush=True)


def worker(args, sender, receiver, parent_pid):
    receiver.close()
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        with EC() as ec:
            run_control(args, Sensors(), ec, lambda *msg: sender.send(msg), parent_pid)
    except (StopControl, KeyboardInterrupt):
        pass
    except BaseException as exc:
        print(f'Error: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        sender.close()


def recover_automatic():
    with EC() as ec:
        changed = restore(ec)
    print('Firmware automatic cooling restored.' if changed else
          'Firmware automatic cooling is already active; no mode change needed.', flush=True)


def diagnose():
    # These command transactions read EC state only; even unknown builds
    # receive no guessed mode commands and no mode/PWM changes.
    info = identity()
    info.update(Sensors().sample())
    with EC(require_supported=False) as ec:
        info['ec_version'] = ec.version
        info['ec_layout'] = ec.layout[0] if ec.layout else 'unverified: fan access disabled'
        if ec.layout is not None:
            info['fan_mode'] = 'automatic' if ec.get_mode() == 0 else 'manual PWM'
    print(json.dumps(info, indent=2))


def supervised(args):
    """Separate process recovers after worker death, stalls, or Ctrl+C.

    The worker also checks its parent on every iteration. Neither process can
    recover if both are killed, or if the OS/EC becomes unresponsive.
    """
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(args, sender, receiver, os.getpid()))
    armed = restored = False
    stopped = False
    failed = False
    last_heartbeat = time.monotonic()
    old_handlers = {sig: signal.signal(sig, stop_signal)
                    for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        process.start()
        sender.close()
        while True:
            if receiver.poll(0.2):
                try:
                    kind, _ = receiver.recv()
                except EOFError:
                    break
                if kind == 'armed':
                    armed = True
                elif kind == 'restored':
                    restored = True
                last_heartbeat = time.monotonic()
            if time.monotonic() - last_heartbeat > 10:
                failed = True
                print('Controller stopped responding; recovering automatic cooling.',
                      file=sys.stderr, flush=True)
                break
    except (StopControl, KeyboardInterrupt):
        stopped = True
    finally:
        # A second Ctrl+C must not interrupt the recovery path.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        if process.pid is not None:
            if process.is_alive() and (stopped or failed):
                process.terminate()
            process.join(timeout=4)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            # Drain messages sent during worker cleanup before deciding whether
            # recovery is necessary. Avoid interfering with an initial refusal.
            while receiver.poll():
                try:
                    kind, _ = receiver.recv()
                except EOFError:
                    break
                armed |= kind == 'armed'
                restored |= kind == 'restored'
        sender.close()
        receiver.close()
        try:
            if armed and not restored:
                recover_automatic()
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
    if stopped:
        return 130
    return 1 if failed or process.exitcode != 0 else 0


def duration(text):
    try:
        value = float(text)
        if not math.isfinite(value) or not 1 <= value <= 3600:
            raise ValueError
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Duration must be 1..3600 seconds.') from exc


def percent(text):
    try:
        value = int(text)
        if not MIN_PERCENT <= value <= 100:
            raise ValueError
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'Duty must be an integer from {MIN_PERCENT} to 100%.') from exc


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command')
    status = commands.add_parser('status', help='Show sensors and model without changing anything')
    status.add_argument('--json', action='store_true')
    commands.add_parser('monitor', help='Watch temperature and fan RPM (Ctrl+C to stop)')
    commands.add_parser('diagnose', help='Read EC version and verified fan mode; no mode/speed changes')
    commands.add_parser('auto', help='Return control to the firmware')
    for name, help_text in [('fixed', 'Hold a duty with an 85 C full-speed override'),
                            ('curve', 'Follow a temperature curve')]:
        command = commands.add_parser(name, help=help_text)
        if name == 'fixed':
            command.add_argument('percent', type=percent)
        else:
            command.add_argument('--points', type=parse_curve, default=parse_curve(DEFAULT_CURVE),
                                 metavar='TEMP:PERCENT,...')
        runtime = command.add_mutually_exclusive_group()
        runtime.add_argument('--seconds', type=duration, default=60.0,
                             help='Run for 1..3600 seconds; default 60')
        runtime.add_argument('--continuous', action='store_true',
                             help='Keep running until stopped; restore automatic cooling on exit')
        command.add_argument('--dry-run', action='store_true',
                             help='Show planned targets using live sensors; no fan writes or root needed')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    args.command = args.command or 'status'
    try:
        if args.command == 'status':
            info = identity()
            info.update(Sensors().sample())
            info['manual_control'] = 'EC runtime check required; sysfs PWM is read-only'
            if getattr(args, 'json', False):
                print(json.dumps(info, indent=2))
            else:
                print(f"{info['vendor']} {info['product']} ({info['model']})")
                print(f"Firmware: {info['firmware']}")
                print(f"CPU/EC maximum: {info['temperature_c']:.1f} C | "
                      f"fan: {info['rpm']} RPM | duty: {info['duty_percent']:.1f}%")
                print('Manual control: requires root, /dev/port, and compatible Open EC firmware.')
            return 0
        if args.command == 'monitor':
            sensors = Sensors()
            while True:
                sample = sensors.sample()
                print(f"{sample['temperature_c']:.1f} C | {sample['rpm']} RPM | "
                      f"{sample['duty_percent']:.1f}%", flush=True)
                time.sleep(INTERVAL)
        if args.command == 'auto':
            recover_automatic()
            return 0
        if args.command == 'diagnose':
            diagnose()
            return 0
        check_model()
        if args.dry_run:
            run_control(args, Sensors())
            return 0
        if os.geteuid() != 0:
            raise FanError('Fan control needs root. Run with sudo, or add --dry-run.')
        if not Path('/dev/port').exists():
            raise FanError('/dev/port is unavailable here. Run on the laptop in a native '
                           'terminal; containers/sandboxes may hide hardware access.')
        return supervised(args)
    except (FanError, OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopControl):
        return 130


if __name__ == '__main__':
    sys.exit(main())
