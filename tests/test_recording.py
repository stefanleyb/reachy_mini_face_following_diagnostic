import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diag.recording import SessionRecorder, pose_euler_xyz  # noqa: E402


def _load(path):
    with open(path) as f:
        return json.load(f)


def _read(path):
    with open(path) as f:
        return f.read()


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_session_and_trial_files(self):
        s = SessionRecorder(self.tmp, {"window": 30})
        self.assertTrue(os.path.isdir(s.dir))
        s.write_session({"version": "1.10.0"}, {"name": "cam"}, extra={"instrumented": True})
        doc = _load(s.session_path)
        self.assertEqual(doc["daemon_identity"]["version"], "1.10.0")
        self.assertIs(doc["instrumented"], True)

        tr = s.new_trial("trial_1_seated_center", {"height": "seated", "position": "center"})
        for i in range(10):
            tr.add_sample(
                {
                    "t_mono": float(i),
                    "head_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                    "ft_debug": {"tracking_target_pose": None, "tracking_aim_pose": None},
                }
            )
        self.assertEqual(tr.sample_count, 10)
        result = tr.finish(
            verdict={"face_acquired": "y", "settled": "n", "visible_oscillation": "slight"},
            instrumented=True,
            measured_sample_hz=49.5,
            face_frames_seen=42,
        )
        s.register_trial_result(result)

        rows = [json.loads(l) for l in _read(tr.samples_path).splitlines()]
        self.assertEqual(len(rows), 10)
        self.assertIn("derived", rows[0])
        self.assertIn("head_euler", rows[0]["derived"])

        meta = _load(tr.meta_path)
        self.assertEqual(meta["sample_count"], 10)
        self.assertEqual(meta["observer_verdict"]["settled"], "n")

        self.assertTrue(os.path.exists(os.path.join(s.dir, "summary.json")))
        md = _read(os.path.join(s.dir, "summary.md"))
        self.assertIn("trial_1_seated_center", md)

    def test_pose_euler_identity(self):
        e = pose_euler_xyz([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.assertLess(abs(e["roll"]), 1e-9)
        self.assertLess(abs(e["pitch"]), 1e-9)
        self.assertLess(abs(e["yaw"]), 1e-9)
        self.assertIsNone(pose_euler_xyz(None))


if __name__ == "__main__":
    unittest.main()
