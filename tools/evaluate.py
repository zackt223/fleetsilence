"""Score the status logic against the labelled history.

    python3 -I tools/evaluate.py PATH/TO/quiet-fleet-data-pack

Two independent sources of truth from the data pack:
1. support_cases.csv - 179 resolved cases, each with a root cause. For every
   case we ask the service for that device's status at the moment the case was
   opened and compare likely_cause with the labelled root_cause.
2. exam/practice.json - at each practice checkpoint the file lists the assets
   that could not be seen or recorded. We compare that list with the assets
   our job-cover logic refuses to offer as replacements.
"""
import csv
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.replay import replay                    # noqa: E402
from fleetsilence.timeutil import parse_ts          # noqa: E402


def main():
    pack = sys.argv[1]
    verbose = "-v" in sys.argv
    with open(os.path.join(pack, "data", "support_cases.csv"), newline="", encoding="utf-8") as f:
        cases = [r for r in csv.DictReader(f)]
    with open(os.path.join(pack, "exam", "practice.json"), encoding="utf-8") as f:
        practice = json.load(f)
    by_time = defaultdict(list)
    for c in cases:
        by_time[c["opened_at"]].append(c)
    cps = [{"id": f"case@{t}", "at": t, "cases": cs} for t, cs in by_time.items()]
    cps += [{"id": cp["id"], "at": cp["at"], "unavailable": cp["unavailable_assets"]}
            for cp in practice["checkpoints"] if cp.get("unavailable_assets")]
    scen = os.path.join(tempfile.mkdtemp(), "scenario.json")
    with open(scen, "w", encoding="utf-8") as f:
        json.dump({"checkpoints": [{"id": c["id"], "at": c["at"]} for c in cps]}, f)
    extra = {c["id"]: c for c in cps}
    table, misses = Counter(), []

    def on_checkpoint(app, cp):
        info = extra[cp["id"]]
        at = parse_ts(cp["at"])
        snap = app.engine.snapshot(at)
        for case in info.get("cases", []):
            if not case["serial"]:
                found = any(o.depot == case["depot"] for o in snap.outages().values())
                table[(case["root_cause"], "carrier_outage" if found else "(no outage seen)")] += 1
                continue
            a = snap.assess(case["serial"])
            table[(case["root_cause"], f"{a.cause} / {a.state}")] += 1
            if a.cause != case["root_cause"]:
                misses.append(f"{case['case_id']} {case['serial']} at {cp['at']}: labelled {case['root_cause']},"
                              f" we say {a.cause}/{a.state}: {a.reasons[0][:150]}")
        if "unavailable" in info:
            truth, memo, ours = set(info["unavailable"]), {}, set()
            for aid, row in app.ref.assets.items():
                h = app.cover.health(snap, aid, memo)
                if h.problem or h.outage is not None:
                    ours.add(aid)
            print(f"\n{cp['id']}: exam lists {len(truth)} unavailable assets; we block {len(ours)}."
                  f" Missed by us: {sorted(truth - ours)}. Blocked only by us: {sorted(ours - truth)}")

    replay(pack, scenario=scen, on_checkpoint=on_checkpoint, log=lambda *_: None)
    print("\nlabelled root cause -> our answer at the time the case was opened")
    ok = total = 0
    for (truth, ours), n in sorted(table.items()):
        hit = ours.split(" / ")[0] == truth
        ok += n * hit
        total += n
        print(f"  {'ok ' if hit else 'MISS'} {truth:20s} -> {ours:32s} {n}")
    print(f"\n{ok}/{total} cases agree with the labelled root cause")
    if verbose:
        print("\n".join(misses))


if __name__ == "__main__":
    main()
