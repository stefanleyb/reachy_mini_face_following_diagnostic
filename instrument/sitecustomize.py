"""Auto-loaded at Python startup when this directory is on PYTHONPATH.

Used by ``deploy_to_robot.sh``: it runs the robot's *normal* daemon launcher
but with ``PYTHONPATH=~/ft_debug`` and ``REACHY_FT_DEBUG=1``, so the daemon
does everything it usually does and this hook applies the face-tracking debug
instrumentation at interpreter start -- no need to reconstruct the launch
command.

CPython imports ``sitecustomize`` automatically if it is found on ``sys.path``.
Guarded by an env var so a stray PYTHONPATH never changes daemon behaviour.
"""

import os
import sys

if os.environ.get("REACHY_FT_DEBUG") == "1":
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        import daemon_ft_debug

        daemon_ft_debug.apply()
        print("[ft_debug] instrumentation applied via sitecustomize", file=sys.stderr)
    except Exception as exc:  # never break the daemon over the debug aid
        print(f"[ft_debug] sitecustomize apply failed: {exc}", file=sys.stderr)

if os.environ.get("REACHY_CAMERA_PTS_PROBE") == "1":
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        import daemon_camera_pts_probe

        daemon_camera_pts_probe.apply()
        print("[camera_pts_probe] applied via sitecustomize", file=sys.stderr)
    except Exception as exc:  # never break the daemon over the passive probe
        print(f"[camera_pts_probe] apply failed: {exc}", file=sys.stderr)
