"""Minimal, reversible daemon-side instrumentation for the fixed-body face baseline.

Why this exists
---------------
The public ``reachy_mini`` 1.10.0 API only surfaces the *filtered* face centre
(``FaceTarget`` inside ``daemon_status``), and ``daemon_status`` is published at
1 Hz. That is far too slow to characterise a head oscillation whose face-target
stream runs at ~8.5 Hz on a 50 Hz control loop, and it never exposes:

* the raw (pre-filter) normalised face centre,
* the daemon's computed ``_tracking_target_pose`` (look-at result), or
* the eased ``_tracking_aim`` that is actually blended into the IK.

This module adds exactly those, at the control-loop rate, without editing any
file under ``site-packages``. It is applied by importing it and calling
``apply()`` *before* the daemon's ``main()`` runs -- see
``run_instrumented_daemon.py``.

What it changes (all in-process monkeypatches)
---------------------------------------------
1. ``FaceTracker._process_detections`` is replaced with a copy that also stashes
   the raw normalised centre and its capture timestamp on the tracker instance
   (``_dbg_raw_center`` / ``_dbg_obs_ts``). The tracking behaviour is byte-for-byte
   identical; only two assignments are added.
2. ``Backend.set_ws_broadcast_callback`` is wrapped so that, the moment the WS
   broadcast hook is wired by ``WSServer.start()``, a low-priority daemon thread
   starts and the WS fan-out callback is captured. That thread samples the
   tracker internals at ``REACHY_FT_DEBUG_HZ`` (default 50) and sends one
   ``{"type": "face_tracking_debug", ...}`` JSON frame per tick **only through
   the WS callback** -- NOT ``broadcast_to_all_clients``, whose WebRTC path
   asserts ``channel->opened`` and floods GStreamer CRITICALs at emit rate when
   no WebRTC peer is connected. ``WSServer._broadcast`` is thread-safe and
   no-ops when no WS client is connected.

Serialization happens on the *emitter thread*, never on the 50 Hz control
thread, so the added cost to the control loop is a handful of attribute reads
under a lock that is already held briefly each tick.

Reverting
---------
Stop launching the daemon through ``run_instrumented_daemon.py``. Nothing on
disk was modified. ``remove()`` is also provided for tests / long-lived procs.

This is deliberately a throwaway measurement aid. The durable version of this
data path belongs in a separate upstream ``reachy_mini`` checkout as a real
protocol message (see the project workstream history).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("reachy_mini.ft_debug")

FT_DEBUG_MSG_TYPE = "face_tracking_debug"

_state: dict[str, Any] = {
    "applied": False,
    "orig_process_detections": None,
    "orig_set_ws_broadcast_callback": None,
    "emitter": None,  # _DebugEmitter
    "ws_send": None,  # WSServer._broadcast, captured in the wrapper
}


def _hz() -> float:
    # 30 Hz is ~3.5x the ~8.5 Hz face-detector rate and plenty to resolve a
    # 1-2 Hz head oscillation and the first-move overshoot, at modest Pi cost.
    try:
        v = float(os.environ.get("REACHY_FT_DEBUG_HZ", "30"))
    except ValueError:
        v = 30.0
    return min(max(v, 1.0), 100.0)


def _mat_to_list(m: Optional[np.ndarray]) -> Optional[list[list[float]]]:
    if m is None:
        return None
    return np.asarray(m, dtype=float).tolist()


# --------------------------------------------------------------------------
# 1. Instrumented FaceTracker._process_detections
# --------------------------------------------------------------------------


def _install_process_detections() -> None:
    from reachy_mini.vision import face_tracking as ft

    if _state["orig_process_detections"] is not None:
        return
    _state["orig_process_detections"] = ft.FaceTracker._process_detections

    to_observation = ft.to_observation

    def _process_detections(  # noqa: ANN001 - mirrors upstream signature
        self,
        faces,
        width,
        height,
        camera_matrix,
        distortion,
        timestamp,
    ) -> None:
        face = self._selector.select(faces, width, height)
        if face is None and not self._selector.has_target:
            self._center_filter.reset()
        observation = to_observation(
            face, width, height, camera_matrix, distortion, timestamp
        )
        # --- instrumentation: capture the raw centre before filtering ---
        self._dbg_raw_center = observation.center
        self._dbg_obs_ts = timestamp
        self._dbg_roll = observation.roll
        # ---------------------------------------------------------------
        if observation.center is not None:
            observation.center = self._center_filter.update(observation.center)
        self._observations.put(observation)

    ft.FaceTracker._process_detections = _process_detections
    logger.info("ft_debug: FaceTracker._process_detections instrumented")


# --------------------------------------------------------------------------
# 2. Emitter thread, started when the WS broadcast hook is wired
# --------------------------------------------------------------------------


class _DebugEmitter(threading.Thread):
    def __init__(self, backend: Any) -> None:
        super().__init__(daemon=True, name="ft-debug-emitter")
        self._backend = backend
        self._stop = threading.Event()
        self._seq = 0

    def stop(self) -> None:
        self._stop.set()

    def _snapshot(self) -> dict[str, Any]:
        b = self._backend
        now_mono = time.monotonic()
        now_wall = time.time()

        # Read the tracking internals under the lock the daemon already uses,
        # holding it only for cheap attribute reads (no serialization here).
        lock = getattr(b, "_tracking_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            tracking_enabled = bool(getattr(b, "_tracking_enabled", False))
            requested_weight = float(getattr(b, "_tracking_requested_weight", 0.0))
            weight = float(getattr(b, "_tracking_weight", 0.0))
            alpha = float(getattr(b, "_tracking_alpha", float("nan")))
            lost_timeout = float(getattr(b, "_tracking_lost_timeout", float("nan")))
            last_face_seen = getattr(b, "_last_face_seen", None)
            target_pose = getattr(b, "_tracking_target_pose", None)
            aim_pose = getattr(b, "_tracking_aim", None)
            face_target = getattr(b, "_face_target", None)
            tracker = getattr(b, "_tracker", None)
            raw_center = getattr(tracker, "_dbg_raw_center", None) if tracker else None
            raw_ts = getattr(tracker, "_dbg_obs_ts", None) if tracker else None
            raw_roll = getattr(tracker, "_dbg_roll", None) if tracker else None
            target_pose = None if target_pose is None else np.array(target_pose)
            aim_pose = None if aim_pose is None else np.array(aim_pose)
        finally:
            if lock is not None:
                lock.release()

        try:
            head_pose = _mat_to_list(b.get_current_head_pose())
        except Exception:
            head_pose = None

        ft_detected = ft_x = ft_y = ft_roll = ft_ts = None
        if face_target is not None:
            ft_detected = bool(getattr(face_target, "detected", False))
            ft_x = getattr(face_target, "x", None)
            ft_y = getattr(face_target, "y", None)
            ft_roll = getattr(face_target, "roll", None)
            ft_ts = getattr(face_target, "ts", None)

        self._seq += 1
        return {
            "type": FT_DEBUG_MSG_TYPE,
            "seq": self._seq,
            "t_mono": now_mono,
            "t_wall": now_wall,
            "tracking_enabled": tracking_enabled,
            "requested_weight": requested_weight,
            "weight": weight,
            "alpha": alpha,
            "lost_timeout": lost_timeout,
            "last_face_seen_mono": last_face_seen,
            "since_face_seen": (
                None if last_face_seen is None else now_mono - float(last_face_seen)
            ),
            # raw (pre-filter) normalised nose centre, in [-1, 1]
            "raw_center": list(raw_center) if raw_center is not None else None,
            "raw_obs_ts_mono": raw_ts,
            "raw_roll": raw_roll,
            # filtered face target the daemon actually latched (FaceTarget)
            "filtered_center": (
                [ft_x, ft_y] if ft_x is not None and ft_y is not None else None
            ),
            "filtered_detected": ft_detected,
            "filtered_roll": ft_roll,
            "filtered_obs_ts_mono": ft_ts,
            # geometry
            "current_head_pose": head_pose,
            "tracking_target_pose": _mat_to_list(target_pose),
            "tracking_aim_pose": _mat_to_list(aim_pose),
        }

    def run(self) -> None:
        if platform.system() == "Linux":
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except OSError:
                pass
        logger.info("ft_debug: emitter thread started at %.1f Hz", _hz())
        period = 1.0 / _hz()
        next_t = time.monotonic()
        while not self._stop.is_set():
            # WS-only: WSServer._broadcast is thread-safe and no-ops when no
            # client is connected. We must NOT use broadcast_to_all_clients --
            # its WebRTC path spam-fails ("channel->opened" assertion) at our
            # emit rate when no WebRTC peer is connected.
            ws_send = _state.get("ws_send")
            if ws_send is not None:
                try:
                    ws_send(json.dumps(self._snapshot()))
                except Exception as e:  # never let the aid kill the daemon
                    logger.warning("ft_debug: emit failed: %s", e)
            next_t += period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                self._stop.wait(sleep)
            else:
                next_t = time.monotonic()


def _install_emitter_hook() -> None:
    from reachy_mini.daemon.backend.abstract import Backend

    if _state["orig_set_ws_broadcast_callback"] is not None:
        return
    _state["orig_set_ws_broadcast_callback"] = Backend.set_ws_broadcast_callback

    orig = Backend.set_ws_broadcast_callback

    def set_ws_broadcast_callback(self, cb):  # noqa: ANN001
        orig(self, cb)
        _state["ws_send"] = cb  # WS-only fan-out; picked up live by the emitter
        if _state["emitter"] is None:
            emitter = _DebugEmitter(self)
            _state["emitter"] = emitter
            emitter.start()

    Backend.set_ws_broadcast_callback = set_ws_broadcast_callback
    logger.info("ft_debug: Backend.set_ws_broadcast_callback wrapped")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def apply() -> None:
    """Install the instrumentation. Idempotent."""
    if _state["applied"]:
        return
    _install_process_detections()
    _install_emitter_hook()
    _state["applied"] = True
    logger.info(
        "ft_debug: instrumentation applied (msg type %r, %.1f Hz)",
        FT_DEBUG_MSG_TYPE,
        _hz(),
    )


def remove() -> None:
    """Best-effort restore of patched callables (mainly for tests)."""
    emitter = _state["emitter"]
    if emitter is not None:
        emitter.stop()
        _state["emitter"] = None
    _state["ws_send"] = None

    if _state["orig_process_detections"] is not None:
        from reachy_mini.vision import face_tracking as ft

        ft.FaceTracker._process_detections = _state["orig_process_detections"]
        _state["orig_process_detections"] = None

    if _state["orig_set_ws_broadcast_callback"] is not None:
        from reachy_mini.daemon.backend.abstract import Backend

        Backend.set_ws_broadcast_callback = _state["orig_set_ws_broadcast_callback"]
        _state["orig_set_ws_broadcast_callback"] = None

    _state["applied"] = False
