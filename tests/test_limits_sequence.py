import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diag import limits as L  # noqa: E402


class LimitsSequenceTest(unittest.TestCase):
    def test_eight_marks_alternating_order(self):
        seq = L.MARK_SEQUENCE
        self.assertEqual(len(seq), 8)
        # user's order: left small, right small, left big, right big, then repeat
        got = [(m["side"], m["kind"], m["rep"]) for m in seq]
        self.assertEqual(
            got,
            [
                ("left", "sustained", 1),
                ("right", "sustained", 1),
                ("left", "immediate", 1),
                ("right", "immediate", 1),
                ("left", "sustained", 2),
                ("right", "sustained", 2),
                ("left", "immediate", 2),
                ("right", "immediate", 2),
            ],
        )
        self.assertEqual([m["n"] for m in seq], list(range(1, 9)))

    def test_two_readings_per_bucket(self):
        from collections import Counter

        buckets = Counter((m["side"], m["kind"]) for m in L.MARK_SEQUENCE)
        self.assertEqual(set(buckets.values()), {2})
        self.assertEqual(len(buckets), 4)

    def test_labels_and_prompts(self):
        self.assertEqual(L.mark_label(L.MARK_SEQUENCE[0]), "mark_1_left_sustained_r1")
        self.assertEqual(L.mark_label(L.MARK_SEQUENCE[7]), "mark_8_right_immediate_r2")
        p1 = L.mark_prompt(L.MARK_SEQUENCE[0])
        self.assertIn("LEFT", p1)
        self.assertIn("several seconds", p1)
        p3 = L.mark_prompt(L.MARK_SEQUENCE[2])
        self.assertIn("immediately", p3)
        self.assertIn("Mark 1 of 8", L.mark_headline(L.MARK_SEQUENCE[0]))


if __name__ == "__main__":
    unittest.main()
