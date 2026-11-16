"""Replays the take-home data pack (all 640,000 events) through the service and runs the three
practice checkpoints, plus a check of our swap rules at every checkpoint.

The pack is not in this repository. The test looks for it in QF_PACK_DIR or
./data-pack/quiet-fleet-data-pack and is skipped if it isn't there.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.environ.get("QF_PACK_DIR") or os.path.join(ROOT, "data-pack", "quiet-fleet-data-pack")
HAVE_PACK = os.path.isfile(os.path.join(PACK, "exam", "practice.json")) and os.path.isdir(os.path.join(PACK, "data", "telemetry"))


@unittest.skipUnless(HAVE_PACK, "data pack not found (set QF_PACK_DIR)")
class PracticeReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.replay import replay
        cls.end = {}

        def at_checkpoint(app, cp):
            if cp["id"] == "END":
                for depot in ("MSS", "REG"):
                    cls.end[depot] = app.job_cover({"depot": [depot], "at": [cp["at"]], "hours": ["24"]})
        cls.app, cls.results = replay(PACK, on_checkpoint=at_checkpoint, log=lambda *_: None)

    def test_all_practice_checks_pass(self):
        self.assertEqual(len(self.results), 11)
        failed = [(cp, label, errs) for cp, label, errs in self.results if errs]
        self.assertEqual(failed, [])

    def test_every_event_was_accepted_and_duplicates_were_folded(self):
        stats = self.app.store.stats
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(stats["bx_undecodable"], 0)
        self.assertEqual(stats["stored"] + stats["duplicates"], stats["received"])
        self.assertGreater(stats["duplicates"], 5000)

    def test_end_of_history_answers_follow_the_rules(self):
        from tools.replay import check_job_cover_rules
        from fleetsilence.timeutil import parse_ts
        at = parse_ts("2026-10-31T23:59:00Z")
        for depot, body in self.end.items():
            self.assertEqual(check_job_cover_rules(self.app, body, depot, at, 24), [])


if __name__ == "__main__":
    unittest.main()
