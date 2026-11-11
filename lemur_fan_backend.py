"""Restricted CLI bridge shared by the GTK GUI and installed root helper."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

import lemur_fan as fan

HERE = Path(__file__).resolve().parent
READY = '__LEMUR_GUI_READY__'
HEARTBEAT_TIMEOUT = 8.0


def backend_arguments(argv):
    """Validate with the CLI's own rules before launching anything privileged."""
    args = fan.parser().parse_args(argv)
    if args.command not in ('curve', 'fixed', 'auto', 'diagnose'):
        raise ValueError('The GUI backend only accepts curve, fixed, auto, or diagnose.')
    if args.command in ('curve', 'fixed') and not args.continuous:
        raise ValueError('GUI control must use --continuous.')
    return args


def stop_child(child):
    """Signal only the CLI supervisor, leaving its recovery path in charge.

    Never SIGKILL the controller group: that could kill both the supervisor
    and the worker while the EC is still in manual mode.
    """
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
    # Discard output if the GUI's output pipe has already disappeared. Draining
    # the CLI pipe prevents its final recovery messages from blocking cleanup.
    child.communicate()
    return child.returncode


def run_backend(argv, *, cli=None, timeout=HEARTBEAT_TIMEOUT):
    backend_arguments(argv)
    child = None
    stopping = False
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    previous = {sig: signal.signal(sig, interrupt)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    selector = selectors.DefaultSelector()
    try:
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ, 'input')
        # Do not touch the hardware until the live window acknowledges this.
        # This also prevents a delayed authentication dialog from starting a
        # controller after the window has already closed.
        print(READY, flush=True)
        deadline = time.monotonic() + timeout
        pending = b''
        while not stopping:
            if interrupted or time.monotonic() >= deadline:
                print('GUI connection ended; stopping the controller.', flush=True)
                break
            if child is not None and child.poll() is not None:
                # Drain trailing restoration/error messages before exit.
                remainder = child.stdout.read()
                if remainder:
                    sys.stdout.buffer.write(remainder)
                    sys.stdout.buffer.flush()
                return child.returncode if child.returncode >= 0 else 128 - child.returncode
            for key, _ in selector.select(timeout=0.2):
                data = os.read(key.fd, 4096)
                if key.data == 'output':
                    if data:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    else:
                        selector.unregister(key.fd)
                    continue
                if not data:
                    stopping = True
                    break
                pending += data
                if len(pending) > 4096:
                    stopping = True
                    break
                while b'\n' in pending:
                    command, pending = pending.split(b'\n', 1)
                    if command == b'ping':
                        deadline = time.monotonic() + timeout
                    elif command == b'start' and child is None:
                        child = subprocess.Popen(
                            [sys.executable, '-I', '-B', '-u', str(cli or HERE / 'lemur_fan.py'), *argv],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
                        selector.register(child.stdout, selectors.EVENT_READ, 'output')
                        deadline = time.monotonic() + timeout
                    else:
                        stopping = True
                        break
                if stopping:
                    break
        if child is not None:
            # Continue draining output while the CLI performs its cleanup;
            # a full output pipe must never block firmware restoration.
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            for line in child.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            code = child.wait()
            # The CLI's read-only dry-run path has no SIGTERM handler. Its
            # deliberate stop is still a normal user cancellation.
            return 130 if code == -signal.SIGTERM else code
        return 0
    finally:
        selector.close()
        try:
            if child is not None and child.poll() is None:
                stop_child(child)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


