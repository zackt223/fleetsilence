"""GET /job-cover: swaps, gaps, keeps, and saying a carrier outage once."""
import unittest

from fleetsilence.server import BadRequest
from fleetsilence.timeutil import iso

from .helpers import HOUR, MIN, T0, cover, drive, ev, healthy_fleet, make_app, record, status

AT = T0                      # 08:00 in Mississauga
JOB = (T0 + 2 * HOUR, T0 + 4 * HOUR)


def power_loss(serial, at):
    """A tracker that lost power an hour before `at` and drained its backup battery."""
    return [ev(serial, "heartbeat", at - 5 * HOUR, {"battery_v": 12.5, "signal": 4}),
            ev(serial, "power_disconnect", at - HOUR, event_id=f"pd-{serial}"),
            ev(serial, "low_voltage", at - 50 * MIN, {"volts": 3.5, "source": "backup_battery"}, event_id=f"lv-{serial}")]


class SwapTest(unittest.TestCase):
    def test_job_on_an_untrackable_pickup_moves_to_the_free_one(self):
        app = make_app(jobs=[("J-1", "K-1", *JOB)])
        healthy_fleet(app, AT, skip={"T-21"})
        app.ingest({"events": power_loss("T-21", AT)})
        body = cover(app, AT)
        self.assertEqual({k: body[k] for k in ("depot", "at", "hours")}, {"depot": "MSS", "at": iso(AT), "hours": 24})
        (job,) = body["jobs"]
        self.assertEqual((job["job_id"], job["asset_id"], job["problem"], job["decision"], job["swap_to"]),
                         ("J-1", "K-1", "no_location", "swap", "K-2"))
        self.assertTrue({"pd-T-21", "lv-T-21"} <= set(job["evidence"]))
        self.assertTrue(job["reasons"] and job["manager_action"])

    def test_gap_when_the_only_other_pickup_is_busy_for_part_of_the_slot(self):
        app = make_app(jobs=[("J-1", "K-1", *JOB), ("J-2", "K-2", JOB[1] - 10 * MIN, JOB[1] + HOUR)])
        healthy_fleet(app, AT, skip={"T-21"})
        app.ingest({"events": power_loss("T-21", AT)})
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual((job["decision"], job["swap_to"]), ("gap", None))
        self.assertIn("1 already has a job in that slot", " ".join(job["reasons"]))

    def test_gap_when_the_only_other_pickup_cannot_be_seen_either(self):
        app = make_app(jobs=[("J-1", "K-1", *JOB)])
        healthy_fleet(app, AT, skip={"T-21", "T-22"})
        app.ingest({"events": power_loss("T-21", AT) + power_loss("T-22", AT)})
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual((job["decision"], job["swap_to"]), ("gap", None))
        self.assertIn("1 can't be seen or recorded itself", " ".join(job["reasons"]))

    def test_a_replacement_is_never_given_two_overlapping_jobs(self):
        jobs = [("J-1", "V-5", *JOB), ("J-2", "V-6", JOB[0] + 30 * MIN, JOB[1])]
        jobs += [(f"J-busy{n}", f"V-{n}", *JOB) for n in (1, 2, 3, 4, 7, 8, 9, 10, 11)]   # only V-12 is free
        app = make_app(jobs=jobs)
        healthy_fleet(app, AT, skip={"T-5", "T-6"})
        app.ingest({"events": power_loss("T-5", AT) + power_loss("T-6", AT)})
        got = {j["job_id"]: (j["decision"], j["swap_to"]) for j in cover(app, AT)["jobs"]}
        self.assertEqual(got, {"J-1": ("swap", "V-12"), "J-2": ("gap", None)})

    def test_all_jobs_of_one_vehicle_move_to_the_same_replacement(self):
        app = make_app(jobs=[("J-1", "V-5", *JOB), ("J-2", "V-5", JOB[1] + HOUR, JOB[1] + 2 * HOUR)])
        healthy_fleet(app, AT, skip={"T-5"})
        app.ingest({"events": power_loss("T-5", AT)})
        targets = {j["swap_to"] for j in cover(app, AT)["jobs"]}
        self.assertEqual(len(targets), 1)
        self.assertNotIn(None, targets)

    def test_only_jobs_starting_in_the_window_at_that_depot_are_listed(self):
        app = make_app(jobs=[("J-early", "K-1", AT - HOUR, AT + HOUR), ("J-in", "K-1", AT + HOUR, AT + 2 * HOUR),
                             ("J-late", "K-1", AT + 30 * HOUR, AT + 31 * HOUR)])
        healthy_fleet(app, AT, skip={"T-21"})
        app.ingest({"events": power_loss("T-21", AT)})
        self.assertEqual([j["job_id"] for j in cover(app, AT, hours=24)["jobs"]], ["J-in"])
        self.assertEqual([j["job_id"] for j in cover(app, AT, hours=72)["jobs"]], ["J-in", "J-late"])
        self.assertEqual(cover(app, AT, depot="REG")["jobs"], [])


