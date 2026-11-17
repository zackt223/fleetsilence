"""Generate a complete, made-up data pack that shares nothing with the take-home pack but its formats.

    python3 tools/make_test_pack.py OUT_DIR

Writes OUT_DIR/data/*.csv, OUT_DIR/data/telemetry/*.jsonl and OUT_DIR/scenario.json. The scenario runs
with the pack's own exam tool:

    python3 qf_exam.py run OUT_DIR/scenario.json --data OUT_DIR/data --persona fleet

What is different on purpose:
* depots YYC (Calgary) and HFX (Halifax), which settings.toml does not list; new ids, VINs, carriers
* a vehicle type the original pack doesn't have (truck), and trailers with no jobs
* March 2027, across the daylight-saving change on Sunday March 14
* messy files: extra and reordered columns, a byte-order mark (devices.csv), Windows line endings
  (schedule.csv), no plan_changes.csv
* messy events: duplicates (some with a new id), late buffered uploads, a malformed event, unknown types

What happens in it (each one has a checkpoint with the expected answer):
* C1 Wed Mar 10: a van's tracker loses power mid-shift (YYC-V07)
* C2 Thu Mar 11: a camera fails while its van keeps driving (HFX-V02); another van's camera is asleep
* C3 Sat Mar 13: a trailer tag misses its check-in after a -18 C night (YYC-TL2)
* C4 Mon Mar 15: the Atlantel network is down in Halifax for 45 minutes
* C5 Tue Mar 16: a pickup drives through a dead zone for 30 minutes (HFX-P01)
* C6 Wed Mar 17: a trailer tag that was at 9 % stops checking in (YYC-TL1)
"""
import csv
import json
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
R = random.Random(20270308)
DAYS = [date(2027, 3, 8) + timedelta(days=i) for i in range(12)]          # Mon Mar 8 .. Fri Mar 19
DEPOTS = {"YYC": {"base": -7, "lat": 51.05, "lng": -114.07},
          "HFX": {"base": -4, "lat": 44.65, "lng": -63.58}}
DST_START = datetime(2027, 3, 14, 2, 0)                                    # local wall time


def offset(depot, local_naive):
    return DEPOTS[depot]["base"] + (1 if local_naive >= DST_START else 0)


def utc(depot, d, hh, mm=0):
    local = datetime(d.year, d.month, d.day, hh, mm)
    return (local - timedelta(hours=offset(depot, local))).replace(tzinfo=UTC)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def vin():
    return "".join(R.choice("ABCDEFGHJKLMNPRSTUVWXYZ0123456789") for _ in range(17))


# ----------------------------------------------------------------------------------------------- fleet
assets, devices = [], []
tracker, camera, tag = {}, {}, {}
nums = iter(range(70001, 79999))
for dep in DEPOTS:
    def add(aid, typ, use, carrier, cam=False, tagfam=None):
        assets.append({"asset_id": aid, "type": typ, "depot": dep, "use": use, "make_model": "Test model",
                       "year": 2025, "vin": vin(), "status": "active", "notes": "generated"})
        if tagfam:
            s = f"A-{next(nums) + 20000}" if tagfam == "A" else f"BX{next(nums)}"
            tag[aid] = s
            devices.append((s, tagfam, carrier, aid))
            return
        tracker[aid] = f"T-{next(nums)}"
        devices.append((tracker[aid], "T", carrier, aid))
        if cam:
            camera[aid] = f"C-{next(nums) - 60000}"
            devices.append((camera[aid], "C", carrier, aid))
    alt = "Coastline" if dep == "HFX" else "Prairienet"
    main = "Atlantel" if dep == "HFX" else "Prairienet"
    for n in range(1, 9):
        add(f"{dep}-V{n:02d}", "van", "service", main if n <= 6 else alt, cam=n <= 6)
    for n in range(1, 4):
        add(f"{dep}-P{n:02d}", "pickup", "locating", main if n <= 2 else alt)
    for n in range(1, 3):
        add(f"{dep}-TK{n}", "truck", "hauling", main)
    for n in range(1, 4):
        add(f"{dep}-TL{n}", "trailer", "storage", main, tagfam="BX" if (dep == "YYC" and n == 3) else "A")
devices.append(("T-79998", "T", "Prairienet", ""))                       # a spare in stock
vehicles = [a for a in assets if a["type"] != "trailer"]
vin_of = {a["asset_id"]: a["vin"] for a in assets}

