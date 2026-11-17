"""The service runs with no network and on a data pack it has never seen.

The service is started with the README's start command inside a Python audit hook that blocks, and
reports, any attempt to resolve a name or open a connection to anything but this machine. A generated
pack (tools/make_test_pack.py: other depots, ids, carriers, dates across a daylight-saving change, messy
files and events) is streamed to it, and six checkpoints are checked the way the exam tool checks them.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .test_http import ROOT, free_port

GUARD = r"""
import runpy, sys
LOCAL = {"127.0.0.1", "::1", "localhost", "", None, b"127.0.0.1", b"::1", b"localhost"}
def host_of(event, args):
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        return args[0]
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.connect_ex"):
        addr = args[1]
        return addr[0] if isinstance(addr, tuple) else addr
    if event in ("http.client.connect", "urllib.Request", "ftplib.connect", "smtplib.connect"):
        return "<outbound client>"
    return None
def hook(event, args):
    if not (event.startswith("socket.") or event in ("http.client.connect", "urllib.Request",
                                                     "ftplib.connect", "smtplib.connect")):
        return
    h = host_of(event, args)
    if h not in LOCAL and h is not None or h == "<outbound client>":
        sys.stderr.write(f"NETWORK BLOCKED: {event} {h!r}\n")
        sys.stderr.flush()
        raise PermissionError(f"network access blocked in test: {event} {h!r}")
