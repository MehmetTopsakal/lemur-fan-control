"""Opt-in desktop integration test with live sensors and read-only dry run.

LEMUR_FAN_DESKTOP_TESTS=1 /usr/bin/python3 -m unittest test_tray_desktop -v
"""

import os
import time
import unittest


@unittest.skipUnless(os.environ.get('LEMUR_FAN_DESKTOP_TESTS') == '1', 'requires a desktop tray')
class TrayDesktopTests(unittest.TestCase):
    def test_close_keeps_dry_run_alive_then_tray_show_and_quit_work(self):
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gio, GLib
        import lemur_fan_gui as gui
        from lemur_fan_control.tray import ITEM, ITEM_PATH, MENU, MENU_PATH

        phase = 'waiting for tray'
        deadline = time.monotonic() + 30
        hidden_at = heartbeat_at = None
        window = None
        failures = []
        menu_checked = False

        def fail(error):
            failures.append(str(error))
            window.request_quit()

        def call(path, interface, method, args, callback):
            def finished(bus, result):
                try:
                    callback(bus.call_finish(result))
                except Exception as error:
                    fail(error)
            window.tray.bus.call(window.tray.bus.get_unique_name(), path, interface,
                                 method, args, None, Gio.DBusCallFlags.NONE,
                                 2000, None, finished)

        def layout_received(reply):
            nonlocal menu_checked
            _, layout = reply.unpack()
            children = {child[0]: child[1] for child in layout[2]}
            self.assertEqual(children[1]['label'], 'Show window')
            self.assertEqual(children[4]['label'], 'Quit & restore')
            self.assertTrue(children[2]['enabled'])
            menu_checked = True

        def drive(created):
            nonlocal window
            window = created
            GLib.timeout_add(100, advance)

        def advance():
            nonlocal phase, hidden_at, heartbeat_at
            try:
                self.assertLess(time.monotonic(), deadline, f'Timed out {phase}')
                if phase == 'waiting for tray':
                    self.assertIsNotNone(window.tray)
                    if window.tray.available:
                        window.start(True)
                        phase = 'waiting for dry-run output'
                elif phase == 'waiting for dry-run output':
                    self.assertIsNotNone(window.controller.process)
                    if window.target is not None:
                        # Emit the actual titlebar delete signal. The handler
                        # must prevent GTK from destroying the window.
                        self.assertTrue(window.emit('delete-event', None))
                        self.assertFalse(window.get_visible())
                        self.assertFalse(window.closing)
                        self.assertFalse(window.sensor_stop.is_set())
                        hidden_at = time.monotonic()
                        heartbeat_at = window.controller.last_ping
                        phase = 'checking hidden controller'
                        # The poll callback updates tray state after processing
                        # the CLI output, so wait one UI tick before querying.
                        GLib.timeout_add(200, query_menu)
                elif phase == 'checking hidden controller':
                    self.assertFalse(window.get_visible())
                    self.assertIsNotNone(window.controller.process)
                    self.assertIsNone(window.controller.process.poll())
                    if time.monotonic() - hidden_at > 9 and menu_checked:
                        self.assertGreater(window.controller.last_ping, heartbeat_at + 8)
                        phase = 'waiting for tray activation'
                        call(ITEM_PATH, ITEM, 'Activate', GLib.Variant('(ii)', (0, 0)), lambda _: None)
                elif phase == 'waiting for tray activation':
                    if window.get_visible():
                        self.assertFalse(window.closing)
                        window.emit('delete-event', None)
                        self.assertFalse(window.get_visible())
                        phase = 'waiting for tray quit and controller cleanup'
                        call(MENU_PATH, MENU, 'Event',
                             GLib.Variant('(isvu)', (4, 'clicked', GLib.Variant('i', 0), 0)),
                             lambda _: None)
                return True
            except Exception as error:
                fail(error)
                return False

        def query_menu():
            call(MENU_PATH, MENU, 'GetLayout', GLib.Variant('(iias)', (0, -1, [])), layout_received)
            return False

        self.assertEqual(gui.run_gui(test_driver=drive), 0)
        self.assertEqual(failures, [])
        self.assertEqual(phase, 'waiting for tray quit and controller cleanup')
        self.assertTrue(menu_checked)
        self.assertTrue(window.closing)
        self.assertIsNone(window.controller.process)
        self.assertTrue(window.sensor_stop.is_set())
        self.assertIn('preview stopped', window.state.get_text())


if __name__ == '__main__':
    unittest.main()
