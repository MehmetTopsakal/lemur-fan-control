"""Installed entry points use Pop!_OS's GTK-enabled system Python.

No files are installed into /usr or /etc by pip itself. Administrator setup
remains a separate, explicitly authorized operation.
"""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


SYSTEM_PYTHON = Path('/usr/bin/python3')


def module_path(name):
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f'The installation is missing {name}. Reinstall lemur-fan-control.')
    return Path(spec.origin).resolve()


def desktop_file(remove=False):
    directory = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'applications'
    destination = directory / 'lemur-fan-control.desktop'
    if remove:
        destination.unlink(missing_ok=True)
        print(f'Removed {destination}')
        return 0
    # Resolve the environment's entry point without resolving the interpreter
    # symlink: a virtual environment's bin/ directory owns its launchers.
    command = Path(sys.executable).absolute().parent / 'lemur-fan-gui'
    if not command.is_file():
        candidate = shutil.which('lemur-fan-gui')
        if candidate is None:
            raise RuntimeError('Install the package with pip before adding a desktop shortcut.')
        command = Path(candidate).absolute()
    # Escape the desktop specification's quoted-string and Exec field syntax.
    executable = str(command).replace('\\', '\\\\').replace('"', '\\"')
    executable = executable.replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    # Backslashes are escaped again by the desktop entry string parser.
    executable = executable.replace('\\', '\\\\')
    directory.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        '[Desktop Entry]\nType=Application\nName=Lemur Fan Control\n'
        'Comment=Monitor and control the System76 Lemur Pro fan\n'
        f'Exec="{executable}"\nIcon=utilities-system-monitor\n'
        'Terminal=false\nCategories=System;Monitor;\nStartupNotify=true\n')
    print(f'Installed {destination}')
    return 0


def system_command(module, argv):
    if not SYSTEM_PYTHON.is_file():
        raise RuntimeError('This application requires Linux with /usr/bin/python3 and GTK 3.')
    return [str(SYSTEM_PYTHON), str(module_path(module)), *argv]


def gui_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv == ['--install-desktop']:
            return desktop_file()
        if argv == ['--remove-desktop']:
            return desktop_file(remove=True)
        command = system_command('lemur_fan_gui', argv)
        # Replacing the launcher preserves signals and the GUI's stop/restore
        # lifecycle, including when launched by a desktop application menu.
        os.execv(command[0], command)
    except (OSError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


def setup_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    import setup_passwordless as setup
    # Validate and handle --help without requesting any privileges.
    setup.parser().parse_args(argv)
    try:
        command = system_command('setup_passwordless', argv)
        if os.geteuid() != 0:
            pkexec = shutil.which('pkexec')
            if pkexec is None:
                raise RuntimeError('Install pkexec, or run this command once with sudo.')
            command.insert(0, pkexec)
        return subprocess.call(command)
    except (OSError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
