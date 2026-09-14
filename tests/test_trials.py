import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diag import trials as T  # noqa: E402


class TrialsTest(unittest.TestCase):
    def test_trial_matrix(self):
        self.assertEqual(len(T.TRIALS), 6)
        self.assertEqual(T.trial_label(0), "trial_1_seated_center")
        self.assertEqual(T.trial_label(5), "trial_6_standing_right")
        self.assertEqual({t["height"] for t in T.TRIALS}, {"seated", "standing"})
        self.assertEqual({t["position"] for t in T.TRIALS}, {"center", "left", "right"})

    def test_countdown_blocks_for_duration(self):
        t0 = time.monotonic()
        T.countdown(3, "test", bell_enabled=False)
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, 2.0)
        self.assertLessEqual(dt, 4.0)

    def test_trial_where(self):
        w0 = T.trial_where(0)  # seated center
        self.assertIn("Sit", w0)
        w4 = T.trial_where(4)  # standing left
        self.assertIn("Stand", w4)
        self.assertIn("left", w4)

    def test_run_config_defaults(self):
        cfg = T.RunConfig()
        self.assertEqual(cfg.tracking_window_s, 15.0)
        self.assertEqual(cfg.countdown_s, 10.0)
        self.assertEqual(cfg.sample_hz, 50.0)
        self.assertIn("host", cfg.as_dict())


if __name__ == "__main__":
    unittest.main()
