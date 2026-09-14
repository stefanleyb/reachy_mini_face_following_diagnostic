import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diag import limits as L  # noqa: E402

_DEG = 180.0 / math.pi


class _StubClient:
    def __init__(self, body_yaw=0.0):
        self._body_yaw = body_yaw

    def present_body_yaw(self):
        if self._body_yaw is None:
            raise RuntimeError("body yaw read failed")
        return self._body_yaw

    def snapshot(self):
        return {"t_mono": time.monotonic(), "head_pose": None, "derived": {}}


def _row(t_mono, yaw_rad):
    return {
        "t_mono": t_mono,
        "head_pose": None,
        "ft_debug": None,
        "derived": {"head_euler": {"roll": 0.0, "pitch": 0.0, "yaw": yaw_rad}},
    }


class LimitsRecordingTest(unittest.TestCase):
    def _mk(self, tol_rad=None):
        tmp = tempfile.mkdtemp()
        rec = L.LimitsRecorder(tmp, {"window_s": 1.0}, body_yaw_tol_rad=tol_rad)
        self.addCleanup(lambda: not rec._marks_fp.closed and rec._marks_fp.close())
        return tmp, rec

    def _fill(self, rec, yaw_rad, n=3):
        rec._buf.clear()
        for dt in [0.1 * (i + 1) for i in range(n)]:
            rec._buf.append(_row(time.monotonic() - dt, yaw_rad))

    def test_window_stats_relative_to_body(self):
        _, rec = self._mk()
        now = time.monotonic()
        for dt, yaw in [(0.4, 0.30), (0.3, 0.32), (0.2, 0.28), (0.1, 0.30)]:
            rec._buf.append(_row(now - dt, yaw))
        client = _StubClient(body_yaw=0.02)  # ~1.1 deg, within default 3 deg tol
        res = rec.mark(L.MARK_SEQUENCE[0], client, window_s=1.0)

        self.assertEqual(res["n_samples"], 4)
        self.assertTrue(res["rel_reliable"])
        # rel = world - body_yaw(0.02); mean world = 0.30 -> rel 0.28 rad
        self.assertAlmostEqual(res["rel_deg_mean"], 0.28 * _DEG, places=3)
        self.assertLess(res["rel_deg_min"], res["rel_deg_max"])

    def test_missing_body_yaw_flags_mark_unreliable(self):
        _, rec = self._mk()
        self._fill(rec, 0.30)
        res = rec.mark(L.MARK_SEQUENCE[0], _StubClient(body_yaw=None), window_s=1.0)
        self.assertFalse(res["body_yaw_known"])
        self.assertFalse(res["rel_reliable"])
        # world yaw still reported (rel computed against assumed 0)
        self.assertAlmostEqual(res["rel_deg_mean"], 0.30 * _DEG, places=3)

    def test_out_of_tolerance_body_yaw_flags_mark(self):
        _, rec = self._mk()
        self._fill(rec, 0.30)
        res = rec.mark(L.MARK_SEQUENCE[0], _StubClient(body_yaw=0.20), window_s=1.0)  # ~11 deg
        self.assertTrue(res["body_yaw_known"])
        self.assertFalse(res["rel_reliable"])

    def test_recent_watch_sample_used_when_press_read_fails(self):
        _, rec = self._mk()
        rec._record_body_yaw(0.01)  # a fresh watch sample
        self._fill(rec, 0.30)
        res = rec.mark(L.MARK_SEQUENCE[0], _StubClient(body_yaw=None), window_s=1.0)
        self.assertTrue(res["body_yaw_known"])
        self.assertTrue(res["rel_reliable"])

    def test_valid_session_and_asymmetry_warning(self):
        _, rec = self._mk()
        rec.write_session({"version": "1.10.0"}, {"name": "cam"})
        rec.set_baseline_body_yaw(0.0)
        client = _StubClient(body_yaw=0.0)
        # left consistently earlier than right; immediate wider than sustained
        plan = {
            ("left", "sustained"): -0.20, ("right", "sustained"): 0.40,
            ("left", "immediate"): -0.35, ("right", "immediate"): 0.55,
        }
        for m in L.MARK_SEQUENCE:
            self._fill(rec, plan[(m["side"], m["kind"])])
            rec.mark(m, client, window_s=1.0)
        summary = rec.finish()

        self.assertTrue(summary["session_validity"]["valid"])
        sug = summary["rollup"]["suggestion"]
        self.assertEqual(sug["sustained"]["suggested_symmetric_deg"] % 5, 0)
        self.assertGreater(sug["immediate"]["mean_abs_deg"], sug["sustained"]["mean_abs_deg"])
        joined = " ".join(summary["review_warnings"])
        self.assertIn("left", joined)
        self.assertIn("do not average", joined)

        with open(os.path.join(rec.dir, "summary.md")) as f:
            md = f.read()
        self.assertIn("starting point, not a decision", md)
        self.assertNotIn("SESSION VALIDITY", md)
        self.assertNotIn("window_samples", json.dumps(summary["marks"][0]))

    def test_invalid_session_when_baseline_unknown(self):
        _, rec = self._mk()
        rec.set_baseline_body_yaw(None)
        client = _StubClient(body_yaw=None)
        for m in L.MARK_SEQUENCE:
            self._fill(rec, 0.3 if m["side"] == "right" else -0.3)
            rec.mark(m, client, window_s=1.0)
        summary = rec.finish()

        self.assertFalse(summary["session_validity"]["valid"])
        self.assertTrue(summary["session_validity"]["reasons"])
        with open(os.path.join(rec.dir, "summary.md")) as f:
            md = f.read()
        self.assertIn("SESSION VALIDITY", md)

    def test_immediate_smaller_than_sustained_warns(self):
        _, rec = self._mk()
        rec.set_baseline_body_yaw(0.0)
        client = _StubClient(body_yaw=0.0)
        plan = {
            ("left", "sustained"): -0.50, ("right", "sustained"): 0.50,
            ("left", "immediate"): -0.20, ("right", "immediate"): 0.20,  # mistakenly small
        }
        for m in L.MARK_SEQUENCE:
            self._fill(rec, plan[(m["side"], m["kind"])])
            rec.mark(m, client, window_s=1.0)
        summary = rec.finish()
        joined = " ".join(summary["review_warnings"]).lower()
        self.assertIn("smaller than", joined)


if __name__ == "__main__":
    unittest.main()
