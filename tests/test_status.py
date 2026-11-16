"""GET /devices/{serial}/status: normal silence versus a real problem, one device at a time."""
import unittest

from fleetsilence.server import BadRequest, NotFound
from fleetsilence.timeutil import iso

from .helpers import DAY, HOUR, MIN, T0, drive, ev, make_app, park, record, status, vin

ALLOWED_STATES = {"reporting", "quiet_expected", "needs_action", "unknown"}


def verdict(app, serial, at):
    s = status(app, serial, at)
    assert s["state"] in ALLOWED_STATES and s["reasons"] and isinstance(s["evidence"], list)
    assert 0 <= s["confidence"] <= 1
    return s["state"], s["likely_cause"]


class TrackerTest(unittest.TestCase):
    def test_driving_is_reporting_and_parked_is_quiet_as_expected(self):
        app = make_app()
        app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1") + park("T-1", T0 + HOUR, T0 + 10 * HOUR, "V-1")})
        self.assertEqual(verdict(app, "T-1", T0 + 30 * MIN), ("reporting", "none"))
        self.assertEqual(verdict(app, "T-1", T0 + 3 * HOUR), ("quiet_expected", "parked_normal"))
        # Right up to the 4 h 30 min limit a parked tracker is fine...
        self.assertEqual(verdict(app, "T-1", T0 + 9 * HOUR + 4 * HOUR + 25 * MIN), ("quiet_expected", "parked_normal"))
        # ...after it, with nothing to explain the silence, someone has to look.
        self.assertEqual(verdict(app, "T-1", T0 + 9 * HOUR + 4 * HOUR + 35 * MIN), ("needs_action", "unknown"))

    def test_power_loss_names_the_alerts_as_evidence(self):
        app = make_app()
        events = drive("T-1", T0, T0 + 40 * MIN, "V-1")
        events.append(ev("T-1", "power_disconnect", T0 + 30 * MIN, event_id="pd"))
        events.append(ev("T-1", "low_voltage", T0 + 40 * MIN, {"volts": 3.5, "source": "backup_battery"}, event_id="lv"))
        app.ingest({"events": events})
        s = status(app, "T-1", T0 + 5 * HOUR)
        self.assertEqual((s["state"], s["likely_cause"]), ("needs_action", "power_loss"))
        self.assertTrue({"pd", "lv"} <= set(s["evidence"]))
        # A disconnect with no low-voltage alert yet is not called a fault for the first half hour...
        self.assertEqual(verdict(app, "T-1", T0 + 35 * MIN), ("unknown", "power_loss"))
        # ...and a restore clears it.
        app.ingest({"events": [ev("T-1", "power_restore", T0 + 6 * HOUR), ev("T-1", "heartbeat", T0 + 6 * HOUR + 60)]})
        self.assertEqual(verdict(app, "T-1", T0 + 7 * HOUR), ("quiet_expected", "parked_normal"))

    def test_short_mid_drive_silence_is_a_coverage_gap_then_a_problem(self):
        app = make_app()
        app.ingest({"events": drive("T-1", T0, T0 + 20 * MIN, "V-1")})
        self.assertEqual(verdict(app, "T-1", T0 + 60 * MIN), ("unknown", "coverage_gap"))
        self.assertEqual(verdict(app, "T-1", T0 + 3 * HOUR), ("needs_action", "unknown"))

    def test_swapped_unit_is_recognised_by_the_vin_it_reports(self):
        app = make_app()
        events = drive("T-1", T0, T0 + HOUR, "V-1")                      # the recorded tracker stops at 09:00
        events += drive("T-99", T0 + HOUR + 2 * MIN, T0 + 2 * HOUR)      # a spare starts from the same spot
        events += park("T-99", T0 + 2 * HOUR, T0 + 12 * HOUR, "V-1")     # and later reports V-1's VIN
        app.ingest({"events": events})
        for at in (T0 + 90 * MIN, T0 + 8 * HOUR):                        # before and after the VIN is known
            old = status(app, "T-1", at)
            self.assertEqual((old["state"], old["likely_cause"]), ("needs_action", "device_swapped"), at)
            new = status(app, "T-99", at)
            self.assertEqual(new["asset_id"], "V-1")
            self.assertIn(new["state"], ("reporting", "quiet_expected"))

    def test_spare_that_never_reported_is_unknown(self):
        app = make_app()
        s = status(app, "T-99", T0)
        self.assertEqual((s["state"], s["likely_cause"], s["last_heard_at"], s["asset_id"]), ("unknown", "unknown", None, None))


class CameraTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1") + record("C-1", T0 + 30, T0 + HOUR)})

    def test_recording_then_asleep_after_ignition_off(self):
        app = self.app
        self.assertEqual(verdict(app, "C-1", T0 + 30 * MIN), ("reporting", "none"))
        app.ingest({"events": [ev("T-1", "ignition_off", T0 + HOUR + 60),
                               ev("C-1", "camera_status", T0 + HOUR + 16 * MIN, {"status": "sleeping"})]})
        self.assertEqual(verdict(app, "C-1", T0 + 5 * HOUR), ("quiet_expected", "sleeping_by_design"))

    def test_asleep_even_if_the_sleeping_message_never_arrives(self):
        self.app.ingest({"events": [ev("T-1", "ignition_off", T0 + HOUR + 60)]})
        self.assertEqual(verdict(self.app, "C-1", T0 + 5 * HOUR), ("quiet_expected", "sleeping_by_design"))

    def test_storage_error_is_a_fault_until_recording_resumes(self):
        app = self.app
        app.ingest({"events": drive("T-1", T0 + HOUR, T0 + 3 * HOUR)[1:] + [
            ev("C-1", "camera_status", T0 + 62 * MIN, {"status": "storage_error", "code": "SD_WRITE_FAIL"}, event_id="err")]})
        s = status(app, "C-1", T0 + 100 * MIN)
        self.assertEqual((s["state"], s["likely_cause"]), ("needs_action", "camera_fault"))
        self.assertIn("err", s["evidence"])
        app.ingest({"events": [ev("C-1", "command", T0 + 110 * MIN, {"command": "reboot", "issued_by": "support"}, event_id="rb")]
                    + record("C-1", T0 + 111 * MIN, T0 + 2 * HOUR)})
        self.assertEqual(verdict(app, "C-1", T0 + 2 * HOUR), ("reporting", "none"))
        # The question "as of 09:40" still gets the 09:40 answer after later events have arrived.
        self.assertEqual(verdict(app, "C-1", T0 + 100 * MIN), ("needs_action", "camera_fault"))

    def test_silent_while_the_van_keeps_driving_is_a_fault(self):
        self.app.ingest({"events": drive("T-1", T0 + HOUR, T0 + 2 * HOUR)[1:]})
        self.assertEqual(verdict(self.app, "C-1", T0 + 2 * HOUR), ("needs_action", "camera_fault"))

    def test_paused_plan(self):
        self.app.ingest({"events": [ev("C-1", "plan_change", T0 + 30 * MIN,
                                       {"plan": "video", "status": "paused", "reason": "data_limit"}, event_id="pc")]})
        s = status(self.app, "C-1", T0 + 2 * HOUR)
        self.assertEqual((s["state"], s["likely_cause"]), ("needs_action", "plan_paused"))
        self.assertIn("pc", s["evidence"])
        self.app.ingest({"events": [ev("C-1", "plan_change", T0 + 3 * HOUR, {"plan": "video", "status": "active", "reason": "limit_reset"}),
                                    ev("T-1", "ignition_off", T0 + HOUR + 60)]})
        self.assertEqual(verdict(self.app, "C-1", T0 + 4 * HOUR), ("quiet_expected", "sleeping_by_design"))


class BatteryTagTest(unittest.TestCase):
    def checkin(self, t, pct):
        return ev("A-1", "check_in", t, {"battery_pct": pct, "lat": 50.4, "lng": -104.6, "temp_c": -5})

    def test_daily_check_in_on_time(self):
        app = make_app()
        app.ingest({"events": [self.checkin(T0, 60)]})
        self.assertEqual(verdict(app, "A-1", T0 + 20 * HOUR), ("reporting", "none"))

    def test_missed_check_in_after_a_cold_night_is_expected(self):
        app = make_app(weather=[("2026-10-21", "REG", -12), ("2026-10-22", "REG", -3)])
        app.ingest({"events": [self.checkin(T0, 60)]})
        self.assertEqual(verdict(app, "A-1", T0 + DAY + 3 * HOUR), ("quiet_expected", "cold_weather"))
        # Still silent after a mild night: the cold no longer explains it.
        self.assertEqual(verdict(app, "A-1", T0 + 2 * DAY + 3 * HOUR), ("needs_action", "unknown"))

    def test_missed_check_in_on_a_mild_night_needs_action(self):
        app = make_app(weather=[("2026-10-21", "REG", 1)])
        app.ingest({"events": [self.checkin(T0, 60)]})
        self.assertEqual(verdict(app, "A-1", T0 + DAY + 3 * HOUR), ("needs_action", "unknown"))

    def test_low_battery_wins_over_cold(self):
        app = make_app(weather=[("2026-10-21", "REG", -15)])
        app.ingest({"events": [self.checkin(T0, 9)]})
        self.assertEqual(verdict(app, "A-1", T0 + DAY + 3 * HOUR), ("needs_action", "battery_low"))


class ContractTest(unittest.TestCase):
    def test_response_shape_matches_the_contract(self):
        app = make_app()
        app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1")})
        s = status(app, "T-1", T0 + 30 * MIN)
        self.assertEqual(set(s), {"serial", "asset_id", "at", "state", "likely_cause", "confidence",
                                  "last_heard_at", "reasons", "evidence"})
        self.assertEqual((s["serial"], s["asset_id"], s["at"]), ("T-1", "V-1", iso(T0 + 30 * MIN)))
        self.assertRegex(s["last_heard_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_unknown_serial_is_404_and_bad_at_is_400(self):
        app = make_app()
        with self.assertRaises(NotFound):
            status(app, "ZZ-00000", T0)
        for bad in ({}, {"at": [""]}, {"at": ["yesterday"]}, {"at": ["12"]}):
            with self.assertRaises(BadRequest):
                app.device_status("T-1", bad)

    def test_only_events_received_by_at_count(self):
        app = make_app()
        app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1")})
        s = status(app, "T-1", T0 - HOUR)
        self.assertEqual((s["state"], s["last_heard_at"]), ("unknown", None))


if __name__ == "__main__":
    unittest.main()
