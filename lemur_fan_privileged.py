"""Entry point installed in a root-owned directory, invoked by an isolated Python.

No user paths, environment configuration, arbitrary commands, or CLI file
overrides are accepted. The supported fan operations are validated by the
same parser as the controller.
"""

import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    if os.geteuid() != 0:
        print('This helper must be launched through pkexec.', file=sys.stderr)
        return 1
    directory = Path(__file__).resolve().parent
    # -I excludes user site packages, PYTHONPATH, and the calling directory.
    # Add only the root-owned installation directory to load our own modules.
    sys.path.insert(0, str(directory))
    if sys.argv[1:] == ['--check']:
        print(json.dumps({'uid': os.geteuid(), 'helper': str(directory),
                          'controller_sha256': hashlib.sha256(
                              (directory / 'lemur_fan.py').read_bytes()).hexdigest()}))
        return 0
    from lemur_fan_backend import run_backend
    try:
        return run_backend(sys.argv[1:])
    except (OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
