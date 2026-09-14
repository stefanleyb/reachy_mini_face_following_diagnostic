"""Low-level daemon client for the diagnostic runner.

Deliberately does **not** use ``reachy_mini.ReachyMini``: that class builds a
``MediaManager`` on construction, and for a network (Wireless) connection that
means opening a WebRTC camera stream -- exactly the extra processing/network
load the baseline protocol says to avoid, and passing ``media_backend="no_media"``
would instead tell the daemon to *release* the camera and break its own face
tracker.

Instead this talks straight to the daemon:

* WebSocket ``/ws/sdk``  -- 50 Hz ``head_pose`` / ``joint_positions``, 1 Hz
  ``daemon_status``, ``task_progress``, and (when the daemon is launched through
  ``run_instrumented_daemon.py``) 50 Hz ``face_tracking_debug``. Also the
  command channel for head-tracking enable/disable and goto tasks.
* REST ``/api/...``   -- motor mode get/set and camera-calibration identity.

Only the typed command *models* are imported from ``reachy_mini`` (pure pydantic,
no side effects).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import numpy as np
import requests
import websockets.sync.client as ws_sync

from reachy_mini.io.protocol import (
    GotoSleepCmd,
    GotoTaskRequest,
    SetFullTargetCmd,
    SetHeadTrackingCmd,
    SetMotorModeCmd,
    SetTorqueCmd,
    TaskRequest,
    WakeUpCmd,
)
from reachy_mini.utils.interpolation import InterpolationTechnique

FT_DEBUG_MSG_TYPE = "face_tracking_debug"

# The daemon's own definition of the awake-neutral pose (reachy_mini.reachy_mini).
# Every centring goto in the runner targets exactly this so "neutral" is
# identical at every step and matches a fresh daemon wake_up.
INIT_HEAD_POSE = np.eye(4)
INIT_ANTENNAS_JOINT_POSITIONS = [-0.1745, 0.1745]  # ~10 deg rest offset


@dataclass
class Cached:
    """Latest value of a streamed quantity plus when we received it."""

    value: Any = None
    recv_mono: float = 0.0
    recv_wall: float = 0.0

    def age(self, now_mono: Optional[float] = None) -> Optional[float]:
        if self.value is None:
            return None
        return (now_mono if now_mono is not None else time.monotonic()) - self.recv_mono


@dataclass
class _Task:
    event: threading.Event = field(default_factory=threading.Event)
    error: Optional[str] = None


class DiagnosticClient:
    """Thread-safe cache of daemon state + a minimal command surface."""

    def __init__(self, host: str, port: int = 8000, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

        # Resolve a ``.local`` name to an IP once: macOS re-runs mDNS on every
        # fresh HTTP connection (~1 s each), which throttles REST polling to
        # ~1 Hz. A pinned IP + keep-alive Session brings it to ~15 ms.
        rest_host = host
        try:
            rest_host = socket.gethostbyname(host)
        except OSError:
            pass
        self._rest_host = rest_host
        self._http = f"http://{rest_host}:{port}"
        self._session = requests.Session()

        self._ws: Optional[ws_sync.ClientConnection] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.head_pose = Cached()
        self.joint_positions = Cached()
        self.daemon_status = Cached()
        self.ft_debug = Cached()

        self._first_head_pose = threading.Event()
        self._first_status = threading.Event()
        self._ft_debug_seen = threading.Event()

        # Fast REST poller of /api/daemon/status. A REST GET recomputes
        # face_target live, so polling it ~20 Hz recovers the ~8.5 Hz detector
        # stream that the 1 Hz daemon_status push cannot show -- the runner's
        # main fallback when the daemon is not instrumented.
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_hz = 25.0
        self._poll_active = threading.Event()
        self._poll_ok = 0
        self._poll_fail = 0

        self._tasks: dict[UUID, _Task] = {}

        # Optional raw-message sink (used by tests / verbose logging).
        self.on_message: Optional[Callable[[dict], None]] = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        uri = f"ws://{self.host}:{self.port}/ws/sdk"
        try:
            self._ws = ws_sync.connect(uri, compression=None, open_timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise ConnectionError(f"cannot connect to {uri}: {e}") from e
        self._stop.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def wait_ready(self, timeout: float = 10.0) -> None:
        if not self._first_head_pose.wait(timeout):
            raise TimeoutError("no head_pose from daemon within %.0fs" % timeout)

    def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def __enter__(self) -> "DiagnosticClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- receive loop ----------------------------------------------------

    def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            for raw in self._ws:
                if self._stop.is_set():
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        except Exception:  # noqa: BLE001 - connection closed / teardown
            pass

    def _poll_loop(self) -> None:
        """Poll /api/daemon/status while a tracking window is active."""
        url = f"{self._http}/api/daemon/status"
        while not self._stop.is_set():
            if not self._poll_active.wait(0.25):
                continue
            t0 = time.monotonic()
            try:
                r = self._session.get(url, timeout=2.0)
                r.raise_for_status()
                doc = r.json()
                now_m, now_w = time.monotonic(), time.time()
                doc["_poll"] = True
                with self._lock:
                    self.daemon_status = Cached(doc, now_m, now_w)
                self._first_status.set()
                self._poll_ok += 1
            except Exception:  # noqa: BLE001 - flaky wifi; keep going
                self._poll_fail += 1
            dt = time.monotonic() - t0
            rest = (1.0 / self._poll_hz) - dt
            if rest > 0:
                self._stop.wait(rest)

    def start_status_polling(self) -> None:
        self._poll_ok = self._poll_fail = 0
        self._poll_active.set()

    def stop_status_polling(self) -> tuple[int, int]:
        self._poll_active.clear()
        return self._poll_ok, self._poll_fail

    def ingest(self, msg: dict) -> None:
        """Feed one already-parsed message (used by tests)."""
        self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        now_m, now_w = time.monotonic(), time.time()
        mtype = msg.get("type")
        if self.on_message is not None:
            try:
                self.on_message(msg)
            except Exception:  # noqa: BLE001
                pass

        if mtype == "head_pose":
            with self._lock:
                self.head_pose = Cached(np.array(msg["head_pose"]), now_m, now_w)
            self._first_head_pose.set()
        elif mtype == "joint_positions":
            with self._lock:
                self.joint_positions = Cached(
                    {
                        "head": list(msg.get("head_joint_positions", [])),
                        "antennas": list(msg.get("antennas_joint_positions", [])),
                    },
                    now_m,
                    now_w,
                )
        elif mtype == "daemon_status":
            with self._lock:
                self.daemon_status = Cached(msg, now_m, now_w)
            self._first_status.set()
        elif mtype == FT_DEBUG_MSG_TYPE:
            with self._lock:
                self.ft_debug = Cached(msg, now_m, now_w)
            self._ft_debug_seen.set()
        elif mtype == "task_progress":
            try:
                uid = UUID(msg["uuid"])
            except (KeyError, ValueError):
                return
            task = self._tasks.get(uid)
            if task is not None:
                if msg.get("error"):
                    task.error = msg["error"]
                if msg.get("finished"):
                    task.event.set()

    # -- snapshot ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Consistent copy of every cached quantity, with ages."""
        now_m = time.monotonic()
        with self._lock:
            hp, jp, ds, fd = (
                self.head_pose,
                self.joint_positions,
                self.daemon_status,
                self.ft_debug,
            )
        return {
            "t_mono": now_m,
            "t_wall": time.time(),
            "head_pose": None if hp.value is None else hp.value.tolist(),
            "head_pose_age": hp.age(now_m),
            "joint_positions": jp.value,
            "joint_positions_age": jp.age(now_m),
            "daemon_status": ds.value,
            "daemon_status_age": ds.age(now_m),
            "ft_debug": fd.value,
            "ft_debug_age": fd.age(now_m),
        }

    def has_ft_debug(self) -> bool:
        return self._ft_debug_seen.is_set()

    def wait_ft_debug(self, timeout: float) -> bool:
        return self._ft_debug_seen.wait(timeout)

    # -- commands ----------------------------------------------------

    def _send(self, model: Any) -> None:
        if self._ws is None:
            raise ConnectionError("not connected")
        self._ws.send(model.model_dump_json())

    def set_head_tracking(self, enabled: bool, weight: float = 1.0) -> None:
        self._send(SetHeadTrackingCmd(enabled=enabled, weight=weight))

    def set_torque(self, on: bool, ids: Optional[list[str]] = None) -> None:
        self._send(SetTorqueCmd(on=on, ids=ids))

    def set_motor_mode_cmd(self, mode: str) -> None:
        """Set motor mode over the WS command channel (enabled/disabled/...)."""
        self._send(SetMotorModeCmd(mode=mode))

    def wake_up_motion(self) -> None:
        """Fire the daemon's standard wake_up motion (unfold + emote).

        Motors must already be enabled -- wake_up() only sends gotos. This is
        fire-and-forget over WS; poll the head pose to see it finish.
        """
        self._send(WakeUpCmd())

    def goto_sleep_motion(self) -> None:
        """Fire the daemon's standard goto_sleep motion (fold into body)."""
        self._send(GotoSleepCmd())

    def center_head(self, duration: float = 1.5) -> None:
        """Goto the daemon's awake-neutral pose: INIT head + INIT antennas, yaw 0.

        Identical target at every trial step so 'neutral' never drifts.
        """
        self.goto(
            head=INIT_HEAD_POSE,
            antennas=list(INIT_ANTENNAS_JOINT_POSITIONS),
            duration=duration,
            body_yaw=0.0,
        )

    def head_pose_offset_from_neutral(self) -> Optional[float]:
        """Frobenius norm of (current head pose - INIT_HEAD_POSE), or None."""
        with self._lock:
            hp = self.head_pose.value
        if hp is None:
            return None
        return float(np.linalg.norm(np.asarray(hp) - INIT_HEAD_POSE))

    def set_full_target(
        self,
        head: Optional[np.ndarray] = None,
        antennas: Optional[list[float]] = None,
        body_yaw: Optional[float] = None,
    ) -> None:
        self._send(
            SetFullTargetCmd(
                head=None if head is None else list(np.asarray(head).flatten()),
                antennas=antennas,
                body_yaw=body_yaw,
            )
        )

    def goto(
        self,
        head: Optional[np.ndarray] = None,
        antennas: Optional[list[float]] = None,
        duration: float = 1.5,
        body_yaw: Optional[float] = 0.0,
        method: InterpolationTechnique = InterpolationTechnique.MIN_JERK,
        wait: bool = True,
    ) -> None:
        req = GotoTaskRequest(
            head=None if head is None else list(np.asarray(head).flatten()),
            antennas=antennas,
            duration=duration,
            method=method,
            body_yaw=body_yaw,
        )
        task = TaskRequest(uuid=uuid4(), req=req, timestamp=datetime.now())
        self._tasks[task.uuid] = _Task()
        if self._ws is None:
            raise ConnectionError("not connected")
        self._ws.send(task.model_dump_json())
        if not wait:
            return
        t = self._tasks[task.uuid]
        if not t.event.wait(duration + 3.0):
            raise TimeoutError("goto task did not finish")
        err = t.error
        self._tasks.pop(task.uuid, None)
        if err:
            raise RuntimeError(f"goto failed: {err}")

    # -- REST -------------------------------------------------------

    def motor_status(self) -> str:
        r = self._session.get(f"{self._http}/api/motors/status", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["mode"]

    def set_motor_mode(self, mode: str) -> None:
        r = self._session.post(
            f"{self._http}/api/motors/set_mode/{mode}", timeout=self.timeout
        )
        r.raise_for_status()

    def camera_specs(self) -> dict:
        r = self._session.get(f"{self._http}/api/camera/specs", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def daemon_status_rest(self) -> dict:
        r = self._session.get(f"{self._http}/api/daemon/status", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def present_body_yaw(self) -> Optional[float]:
        """Current body yaw (rad) from REST. In the fixed-body experiments this
        stays ~0 because nothing commands the body; recording it keeps the
        head-relative-yaw math correct (present head pose yaw is world-frame)."""
        r = self._session.get(
            f"{self._http}/api/state/present_body_yaw", timeout=self.timeout
        )
        r.raise_for_status()
        val = r.json()
        if isinstance(val, dict):
            val = val.get("body_yaw", val.get("present_body_yaw"))
        return None if val is None else float(val)
