"""End-to-end rehearsal: the runner drives a full (short) trial against the
fake client and leaves a complete result directory behind, with no daemon.
"""

import contextlib
import glob
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnostic_runner as R  # noqa: E402


def _load(path):
    with open(path) as f:
        return json.load(f)


class DryRunTest(unittest.TestCase):
    def test_dry_run_single_trial(self):
        tmp = tempfile.mkdtemp()
        answers = [
            "y",   # wake: head clear of the body? (no default)
            "",    # trial 1: press Enter to start countdown
            "",    # sleep: put robot to sleep (default y)
        ]
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("\n".join(answers) + "\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = R.main([
                    "--dry-run",
                    "--only", "1",
                    "--countdown", "1",
                    "--window", "2",
                    "--sample-hz", "20",
                    "--center-duration", "0.1",
                    "--no-bell",
                    "--results-dir", tmp,
                ])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)

        sess_dirs = glob.glob(os.path.join(tmp, "baseline_*"))
        self.assertEqual(len(sess_dirs), 1)
        d = sess_dirs[0]

        session = _load(os.path.join(d, "session.json"))
        self.assertIs(session["instrumented"], True)

        meta = _load(os.path.join(d, "trial_1_seated_center.json"))
        self.assertEqual(meta["meta"]["position"], "center")
        self.assertIn("where", meta["meta"])
        self.assertGreaterEqual(meta["sample_count"], 20)

        with open(os.path.join(d, "trial_1_seated_center.samples.jsonl")) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(rows[0]["ft_debug"]["type"], "face_tracking_debug")
        self.assertIn("head_euler", rows[0]["derived"])

        summary = _load(os.path.join(d, "summary.json"))
        self.assertEqual(len(summary["trials"]), 1)
        self.assertTrue(os.path.exists(os.path.join(d, "summary.md")))


if __name__ == "__main__":
    unittest.main()
