#!/usr/bin/env python3
"""Run-lock probe for the AnomalyGuessr generator's flock (ticket #2235).

pipeline/ag_generate.py holds a BSD flock on data/anomalyguessr/generate.lock
for a whole run, and api/src/generate.ts probes it to report whether a run is
live. util-linux's `flock` binary does not exist on macOS, so the API probes
through this helper instead: fcntl.flock() is the same flock(2) call the
generator takes, on Linux and macOS alike.

Modes:
  flockProbe.py probe <lock>          exit 0 when free, 3 when held
  flockProbe.py hold <lock> [secs]    take the lock, sleep (default 30s)
                                      retrying for up to 5s first, so a probe
                                      racing this process cannot make it give up

The kernel drops the lock when the process exits (SIGKILL included), so a
killed holder releases it.
"""

import fcntl
import os
import sys
import time

# Distinct from the interpreter's own 1 (uncaught exception) and 2 (usage), so
# a broken probe is never mistaken for a held lock.
HELD = 3


def acquire(path, wait_s):
    """Open the lock file and take the flock; (fd, None) or (None, exit code)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, None
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None, HELD
            time.sleep(0.005)


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    mode, path = argv[1], argv[2]
    secs = float(argv[3]) if len(argv) > 3 else 30.0
    wait_s = float(argv[4]) if len(argv) > 4 else 5.0
    if mode == "probe":
        # A probe must not wait: "not free right now" is the answer.
        fd, code = acquire(path, 0.0)
        return code if code is not None else 0
    if mode == "hold":
        fd, code = acquire(path, wait_s)
        if code is not None:
            return code
        time.sleep(secs)
        return 0
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