# ----------------------------------------------------------------------------------------------- jobs
jobs = []
busy_days = defaultdict(set)
for d in DAYS:
    if d.weekday() >= 5:
        continue
    for dep in DEPOTS:
        dep_vehicles = [a for a in vehicles if a["depot"] == dep]
        always = {"YYC-V07", "HFX-V02", "HFX-P01"}                 # the vehicles the checkpoints are about work every day
        idle = set(R.sample([a["asset_id"] for a in dep_vehicles if a["type"] == "van" and a["asset_id"] not in always], 2))
        idle.add(f"{dep}-P03")
        if d == date(2027, 3, 11) and dep == "HFX":
            idle.add("HFX-V05")                      # parked all day: its camera stays asleep (C2)
        for a in dep_vehicles:
            aid = a["asset_id"]
            if aid in idle:
                continue
            t = utc(dep, d, 7, 30) + timedelta(minutes=R.randint(0, 25))
            for k in range(3 if aid in always else R.randint(2, 3)):
                dur = timedelta(minutes=R.randint(60, 120))
                jobs.append({"job_id": f"J-{dep}-{d:%m%d}-{len(jobs) + 1:05d}", "asset_id": aid, "depot": dep,
                             "start": iso(t), "end": iso(t + dur), "job_type": R.choice(["repair", "install", "inspection"])})
                t += dur + timedelta(minutes=R.randint(20, 45))
            busy_days[aid].add(d)

# ----------------------------------------------------------------------------------------------- events
events = []
ids = iter(range(10_000_001, 99_999_999))
marks = {}                                                                 # named events, for evidence checks


def ev(serial, typ, t, payload=None, lag=None, name=None):
    e = {"event_id": f"x{next(ids)}", "serial": serial, "type": typ, "device_time": iso(t),
         "received_at": iso(t + timedelta(seconds=lag if lag is not None else R.randint(1, 4))), "payload": payload or {}}
    events.append(e)
    if name:
        marks[name] = e["event_id"]
    return e


def silent(serial, t):
    """Windows in which a device sends nothing (outage, fault, power loss)."""
    for s, a, b in quiet:
        if s == serial and a <= t < b:
            return True
    return False


def buffered(serial, t):
    for s, a, b in late:
        if s == serial and a <= t < b:
            return b
    return None


quiet, late = [], []
# C1: YYC-V07 loses power Wed Mar 10 10:00 MST, fixed Thu 11:00 MST.
pl_start, pl_fix = utc("YYC", date(2027, 3, 10), 10), utc("YYC", date(2027, 3, 11), 11)
quiet.append((tracker["YYC-V07"], pl_start + timedelta(minutes=21), pl_fix))
# C4: Atlantel down in Halifax Mon Mar 15 10:00-10:45 ADT; devices buffer and upload when it is back.
out_a, out_b = utc("HFX", date(2027, 3, 15), 10), utc("HFX", date(2027, 3, 15), 10, 45)
atlantel = [s for s, fam, car, aid in devices if car == "Atlantel" and fam in ("T", "C")]
for s in atlantel:
    late.append((s, out_a, out_b))
# C5: HFX-P01 in a dead zone Tue Mar 16 11:00-11:30 ADT.
dz_a, dz_b = utc("HFX", date(2027, 3, 16), 11), utc("HFX", date(2027, 3, 16), 11, 30)
late.append((tracker["HFX-P01"], dz_a, dz_b))
# C2: HFX-V02's camera fails Thu Mar 11 09:30 AST; a support reboot at 10:30 brings it back.
cf_a, cf_b = utc("HFX", date(2027, 3, 11), 9, 30), utc("HFX", date(2027, 3, 11), 10, 30)
quiet.append((camera["HFX-V02"], cf_a, cf_b))


def emit(serial, typ, t, payload=None, name=None):
    if silent(serial, t):
        return None
    b = buffered(serial, t)
    return ev(serial, typ, t, payload, lag=int((b - t).total_seconds()) + R.randint(5, 90) if b else None, name=name)


