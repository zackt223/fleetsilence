"""POST /ingest: duplicates, late and out-of-order messages, and bad input."""
import copy
import random
import unittest

from fleetsilence.server import BadRequest
from fleetsilence.timeutil import iso

from .helpers import HOUR, MIN, T0, cover, drive, ev, make_app, park, record, status


def story():
    """V-1 drives 08:00-09:00, parks; its camera records, then sleeps."""
    events = drive("T-1", T0, T0 + HOUR, "V-1") + park("T-1", T0 + HOUR, T0 + 9 * HOUR, "V-1")
    events += record("C-1", T0 + 30, T0 + HOUR)
    events.append(ev("C-1", "camera_status", T0 + HOUR + 15 * MIN, {"status": "sleeping"}))
    return events


def answers(app):
    return [status(app, s, at) for s in ("T-1", "C-1") for at in (T0 + 30 * MIN, T0 + 2 * HOUR, T0 + 9 * HOUR)]


class DuplicateTest(unittest.TestCase):
    def test_same_batch_twice_changes_nothing(self):
        events = story()
        once, twice = make_app(), make_app()
        once.ingest({"events": events})
        twice.ingest({"events": events})
        self.assertEqual(twice.ingest({"events": events}), {"accepted": len(events)})
        self.assertEqual(answers(once), answers(twice))
        self.assertEqual(twice.store.stats["stored"], len(events))

    def test_a_retry_with_a_new_event_id_is_the_same_event(self):
        app = make_app()
        events = story()
        app.ingest({"events": events})
        before = answers(app)
        retries = copy.deepcopy(events)
        for i, e in enumerate(retries):
            e["event_id"] = f"retry-{i}"
        app.ingest({"events": retries})
        self.assertEqual(app.store.stats["stored"], len(events))
        after = answers(app)
        for b, a in zip(before, after):
            # Same verdicts; the evidence may now also name the retry ids, never fewer.
            self.assertEqual({k: v for k, v in b.items() if k != "evidence"}, {k: v for k, v in a.items() if k != "evidence"})
            self.assertTrue(set(b["evidence"]) <= set(a["evidence"]))

    def test_either_id_of_a_duplicated_event_appears_as_evidence(self):
        app = make_app()
        fault = ev("C-1", "camera_status", T0 + 20 * MIN, {"status": "storage_error"}, event_id="orig")
        again = dict(fault, event_id="copy", received_at=iso(T0 + 21 * MIN))
        app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1") + record("C-1", T0, T0 + 15 * MIN) + [fault, again]})
        self.assertEqual(set(status(app, "C-1", T0 + HOUR)["evidence"]) & {"orig", "copy"}, {"orig", "copy"})
        # Asked about a moment before the retry arrived, only the original id is known.
        self.assertNotIn("copy", status(app, "C-1", T0 + 20 * MIN + 30)["evidence"])


class OrderTest(unittest.TestCase):
    def test_shuffled_batches_give_the_same_answers(self):
        events = story()
        ordered, shuffled = make_app(), make_app()
        ordered.ingest({"events": events})
        mixed = events[:]
        random.Random(7).shuffle(mixed)
        for i in range(0, len(mixed), 9):
            shuffled.ingest({"events": mixed[i:i + 9]})
        self.assertEqual(answers(ordered), answers(shuffled))

    def test_late_events_are_ignored_until_they_have_arrived(self):
        """A van in a dead zone: positions for 08:20-09:00 only reach the platform at 09:01."""
        app = make_app()
        live = drive("T-1", T0, T0 + 20 * MIN, "V-1")
        buffered = [e for e in drive("T-1", T0, T0 + HOUR) if e["device_time"] > live[-1]["device_time"]]
        for e in buffered:
            e["received_at"] = iso(T0 + 61 * MIN)
        app.ingest({"events": live + buffered})
        during = status(app, "T-1", T0 + 50 * MIN)      # the burst has not arrived yet at 08:50
        self.assertEqual((during["state"], during["likely_cause"]), ("unknown", "coverage_gap"))
        self.assertEqual(during["last_heard_at"], live[-1]["device_time"])
        after = status(app, "T-1", T0 + 62 * MIN)
        self.assertEqual((after["state"], after["likely_cause"]), ("reporting", "none"))

    def test_an_old_event_arriving_late_does_not_overwrite_newer_state(self):
        app = make_app()
        app.ingest({"events": drive("T-1", T0, T0 + HOUR, "V-1") + [ev("T-1", "ignition_off", T0 + HOUR + 60)]})
        stale = ev("T-1", "position", T0 + 10 * MIN + 1, {"lat": 43.6, "lng": -79.6, "speed_kmh": 30, "ignition": True},
                   received=T0 + HOUR + 5 * MIN)
        app.ingest({"events": [stale]})
        s = status(app, "T-1", T0 + HOUR + 10 * MIN)
        self.assertEqual((s["state"], s["likely_cause"]), ("quiet_expected", "parked_normal"))


class BadInputTest(unittest.TestCase):
    def test_one_bad_event_never_fails_the_batch(self):
        app = make_app()
        good = drive("T-1", T0, T0 + 10 * MIN, "V-1")
        junk = [{}, None, 7, "x", {"event_id": "a"}, {"event_id": "b", "type": "position", "serial": "T-1",
                                                      "device_time": 12, "received_at": "not a time", "payload": None},
                {"event_id": "c", "type": "mystery", "serial": "T-1", "device_time": iso(T0), "received_at": iso(T0),
                 "payload": {"x": 1}},
                {"event_id": "d", "type": "position", "serial": "T-1", "device_time": iso(T0 + 4 * MIN + 30),
                 "received_at": iso(T0 + 4 * MIN + 31), "payload": "oops"}]
        got = app.ingest({"events": junk + good})
        self.assertEqual(got["accepted"], len(good) + 2)     # the unknown type and the payload-less position are kept
        self.assertEqual(status(app, "T-1", T0 + 10 * MIN)["state"], "reporting")

    def test_body_must_have_an_events_list(self):
        app = make_app()
        for body in (None, [], {}, {"events": "nope"}):
            with self.assertRaises(BadRequest):
                app.ingest(body)

    def test_device_time_as_epoch_number_or_nonsense(self):
        app = make_app(weather=[("2026-10-21", "REG", 2)])
        e = ev("A-1", "check_in", T0, {"battery_pct": 60, "lat": 50.4, "lng": -104.6, "temp_c": 3})
        e["device_time"] = T0                       # some firmware sends Unix seconds
        app.ingest({"events": [e]})
        self.assertEqual(status(app, "A-1", T0 + HOUR)["last_heard_at"], iso(T0))
        # A clock that is wildly wrong is not believed: the event is placed at the time it arrived.
        wrong = ev("A-1", "check_in", T0 + 24 * HOUR, {"battery_pct": 59})
        wrong["device_time"] = "1999-01-01T00:00:00Z"
        app.ingest({"events": [wrong]})
        s = status(app, "A-1", T0 + 25 * HOUR)
        self.assertEqual(s["state"], "reporting")
        self.assertEqual(s["last_heard_at"], wrong["received_at"])

    def test_events_for_job_cover_survive_a_messy_stream(self):
        app = make_app(jobs=[("J-1", "V-1", T0 + 2 * HOUR, T0 + 3 * HOUR)])
        app.ingest({"events": [{"event_id": "x"}] + story()})
        self.assertEqual(cover(app, T0 + 90 * MIN)["jobs"], [])


if __name__ == "__main__":
    unittest.main()
