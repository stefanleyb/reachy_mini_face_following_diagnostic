"""In-process fake daemon client for ``--dry-run`` rehearsal and tests.

Synthesises a plausible, gently oscillating head-tracking trace so the operator
can rehearse the whole six-trial flow (countdowns, cues, prompts, file output)
with no robot and no daemon. It is NOT a simulator: the numbers are cosmetic.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Optional

import numpy as np


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 1] = c, -s
    m[1, 0], m[1, 1] = s, c
    return m


class FakeClient:
    def __init__(self, oscillate: bool = True, instrumented: bool = True) -> None:
        self._oscillate = oscillate
        self._instrumented = instrumented
        self._t0 = time.monotonic()
        self._tracking = False
        self._motor_mode = "enabled"
        self._lock = threading.Lock()
        self._seq = 0
        self._poll_ok = 0
        self._poll_fail = 0

    # lifecycle -------------------------------------------------------
    def connect(self) -> None:  # noqa: D401
        pass

    def wait_ready(self, timeout: float = 10.0) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    # streamed state ------------------------------------------------
    def _head_yaw(self, now: float) -> float:
        if not self._tracking or not self._oscillate:
            return 0.0
        el = now - self._t0
        # decaying wobble around a small steady offset
        return 0.12 * math.exp(-el / 18.0) * math.sin(2 * math.pi * 1.1 * el) + 0.03

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        yaw = self._head_yaw(now)
        head_pose = _rot_z(yaw)
        self._seq += 1
        ftd: Optional[dict[str, Any]] = None
        if self._instrumented:
            raw = [0.05 * math.sin(9.0 * (now - self._t0)) + 0.02, 0.01]
            ftd = {
                "type": "face_tracking_debug",
                "seq": self._seq,
                "t_mono": now,
                "t_wall": time.time(),
                "tracking_enabled": self._tracking,
                "requested_weight": 1.0,
                "weight": 1.0 if self._tracking else 0.0,
                "alpha": 0.15,
                "lost_timeout": 2.0,
                "last_face_seen_mono": now if self._tracking else None,
                "since_face_seen": 0.0 if self._tracking else None,
                "raw_center": raw if self._tracking else None,
                "raw_obs_ts_mono": now - 0.08,
                "raw_roll": 0.0,
                "filtered_center": [raw[0] * 0.6, raw[1] * 0.6] if self._tracking else None,
                "filtered_detected": self._tracking,
                "filtered_roll": 0.0,
                "filtered_obs_ts_mono": now - 0.08,
                "current_head_pose": head_pose.tolist(),
                "tracking_target_pose": _rot_z(yaw * 1.4).tolist() if self._tracking else None,
                "tracking_aim_pose": _rot_z(yaw).tolist() if self._tracking else None,
            }
        return {
            "t_mono": now,
            "t_wall": time.time(),
            "head_pose": head_pose.tolist(),
            "head_pose_age": 0.005,
            "joint_positions": {"head": [yaw, 0, 0, 0, 0, 0, 0], "antennas": [0.0, 0.0]},
            "joint_positions_age": 0.005,
            "daemon_status": {
                "type": "daemon_status",
                "version": "1.10.0",
                "robot_name": "reachy_mini",
                "camera_specs_name": "ReachyMiniWideCam",
                "backend_status": {
                    "motor_control_mode": self._motor_mode,
                    "control_loop_stats": {
                        "mean_control_loop_frequency": 49.7,
                        "max_control_loop_interval": 0.031,
                    },
                },
                "face_target": (
                    {"detected": True, "x": 0.03, "y": 0.01, "roll": 0.0, "ts": time.monotonic()}
                    if self._tracking
                    else {"detected": False}
                ),
            },
            "daemon_status_age": 0.4,
            "ft_debug": ftd,
            "ft_debug_age": 0.02 if ftd else None,
        }

    def has_ft_debug(self) -> bool:
        return self._instrumented

    def wait_ft_debug(self, timeout: float) -> bool:
        return self._instrumented

    def start_status_polling(self) -> None:
        self._poll_ok = 60
        self._poll_fail = 0

    def stop_status_polling(self) -> tuple:
        return self._poll_ok, self._poll_fail

    # commands -----------------------------------------------------
    def set_head_tracking(self, enabled: bool, weight: float = 1.0) -> None:
        with self._lock:
            self._tracking = enabled
            if enabled:
                self._t0 = time.monotonic()

    def set_torque(self, on: bool, ids: Optional[list[str]] = None) -> None:
        self._motor_mode = "enabled" if on else "disabled"

    def set_motor_mode_cmd(self, mode: str) -> None:
        self._motor_mode = mode

    def wake_up_motion(self) -> None:
        pass

    def goto_sleep_motion(self) -> None:
        pass

    def center_head(self, duration: float = 1.5) -> None:
        time.sleep(0.02)

    def head_pose_offset_from_neutral(self) -> float:
        return 0.02

    def set_full_target(self, head=None, antennas=None, body_yaw=None) -> None:  # noqa: ANN001
        pass

    def goto(self, *a: object, **k: object) -> None:
        time.sleep(0.05)

    # REST --------------------------------------------------------
    def motor_status(self) -> str:
        return self._motor_mode

    def set_motor_mode(self, mode: str) -> None:
        self._motor_mode = mode

    def camera_specs(self) -> dict:
        return {"name": "ReachyMiniWideCam(fake)", "K": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "D": [0, 0, 0, 0, 0]}

    def daemon_status_rest(self) -> dict:
        return self.snapshot()["daemon_status"]

    def present_body_yaw(self) -> float:
        return 0.0
