"""Close/quit lifecycle checks without a display, privileges, or fan writes."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from lemur_fan_gui import WindowLifecycle
from lemur_fan_control.tray import SystemTray


class FakeWindow(WindowLifecycle):
    def __init__(self, available=True, running=True):
        self.closing = False
        self.tray = SimpleNamespace(available=available)
        self.controller = SimpleNamespace(process=object() if running else None)
        for name in ('hide', 'iconify', 'deiconify', 'show', 'present', 'stop', 'finish_close'):
            setattr(self, name, Mock())
        self.sensor_stop = Mock()
        self.note = Mock()
        self.get_visible = Mock(return_value=False)


class WindowLifecycleTests(unittest.TestCase):
    def test_titlebar_close_hides_without_stopping_control_or_sensors(self):
        window = FakeWindow()
        self.assertTrue(window.close())
        window.hide.assert_called_once()
        window.stop.assert_not_called()
        window.sensor_stop.set.assert_not_called()
        window.finish_close.assert_not_called()
        self.assertFalse(window.closing)

    def test_titlebar_close_keeps_idle_app_in_tray(self):
        window = FakeWindow(running=False)
        window.close()
        window.hide.assert_called_once()
        window.finish_close.assert_not_called()

    def test_missing_tray_minimizes_to_taskbar_without_stopping(self):
        window = FakeWindow(available=False)
        window.close()
        window.iconify.assert_called_once()
        window.hide.assert_not_called()
        window.stop.assert_not_called()
        self.assertIn('taskbar', window.note.set_text.call_args.args[0])

    def test_quit_waits_for_running_controller_cleanup(self):
        window = FakeWindow()
        window.request_quit()
        self.assertTrue(window.closing)
        window.stop.assert_called_once()
        window.sensor_stop.set.assert_called_once()
        window.finish_close.assert_not_called()
        window.request_quit()  # Repeated Quit must not interrupt restoration.
        window.stop.assert_called_once()

    def test_idle_quit_exits_instead_of_hiding(self):
        window = FakeWindow(running=False)
        window.request_quit()
        window.finish_close.assert_called_once()
        window.hide.assert_not_called()

    def test_show_reopens_a_hidden_or_minimized_window(self):
        window = FakeWindow()
        window.reveal()
        window.deiconify.assert_called_once()
        window.show.assert_called_once()
        window.present.assert_called_once()

    def test_tray_disappearing_recovers_hidden_window(self):
        window = FakeWindow()
        window.tray_unavailable()
        window.present.assert_called_once()
        window.stop.assert_not_called()
        self.assertIn('disconnected', window.note.set_text.call_args.args[0])

    def test_tray_disappearing_during_quit_does_not_reopen(self):
        window = FakeWindow()
        window.closing = True
        window.tray_unavailable()
        window.present.assert_not_called()
        window.close()
        window.hide.assert_not_called()


class TrayActionTests(unittest.TestCase):
    def setUp(self):
        self.tray = SystemTray.__new__(SystemTray)
        self.tray.GLib = Mock()
        self.tray.on_show, self.tray.on_stop, self.tray.on_quit = Mock(), Mock(), Mock()
        self.tray.can_stop = True
        self.tray.quitting = self.tray.closed = False

    def click(self, item, event='clicked'):
        result = self.tray._event(item, event)
        if self.tray.GLib.idle_add.called:
            callback = self.tray.GLib.idle_add.call_args.args[0]
            self.assertFalse(callback())
        return result

    def test_tray_quit_uses_cleanup_callback(self):
        self.assertTrue(self.click(4))
        self.tray.on_quit.assert_called_once()
        self.tray.on_stop.assert_not_called()

    def test_tray_show_reveals_window(self):
        self.click(1)
        self.tray.on_show.assert_called_once()

    def test_stop_action_does_not_exit_application(self):
        self.click(2)
        self.tray.on_stop.assert_called_once()
        self.tray.on_quit.assert_not_called()

    def test_disabled_stop_cannot_interrupt_setup_or_idle_monitoring(self):
        self.tray.can_stop = False
        self.click(2)
        self.tray.GLib.idle_add.assert_not_called()

    def test_hover_and_unknown_items_do_not_activate(self):
        self.assertTrue(self.click(4, 'hovered'))
        self.assertFalse(self.click(99))
        self.tray.GLib.idle_add.assert_not_called()

    def test_queued_action_is_ignored_after_shutdown(self):
        self.tray._event(4, 'clicked')
        self.tray.closed = True
        self.tray.GLib.idle_add.call_args.args[0]()
        self.tray.on_quit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
