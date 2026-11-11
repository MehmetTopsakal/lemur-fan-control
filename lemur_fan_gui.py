#!/usr/bin/python3
"""GTK desktop interface for lemur_fan.py; only its controller needs root.

The --backend mode deliberately imports no GTK and accepts only CLI fan
commands. A pipe handshake and heartbeat tie its lifetime to the window.
"""

import argparse
import hashlib
import json
import pwd
import stat
from collections import deque
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import lemur_fan as fan
from lemur_fan_backend import READY, backend_arguments, run_backend


HERE = Path(__file__).resolve().parent
INSTALLED_HELPER = Path('/usr/local/libexec/lemur-fan-control-helper')
INSTALLED_LIB = Path('/usr/local/lib/lemur-fan-control')
TARGET = re.compile(r'\btarget (\d+)%')


def protected_file(path):
    """Check both a root-owned regular file and each of its parent directories."""
    try:
        for item in (*reversed(path.parents), path):
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                return False
            if item == path:
                if not stat.S_ISREG(info.st_mode):
                    return False
            elif not stat.S_ISDIR(info.st_mode):
                return False
        return True
    except OSError:
        return False


def installed_access():
    files = ('lemur_fan.py', 'lemur_fan_backend.py', 'lemur_fan_privileged.py', 'installed.json')
    if not protected_file(INSTALLED_HELPER) or not all(
            protected_file(INSTALLED_LIB / name) for name in files):
        return False, 'One-time setup enables password-free local fan control.'
    try:
        manifest = json.loads((INSTALLED_LIB / 'installed.json').read_text())
        if manifest.get('uid') != os.getuid():
            return False, 'Run one-time setup for your desktop account.'
        if manifest.get('protocol') != 1:
            return False, 'Run one-time setup again to update the installed helper.'
        for name in ('lemur_fan.py', 'lemur_fan_backend.py', 'lemur_fan_privileged.py'):
            if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != manifest['sha256'][name]:
                return False, 'Controller code changed. Run one-time setup again to update the helper.'
    except (OSError, ValueError, KeyError, TypeError):
        return False, 'Run one-time setup again to repair the installed helper.'
    return True, 'Password-free helper installed for your local desktop account.'


class Controller:
    """Nonblocking window-side transport; worker threads never touch GTK."""

    def __init__(self, events):
        self.events = events
        self.process = None
        self.stopping = False
        self.last_ping = 0
        self.preview = False
        self.command = None
        self.restored = False
        self.active = False
        self.passwordless = False

    def start(self, argv, preview=False):
        if self.process is not None:
            raise ValueError('Stop the current operation first.')
        backend_arguments(argv)
        command = [sys.executable, '-u', str(Path(__file__).resolve()), '--backend', *argv]
        self.passwordless = False
        if not preview and os.geteuid() != 0:
            pkexec = shutil.which('pkexec')
            if pkexec is None:
                raise ValueError('pkexec is unavailable. Launch with sudo -H /usr/bin/python3 '
                                 'lemur_fan_gui.py from a terminal instead.')
            self.passwordless, message = installed_access()
            if self.passwordless:
                command = [pkexec, str(INSTALLED_HELPER), *argv]
            elif INSTALLED_HELPER.exists():
                # A stale or damaged privileged copy should be repaired, rather
                # than silently elevating the editable development copy.
                raise ValueError(message)
            else:
                command.insert(0, pkexec)
        self._launch(command, argv[0], preview)

    def start_setup(self):
        if self.process is not None:
            raise ValueError('Stop the current operation before setup.')
        command = ['/usr/bin/python3', str(HERE / 'setup_passwordless.py'), 'install',
                   '--user', os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name]
        if os.geteuid() != 0:
            pkexec = shutil.which('pkexec')
            if pkexec is None:
                raise ValueError('Run sudo /usr/bin/python3 setup_passwordless.py install in a terminal.')
            command.insert(0, pkexec)
        self.passwordless = False
        self._launch(command, 'setup', False)

    def _launch(self, command, operation, preview):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=True, bufsize=0)
        self.preview = preview
        self.command = operation
        self.restored = self.active = self.stopping = False
        self.last_ping = 0
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            self.events.put(('line', line.decode('utf-8', errors='replace').rstrip()))
        self.events.put(('exit', process.wait()))

    def send(self, message):
        if self.process is None or self.process.stdin is None:
            return
        try:
            self.process.stdin.write(message.encode('ascii') + b'\n')
        except (BrokenPipeError, OSError):
            pass

    def tick(self):
        now = time.monotonic()
        if (self.process is not None and self.command != 'setup' and
                not self.stopping and now - self.last_ping >= 1):
            self.send('ping')
            self.last_ping = now

    def stop(self):
        if self.command == 'setup':
            # The atomic system installation should finish. A pending desktop
            # authentication dialog can be cancelled through that dialog.
            return
        self.stopping = True
        self.send('stop')
        # EOF still reaches a privileged helper if this window disappears.
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.close()
            self.process.stdin = None

    def finished(self):
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.stdout.close()
        self.process = None