class VideoTest(unittest.TestCase):
    def camera_fault(self, app, reboot=False):
        events = [ev("C-1", "camera_status", AT - 3 * HOUR, {"status": "recording"}),
                  ev("C-1", "camera_status", AT - 2 * HOUR, {"status": "storage_error"}, event_id="err")]
        if reboot:
            events.append(ev("C-1", "command", AT - HOUR, {"command": "reboot", "issued_by": "support"}))
        app.ingest({"events": events})

    def test_camera_van_only_swaps_to_a_van_with_a_working_camera(self):
        app = make_app(jobs=[("J-1", "V-1", AT + HOUR, AT + 2 * HOUR)])
        healthy_fleet(app, AT, skip={"C-1"})
        self.camera_fault(app)
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual((job["problem"], job["decision"]), ("no_video", "swap"))
        self.assertIn(job["swap_to"], {"V-2", "V-3", "V-4", "V-7", "V-8", "V-9", "V-10"})
        self.assertIn("err", job["evidence"])

    def busy_camera_vans(self, asset):
        """A schedule in which every other van with a camera is busy; camera-less V-5, V-6, V-11, V-12 are free."""
        slot = (AT + HOUR, AT + 2 * HOUR)
        return [("J-1", asset, *slot)] + [(f"J-b{n}", f"V-{n}", *slot) for n in (1, 2, 3, 4, 7, 8, 9, 10) if f"V-{n}" != asset]

    def test_recording_problem_is_a_gap_when_only_camera_less_vans_are_free(self):
        app = make_app(jobs=self.busy_camera_vans("V-1"))
        healthy_fleet(app, AT, skip={"C-1"})
        self.camera_fault(app)
        job = next(j for j in cover(app, AT)["jobs"] if j["job_id"] == "J-1")
        self.assertEqual((job["problem"], job["decision"], job["swap_to"]), ("no_video", "gap", None))

    def test_untrackable_camera_van_falls_back_to_a_tracked_camera_less_van(self):
        app = make_app(jobs=self.busy_camera_vans("V-1"))
        healthy_fleet(app, AT, skip={"T-1"})
        app.ingest({"events": power_loss("T-1", AT)})
        job = next(j for j in cover(app, AT)["jobs"] if j["job_id"] == "J-1")
        self.assertEqual((job["problem"], job["decision"]), ("no_location", "swap"))
        self.assertIn(job["swap_to"], {"V-5", "V-6", "V-11", "V-12"})
        self.assertIn("not recorded", job["manager_action"])

    def test_fresh_fault_on_a_job_hours_away_is_kept_until_a_reboot_has_failed(self):
        app = make_app(jobs=[("J-1", "V-1", AT + 6 * HOUR, AT + 7 * HOUR)])
        healthy_fleet(app, AT, skip={"C-1"})
        self.camera_fault(app)
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual((job["problem"], job["decision"], job["swap_to"]), ("no_video", "keep", None))
        self.camera_fault(app, reboot=True)           # a reboot an hour ago did not bring it back
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual(job["decision"], "swap")

    def test_sleeping_camera_and_camera_less_van_are_not_flagged(self):
        app = make_app(jobs=[("J-1", "V-1", *JOB), ("J-2", "V-5", *JOB)])
        healthy_fleet(app, AT)
        self.assertEqual(status(app, "C-1", AT)["likely_cause"], "sleeping_by_design")
        self.assertEqual(cover(app, AT)["jobs"], [])


