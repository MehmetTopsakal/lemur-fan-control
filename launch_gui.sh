#!/bin/sh
# Resolve the folder even when launched from a desktop shortcut.
set -eu
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
exec /usr/bin/python3 ./lemur_fan_gui.py "$@"
