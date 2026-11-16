"""A small made-up fleet for tests, so they run without the take-home data pack.

Mississauga (MSS): vans V-1..V-12, each with tracker T-<n>. V-1..V-6 are on
Lakeline, V-7..V-12 on Northwave. V-1..V-4 and V-7..V-10 also carry a camera
C-<n> on the same carrier. Pickups K-1 and K-2 (trackers T-21, T-22).
Regina (REG): trailer TR-1 with battery tag A-1, trailer TR-2 with BX tracker
BX00002, pickup K-3 (T-23). T-99 is a spare in stock.
"""
import csv
import itertools
import os
import tempfile

from fleetsilence import config
from fleetsilence.server import App
from fleetsilence.timeutil import iso, parse_ts

T0 = parse_ts("2026-10-20T12:00:00Z")      # a Tuesday, 08:00 in Mississauga
MIN, HOUR, DAY = 60, 3600, 86400
_ids = itertools.count(1)


def vin(asset_id):
    return "VIN" + asset_id.replace("-", "").rjust(14, "0")


def make_data(folder, jobs=(), weather=()):
    assets, devices = [], []
    for n in range(1, 13):
        carrier = "Lakeline" if n <= 6 else "Northwave"
        assets.append((f"V-{n}", "van", "MSS", "service"))
        devices.append((f"T-{n}", "T", carrier, f"V-{n}", "active"))
        if n <= 4 or 7 <= n <= 10:
            devices.append((f"C-{n}", "C", carrier, f"V-{n}", "active"))
    for n, depot in ((1, "MSS"), (2, "MSS"), (3, "REG")):
        assets.append((f"K-{n}", "pickup", depot, "service"))
        devices.append((f"T-2{n}", "T", "Northwave", f"K-{n}", "active"))
    assets += [("TR-1", "trailer", "REG", "snow"), ("TR-2", "trailer", "REG", "snow")]
    devices += [("A-1", "A", "Northwave", "TR-1", "active"), ("BX00002", "BX", "Northwave", "TR-2", "active"),
                ("T-99", "T", "Northwave", "", "stock")]

    def write(name, header, rows):
        with open(os.path.join(folder, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write("assets.csv", ["asset_id", "type", "depot", "use", "make_model", "year", "vin", "status"],
          [(a, t, d, u, "Test", 2024, vin(a), "active") for a, t, d, u in assets])
    write("devices.csv", ["serial", "family", "model", "firmware", "carrier", "asset_id", "status", "installed_at"],
          [(s, f, "Test", "1.0", c, a, st, "2025-01-01") for s, f, c, a, st in devices])
    write("schedule.csv", ["job_id", "asset_id", "depot", "start", "end", "job_type"],
          [(j, a, "REG" if a == "K-3" else "MSS", iso(s), iso(e), "repair") for j, a, s, e in jobs])
    write("weather.csv", ["date", "depot", "low_c", "high_c"], [(d, dep, low, low + 8) for d, dep, low in weather])
    write("plan_changes.csv", ["serial", "time", "plan", "status", "reason", "event_id"], [])
    write("support_cases.csv", ["case_id", "opened_at", "closed_at", "depot", "asset_id", "serial", "reported_by",
                                "symptom", "root_cause", "action", "outcome", "notes"], [])


def make_app(jobs=(), weather=()):
    folder = tempfile.mkdtemp(prefix="fleetsilence-test-")
    make_data(folder, jobs, weather)
    return App(folder, config.load())


def ev(serial, type_, t, payload=None, lag=2, event_id=None, received=None):
    """One event as the platform would send it. `lag` = seconds between device time and receipt."""
    return {"event_id": event_id or f"e{next(_ids):06d}", "serial": serial, "type": type_,
            "device_time": iso(t), "received_at": iso(received if received is not None else t + lag),
            "payload": payload if payload is not None else {}}


def drive(serial, start, end, asset=None, lag=2):
    """Ignition on at `start`, then a position every 2 minutes up to `end` (no ignition off)."""
    out = [ev(serial, "ignition_on", start, {"vin": vin(asset)} if asset else {}, lag)]
    t = start + 30
    while t <= end:
        out.append(ev(serial, "position", t, {"lat": 43.6, "lng": -79.6, "speed_kmh": 40, "ignition": True}, lag))
        t += 2 * MIN
    return out


def park(serial, off, until, asset=None):
    """Ignition off at `off`, then a heartbeat every 4 hours up to `until`."""
    out = [ev(serial, "ignition_off", off)]
    t = off + 4 * HOUR
    while t <= until:
        out.append(ev(serial, "heartbeat", t, {"battery_v": 12.5, "signal": 4, "vin": vin(asset) if asset else None}))
        t += 4 * HOUR
    return out


def record(serial, start, end):
    """Camera boots at `start` and reports 'recording' every 5 minutes up to `end`."""
    out = [ev(serial, "camera_status", start, {"status": "booting"})]
    t = start + 60
    while t <= end:
        out.append(ev(serial, "camera_status", t, {"status": "recording"}))
        t += 5 * MIN
    return out


def healthy_fleet(app, at, skip=()):
    """Every MSS vehicle parked since 05:00 with heartbeats on time and its camera asleep."""
    events = []
    off = at - 7 * HOUR
    for row in list(app.ref.devices.values()):
        s, fam, asset = row["serial"], row["family"], row["asset_id"]
        if s in skip or not asset or app.ref.depot_of(asset) != "MSS":
            continue
        if fam == "T":
            events += park(s, off, at, asset)
        elif fam == "C":
            events.append(ev(s, "camera_status", off + 15 * MIN, {"status": "sleeping"}))
    app.ingest({"events": events})


def status(app, serial, at):
    return app.device_status(serial, {"at": [iso(at)]})


def cover(app, at, depot="MSS", hours=24):
    return app.job_cover({"depot": [depot], "at": [iso(at)], "hours": [str(hours)]})
