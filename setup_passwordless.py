#!/usr/bin/python3
"""One-time administrator setup for password-free local fan control.

Install: sudo /usr/bin/python3 setup_passwordless.py install
Remove:  sudo /usr/bin/python3 setup_passwordless.py uninstall
No fan commands are issued during installation or removal.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent
LIB = Path('/usr/local/lib/lemur-fan-control')
HELPER = Path('/usr/local/libexec/lemur-fan-control-helper')
ACTION = 'io.github.lemur-fan-control.manage'
POLICY = Path('/usr/share/polkit-1/actions/io.github.lemur-fan-control.policy')
RULE = Path('/etc/polkit-1/rules.d/49-lemur-fan-control.rules')
MODULES = ('lemur_fan.py', 'lemur_fan_backend.py', 'lemur_fan_privileged.py')


def account(username=None):
    if username is None:
        username = os.environ.get('SUDO_USER')
        if not username and os.environ.get('PKEXEC_UID'):
            username = pwd.getpwuid(int(os.environ['PKEXEC_UID'])).pw_name
    if not username:
        raise ValueError('Specify --user USER when running directly as root.')
    user = pwd.getpwnam(username)
    if user.pw_uid < 1000 or user.pw_name == 'nobody':
        raise ValueError('Choose a regular desktop account, not root or a system account.')
    return user


def trusted(path, *, directory=False):
    """Refuse links and writable/non-root destinations, including every parent."""
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'Refusing unprotected installation path: {item}')
        if item != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f'Not a directory: {item}')
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Not a regular file: {item}')


def ensure_directory(path):
    if path.exists() or path.is_symlink():
        trusted(path, directory=True)
    else:
        ensure_directory(path.parent)
        path.mkdir(mode=0o755)
        os.chown(path, 0, 0)
        trusted(path, directory=True)


def write_file(path, data, mode=0o644):
    ensure_directory(path.parent)
    if path.exists() or path.is_symlink():
        trusted(path)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.lemur-', delete=False) as output:
            name = output.name
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), 0, 0)
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def policy_text():
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD polkit Policy Configuration 1.0//EN"
  "http://www.freedesktop.org/software/polkit/policyconfig-1.dtd">
<policyconfig>
  <action id="{ACTION}">
    <description>Control the Lemur Pro cooling fan</description>
    <message>Fan control requires local desktop authorization</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>no</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{HELPER}</annotate>
  </action>
</policyconfig>
'''


def rule_text(username):
    # JSON quoting makes the username a single JavaScript string literal.
    return f'''// Local administrator configuration for Lemur Fan Control.
polkit.addRule(function(action, subject) {{
    if (action.id === "{ACTION}") {{
        if (subject.user === {json.dumps(username)} && subject.local === true &&
            subject.active === true &&
            action.lookup("program") === "{HELPER}" &&
            action.lookup("user") === "root") {{
            return polkit.Result.YES;
        }}
        return polkit.Result.NO;
    }}
}});
'''


def install(username=None):
    user = account(username)
    # Snapshot all source bytes before touching the existing installation.
    payload = {name: (SOURCE / name).read_bytes() for name in MODULES}
    for name, data in payload.items():
        compile(data, str(SOURCE / name), 'exec')
    for directory in (LIB, HELPER.parent, POLICY.parent, RULE.parent):
        ensure_directory(directory)
    # Fail before changes if a prior installation has unsafe permissions.
    for path in (*[LIB / name for name in MODULES], LIB / 'installed.json', HELPER, POLICY, RULE):
        if path.exists() or path.is_symlink():
            trusted(path)
    wrapper = ('#!/bin/sh\n'
               f'exec /usr/bin/python3 -I -B "{LIB / "lemur_fan_privileged.py"}" "$@"\n')
    manifest = {'user': user.pw_name, 'uid': user.pw_uid, 'protocol': 1,
                'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    # Revoke an old grant during updates. Put the grant back only after every
    # protected code file and its matching policy have been installed.
    if RULE.exists():
        RULE.unlink()
    for name, data in payload.items():
        write_file(LIB / name, data)
    write_file(LIB / 'installed.json', (json.dumps(manifest, indent=2) + '\n').encode())
    write_file(HELPER, wrapper.encode(), mode=0o755)
    write_file(POLICY, policy_text().encode())
    write_file(RULE, rule_text(user.pw_name).encode())
    print(f'Password-free fan control installed for {user.pw_name} in active local desktop sessions.')
    print('Restart the GUI normally. No fan settings or firmware modes were changed.')


def uninstall():
    paths = (RULE, POLICY, HELPER, LIB / 'installed.json', *[LIB / name for name in MODULES])
    for path in paths:
        if path.exists() or path.is_symlink():
            trusted(path)
    for path in paths:
        if path.exists():
            path.unlink()
    if LIB.exists():
        try:
            LIB.rmdir()
        except OSError:
            print(f'Left nonempty directory {LIB}; only known installation files were removed.')
    print('Password-free fan control removed. The GUI will request authorization again.')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('operation', choices=('install', 'uninstall'))
    result.add_argument('--user', help='Desktop account; defaults to SUDO_USER or PKEXEC_UID')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print('Run this one-time setup with sudo, or use the GUI setup button.', file=sys.stderr)
        return 1
    try:
        if args.operation == 'install':
            install(args.user)
        else:
            uninstall()
    except (OSError, ValueError, KeyError, SyntaxError) as exc:
        print(f'Setup failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