class WindowLifecycle:
    """Separate hiding the window from explicitly ending the controller."""

    def close(self, *_):
        if self.closing:
            return True
        if self.tray is not None and self.tray.available:
            self.hide()
        else:
            # Never leave a running controller behind an unreachable window.
            self.iconify()
            self.note.set_text('System tray unavailable. Minimized to the taskbar; fan control continues. Use Quit to exit.')
        return True

    def reveal(self, *_):
        self.deiconify()
        self.show()
        self.present()

    def tray_unavailable(self):
        if not self.closing and not self.get_visible():
            self.reveal()
            self.note.set_text('System tray disconnected. Fan control continues; the window has been reopened.')

    def request_quit(self, *_):
        if self.closing:
            return True
        self.closing = True
        self.sensor_stop.set()
        if self.controller.process is not None:
            self.stop()
        else:
            self.finish_close()
        return True


def run_gui(demo=False, smoke_test=None, test_driver=None):
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('Gdk', '3.0')
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango
    except (ImportError, ValueError) as exc:
        print(f'GTK is unavailable: {exc}\n'
              'Install with: sudo apt install python3-gi gir1.2-gtk-3.0 librsvg2-common',
              file=sys.stderr)
        return 1
    if not Gtk.init_check()[0]:
        print('No graphical display is available. Run from your desktop terminal.', file=sys.stderr)
        return 1

    class Window(WindowLifecycle, Gtk.Window):
        def __init__(self):
            super().__init__(title='Lemur Fan Control')
            self.set_default_size(960, 900)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.connect('delete-event', self.close)
            self.events = queue.Queue()
            self.controller = Controller(self.events)
            self.sensor_stop = threading.Event()
            self.closing = False
            self.tray = None
            self.smoke_failed = False
            self.latest = None
            self.target = None
            self.points = fan.parse_curve(fan.DEFAULT_CURVE)
            self.log_lines = deque(maxlen=300)
            self.editable = []
            self.build()
            try:
                from lemur_fan_control.tray import SystemTray
                self.tray = SystemTray(self.reveal, self.stop, self.request_quit, self.tray_unavailable)
            except (GLib.Error, ImportError, RuntimeError) as exc:
                self.add_log(f'System tray unavailable: {exc}')
            self.show_all()
            self.update_mode()
            self.set_busy(False)
            if demo:
                self.state.set_text('DEMO · no hardware access')
                self.on_sample(dict(temperature_c=56.0, rpm=2480, duty_percent=25.9))
                self.note.set_text('Demo preview. Close hides to the tray; use Quit to exit.')
                self.start_button.set_sensitive(False)
                self.preview_button.set_sensitive(False)
                self.auto_button.set_sensitive(False)
                self.diagnose_button.set_sensitive(False)
                self.setup_button.set_sensitive(False)
            else:
                threading.Thread(target=self.read_sensors, daemon=True).start()
            GLib.timeout_add(100, self.poll)
            if smoke_test:
                GLib.timeout_add(800, self.capture_test)

        def label(self, text, css=None):
            label = Gtk.Label(label=text, xalign=0)
            if css:
                label.get_style_context().add_class(css)
            return label

        def build(self):
            css = Gtk.CssProvider()
            css.load_from_data(b'''
                window { background: #f3f5f4; color: #20342d; }
                .title { font-size: 27px; font-weight: bold; }
                .subtitle { color: #65756c; font-size: 13px; }
                .status { background: #e0eae4; color: #234a38; border-radius: 18px;
                          padding: 9px 14px; font-size: 12px; font-weight: bold; }
                .card { background: #ffffff; border: 1px solid #dce4df;
                        border-radius: 12px; padding: 16px; }
                .metric { font-size: 29px; font-weight: bold; color: #174c38; }
                .section { font-size: 16px; font-weight: bold; }
                .small { color: #65756c; font-size: 12px; }
                button { border-radius: 7px; padding: 9px 13px; }
                button.suggested-action { background: #1c704e; color: white; }
                button.destructive-action { background: #a23f36; color: white; }
                entry { padding: 8px; border-radius: 6px; }
                .error { color: #a23f36; }
                textview, textview text { background: #ffffff; color: #344f40;
                                         font-family: monospace; font-size: 11px; }
            ''')
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.add(scroll)
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            root.set_border_width(24)
            scroll.add(root)
            heading = Gtk.Box(spacing=16)
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            names.pack_start(self.label('Lemur Fan Control', 'title'), False, False, 0)
            names.pack_start(self.label('Continuous cooling for your System76 Lemur Pro', 'subtitle'),
                             False, False, 0)
            heading.pack_start(names, True, True, 0)
            self.state = self.label('Monitoring · mode unverified', 'status')
            self.state.set_valign(Gtk.Align.CENTER)
            heading.pack_end(self.state, False, False, 0)
            root.pack_start(heading, False, False, 0)

            metrics = Gtk.Box(spacing=12, homogeneous=True)
            self.values = {}
            for key, name, hint in (
                ('temperature', 'CPU / EC temperature', 'Hottest sensor'),
                ('rpm', 'Fan speed', 'Measured rotation'),
                ('duty', 'Measured duty', 'Current PWM signal'),
                ('target', 'Requested duty', 'Shown while running')):
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.get_style_context().add_class('card')
                card.pack_start(self.label(name, 'small'), False, False, 0)
                self.values[key] = self.label('—', 'metric')
                card.pack_start(self.values[key], False, False, 0)
                card.pack_start(self.label(hint, 'small'), False, False, 0)
                metrics.pack_start(card, True, True, 0)
            root.pack_start(metrics, False, False, 0)

            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            panel.get_style_context().add_class('card')
            row = Gtk.Box(spacing=12)
            row.pack_start(self.label('Cooling profile', 'section'), True, True, 0)
            self.mode = Gtk.ComboBoxText()
            self.mode.append('curve', 'Temperature curve')
            self.mode.append('fixed', 'Fixed duty')
            self.mode.set_active_id('curve')
            self.mode.connect('changed', lambda *_: self.update_mode())
            self.editable.append(self.mode)
            row.pack_end(self.mode, False, False, 0)
            panel.pack_start(row, False, False, 0)
            self.stack = Gtk.Stack()
            curve_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            row = Gtk.Box(spacing=10)
            self.curve_entry = Gtk.Entry()
            self.curve_entry.set_text(fan.DEFAULT_CURVE)
            self.curve_entry.set_hexpand(True)
            self.curve_entry.connect('changed', self.change_curve)
            self.curve_entry.set_tooltip_text('Temperature in °C : PWM duty in %, separated by commas')
            self.editable.append(self.curve_entry)
            row.pack_start(self.curve_entry, True, True, 0)
            defaults = Gtk.Button(label='Reset')
            defaults.connect('clicked', lambda *_: self.curve_entry.set_text(fan.DEFAULT_CURVE))
            self.editable.append(defaults)
            row.pack_end(defaults, False, False, 0)
            curve_box.pack_start(self.label('Curve points · temperature °C : duty %', 'small'), False, False, 0)
            curve_box.pack_start(row, False, False, 0)
            self.curve_hint = self.label('', 'small')
            self.curve_hint.set_line_wrap(True)
            curve_box.pack_start(self.curve_hint, False, False, 0)
            self.stack.add_named(curve_box, 'curve')
            fixed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            fixed_box.pack_start(self.label('Requested duty (%)', 'small'), False, False, 0)
            self.fixed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, fan.MIN_PERCENT, 100, 1)
            self.fixed.set_digits(0)
            self.fixed.set_value(60)
            self.fixed.connect('value-changed', lambda *_: self.update_graph())
            self.editable.append(self.fixed)
            fixed_box.pack_start(self.fixed, False, False, 0)
            fixed_box.pack_start(self.label('Full speed at 85 °C; automatic restoration when stopped.', 'small'),
                                 False, False, 0)
            self.stack.add_named(fixed_box, 'fixed')
            panel.pack_start(self.stack, False, False, 0)
            self.graph = Gtk.Image()
            self.graph.set_size_request(-1, 190)
            self.graph.connect('size-allocate', lambda widget, rect: self.update_graph(rect.width))
            panel.pack_start(self.graph, False, False, 0)
            self.profile_info = self.label('', 'small')
            panel.pack_start(self.profile_info, False, False, 0)
            root.pack_start(panel, False, False, 0)

            actions = Gtk.Box(spacing=10)
            self.start_button = Gtk.Button(label='Start curve')
            self.start_button.get_style_context().add_class('suggested-action')
            self.start_button.connect('clicked', lambda *_: self.start(False))
            actions.pack_start(self.start_button, False, False, 0)
            self.preview_button = Gtk.Button(label='Dry run')
            self.preview_button.connect('clicked', lambda *_: self.start(True))
            self.preview_button.set_tooltip_text('Preview targets using live sensors; no password or fan changes')
            actions.pack_start(self.preview_button, False, False, 0)
            self.stop_button = Gtk.Button(label='Stop & restore')
            self.stop_button.get_style_context().add_class('destructive-action')
            self.stop_button.connect('clicked', lambda *_: self.stop())
            actions.pack_start(self.stop_button, False, False, 0)
            self.auto_button = Gtk.Button(label='Firmware automatic')
            self.auto_button.set_tooltip_text('Run the existing auto command to return control to firmware')
            self.auto_button.connect('clicked', lambda *_: self.start_command(['auto']))
            actions.pack_end(self.auto_button, False, False, 0)
            self.diagnose_button = Gtk.Button(label='Diagnostics')
            self.diagnose_button.connect('clicked', lambda *_: self.start_command(['diagnose']))
            actions.pack_end(self.diagnose_button, False, False, 0)
            root.pack_start(actions, False, False, 0)
            setup_row = Gtk.Box(spacing=12)
            self.access_note = self.label(installed_access()[1], 'small')
            self.access_note.set_line_wrap(True)
            setup_row.pack_start(self.access_note, True, True, 0)
            self.setup_button = Gtk.Button(label='One-time setup')
            self.setup_button.set_tooltip_text('Authorize once to install password-free fan control for this desktop account')
            self.setup_button.connect('clicked', lambda *_: self.setup())
            setup_row.pack_end(self.setup_button, False, False, 0)
            self.quit_button = Gtk.Button(label='Quit')
            self.quit_button.set_tooltip_text('Exit the application and restore firmware automatic cooling')
            self.quit_button.connect('clicked', self.request_quit)
            setup_row.pack_end(self.quit_button, False, False, 0)
            root.pack_start(setup_row, False, False, 0)
            self.note = self.label('Monitoring only. Close hides to the tray; Stop & restore or Quit ends control.',
                                   'subtitle')
            self.note.set_line_wrap(True)
            self.note.set_max_width_chars(105)
            root.pack_start(self.note, False, False, 0)
            self.sensor_note = self.label('', 'small')
            self.sensor_note.set_ellipsize(Pango.EllipsizeMode.END)
            root.pack_start(self.sensor_note, False, False, 0)

            expander = Gtk.Expander(label='Controller output')
            self.log = Gtk.TextView(editable=False, cursor_visible=False)
            self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.log.set_left_margin(10)
            self.log.set_right_margin(10)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_min_content_height(140)
            log_scroll.add(self.log)
            expander.add(log_scroll)
            self.expander = expander
            root.pack_start(expander, True, True, 0)
            self.change_curve()

        def update_mode(self):
            mode = self.mode.get_active_id()
            self.stack.set_visible_child_name(mode)
            self.start_button.set_label('Start curve' if mode == 'curve' else 'Start fixed duty')
            self.update_graph()

        def change_curve(self, *_):
            try:
                points = fan.parse_curve(self.curve_entry.get_text())
            except argparse.ArgumentTypeError as exc:
                self.points = None
                self.curve_hint.set_text(str(exc))
                self.curve_hint.get_style_context().add_class('error')
            else:
                self.points = points
                self.curve_hint.get_style_context().remove_class('error')
                self.curve_hint.set_text(f'{points[0][1]:g}% minimum · 100% at {points[-1][0]:g} °C · interpolated between points')
            self.profile_info.set_text(f'Continuous session  •  Sample every {fan.INTERVAL:g} s  •  Full-speed override at {fan.EMERGENCY_C:g} °C')
            self.update_graph()

        def update_graph(self, width=None):
            width, height = max(300, width or self.graph.get_allocated_width()), 190
            left, top, right, bottom = 42, 18, width - 18, height - 30
            x = lambda t: left + (t - 20) / 70 * (right - left)
            y = lambda p: bottom - p / 100 * (bottom - top)
            svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                   'font-family="sans-serif" font-size="11" fill="#66756b">']
            for p in (0, 25, 50, 75, 100):
                svg.append(f'<path d="M {left} {y(p)} H {right}" stroke="#e0e8e3"/>'
                           f'<text x="4" y="{y(p) + 4}">{p}%</text>')
            for t in (20, 30, 40, 50, 60, 70, 80, 90):
                svg.append(f'<text x="{x(t) - 12}" y="{height - 8}">{t}°C</text>')
            if self.mode.get_active_id() == 'fixed':
                duty = self.fixed.get_value()
                points = [(20, duty), (84.99, duty), (85, 100), (90, 100)]
            elif self.points:
                points = [(20, self.points[0][1]), *self.points, (90, 100)]
            else:
                points = []
            path = ' '.join(f'{"M" if i == 0 else "L"} {x(t)} {y(p)}'
                            for i, (t, p) in enumerate(points))
            if points:
                svg.append(f'<path d="{path} L {x(points[-1][0])} {bottom} '
                           f'L {x(points[0][0])} {bottom} Z" fill="#ecf3ef"/>')
                svg.append(f'<path d="{path}" fill="none" stroke="#1e704f" stroke-width="2.5"/>')
            for t, p in points[1:-1]:
                svg.append(f'<circle cx="{x(t)}" cy="{y(p)}" r="3.5" fill="#1e704f"/>')
            if points and self.latest is not None:
                temperature = self.latest['temperature_c']
                t = max(20, min(90, temperature))
                p = (self.fixed.get_value() if self.mode.get_active_id() == 'fixed'
                     else fan.curve_duty(self.points, temperature))
                p = 100 if temperature >= fan.EMERGENCY_C else p
                svg.append(f'<path d="M {x(t)} {bottom} V {y(p)}" stroke="#d17029" '
                           f'stroke-dasharray="4 4"/><circle cx="{x(t)}" cy="{y(p)}" '
                           'r="5" fill="#d17029"/>')
            svg.append('</svg>')
            data = ''.join(svg).encode('utf-8')
            if data == getattr(self, '_graph_svg', None):
                return
            self._graph_svg = data
            loader = GdkPixbuf.PixbufLoader.new_with_type('svg')
            loader.write(data)
            loader.close()
            self.graph.set_from_pixbuf(loader.get_pixbuf())

        def read_sensors(self):
            sensors = None
            while not self.sensor_stop.is_set():
                try:
                    if sensors is None:
                        sensors = fan.Sensors()
                    self.events.put(('sample', sensors.sample()))
                except (fan.FanError, OSError, ValueError) as exc:
                    sensors = None
                    self.events.put(('sensor_error', str(exc)))
                self.sensor_stop.wait(fan.INTERVAL)

        def on_sample(self, sample):
            self.latest = sample
            self.values['temperature'].set_text(f"{sample['temperature_c']:.1f} °C")
            self.values['rpm'].set_text(f"{sample['rpm']:,}")
            self.values['duty'].set_text(f"{sample['duty_percent']:.1f}%")
            self.sensor_note.set_text('Live sensors · measured duty is PWM, not a percentage of maximum RPM.')
            self.update_graph()

        def set_busy(self, busy):
            for widget in self.editable + [self.start_button, self.preview_button,
                                            self.auto_button, self.diagnose_button, self.setup_button]:
                widget.set_sensitive(not busy)
            self.stop_button.set_sensitive(busy and not self.controller.stopping and
                                           self.controller.command != 'setup')
            self.stop_button.set_label('Stop preview' if self.controller.preview else 'Stop & restore')

        def start(self, preview):
            mode = self.mode.get_active_id()
            argv = [mode]
            if mode == 'curve':
                try:
                    fan.parse_curve(self.curve_entry.get_text())
                except argparse.ArgumentTypeError as exc:
                    self.note.set_text(str(exc))
                    return
                argv += ['--points', self.curve_entry.get_text()]
            else:
                argv.append(str(round(self.fixed.get_value())))
            argv.append('--continuous')
            if preview:
                argv.append('--dry-run')
            self.start_command(argv, preview)

        def start_command(self, argv, preview=False):
            try:
                self.controller.start(argv, preview)
            except (OSError, ValueError) as exc:
                self.note.set_text(str(exc))
                return
            self.target = None
            self.values['target'].set_text('—')
            self.set_busy(True)
            self.state.set_text('Starting preview…' if preview else
                                'Starting installed helper…' if self.controller.passwordless else
                                'Awaiting authorization…')
            self.note.set_text('Dry run uses live sensors and makes no fan changes.' if preview else
                               'Starting continuous control. Close hides to the tray; use Quit to exit.'
                               if self.controller.passwordless else
                               'Authorize the fan controller in the system password dialog. Close hides to the tray; use Quit to exit.')
            self.add_log('$ lemur_fan.py ' + ' '.join(argv))

        def setup(self):
            try:
                self.controller.start_setup()
            except (OSError, ValueError) as exc:
                self.note.set_text(str(exc))
                return
            self.set_busy(True)
            self.state.set_text('One-time setup…')
            self.note.set_text('Authorize this installation once in the system password dialog. No fan changes are made during setup.')
            self.expander.set_expanded(True)

        def add_log(self, line):
            self.log_lines.append(line)
            buffer = self.log.get_buffer()
            buffer.set_text('\n'.join(self.log_lines))
            self.log.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 1)

        def stop(self):
            if self.controller.process is None:
                return
            if self.controller.command == 'setup':
                self.note.set_text('Waiting for one-time setup. Complete or cancel the system authorization dialog.')
                return
            self.controller.stop()
            self.stop_button.set_sensitive(False)
            self.state.set_text('Stopping…')
            self.note.set_text('Waiting for the controller to exit and finish any firmware restoration.')

        def on_line(self, line):
            if line == READY:
                if not self.controller.stopping and not self.closing:
                    self.controller.send('start')
                    self.state.set_text('Starting…')
                return
            self.add_log(line)
            match = TARGET.search(line)
            if match:
                self.controller.active = True
                self.target = int(match[1])
                self.values['target'].set_text(f'{self.target}%')
                self.state.set_text('Dry run · no fan changes' if self.controller.preview else 'Continuous control running')
                self.note.set_text('Previewing requested duty with live sensors.' if self.controller.preview else
                                   'Continuous control is active. Close hides to the tray; Stop & restore or Quit returns control to firmware.')
            if line in ('Firmware automatic cooling restored.',
                        'Firmware automatic cooling is already active; no mode change needed.'):
                self.controller.restored = True
            if line.startswith('Error:'):
                self.note.set_text(line)
                self.expander.set_expanded(True)

        def on_exit(self, code):
            preview, command = self.controller.preview, self.controller.command
            restored, stopped = self.controller.restored, self.controller.stopping
            self.controller.finished()
            self.values['target'].set_text('—')
            self.set_busy(False)
            if command == 'setup':
                self.access_note.set_text(installed_access()[1])
                self.state.set_text('Monitoring · setup complete' if code == 0 else 'Monitoring · setup incomplete')
                self.note.set_text('Setup complete. Fan control is ready without a password in your local desktop session.'
                                   if code == 0 else 'Setup was cancelled or failed. Review Controller output, then try One-time setup again.')
                if self.closing:
                    self.finish_close()
                return
            if restored:
                self.state.set_text('Firmware automatic · confirmed')
                self.note.set_text('Firmware automatic cooling was confirmed when the controller exited.')
            elif preview:
                self.state.set_text('Monitoring · preview stopped')
                self.note.set_text('Dry run stopped. No fan changes were made.')
            elif command == 'diagnose' and code == 0:
                self.state.set_text('Monitoring · diagnostics complete')
                self.note.set_text('Read-only diagnostics are shown in Controller output.')
                self.expander.set_expanded(True)
            else:
                self.state.set_text('Stopped · mode unverified')
                self.note.set_text('Operation cancelled or failed. Review Controller output for details.')
            if code not in (0, 130) and not (stopped and code == -signal.SIGTERM):
                self.add_log(f'Operation exited with status {code}.')
                self.expander.set_expanded(True)
                if code in (126, 127):
                    self.note.set_text('Authorization was cancelled or unavailable. Use One-time setup, and run fan control from your active local desktop session.')
                elif preview:
                    self.note.set_text('Dry run failed. Review Controller output for details.')
                if not self.closing:
                    self.reveal()
            if self.closing:
                self.finish_close()

        def poll(self):
            self.controller.tick()
            for _ in range(100):
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'sample':
                    self.on_sample(value)
                elif kind == 'sensor_error':
                    self.latest = None
                    for key in ('temperature', 'rpm', 'duty'):
                        self.values[key].set_text('—')
                    self.sensor_note.set_text(value)
                    self.update_graph()
                elif kind == 'line':
                    self.on_line(value)
                elif kind == 'exit':
                    self.on_exit(value)
            if self.tray is not None:
                self.tray.update(self.state.get_text(),
                                 can_stop=(self.controller.process is not None and
                                           self.controller.command != 'setup' and not self.controller.stopping),
                                 preview=self.controller.preview, quitting=self.closing)
            return not self.closing or self.controller.process is not None

        def finish_close(self):
            self.sensor_stop.set()
            if self.tray is not None:
                self.tray.close()
            self.destroy()
            Gtk.main_quit()

        def capture_test(self):
            # Read-only UI verification for developers; never starts control.
            try:
                path = Path(smoke_test)
                path.parent.mkdir(parents=True, exist_ok=True)
                self.curve_entry.set_text('45:0,85:100')
                assert self.points is None
                self.curve_entry.set_text(fan.DEFAULT_CURVE)
                assert self.points == fan.parse_curve(fan.DEFAULT_CURVE)
                self.mode.set_active_id('fixed')
                assert self.stack.get_visible_child_name() == 'fixed'
                self.mode.set_active_id('curve')
                if not demo:
                    self.smoke_deadline = time.monotonic() + 12
                    self.smoke_stopped = False
                    self.start(True)
                    assert self.controller.process is not None
                    GLib.timeout_add(100, self.check_preview)
                else:
                    GLib.timeout_add(300, self.save_capture, str(path))
            except Exception as exc:
                self.fail_smoke(exc)
            return False

        def check_preview(self):
            try:
                assert time.monotonic() < self.smoke_deadline, 'Dry-run UI test timed out'
                if not self.smoke_stopped:
                    assert self.controller.process is not None, 'Dry-run process exited early'
                    if self.target is not None:
                        assert self.latest is not None
                        assert self.controller.preview
                        assert not self.start_button.get_sensitive()
                        assert self.stop_button.get_sensitive()
                        self.stop()
                        self.smoke_stopped = True
                elif self.controller.process is None:
                    assert self.values['target'].get_text() == '—'
                    assert self.start_button.get_sensitive()
                    assert not self.stop_button.get_sensitive()
                    assert 'preview stopped' in self.state.get_text()
                    GLib.timeout_add(300, self.save_capture, smoke_test)
                    return False
            except Exception as exc:
                self.fail_smoke(exc)
                return False
            return True

        def fail_smoke(self, exc):
            self.smoke_failed = True
            print(f'UI verification failed: {exc}', file=sys.stderr)
            self.request_quit()

        def save_capture(self, path):
            window = self.get_window()
            pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, self.get_allocated_width(),
                                               self.get_allocated_height())
            pixbuf.savev(path, 'png', [], [])
            self.request_quit()
            return False

    window = Window()
    if test_driver is not None:
        def drive_test():
            try:
                test_driver(window)
            except Exception as exc:
                window.fail_smoke(exc)
            return False
        GLib.idle_add(drive_test)

    def quit_signal(*_):
        def close_once():
            window.request_quit()
            return False
        GLib.idle_add(close_once)

    signal.signal(signal.SIGTERM, quit_signal)
    signal.signal(signal.SIGINT, quit_signal)
    try:
        Gtk.main()
    finally:
        window.sensor_stop.set()
        if window.controller.process is not None:
            window.controller.stop()
    return 1 if window.smoke_failed else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--backend']:
        try:
            return run_backend(argv[1:])
        except (OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Preview the interface without hardware access')
    parser.add_argument('--smoke-test', metavar='PNG', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return run_gui(args.demo, args.smoke_test)


if __name__ == '__main__':
    sys.exit(main())