start, end = utc("YYC", DAYS[0], 0) - timedelta(hours=2), utc("YYC", DAYS[-1], 23, 59)
for a in vehicles:
    aid, dep = a["asset_id"], a["depot"]
    tr, cam = tracker[aid], camera.get(aid)
    lat, lng = DEPOTS[dep]["lat"] + R.uniform(-0.05, 0.05), DEPOTS[dep]["lng"] + R.uniform(-0.05, 0.05)
    shifts = []
    for d in DAYS:
        mine = [j for j in jobs if j["asset_id"] == aid and utc(dep, d, 0) <= datetime.fromisoformat(j["start"].replace("Z", "+00:00")) < utc(dep, d, 23, 59)]
        if mine:
            s = datetime.fromisoformat(mine[0]["start"].replace("Z", "+00:00")) - timedelta(minutes=20)
            e = datetime.fromisoformat(mine[-1]["end"].replace("Z", "+00:00")) + timedelta(minutes=15)
            shifts.append((s, e))
    # parked heartbeats from the start, then each shift, each followed by heartbeats
    t = start
    for s, e in shifts + [(end + timedelta(days=1), None)]:
        hb = t + timedelta(minutes=R.randint(5, 200))
        while hb < min(s, end):
            emit(tr, "heartbeat", hb, {"battery_v": round(R.uniform(12.1, 12.7), 2), "signal": R.randint(2, 5), "vin": vin_of[aid]})
            hb += timedelta(hours=4)
        if e is None:
            break
        emit(tr, "ignition_on", s, {"vin": vin_of[aid]})
        if cam:
            emit(cam, "camera_status", s + timedelta(seconds=5), {"status": "booting"})
        p = s + timedelta(seconds=40)
        while p < e:
            emit(tr, "position", p, {"lat": round(lat + R.uniform(-0.1, 0.1), 5), "lng": round(lng + R.uniform(-0.1, 0.1), 5),
                                     "speed_kmh": R.randint(0, 80), "ignition": True})
            p += timedelta(minutes=2)
        if cam:
            c = s + timedelta(minutes=1)
            while c < e:
                emit(cam, "camera_status", c, {"status": "recording"})
                c += timedelta(minutes=5)
        emit(tr, "ignition_off", e)
        if cam:
            emit(cam, "camera_status", e + timedelta(minutes=15), {"status": "sleeping"})
        t = e

# C1 details: the disconnect and low-voltage alerts, then silence until the technician's fix.
ev(tracker["YYC-V07"], "power_disconnect", pl_start, name="pd")
ev(tracker["YYC-V07"], "low_voltage", pl_start + timedelta(minutes=12), {"volts": 3.5, "source": "backup_battery"}, name="lv")
ev(tracker["YYC-V07"], "power_restore", pl_fix)
events[:] = [e for e in events if not (e.get("serial") == tracker["YYC-V07"] and e["type"] == "position"
                                     and pl_start + timedelta(minutes=21) <= datetime.fromisoformat(e["device_time"][:-1] + "+00:00") < pl_fix)]
# C2 details: the storage error and the reboot that fixes it.
ev(camera["HFX-V02"], "camera_status", cf_a, {"status": "storage_error", "code": "SD_WRITE_FAIL"}, name="cf")
ev(camera["HFX-V02"], "command", cf_b - timedelta(minutes=1), {"command": "reboot", "issued_by": "support"}, lag=0)
ev(camera["HFX-V02"], "camera_status", cf_b, {"status": "booting"})

# Trailer tags: one check-in a day at about 09:00 local.
weather = []
for d in DAYS + [DAYS[-1] + timedelta(days=1)]:
    for dep in DEPOTS:
        low = R.randint(-6, 2)
        if dep == "YYC" and d == date(2027, 3, 13):
            low = -18           # the night before C3; as in the take-home pack, a date's low is that morning's
        weather.append({"date": f"{d:%Y-%m-%d}", "depot": dep, "low_c": low, "high_c": low + R.randint(6, 10)})
