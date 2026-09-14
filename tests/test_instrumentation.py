"""The instrumentation must apply cleanly against the installed reachy_mini 1.10.0
without editing site-packages, and must expose the fields the runner records.
"""

import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instrument"),
)

try:
    import reachy_mini  # noqa: F401

    HAVE_REACHY = True
except Exception:  # noqa: BLE001
    HAVE_REACHY = False

if HAVE_REACHY:
    import daemon_ft_debug as ftd


class _FakeBackend:
    def __init__(self):
        self._tracking_lock = threading.Lock()
        self._tracking_enabled = True
        self._tracking_requested_weight = 1.0
        self._tracking_weight = 1.0
        self._tracking_alpha = 0.15
        self._tracking_lost_timeout = 2.0
        self._last_face_seen = 5.0
        self._tracking_target_pose = np.eye(4)
        self._tracking_aim = np.eye(4)

        class _FT:
            detected = True
            x = 0.1
            y = 0.05
            roll = 0.0
            ts = 9.0

        self._face_target = _FT()

        class _Tr:
            _dbg_raw_center = (0.2, 0.1)
            _dbg_obs_ts = 8.5
            _dbg_roll = 0.0

        self._tracker = _Tr()
        self.sent = []

    def get_current_head_pose(self):
        return np.eye(4)

    def broadcast_to_all_clients(self, payload):
        self.sent.append(payload)


@unittest.skipUnless(HAVE_REACHY, "reachy_mini not importable")
class InstrumentationTest(unittest.TestCase):
    def tearDown(self):
        ftd.remove()

    def test_apply_and_remove_are_reversible(self):
        from reachy_mini.vision import face_tracking as ft
        from reachy_mini.daemon.backend.abstract import Backend

        orig_pd = ft.FaceTracker._process_detections
        orig_cb = Backend.set_ws_broadcast_callback

        ftd.apply()
        self.assertIsNot(ft.FaceTracker._process_detections, orig_pd)
        self.assertIsNot(Backend.set_ws_broadcast_callback, orig_cb)

        ftd.remove()
        self.assertIs(ft.FaceTracker._process_detections, orig_pd)
        self.assertIs(Backend.set_ws_broadcast_callback, orig_cb)

    def test_process_detections_records_raw_center(self):
        from reachy_mini.vision.face_tracking import FaceTracker

        ftd.apply()
        tr = FaceTracker()
        tr._process_detections([], 320, 180, np.eye(3), np.zeros(5), 111.0)
        self.assertEqual(tr._dbg_obs_ts, 111.0)
        self.assertIsNone(tr._dbg_raw_center)

    def test_emitter_snapshot_has_required_fields(self):
        ftd.apply()
        em = ftd._DebugEmitter(_FakeBackend())
        snap = em._snapshot()
        for key in (
            "type",
            "raw_center",
            "raw_obs_ts_mono",
            "filtered_center",
            "filtered_detected",
            "tracking_target_pose",
            "tracking_aim_pose",
            "current_head_pose",
            "alpha",
            "since_face_seen",
        ):
            self.assertIn(key, snap)
        self.assertEqual(snap["type"], "face_tracking_debug")
        self.assertEqual(snap["raw_center"], [0.2, 0.1])
        self.assertEqual(snap["filtered_center"], [0.1, 0.05])
        self.assertEqual(len(snap["tracking_target_pose"]), 4)

    def test_set_ws_broadcast_callback_starts_emitter(self):
        ftd.apply()
        be = _FakeBackend()
        from reachy_mini.daemon.backend.abstract import Backend

        Backend.set_ws_broadcast_callback(be, lambda s: None)
        self.assertIsNotNone(ftd._state["emitter"])
        self.assertTrue(ftd._state["emitter"].is_alive())

    def test_emitter_uses_ws_only_never_broadcast_to_all_clients(self):
        """Regression: broadcast_to_all_clients hits the WebRTC path, which
        spam-fails at emit rate when no WebRTC peer is connected. Must use the
        WS callback only.
        """
        ftd.apply()
        be = _FakeBackend()
        sent = []
        from reachy_mini.daemon.backend.abstract import Backend

        Backend.set_ws_broadcast_callback(be, lambda s: sent.append(s))
        try:
            time.sleep(0.3)
        finally:
            ftd._state["emitter"].stop()
        self.assertEqual(be.sent, [], "must NOT call broadcast_to_all_clients")
        self.assertTrue(sent, "must emit through the WS callback")
        import json as _json

        self.assertEqual(_json.loads(sent[0])["type"], "face_tracking_debug")


if __name__ == "__main__":
    unittest.main()
