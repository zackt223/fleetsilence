"""Measure, from the Oct 4-31 history, the numbers behind every threshold in settings.toml.

    python3 -I tools/calibrate.py PATH/TO/quiet-fleet-data-pack

Reads the raw telemetry and CSVs directly (it does not use the service), so the
thresholds are derived from the data, not from the code that applies them.
THRESHOLDS.md quotes this script's output.
"""
import bisect
import csv
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def ts(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, UTC)
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def pct(values, *ps):
    v = sorted(values)
    return {p: round(v[min(len(v) - 1, int(len(v) * p / 100))], 2) for p in ps}


def minutes(a, b):
    return (b - a).total_seconds() / 60


def main():
    pack = sys.argv[1]
    data = os.path.join(pack, "data")

    def rows(name):
        with open(os.path.join(data, name), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    assets = {r["asset_id"]: r for r in rows("assets.csv")}
    dev = {r["serial"]: r for r in rows("devices.csv")}
    cases = rows("support_cases.csv")
    weather = {(r["depot"], r["date"]): float(r["low_c"]) for r in rows("weather.csv")}

    raw = []
    for path in sorted(glob.glob(os.path.join(data, "telemetry", "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            raw += [json.loads(line) for line in f if line.strip()]
    print(f"events: {len(raw):,}")

    # ---- duplicates and odd clocks
    seen, by, dup_same_id, dup_new_id, odd_time = {}, defaultdict(list), 0, 0, Counter()
    for e in raw:
        key = (e.get("serial"), e["type"], repr(e.get("device_time")), json.dumps(e.get("payload"), sort_keys=True), e.get("raw"))
        if key in seen:
            dup_same_id += seen[key] == e["event_id"]
            dup_new_id += seen[key] != e["event_id"]
            continue
        seen[key] = e["event_id"]
        if "serial" not in e:
            continue
        if not isinstance(e["device_time"], str):
            odd_time[(dev[e["serial"]]["family"], dev[e["serial"]]["firmware"], type(e["device_time"]).__name__)] += 1
        e["_d"], e["_r"] = ts(e["device_time"]), ts(e["received_at"])
        by[e["serial"]].append(e)
    for L in by.values():
        L.sort(key=lambda e: e["_d"])
    print(f"duplicates: {dup_same_id:,} with the same event_id, {dup_new_id:,} with a new event_id")
    print(f"device_time not an ISO string: {dict(odd_time)}")

    # ---- ingest: lateness
    late = [(e["_r"] - e["_d"]).total_seconds() for L in by.values() for e in L if e["type"] not in ("command", "plan_change")]
    print(f"\n[ingest] received - device time, seconds: {pct(late, 50, 90, 98.8, 99, 99.9)} max {max(late):.0f} min {min(late):.0f};"
          f" over 120 s: {sum(x > 120 for x in late):,}; device clock ahead of platform: {sum(x < 0 for x in late)}")

    # ---- tracker cadence and silences
    pos_gap, park_gap = [], []
    for s, L in by.items():
        if s[0] != "T":
            continue
        for a, b in zip(L, L[1:]):
            g = minutes(a["_d"], b["_d"])
            if a["type"] == b["type"] == "position":
                pos_gap.append(g)
            elif a["type"] in ("heartbeat", "ignition_off") and b["type"] == "heartbeat":
                park_gap.append(g)
    print(f"\n[tracker] minutes between positions while driving: {pct(pos_gap, 50, 99, 99.99)}")
    print(f"[tracker] minutes from the previous event to a parked heartbeat: {pct(park_gap, 50, 99)} max {max(park_gap):.0f}")

    silences = []       # (start, end, serial, power alert nearby)
    for s, L in by.items():
        if s[0] != "T":
            continue
        R = sorted(L, key=lambda e: e["_r"])
        newest = None
        for i, e in enumerate(R[:-1]):
            if newest is None or e["_d"] >= newest["_d"]:
                newest = e
            gap = minutes(e["_r"], R[i + 1]["_r"])
            if gap > 5 and newest["type"] == "position" and newest["payload"].get("ignition"):
                alert = any(x["type"] in ("power_disconnect", "low_voltage") for x in L
                            if abs(minutes(x["_d"], e["_d"])) < 45)
                swapped = dev[s]["status"] == "removed"
                silences.append((e["_r"], R[i + 1]["_r"], s, alert or swapped))
    plain = [minutes(a, b) for a, b, s, alert in silences if not alert]
    print(f"[tracker] mid-drive silences over 5 min with no power alert: {len(plain)}; minutes: min {min(plain):.0f}"
          f" {pct(plain, 50, 90, 99)} max {max(plain):.0f}")

    # ---- outages: how many driving trackers are silent together, per carrier and depot
    groups = defaultdict(list)
    for a, b, s, alert in silences:
        if dev[s]["asset_id"]:
            groups[(dev[s]["carrier"], assets[dev[s]["asset_id"]]["depot"])].append((a + timedelta(minutes=6), b))
    print("\n[outage] most driving trackers silent (6+ min) at the same moment, per carrier and depot:")
    peaks = []
    for g, spans in sorted(groups.items()):
        points = sorted([(a, 1) for a, b in spans if b > a] + [(b, -1) for a, b in spans if b > a])
        level, best = 0, []
        for t, d in points:
            level += d
            best.append((level, t))
        top = sorted(best, reverse=True)
        big = [x for x in top if x[0] >= 10]
        when = max(top)[1]
        peaks.append((g, max(top)[0], when))
        normal = max((lv for lv, t in best if not big or abs(minutes(t, when)) > 120), default=0)
        print(f"   {g}: peak {max(top)[0]} at {when:%b %d %H:%M} UTC; highest level on any other occasion: {normal}")

    # ---- power
    print("\n[power] power_disconnect alerts:")
    to_lv, to_restore_quick, talk = [], [], []
    n_pd = 0
    for s, L in by.items():
        for i, e in enumerate(L):
            if e["type"] != "power_disconnect":
                continue
            n_pd += 1
            after = L[i + 1:]
            lv = next((x for x in after if x["type"] == "low_voltage"), None)
            rs = next((x for x in after if x["type"] == "power_restore"), None)
            if lv and (not rs or lv["_d"] < rs["_d"]):
                to_lv.append(minutes(e["_d"], lv["_d"]))
                tail = [x for x in after if x["type"] != "power_restore" and (not rs or x["_d"] < rs["_d"])]
                talk.append(minutes(e["_d"], tail[-1]["_d"]))
            elif rs:
                to_restore_quick.append(minutes(e["_d"], rs["_d"]))
    print(f"   {n_pd} alerts; {len(to_lv)} followed by a backup low-voltage alert after {min(to_lv):.0f}-{max(to_lv):.0f} min"
          f" (unit kept talking {min(talk):.0f}-{max(talk):.0f} min after the disconnect);")
    print(f"   {len(to_restore_quick)} restored with no low-voltage alert, after {sorted(round(x) for x in to_restore_quick)} min")

    # ---- cameras
    rec, sleep_delay, boot_after_reboot, wake = [], [], [], []
    offs, ons = defaultdict(list), defaultdict(list)
    for s, L in by.items():
        if s[0] == "T" and dev[s]["asset_id"]:
            offs[dev[s]["asset_id"]] += [e["_d"] for e in L if e["type"] == "ignition_off"]
            ons[dev[s]["asset_id"]] += [e["_d"] for e in L if e["type"] == "ignition_on"]
    for s, L in by.items():
        if s[0] != "C":
            continue
        o = sorted(offs[dev[s]["asset_id"]])
        on = sorted(ons[dev[s]["asset_id"]])
        for a, b in zip(L, L[1:]):
            sa, sb = a["payload"].get("status"), b["payload"].get("status")
            if sa == "sleeping" and sb == "booting":
                i = bisect.bisect_right(on, b["_d"]) - 1
                if i >= 0:
                    wake.append(minutes(on[i], b["_d"]))
            if sa == sb == "recording":
                rec.append(minutes(a["_d"], b["_d"]))
            if a["type"] == "command" and sb == "booting":
                boot_after_reboot.append(minutes(a["_d"], b["_d"]))
            if sb == "sleeping" and sa == "recording":
                i = bisect.bisect_right(o, b["_d"]) - 1
                if i >= 0:
                    sleep_delay.append(minutes(o[i], b["_d"]))
    print(f"\n[camera] minutes between 'recording' reports: {pct(rec, 1, 50, 99)}")
    print(f"[camera] minutes from ignition off to 'sleeping': {pct(sleep_delay, 1, 50, 95)}")
    print(f"[camera] minutes from ignition on to 'booting' after a sleep: {pct(wake, 50, 99)} max {max(wake):.1f} (n={len(wake)})")
    print(f"[camera] minutes from a reboot command to 'booting' when it worked: {pct(boot_after_reboot, 50, 99)} (n={len(boot_after_reboot)})")
    faults = [c for c in cases if c["root_cause"] == "camera_fault"]
    print(f"[camera] camera_fault cases: {len(faults)}; fixed by remote reboot: {sum(c['action'] == 'remote_reboot' for c in faults)};"
          f" needed a technician after two failed reboots: {sum(c['action'] == 'technician_visit' for c in faults)}")

    # ---- battery tags
    normal, misses = [], []
    for s, L in by.items():
        if s[0] != "A":
            continue
        depot = assets[dev[s]["asset_id"]]["depot"]
        off = -6 if depot == "REG" else -4
        for a, b in zip(L, L[1:]):
            h = minutes(a["_d"], b["_d"]) / 60
            if h < 30:
                normal.append(h)
            else:
                days = [(a["_d"] + timedelta(hours=off, days=k)).strftime("%Y-%m-%d") for k in range(1, round(h / 24))]
                misses.append((a["payload"]["battery_pct"], b["payload"]["battery_pct"], [weather.get((depot, d)) for d in days]))
    print(f"\n[tag] hours between daily check-ins: min {min(normal):.1f} {pct(normal, 50, 99)} max {max(normal):.1f}")
    replaced = [m for m in misses if m[1] == 100]
    healthy = [m for m in misses if m[1] != 100]
    print(f"[tag] silences ending in a new battery (came back at 100 %): {len(replaced)}; battery before: {sorted(m[0] for m in replaced)}")
    print(f"[tag] silences that ended by themselves: {len(healthy)}; battery before: {min(m[0] for m in healthy)}-{max(m[0] for m in healthy)} %;"
          f" warmest night among the missed days: {max(x for m in healthy for x in m[2] if x is not None):g} C;"
          f" longest: {max(len(m[2]) for m in healthy)} missed days")
    lows = Counter(m[0] for c in cases if c["root_cause"] == "battery_low" for m in [(c["notes"],)])
    print(f"[tag] battery_low case notes: {dict(lows)}")
    landscaping = [L[-1]["_d"].strftime("%b %d") for s, L in by.items()
                   if s[0] == "A" and assets[dev[s]["asset_id"]]["use"] == "landscaping"]
    print(f"[tag] last check-in of each landscaping trailer: {dict(Counter(landscaping))}")

    # ---- swaps
    print("\n[swap] tracker swaps (support cases with root cause device_swapped, plus the open one):")
    pairs = [(c["serial"], c["notes"].split("with ")[1].split(";")[0]) for c in cases if c["root_cause"] == "device_swapped"]
    pairs += [(s, n) for n, d in dev.items() if d["status"] == "stock" and n in by
              for s, o in dev.items() if o["asset_id"] and by.get(s) and 0 <= minutes(by[s][-1]["_d"], by[n][0]["_d"]) <= 10]
    for old, new in pairs:
        o, n = by[old], by[new]
        lp = next((e for e in reversed(o) if e["type"] == "position"), None)
        fp = next((e for e in n if e["type"] == "position"), None)
        vin = next((e for e in n if e["payload"].get("vin")), None)
        km = math.hypot((lp["payload"]["lat"] - fp["payload"]["lat"]) * 111,
                        (lp["payload"]["lng"] - fp["payload"]["lng"]) * 111 * math.cos(math.radians(lp["payload"]["lat"])))
        print(f"   {old} -> {new}: new unit first heard {minutes(o[-1]['_d'], n[0]['_d']):.0f} min after the old one's last"
              f" ({o[-1]['type']}); first VIN after {minutes(n[0]['_d'], vin['_d']) / 60:.1f} h;"
              f" positions {km:.2f} km apart" + ("" if o[-1]["type"] == "position" else " (old unit was parked; not comparable)"))

    # ---- BX
    print("\n[bx] decoding the three portal readouts:")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleetsilence import bx
    with open(os.path.join(pack, "new_hardware", "bx_raw_messages.txt"), encoding="utf-8") as f:
        msgs = [bx.decode(line) for line in f if line.strip()]
    print(f"   {len(msgs)} raw messages decoded; status-bit values seen: {dict(sorted(Counter(m['status_bits'] for m in msgs).items()))}")
    with open(os.path.join(pack, "new_hardware", "portal_readout.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            want = datetime.strptime(r["last_report_portal_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) + timedelta(hours=6)
            m = next(x for x in msgs if x["serial"] == r["unit"] and x["device_time"] == int(want.timestamp()))
            yn = lambda b: "Yes" if b else "No"   # noqa: E731
            ok = (f"{m['battery_pct']}%", yn(m["moving"]), yn(m["low_battery"]), yn(m["gps_fix"]), f"{m['lat']:.5f}", f"{m['lng']:.5f}") == \
                 (r["battery"], r["moving"], r["low_battery"], r["gps_fix"], r["latitude"], r["longitude"])
            print(f"   {r['unit']} at {r['last_report_portal_time']} Regina time: {'matches' if ok else 'DOES NOT MATCH'} the portal")


if __name__ == "__main__":
    main()
