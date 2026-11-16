"""Reference files (assets, devices, schedule, weather, plan changes).

Read once at startup from QF_DATA_DIR (default ./data). A missing or broken
file never stops the service: it starts with whatever it could read.
"""
import bisect
import csv
import os
import sys
from collections import defaultdict

from .timeutil import parse_ts

VEHICLE_TYPES = ("van", "pickup")


def _rows(folder, name):
    path = os.path.join(folder, name)
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        print(f"[fleetsilence] warning: could not read {path}: {exc}", file=sys.stderr)
        return []


class Job:
    __slots__ = ("job_id", "asset_id", "depot", "start", "end", "job_type")

    def __init__(self, job_id, asset_id, depot, start, end, job_type):
        self.job_id, self.asset_id, self.depot = job_id, asset_id, depot
        self.start, self.end, self.job_type = start, end, job_type


class RefData:
    def __init__(self, folder):
        self.folder = folder
        self.assets = {}                       # asset_id -> row
        self.devices = {}                      # serial -> row
        self.asset_devices = defaultdict(list)  # asset_id -> [device row] as the records say
        self.vin_asset = {}                    # vin -> asset_id
        self.jobs = []                         # all jobs, sorted by start
        self.jobs_by_depot = defaultdict(list)
        self.jobs_by_asset = defaultdict(list)
        self.weather = {}                      # (depot, 'YYYY-MM-DD' local) -> low_c
        self.plan_changes = []                 # rows, replayed as events at startup
        self.depots = set()
        self._load()

    def _load(self):
        for r in _rows(self.folder, "assets.csv"):
            if r.get("asset_id"):
                self.assets[r["asset_id"]] = r
                if r.get("vin"):
                    self.vin_asset[r["vin"]] = r["asset_id"]
                if r.get("depot"):
                    self.depots.add(r["depot"])
        for r in _rows(self.folder, "devices.csv"):
            if r.get("serial"):
                self.devices[r["serial"]] = r
                if r.get("asset_id"):
                    self.asset_devices[r["asset_id"]].append(r)
        for r in _rows(self.folder, "schedule.csv"):
            s, e = parse_ts(r.get("start")), parse_ts(r.get("end"))
            if not r.get("job_id") or s is None or e is None or e <= s:
                continue
            j = Job(r["job_id"], r.get("asset_id", ""), r.get("depot", ""), s, e, r.get("job_type", ""))
            self.jobs.append(j)
        self.jobs.sort(key=lambda j: (j.start, j.job_id))
        self.longest_job = max((j.end - j.start for j in self.jobs), default=0)
        for j in self.jobs:
            self.jobs_by_depot[j.depot].append(j)
            self.jobs_by_asset[j.asset_id].append(j)
            if j.depot:
                self.depots.add(j.depot)
        for r in _rows(self.folder, "weather.csv"):
            try:
                self.weather[(r["depot"], r["date"])] = float(r["low_c"])
            except (KeyError, ValueError):
                continue
        self.plan_changes = _rows(self.folder, "plan_changes.csv")

    # ------------------------------------------------------------------ lookups
    def depot_of(self, asset_id):
        return (self.assets.get(asset_id) or {}).get("depot", "")

    def jobs_starting(self, depot, start, end):
        """Jobs at the depot with start in [start, end)."""
        jobs = self.jobs_by_depot.get(depot, [])
        lo = bisect.bisect_left(jobs, start, key=lambda j: j.start)
        hi = bisect.bisect_left(jobs, end, key=lambda j: j.start)
        return jobs[lo:hi]

    def jobs_under_way(self, depot, at):
        """Jobs at the depot that started before `at` and have not ended."""
        jobs = self.jobs_by_depot.get(depot, [])
        hi = bisect.bisect_left(jobs, at, key=lambda j: j.start)
        lo = bisect.bisect_left(jobs, at - self.longest_job, key=lambda j: j.start)
        return [j for j in jobs[lo:hi] if j.end > at]

    def busy(self, asset_id, start, end, ignore_job=None):
        """The first scheduled job on the asset overlapping [start, end), or None."""
        for j in self.jobs_by_asset.get(asset_id, []):
            if j.start >= end:
                break
            if j.end > start and j.job_id != ignore_job:
                return j
        return None
