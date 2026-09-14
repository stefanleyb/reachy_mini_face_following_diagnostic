#!/usr/bin/env python3
"""Launch the stock ``reachy-mini-daemon`` with the face-tracking debug aid applied.

Run this **on the machine that hosts the daemon** (the robot's Raspberry Pi for
a Wireless unit) instead of the normal ``reachy-mini-daemon`` command, for the
duration of the fixed-body baseline only:

    python run_instrumented_daemon.py --wireless-version      # example

Every argument is forwarded verbatim to ``reachy_mini.daemon.app.main:main``.
Nothing under ``site-packages`` is modified; stop using this wrapper to revert.

Environment:
    REACHY_FT_DEBUG_HZ   emitter rate in Hz (default 50, clamped 1..100)

The daemon will additionally broadcast ``face_tracking_debug`` frames on
``/ws/sdk``. Stock clients ignore the unknown message type; the diagnostic
runner consumes it.
"""

import sys


def main() -> None:
    from daemon_ft_debug import apply  # local module, same directory

    apply()

    from reachy_mini.daemon.app.main import main as daemon_main

    daemon_main()


if __name__ == "__main__":
    # Make sibling module importable when run as a script from anywhere.
    import os

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