class OutageTest(unittest.TestCase):
    """Mass silence: every Lakeline van is mid-drive and goes quiet at 07:40; Northwave vans keep reporting."""

    def setUp(self):
        jobs = [(f"J-{n}", f"V-{n}", *JOB) for n in range(1, 13)] + [("J-K1", "K-1", *JOB)]
        self.app = app = make_app(jobs=jobs)
        events = []
        for n in range(1, 13):
            stop = AT - 20 * MIN if n <= 6 else AT            # Lakeline: last heard 07:40
            events += drive(f"T-{n}", AT - HOUR, stop, f"V-{n}")
            if n <= 4 or 7 <= n <= 10:
                events += record(f"C-{n}", AT - HOUR + 30, stop)
        events += [ev("T-22", "heartbeat", AT - HOUR, {"battery_v": 12.5, "signal": 4})]
        app.ingest({"events": events})

    def test_every_silent_device_gets_the_same_cause(self):
        for n in range(1, 7):
            s = status(self.app, f"T-{n}", AT)
            self.assertEqual((s["state"], s["likely_cause"]), ("needs_action", "carrier_outage"), n)
        self.assertEqual(status(self.app, "C-1", AT)["likely_cause"], "carrier_outage")
        self.assertEqual(status(self.app, "T-7", AT)["state"], "reporting")

    def test_one_vehicle_in_a_dead_zone_is_not_an_outage(self):
        app = make_app()
        events = []
        for n in range(1, 7):
            events += drive(f"T-{n}", AT - HOUR, AT - 20 * MIN if n == 1 else AT, f"V-{n}")
        app.ingest({"events": events})
        self.assertEqual(status(app, "T-1", AT)["likely_cause"], "coverage_gap")

    def test_job_cover_moves_nothing_and_says_it_once(self):
        app = self.app
        body = cover(app, AT)
        affected = {f"V-{n}" for n in range(1, 7)}
        self.assertFalse([j for j in body["jobs"] if j["asset_id"] in affected and j["decision"] in ("swap", "gap")])
        outages = [n for n in body["notices"] if n["kind"] == "carrier_outage"]
        self.assertEqual(len(outages), 1)
        self.assertEqual((outages[0]["carrier"], outages[0]["devices_silent"]), ("Lakeline", 6))
        self.assertEqual(set(outages[0]["asset_ids"]), affected)
        self.assertTrue(outages[0]["reasons"] and outages[0]["evidence"])

    def test_a_swap_for_another_reason_avoids_vehicles_on_the_dead_carrier(self):
        app = self.app
        # K-1's tracker (Northwave) really has lost power; K-2 is healthy and free.
        app.ingest({"events": power_loss("T-21", AT)})
        jobs = {j["job_id"]: j for j in cover(app, AT)["jobs"]}
        self.assertEqual((jobs["J-K1"]["decision"], jobs["J-K1"]["swap_to"]), ("swap", "K-2"))

    def test_a_free_van_on_the_dead_carrier_is_never_offered(self):
        # Only V-7 has a job. Its tracker has really lost power. The Lakeline vans V-1..V-6 are free
        # but can't be seen during the outage, so the swap must land on a Northwave camera van.
        app = make_app(jobs=[("J-7", "V-7", *JOB)])
        events = []
        for n in range(1, 13):
            stop = AT - 20 * MIN if n <= 6 else AT
            if n != 7:
                events += drive(f"T-{n}", AT - HOUR, stop, f"V-{n}")
            if n <= 4 or 7 <= n <= 10:
                events += record(f"C-{n}", AT - HOUR + 30, stop)
        app.ingest({"events": events + power_loss("T-7", AT)})
        (job,) = cover(app, AT)["jobs"]
        self.assertEqual((job["problem"], job["decision"]), ("no_location", "swap"))
        self.assertIn(job["swap_to"], {"V-8", "V-9", "V-10"})


class ParameterTest(unittest.TestCase):
    def test_bad_parameters_are_rejected(self):
        app = make_app()
        ok = {"depot": ["MSS"], "at": [iso(AT)], "hours": ["24"]}
        app.job_cover(ok)
        for change in ({"hours": ["500"]}, {"hours": ["0"]}, {"hours": ["abc"]}, {"hours": ["2.5"]}, {"depot": ["XXX"]},
                       {"at": ["yesterday"]}, {"hours": None}, {"depot": None}, {"at": None}):
            q = {k: v for k, v in {**ok, **change}.items() if v is not None}
            with self.assertRaises(BadRequest, msg=change):
                app.job_cover(q)


if __name__ == "__main__":
    unittest.main()
