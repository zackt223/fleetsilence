"""Starts the real service with the README's start command and talks to it over HTTP."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from fleetsilence.timeutil import iso

from .helpers import HOUR, MIN, T0, drive, ev, make_data, park

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="fleetsilence-http-")
        make_data(cls.data, jobs=[("J-1", "K-1", T0 + 2 * HOUR, T0 + 4 * HOUR)])
        cls.port = free_port()
        env = dict(os.environ, QF_DATA_DIR=cls.data, QF_PORT=str(cls.port))
        cls.proc = subprocess.Popen([sys.executable, "-m", "fleetsilence"], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if cls.call("GET", "/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.2)
        cls.proc.kill()
        raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    @classmethod
    def call(cls, method, path, body=None):
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_1_health(self):
        self.assertEqual(self.call("GET", "/health"), (200, {"status": "ok"}))

    def test_2_ingest_status_and_job_cover(self):
        events = drive("T-22", T0 - 2 * HOUR, T0 - HOUR, "K-2") + park("T-22", T0 - HOUR, T0, "K-2")
        events += [ev("T-21", "heartbeat", T0 - 5 * HOUR, {"battery_v": 12.5, "signal": 4}),
                   ev("T-21", "power_disconnect", T0 - HOUR, event_id="pd"),
                   ev("T-21", "low_voltage", T0 - 50 * MIN, {"volts": 3.5, "source": "backup_battery"}, event_id="lv")]
        self.assertEqual(self.call("POST", "/ingest", {"events": events}), (200, {"accepted": len(events)}))
        self.assertEqual(self.call("POST", "/ingest", {"events": events}), (200, {"accepted": len(events)}))

        code, s = self.call("GET", f"/devices/T-21/status?at={iso(T0)}")
        self.assertEqual((code, s["state"], s["likely_cause"], s["asset_id"]), (200, "needs_action", "power_loss", "K-1"))
        self.assertTrue({"pd", "lv"} <= set(s["evidence"]))

        code, body = self.call("GET", f"/job-cover?depot=MSS&at={iso(T0)}&hours=24")
        self.assertEqual(code, 200)
        (job,) = body["jobs"]
        self.assertEqual((job["job_id"], job["problem"], job["decision"], job["swap_to"]), ("J-1", "no_location", "swap", "K-2"))

    def test_3_errors_never_crash_the_service(self):
        at = iso(T0)
        self.assertEqual(self.call("GET", f"/devices/ZZ-00000/status?at={at}")[0], 404)
        self.assertEqual(self.call("GET", "/devices/T-1/status")[0], 400)
        self.assertEqual(self.call("GET", "/devices/T-1/status?at=yesterday")[0], 400)
        self.assertEqual(self.call("GET", f"/job-cover?depot=MSS&at={at}&hours=500")[0], 400)
        self.assertEqual(self.call("GET", f"/job-cover?depot=MSS&at={at}")[0], 400)
        self.assertEqual(self.call("GET", f"/job-cover?depot=NOPE&at={at}&hours=4")[0], 400)
        self.assertEqual(self.call("POST", "/ingest", b"this is not json")[0], 400)
        self.assertEqual(self.call("POST", "/ingest", {"nope": 1})[0], 400)
        self.assertEqual(self.call("POST", "/ingest", {"events": [{}, None, {"event_id": "x"}]}), (200, {"accepted": 0}))
        self.assertEqual(self.call("GET", "/triage")[0], 404)
        self.assertEqual(self.call("GET", "/health"), (200, {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()
