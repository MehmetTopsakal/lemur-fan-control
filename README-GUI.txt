Lemur Fan Control - GUI guide
============================

Install the pip package using the instructions in README.md, then activate
the environment containing the application.

Launch:

    lemur-fan-gui

Add the application-menu shortcut:

    lemur-fan-gui --install-desktop

One-time administrator setup
----------------------------

Click One-time setup and authorize the system password dialog once. The
installer creates a protected fan-control helper and grants your account
access in active local desktop sessions. Subsequent fan-control operations
use that helper without password prompts.

The terminal equivalent is:

    lemur-fan-setup install

Using the window
----------------

* Temperature, fan RPM, and measured PWM duty update from live Linux sensors.
* Temperature curve follows the profile shown in the graph. Edit its
  temperature:duty pairs before starting; Reset restores the package default.
  Profile edits apply to this window only.
* Start curve runs continuously until stopped. Fixed duty requests a chosen
  PWM percentage, subject to the temperature override.
* Dry run previews requested targets using live sensors without fan changes.
* Requested duty comes from the running controller. Measured duty is the
  fan's reported PWM signal. Neither value is a percentage of maximum RPM.
* The graph's orange dot marks the instantaneous profile target at the
  measured temperature, before the controller's delayed speed reductions.
* The window's close button hides it to the system tray. Fan control and
  sensor monitoring continue. Click the tray icon or choose Show window in
  its menu to reopen the GUI.
* Stop & restore ends control and waits for automatic-cooling restoration.
  The application remains open. Choose Quit & restore from the tray menu or
  use the GUI's Quit button to restore automatic cooling and exit.
* Firmware automatic explicitly returns control to firmware when no GUI
  operation is running. Diagnostics reads the EC identity and supported mode.
  Results and errors appear in Controller output.

The system tray uses the desktop's StatusNotifierItem service through Gio;
no additional AppIndicator bindings are required. If no tray host is
available, Close minimizes to the taskbar. A disconnected tray or controller
error reopens the window so it remains accessible.

Default profile
---------------

    Temperature    Requested PWM duty
    <= 45 C        20%
       65 C        30%
       75 C        50%
       80 C        85%
    >= 85 C        100%

Targets interpolate between these points. Sampling occurs every two seconds.
Both control modes request full duty at 85 C and hold that override until
below 80 C. Speed increases apply immediately; decreases are delayed and
smoothed. A PWM request does not guarantee physical fan rotation.

Stop any running controller before starting another. The EC lock prevents
concurrent control. Monitoring and dry run can coexist with a controller.
Sensor failures, a stalled fan, and detected suspend/time gaps abort control
and attempt restoration. Restart continuous control after reboot or suspend.

Updates and removal
-------------------

Stop control before upgrading the package. After an upgrade, rerun One-time
setup if the GUI reports that its protected helper needs updating.

Remove password-free access:

    lemur-fan-setup uninstall

Remove the application-menu shortcut:

    lemur-fan-gui --remove-desktop

The complete installation, hardware requirements, operating limits, and build
instructions are in README.md.