for a in assets:
    if a["type"] != "trailer":
        continue
    aid, dep, s = a["asset_id"], a["depot"], tag[a["asset_id"]]
    pct = R.randint(55, 85)
    for d in DAYS:
        t = utc(dep, d, 9, R.randint(0, 20))
        if aid == "YYC-TL1":
            pct = max(9, 40 - 5 * (d - DAYS[0]).days)
            if d >= date(2027, 3, 17):
                continue                                         # C6: flat after the 9 % reading on Mar 16
        if aid == "YYC-TL2" and d == date(2027, 3, 13):
            continue                                             # C3: skipped after the -18 C night
        lat, lng = DEPOTS[dep]["lat"], DEPOTS[dep]["lng"]
        if s.startswith("BX"):
            raw = {"u": s, "t": format(int(t.timestamp()), "X"), "b": int(pct / 100 * 4095),
                   "s": 8, "p": f"{int(lat * 1e5)},{int(lng * 1e5)}"}
            events.append({"event_id": f"x{next(ids)}", "type": "bx_raw", "received_at": iso(t + timedelta(seconds=3)),
                           "raw": json.dumps(raw, separators=(",", ":"))})
        else:
            ev(s, "check_in", t, {"battery_pct": pct, "lat": lat, "lng": lng, "temp_c": R.randint(-15, 5)})
        pct = max(pct - R.randint(0, 2), 20) if aid != "YYC-TL1" else pct

