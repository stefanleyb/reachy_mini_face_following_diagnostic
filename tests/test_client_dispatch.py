import os
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diag.client import DiagnosticClient, FT_DEBUG_MSG_TYPE, _Task  # noqa: E402


class ClientDispatchTest(unittest.TestCase):
    def make(self):
        return DiagnosticClient("localhost", 8000)

    def test_dispatch_populates_caches(self):
        cl = self.make()
        cl.ingest({"type": "head_pose", "head_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]})
        cl.ingest({"type": "joint_positions", "head_joint_positions": [0.1] * 7, "antennas_joint_positions": [0.0, 0.0]})
        cl.ingest({"type": "daemon_status", "version": "1.10.0", "face_target": {"detected": True, "x": 0.1, "y": 0.0, "ts": 123.0}})
        cl.ingest({
            "type": FT_DEBUG_MSG_TYPE,
            "seq": 1,
            "raw_center": [0.2, 0.1],
            "filtered_center": [0.12, 0.06],
            "tracking_target_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "tracking_aim_pose": None,
        })

        snap = cl.snapshot()
        self.assertEqual(snap["head_pose"][0][0], 1)
        self.assertEqual(snap["joint_positions"]["head"][0], 0.1)
        self.assertIs(snap["daemon_status"]["face_target"]["detected"], True)
        self.assertEqual(snap["ft_debug"]["raw_center"], [0.2, 0.1])
        self.assertTrue(cl.has_ft_debug())
        self.assertIsInstance(snap["head_pose_age"], float)

    def test_task_progress_signals_completion(self):
        cl = self.make()
        uid = uuid4()
        cl._tasks[uid] = _Task()
        cl.ingest({"type": "task_progress", "uuid": str(uid), "finished": True})
        self.assertTrue(cl._tasks[uid].event.is_set())

    def test_unknown_message_ignored(self):
        cl = self.make()
        cl.ingest({"type": "something_new", "foo": 1})
        cl.ingest({"no_type": True})
        self.assertIsNone(cl.snapshot()["head_pose"])


if __name__ == "__main__":
    unittest.main()