sys.addaudithook(hook)
sys.argv = ["fleetsilence"]
runpy.run_module("fleetsilence", run_name="__main__")
"""


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Service:
    """The real service in a subprocess, behind the network guard."""

    def __init__(self, data_dir):
        self.port = free_port()
        self.log = tempfile.NamedTemporaryFile(prefix="fleetsilence-", suffix=".log", delete=False)
        env = dict(os.environ, QF_DATA_DIR=data_dir, QF_PORT=str(self.port))
        self.proc = subprocess.Popen([sys.executable, "-c", GUARD], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if self.call("GET", "/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("service did not start:\n" + self.stderr())

    def call(self, method, path, body=None):
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def stderr(self):
        self.log.flush()
        with open(self.log.name, encoding="utf-8", errors="replace") as f:
            return f.read()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class OtherPackOfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = tempfile.mkdtemp(prefix="fleetsilence-pack-")
        subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tools", "make_test_pack.py"), cls.pack],
                       check=True, stdout=subprocess.DEVNULL)
        cls.data = os.path.join(cls.pack, "data")
        with open(os.path.join(cls.pack, "scenario.json"), encoding="utf-8") as f:
            cls.scenario = json.load(f)
        import csv
        with open(os.path.join(cls.data, "assets.csv"), newline="", encoding="utf-8-sig") as f:
            cls.assets = {r["asset_id"]: r for r in csv.DictReader(f)}
        with open(os.path.join(cls.data, "schedule.csv"), newline="", encoding="utf-8-sig") as f:
            cls.jobs = {r["job_id"]: r for r in csv.DictReader(f)}
        cls.svc = Service(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.svc.stop()

    def events(self):
        tel = os.path.join(self.data, "telemetry")
        for name in sorted(os.listdir(tel)):
            with open(os.path.join(tel, name), encoding="utf-8") as f:
                for line in f:
                    yield json.loads(line)

    def validate_cover(self, body, q, at, unavailable):
        """The exam tool's rules for a job-cover answer."""
        errs = []
        end = at + timedelta(hours=q["hours"])
        booked = {}
        for j in self.jobs.values():
            booked.setdefault(j["asset_id"], []).append((parse(j["start"]), parse(j["end"]), j["job_id"]))
        for it in body["jobs"]:
            j = self.jobs.get(it["job_id"])
            if not j or j["depot"] != q["depot"] or not at <= parse(j["start"]) < end:
                errs.append(f"{it['job_id']} is not a job at {q['depot']} starting in the window")
                continue
            if it["decision"] not in ("swap", "keep", "gap") or it["problem"] not in ("no_location", "no_video", "both"):
                errs.append(f"{it['job_id']}: bad decision or problem")
            if it["decision"] == "swap":
                to, a0 = self.assets.get(it["swap_to"]), self.assets[j["asset_id"]]
                if not to or (to["type"], to["depot"]) != (a0["type"], a0["depot"]):
                    errs.append(f"{it['job_id']}: swap_to {it['swap_to']} is not the same type and depot")
                if it["swap_to"] in unavailable:
                    errs.append(f"{it['job_id']}: swap_to {it['swap_to']} can't be seen or recorded")
                s, e = parse(j["start"]), parse(j["end"])
                for bs, be, other in booked.get(it["swap_to"], []):
                    if bs < e and s < be and other != it["job_id"]:
                        errs.append(f"{it['job_id']}: swap_to {it['swap_to']} is busy with {other}")
                booked.setdefault(it["swap_to"], []).append((s, e, it["job_id"]))
            elif it["decision"] == "gap" and it["swap_to"] is not None:
                errs.append(f"{it['job_id']}: a gap should have swap_to null")
        return errs

    def check(self, cp):
        at = parse(cp["at"])
        unavailable = set(cp.get("unavailable_assets", []))
        fails = []
        for c in cp["checks"]:
            if c["type"] == "status":
                code, s = self.svc.call("GET", f"/devices/{c['serial']}/status?at={cp['at']}")
                if code != 200 or s["state"] not in c["state"] or s["likely_cause"] not in c["cause"]:
                    fails.append(f"{cp['id']} {c['label']}: got {code} {s.get('state')}/{s.get('likely_cause')}")
                elif c.get("evidence_any") and not set(c["evidence_any"]) & set(s["evidence"]):
                    fails.append(f"{cp['id']} {c['label']}: evidence {s['evidence']} lacks {c['evidence_any']}")
                continue
            q = c["query"]
            code, body = self.svc.call("GET", f"/job-cover?depot={q['depot']}&at={cp['at']}&hours={q['hours']}")
            if code != 200:
                fails.append(f"{cp['id']} {c['label']}: job-cover returned {code}")
                continue
            fails += [f"{cp['id']} {c['label']}: {e}" for e in self.validate_cover(body, q, at, unavailable)]
            jobs = body["jobs"]
            if c["type"] == "job":
                found = [j for j in jobs if j["job_id"] == c["job_id"]]
                if not found or found[0]["problem"] not in c["problem"] or found[0]["decision"] not in c["decision"]:
                    fails.append(f"{cp['id']} {c['label']}: got {found[:1]}")
            elif c["type"] == "jobs_exclude_assets":
                hit = [j["job_id"] for j in jobs if j["asset_id"] in c["asset_ids"]]
                if hit:
                    fails.append(f"{cp['id']} {c['label']}: listed {hit}")
            elif c["type"] == "jobs_no_swap_or_gap":
                hit = [j["job_id"] for j in jobs if j["asset_id"] in c["asset_ids"] and j["decision"] in ("swap", "gap")]
                if hit:
                    fails.append(f"{cp['id']} {c['label']}: swapped or gapped {hit}")
        return fails

    def test_the_whole_generated_pack_offline(self):
        cps = sorted(self.scenario["checkpoints"], key=lambda c: c["at"])
        fails, batch, i = [], [], 0
        sent = 0

        def flush():
            nonlocal sent
            if batch:
                code, body = self.svc.call("POST", "/ingest", {"events": batch})
                self.assertEqual(code, 200, body)
                sent += len(batch)
                batch.clear()

        for e in self.events():
            while i < len(cps) and e.get("received_at", "") > cps[i]["at"]:
                flush()
                fails += self.check(cps[i])
                i += 1
            batch.append(e)
            if len(batch) >= 5000:
                flush()
        flush()
        for cp in cps[i:]:
            fails += self.check(cp)

        # error handling on this pack too
        at = cps[0]["at"]
        self.assertEqual(self.svc.call("GET", f"/job-cover?depot=NOPE&at={at}&hours=4")[0], 400)
        self.assertEqual(self.svc.call("GET", f"/devices/T-00000/status?at={at}")[0], 404)
        self.assertEqual(self.svc.call("POST", "/ingest", b"not json")[0], 400)
        self.assertEqual(self.svc.call("GET", "/health"), (200, {"status": "ok"}))

        log = self.svc.stderr()
        self.assertNotIn("NETWORK BLOCKED", log)
        self.assertNotIn("Traceback", log)
        # it found both new depots' time zones from the schedule, daylight-saving change included
        self.assertIn("depot YYC in settings.toml; estimated UTC-7/-6", log)
        self.assertIn("depot HFX in settings.toml; estimated UTC-4/-3", log)
        self.assertGreater(sent, 30000)
        self.assertEqual(fails, [], "\n".join(fails))


class GuardWorksTest(unittest.TestCase):
    def test_the_guard_really_blocks_an_outbound_call(self):
        code = GUARD.replace('runpy.run_module("fleetsilence", run_name="__main__")',
                             'import socket\nsocket.getaddrinfo("example.com", 443)')
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NETWORK BLOCKED: socket.getaddrinfo 'example.com'", r.stderr)


if __name__ == "__main__":
    unittest.main()
