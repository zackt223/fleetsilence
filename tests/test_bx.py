"""The BX decoder, checked against the vendor portal's three readouts from the data pack."""
import unittest

from fleetsilence import bx
from fleetsilence.timeutil import Clock, iso, parse_ts

from .helpers import make_app, status

# (raw message, what the portal shows). Portal times are Regina local time (UTC-6).
PORTAL = [
    ('{"u":"BX40471","t":"6ADA16E0","b":3094,"s":8,"p":"5042719,-10456695"}',
     ("BX40471", "2026-10-22 08:00:00", 76, False, False, True, 50.42719, -104.56695)),
    ('{"u":"BX41085","t":"6AD8D76C","b":2583,"s":9,"p":"5043083,-10466817"}',
     ("BX41085", "2026-10-21 09:17:00", 63, True, False, True, 50.43083, -104.66817)),
    ('{"u":"BX47068","t":"6AE4B4B0","b":573,"s":10,"p":"5046073,-10467404"}',
     ("BX47068", "2026-10-30 09:16:00", 14, False, True, True, 50.46073, -104.67404)),
]


class BxDecoderTest(unittest.TestCase):
    def test_matches_the_three_portal_readouts(self):
        clock = Clock({"REG": {"utc_offset_h": -6, "observes_dst": False}})
        for raw, (unit, local, battery, moving, low, fix, lat, lng) in PORTAL:
            with self.subTest(unit=unit):
                d = bx.decode(raw)
                self.assertEqual(d["serial"], unit)
                self.assertEqual(clock.local("REG", d["device_time"]).strftime("%Y-%m-%d %H:%M:%S"), local)
                self.assertEqual(d["battery_pct"], battery)
                self.assertEqual((d["moving"], d["low_battery"], d["gps_fix"]), (moving, low, fix))
                self.assertAlmostEqual(d["lat"], lat, places=5)
                self.assertAlmostEqual(d["lng"], lng, places=5)

    def test_no_gps_fix_and_combined_flags(self):
        d = bx.decode('{"u":"BX1","t":"6AD21EA4","b":4095,"s":0,"p":"5042293,-10462700"}')
        self.assertEqual((d["moving"], d["low_battery"], d["gps_fix"], d["battery_pct"]), (False, False, False, 100))
        d = bx.decode('{"u":"BX1","t":"6AD21EA4","b":400,"s":11,"p":"5042293,-10462700"}')
        self.assertEqual((d["moving"], d["low_battery"], d["gps_fix"]), (True, True, True))

    def test_unreadable_messages_raise_a_clear_error(self):
        for raw in ("", "not json", "[]", '{"t":"6AD21EA4"}', '{"u":"BX1","t":"zz"}', '{"u":"BX1"}', None, 5):
            with self.subTest(raw=raw):
                with self.assertRaises(bx.BxDecodeError):
                    bx.decode(raw)

    def test_missing_optional_fields_are_tolerated(self):
        d = bx.decode('{"u":"BX1","t":"6AD21EA4"}')
        self.assertIsNone(d["battery_pct"])
        self.assertIsNone(d["lat"])


class BxThroughTheServiceTest(unittest.TestCase):
    def test_bx_raw_events_are_read_like_any_other_tracker(self):
        app = make_app()
        t = parse_ts("2026-10-22T14:00:00Z")
        raw = '{"u":"BX00002","t":"%X","b":3094,"s":8,"p":"5042719,-10456695"}' % t
        got = app.ingest({"events": [{"event_id": "bx1", "type": "bx_raw", "received_at": iso(t + 5), "raw": raw}]})
        self.assertEqual(got, {"accepted": 1})
        s = status(app, "BX00002", t + 3600)
        self.assertEqual((s["state"], s["likely_cause"], s["asset_id"]), ("reporting", "none", "TR-2"))
        self.assertEqual(s["last_heard_at"], iso(t))
        self.assertIn("bx1", s["evidence"])

    def test_low_battery_flag_needs_action(self):
        app = make_app()
        t = parse_ts("2026-10-22T14:00:00Z")
        raw = '{"u":"BX00002","t":"%X","b":573,"s":10,"p":"5042719,-10456695"}' % t
        app.ingest({"events": [{"event_id": "bx1", "type": "bx_raw", "received_at": iso(t + 5), "raw": raw}]})
        s = status(app, "BX00002", t + 3600)
        self.assertEqual((s["state"], s["likely_cause"]), ("needs_action", "battery_low"))

    def test_a_garbled_bx_message_does_not_fail_the_batch(self):
        app = make_app()
        t = parse_ts("2026-10-22T14:00:00Z")
        good = '{"u":"BX00002","t":"%X","b":3094,"s":8,"p":"5042719,-10456695"}' % t
        got = app.ingest({"events": [
            {"event_id": "bad", "type": "bx_raw", "received_at": iso(t), "raw": "\\x00garbage"},
            {"event_id": "ok", "type": "bx_raw", "received_at": iso(t + 5), "raw": good}]})
        self.assertEqual(got, {"accepted": 1})
        self.assertEqual(status(app, "BX00002", t + 60)["state"], "reporting")


if __name__ == "__main__":
    unittest.main()
