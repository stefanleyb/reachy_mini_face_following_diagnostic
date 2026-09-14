"""End-to-end rehearsal: the handoff-limit runner drives 8 marks against the
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

import limits_runner as R  # noqa: E402
from diag.fake import FakeClient  # noqa: E402


def _load(path):
    with open(path) as f:
        return json.load(f)


class LimitsDryRunTest(unittest.TestCase):
    def test_full_eight_mark_dry_run(self):
        tmp = tempfile.mkdtemp()
        captured = {}

        real_build = R.build_client

        def _spy(cfg):
            c = FakeClient(oscillate=True, instrumented=True)
            captured["client"] = c
            return c

        answers = ["y"]          # wake: head clear of the body?
        answers += [""]          # ready to enable tracking? (default y)
        answers += [""] * 8      # press Enter for each mark
        answers += [""]          # sleep: put robot to sleep? (default y)

        old_stdin = sys.stdin
        sys.stdin = io.StringIO("\n".join(answers) + "\n")
        R.build_client = _spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = R.main([
                    "--dry-run", "--window", "1", "--sample-hz", "40",
                    "--center-duration", "0.1", "--results-dir", tmp,
                ])
        finally:
            sys.stdin = old_stdin
            R.build_client = real_build

        self.assertEqual(rc, 0)

        d = glob.glob(os.path.join(tmp, "limits_*"))
        self.assertEqual(len(d), 1)
        d = d[0]

        session = _load(os.path.join(d, "session.json"))
        self.assertIs(session["instrumented"], True)
        self.assertEqual(len(session["mark_sequence"]), 8)
        self.assertIn("baseline_body_yaw_rad", session)

        with open(os.path.join(d, "marks.jsonl")) as f:
            marks = [json.loads(x) for x in f]
        self.assertEqual(len(marks), 8)
        self.assertEqual([m["n"] for m in marks], list(range(1, 9)))
        self.assertTrue(all(m["body_yaw_rad"] == 0.0 for m in marks))

        summary = _load(os.path.join(d, "summary.json"))
        self.assertEqual(len(summary["marks"]), 8)
        self.assertIn("sustained", summary["rollup"]["suggestion"])
        self.assertTrue(os.path.exists(os.path.join(d, "summary.md")))
        self.assertGreater(summary["sample_count"], 0)
        # fake client always reports body_yaw 0.0 -> session verified valid
        self.assertTrue(summary["session_validity"]["valid"])
        self.assertTrue(all(m["rel_reliable"] for m in summary["marks"]))
        self.assertGreater(summary["session_validity"]["body_yaw_reads_ok"], 0)

        # robot left safe: tracking off, motors disabled by the sleep sequence
        c = captured["client"]
        self.assertFalse(c._tracking)
        self.assertEqual(c._motor_mode, "disabled")

    def test_preflight_moves_nothing(self):
        captured = {}
        real_build = R.build_client

        def _spy(cfg):
            c = FakeClient(oscillate=True, instrumented=False)
            captured["client"] = c
            return c

        R.build_client = _spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = R.main(["--dry-run", "--preflight"])
        finally:
            R.build_client = real_build

        self.assertEqual(rc, 0)
        c = captured["client"]
        self.assertFalse(c._tracking)
        self.assertEqual(c._motor_mode, "enabled")  # untouched


if __name__ == "__main__":
    unittest.main()
