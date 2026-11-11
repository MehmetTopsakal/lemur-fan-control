# Lemur Fan Control

A GTK 3 desktop app and supervised command-line fan controller for **Linux on
the System76 Lemur Pro lemp13**. It provides live temperature/RPM/PWM readings,
an editable continuous temperature curve, fixed duty, dry-run previews, and
automatic-cooling restoration on stop or window closure.

## Install with Pixi

Install [Pixi](https://pixi.prefix.dev/latest/installation/) and clone or
download this repository. Install the Pop!_OS / Ubuntu desktop prerequisites:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 librsvg2-common pkexec
```

Create a Pixi workspace alongside the repository checkout, or use an existing
workspace with Python 3.10 or newer. This example uses the following layout:

```text
projects/
├── fan-workspace/
│   └── pixi.toml
└── lemur-fan-control/
    └── pyproject.toml
```

For a new workspace, save this as `fan-workspace/pixi.toml`:

```toml
[workspace]
name = "fan-control"
channels = ["conda-forge"]
platforms = ["linux-64"]

[dependencies]
python = ">=3.10"

[target.linux-64.pypi-dependencies]
lemur-fan-control = { path = "../lemur-fan-control", editable = true }
```

For an existing workspace, add the package entry to its
`[target.linux-64.pypi-dependencies]` table, creating that table if needed.
Adjust `path` to the repository checkout, relative to `pixi.toml`. Editable
installation loads the source from that folder; keep it in place while using
the application.

Run from the workspace directory containing `pixi.toml`:

```bash
pixi install
pixi run lemur-fan-gui
```

The package has no additional Python dependencies. The GUI uses
`/usr/bin/python3` to access the system's GTK bindings; other commands use the
Pixi environment's Python. Installation does not change system permissions,
start the fan, or install a boot service.

To add an application-menu shortcut:

```bash
pixi run lemur-fan-gui --install-desktop
```

To remove that shortcut:

```bash
pixi run lemur-fan-gui --remove-desktop
```

Keep the Pixi environment and source folder at their installed paths while
using the shortcut. Run the remaining usage commands from the Pixi workspace
directory.

## One-time administrator setup

Open the GUI and click **One-time setup**, or run:

```bash
pixi run lemur-fan-setup install
```

Authorize the system password dialog once. After setup succeeds, the GUI
controls the fan without password prompts for that account's active local
desktop session. The helper is root-owned and validates only supported fan
commands; Python and arbitrary shell commands receive no password-free grant.

Before this setup, monitoring and dry run work without root; actual control
requests administrator authorization. Stop a controller you already have
running before starting a new one. Closing a controlling GUI waits for its
existing CLI supervisor to finish restoring firmware automatic cooling.

To remove password-free access, stop fan control and run:

```bash
pixi run lemur-fan-setup uninstall
```

The protected helper uses a snapshot of the installed controller modules.
After a package upgrade or controller-code change, rerun one-time setup; the
GUI detects stale helper code. Always stop control before an update or removal.

## Commands

From the Pixi workspace directory:

```bash
pixi run lemur-fan-gui                       # live GUI
pixi run lemur-fan-gui --demo                # preview without hardware access
pixi run python -m lemur_fan_control         # same GUI
pixi run lemur-fan status --json             # read-only sensor readings
pixi run lemur-fan curve --continuous --dry-run
pixi run lemur-fan-setup install             # one administrator authorization
pixi run lemur-fan-setup uninstall           # revoke password-free access
```

Direct CLI control requires root. Use the GUI after one-time setup for
password-free fan control.

## Default fan curve and operating limits

The default curve is `45:20,65:30,75:50,80:85,85:100`, sampled every two
seconds. The requested minimum is 20% PWM, not a percentage of maximum RPM or
a guarantee the motor spins. Duties interpolate between points; increases
apply immediately and decreases are delayed and smoothed. Requested duty and
measured duty are shown separately. Both fixed and curve control request full
duty at 85 °C, holding the thermal override until below 80 °C.

The controller checks the DMI model, EC protocol, board, and exact firmware
build before fan writes. Audited EC build strings are `2025-07-24_c242738`,
`2025-07-02_a832be5`, and `2026-09-23_8faf1c2`. Other hardware or firmware is
refused. Raw hardware access requires `/dev/port`; sensor monitoring and dry
run do not open it. Missing or invalid sensors abort control. A second
controller cannot take over the EC lock. Zero RPM during manual control and
detected suspend/time gaps also stop the controller and attempt restoration.

The worker/supervisor and GUI-heartbeat protections attempt to
restore automatic cooling after failures. They cannot guarantee recovery if
both processes are killed, the kernel/EC fails, or the fan is physically stuck.
Restart continuous control after reboot or suspend. This app has been tested
with simulated ECs and live read-only sensors/dry runs; these checks do not
establish physical fan-write compatibility.

## Build and test

Add these build tools to the workspace's existing `[dependencies]` table:

```toml
python-build = "*"
twine = "*"
```

Run from the Pixi workspace directory. The paths below follow the example
layout above; adjust them if the checkout is elsewhere.

```bash
pixi install
pixi run python -m build ../lemur-fan-control
pixi run python -m twine check --strict ../lemur-fan-control/dist/*
pixi run python -m unittest discover -s ../lemur-fan-control -p 'test_*.py' -v
```

The source distribution includes the project tests. The wheel
contains the five controller/setup modules and the `lemur_fan_control` package.
No files are written to `/etc` or `/usr` until the explicit administrator setup.
Wheel and source archives are locally installable; this version has not been
published to PyPI.

Packaging follows the [Python Packaging User Guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/).
GTK uses the [distribution-provided PyGObject](https://pygobject.gnome.org/getting_started.html#ubuntu-debian)
installation method; this application uses GTK 3.