# Mess: duplicates (half with a new id), a malformed event and unknown types every day.
for e in R.sample(events, len(events) // 50):
    d = dict(e)
    if R.random() < 0.5:
        d["event_id"] = f"x{next(ids)}"
    d["received_at"] = iso(datetime.fromisoformat(e["received_at"][:-1] + "+00:00") + timedelta(seconds=R.randint(1, 600)))
    events.append(d)
for d in DAYS:
    t = utc("YYC", d, 12)
    events.append({"event_id": f"x{next(ids)}", "received_at": iso(t), "type": "position", "payload": "not an object"})
    events.append({"event_id": f"x{next(ids)}", "serial": tracker["YYC-V01"], "type": "diagnostic",
                   "device_time": iso(t), "received_at": iso(t), "payload": {"code": 7}})

# ----------------------------------------------------------------------------------------------- write
out = sys.argv[1]
data = os.path.join(out, "data")
os.makedirs(os.path.join(data, "telemetry"), exist_ok=True)


def write(name, header, rows, bom=False, crlf=False):
    with open(os.path.join(data, name), "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.DictWriter(f, header, lineterminator="\r\n" if crlf else "\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


write("assets.csv", ["notes", "asset_id", "status", "type", "depot", "use", "vin", "make_model", "year"], assets)
write("devices.csv", ["installed_at", "serial", "asset_id", "family", "model", "firmware", "carrier", "status"],
      [{"installed_at": "2026-01-01", "serial": s, "asset_id": a, "family": f, "model": "Test", "firmware": "9.9",
        "carrier": c, "status": "active" if a else "stock"} for s, f, c, a in devices], bom=True)
# (No byte-order mark on schedule.csv or assets.csv: the exam tool reads those two and its reader does not
# strip one. The service does.)
write("schedule.csv", ["job_id", "asset_id", "depot", "start", "end", "job_type"], jobs, crlf=True)
write("weather.csv", ["date", "depot", "low_c", "high_c"], weather)
write("support_cases.csv", ["case_id", "opened_at", "closed_at", "depot", "asset_id", "serial", "reported_by",
                            "symptom", "root_cause", "action", "outcome", "notes"], [])
# plan_changes.csv is left out on purpose.

events.sort(key=lambda e: e["received_at"])
by_day = defaultdict(list)
for e in events:
    by_day[e["received_at"][:10]].append(e)
for day, evs in by_day.items():
    with open(os.path.join(data, "telemetry", f"{day}.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for e in evs:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def job_ids(aid, after, hours):
    lo, hi = after, after + timedelta(hours=hours)
    return [j["job_id"] for j in jobs if j["asset_id"] == aid
            and lo <= datetime.fromisoformat(j["start"][:-1] + "+00:00") < hi]


c1 = utc("YYC", date(2027, 3, 10), 14)
c2 = utc("HFX", date(2027, 3, 11), 9, 50)
c3 = utc("YYC", date(2027, 3, 13), 12)
c4 = utc("HFX", date(2027, 3, 15), 10, 25)
c5 = utc("HFX", date(2027, 3, 16), 11, 15)
c6 = utc("YYC", date(2027, 3, 17), 13)
v07_next = job_ids("YYC-V07", c1, 24)
v02_next = job_ids("HFX-V02", c2, 4)
hfx_atl_assets = sorted({aid for s, f, c, aid in devices if c == "Atlantel" and f == "T"})
# Only vehicles out driving when the network drops go silent; a parked one isn't due to report yet.
outage_driving = [a for a in hfx_atl_assets if job_ids(a, utc("HFX", date(2027, 3, 15), 0), 24)]
scenario = {
    "name": "Generated pack: Calgary and Halifax, March 2027",
    "streams": ["{data}/telemetry/*.jsonl"],
    "checkpoints": [
        {"id": "C1", "at": iso(c1), "title": "Calgary Wed 14:00. A van's tracker lost power at 10:00.",
         "unavailable_assets": ["YYC-V07"],
         "checks": [
             {"type": "status", "label": "YYC-V07 tracker lost power", "serial": tracker["YYC-V07"],
              "state": ["needs_action"], "cause": ["power_loss"], "evidence_any": [marks["pd"], marks["lv"]]},
             {"type": "job", "persona": "fleet", "label": "YYC-V07's first job tomorrow moves to a free van",
              "query": {"depot": "YYC", "hours": 24}, "job_id": v07_next[0], "problem": ["no_location", "both"],
              "decision": ["swap"]}]},
        {"id": "C2", "at": iso(c2), "title": "Halifax Thu 09:50. A camera failed while driving; another is asleep.",
         "unavailable_assets": ["HFX-V02"],
         "checks": [
             {"type": "status", "label": "Failed camera on HFX-V02", "serial": camera["HFX-V02"],
              "state": ["needs_action"], "cause": ["camera_fault"], "evidence_any": [marks["cf"]]},
             {"type": "status", "label": "Camera on parked HFX-V05 is asleep", "serial": camera["HFX-V05"],
              "state": ["quiet_expected"], "cause": ["sleeping_by_design"]},
             {"type": "job", "persona": "fleet", "label": "HFX-V02's next job can't be recorded",
              "query": {"depot": "HFX", "hours": 4}, "job_id": v02_next[0], "problem": ["no_video", "both"],
              "decision": ["swap", "keep", "gap"]},
             {"type": "jobs_exclude_assets", "persona": "fleet", "label": "HFX-V05 isn't flagged",
              "query": {"depot": "HFX", "hours": 24}, "asset_ids": ["HFX-V05"]}]},
        {"id": "C3", "at": iso(c3), "title": "Calgary Sat 12:00. A trailer tag missed its check-in after -18 C.",
         "checks": [
             {"type": "status", "label": "YYC-TL2 tag is quiet because of the cold", "serial": tag["YYC-TL2"],
              "state": ["quiet_expected"], "cause": ["cold_weather"]}]},
        {"id": "C4", "at": iso(c4), "title": "Halifax Mon 10:25 (daylight time). Atlantel is down.",
         "unavailable_assets": hfx_atl_assets,
         "checks": [
             *[{"type": "status", "label": "Outage tracker is explained by the carrier", "serial": tracker[a],
                "state": ["needs_action", "unknown"], "cause": ["carrier_outage"]} for a in outage_driving[:3]],
             {"type": "jobs_no_swap_or_gap", "persona": "fleet", "label": "No swaps or gaps because of the outage",
              "query": {"depot": "HFX", "hours": 4}, "asset_ids": hfx_atl_assets}]},
        {"id": "C5", "at": iso(c5), "title": "Halifax Tue 11:15. A pickup is in a dead zone.",
         "unavailable_assets": ["HFX-P01"],
         "checks": [
             {"type": "status", "label": "HFX-P01 is in a dead zone", "serial": tracker["HFX-P01"],
              "state": ["unknown", "quiet_expected"], "cause": ["coverage_gap"]},
             {"type": "jobs_no_swap_or_gap", "persona": "fleet", "label": "Nothing moved for a dead zone",
              "query": {"depot": "HFX", "hours": 4}, "asset_ids": ["HFX-P01"]}]},
        {"id": "C6", "at": iso(c6), "title": "Calgary Wed 13:00 (daylight time). A tag at 9 % stopped checking in.",
         "checks": [
             {"type": "status", "label": "YYC-TL1 needs a battery", "serial": tag["YYC-TL1"],
              "state": ["needs_action"], "cause": ["battery_low"]},
             {"type": "status", "label": "BX tag on YYC-TL3 decodes and is on time", "serial": tag["YYC-TL3"],
              "state": ["reporting"], "cause": ["none"]}]},
    ],
}
with open(os.path.join(out, "scenario.json"), "w", encoding="utf-8") as f:
    json.dump(scenario, f, indent=1)
print(f"{len(assets)} assets, {len(devices)} devices, {len(jobs)} jobs, {len(events)} events -> {out}")
