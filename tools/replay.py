"""Replay the data pack's telemetry through the service in-process and run the practice checkpoints.

    python3 -I tools/replay.py PATH/TO/quiet-fleet-data-pack [--dump DIR]

This is our own re-implementation of the practice checks (written from
exam/PRACTICE.md and the contract), used by the tests and for calibration.
It does not replace running the official exam tool over HTTP.
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleetsilence import config                      # noqa: E402
from fleetsilence.server import App, BadRequest, NotFound  # noqa: E402
from fleetsilence.timeutil import iso, parse_ts      # noqa: E402

STATES = {"reporting", "quiet_expected", "needs_action", "unknown"}
CAUSES = {"none", "parked_normal", "sleeping_by_design", "cold_weather", "planned_downtime", "coverage_gap",
          "carrier_outage", "power_loss", "camera_fault", "plan_paused", "device_swapped", "battery_low", "unknown"}
PROBLEMS = {"no_location", "no_video", "both"}
DECISIONS = {"swap", "keep", "gap"}


def load_events(pack):
    """All telemetry, in the order the platform received it (stable within equal times)."""
    for path in sorted(glob.glob(os.path.join(pack, "data", "telemetry", "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        rows.sort(key=lambda e: e.get("received_at", ""))
        yield from rows


def check_status_shape(body):
    errs = []
    for k in ("serial", "asset_id", "at", "state", "likely_cause", "confidence", "last_heard_at", "reasons", "evidence"):
        if k not in body:
            errs.append(f"missing {k}")
    if body.get("state") not in STATES:
        errs.append(f"bad state {body.get('state')!r}")
    if body.get("likely_cause") not in CAUSES:
        errs.append(f"bad cause {body.get('likely_cause')!r}")
    c = body.get("confidence")
    if not isinstance(c, (int, float)) or not 0 <= c <= 1:
        errs.append("bad confidence")
    if body.get("state") == "reporting" and body.get("likely_cause") != "none":
        errs.append("reporting must use cause none")
    if not isinstance(body.get("reasons"), list) or not body["reasons"]:
        errs.append("reasons empty")
    if not isinstance(body.get("evidence"), list):
        errs.append("evidence not a list")
    return errs


def check_job_cover_rules(app, body, depot, at, hours, unavailable=()):
    """The contract's and the brief's rules for a job-cover answer."""
    errs = []
    ref = app.ref
    by_id = {j.job_id: j for j in ref.jobs}
    extra = {}
    seen = set()
    for it in body["jobs"]:
        j = by_id.get(it.get("job_id"))
        if not j:
            errs.append(f"unknown job {it.get('job_id')}")
            continue
        if j.job_id in seen:
            errs.append(f"{j.job_id} listed twice")
        seen.add(j.job_id)
        if j.depot != depot or not at <= j.start < at + hours * 3600:
            errs.append(f"{j.job_id} outside the window")
        if it.get("asset_id") != j.asset_id:
            errs.append(f"{j.job_id}: wrong asset")
        if it.get("problem") not in PROBLEMS or it.get("decision") not in DECISIONS:
            errs.append(f"{j.job_id}: bad problem/decision")
        if not it.get("reasons") or not isinstance(it.get("evidence"), list):
            errs.append(f"{j.job_id}: reasons/evidence missing")
        if it.get("decision") == "swap":
            to = it.get("swap_to")
            a0, a1 = ref.assets[j.asset_id], ref.assets.get(to)
            if not a1:
                errs.append(f"{j.job_id}: swap_to {to!r} unknown")
                continue
            if a1["type"] != a0["type"] or a1["depot"] != a0["depot"]:
                errs.append(f"{j.job_id}: {to} is not the same type and depot")
            if to in unavailable:
                errs.append(f"{j.job_id}: {to} can't be seen or recorded")
            if ref.busy(to, j.start, j.end, ignore_job=j.job_id) or any(s < j.end and j.start < e for s, e in extra.get(to, [])):
                errs.append(f"{j.job_id}: {to} is busy in that slot")
            extra.setdefault(to, []).append((j.start, j.end))
        elif it.get("swap_to") is not None:
            errs.append(f"{j.job_id}: swap_to must be null unless decision is swap")
    return errs


def run_check(app, c, at, unavailable):
    t = c["type"]
    if t == "status":
        body = app.device_status(c["serial"], {"at": [iso(at)]})
        errs = check_status_shape(body)
        if body["state"] not in c.get("state", STATES):
            errs.append(f"state {body['state']} not in {c['state']}")
        if body["likely_cause"] not in c.get("cause", CAUSES):
            errs.append(f"cause {body['likely_cause']} not in {c['cause']}")
        if c.get("evidence_any") and not set(c["evidence_any"]) & set(body["evidence"]):
            errs.append(f"evidence lacks any of {c['evidence_any']}")
        return errs, body
    if c.get("persona") != "fleet":
        return None, None
    q = c.get("query", {})
    depot, hours = q.get("depot", "MSS"), q.get("hours", 24)
    body = app.job_cover({"depot": [depot], "at": [iso(at)], "hours": [str(hours)]})
    errs = check_job_cover_rules(app, body, depot, at, hours, unavailable)
    jobs = body["jobs"]
    if t == "job":
        found = [j for j in jobs if j["job_id"] == c["job_id"]]
        if not found:
            errs.append(f"{c['job_id']} should be listed")
        else:
            if found[0]["problem"] not in c.get("problem", PROBLEMS):
                errs.append(f"problem {found[0]['problem']} not in {c['problem']}")
            if found[0]["decision"] not in c.get("decision", DECISIONS):
                errs.append(f"decision {found[0]['decision']} not in {c['decision']}")
    elif t == "jobs_no_swap_or_gap":
        hit = [j["job_id"] for j in jobs if j["asset_id"] in set(c["asset_ids"]) and j["decision"] in ("swap", "gap")]
        if hit:
            errs.append(f"should not be swapped or gapped: {hit[:5]}")
    elif t == "jobs_exclude_assets":
        hit = [j["job_id"] for j in jobs if j["asset_id"] in set(c["asset_ids"])]
        if hit:
            errs.append(f"should not be listed: {hit[:5]}")
    return errs, body


def replay(pack, scenario=None, dump=None, on_checkpoint=None, batch_size=5000, log=print):
    """Stream the history, stopping at each checkpoint. Returns (app, [(checkpoint id, label, errors)])."""
    data = os.path.join(pack, "data")
    app = App(data, config.load())
    with open(scenario or os.path.join(pack, "exam", "practice.json"), encoding="utf-8") as f:
        scen = json.load(f)
    cps = sorted(scen["checkpoints"], key=lambda c: c["at"])
    results, i, batch, sent, slowest = [], 0, [], 0, 0.0

    def flush():
        nonlocal batch, sent, slowest
        if batch:
            t0 = time.perf_counter()
            app.ingest({"events": batch})
            slowest = max(slowest, time.perf_counter() - t0)
            sent += len(batch)
            batch = []

    def checkpoint(cp):
        at = parse_ts(cp["at"])
        log(f"\n{cp['id']} at {cp['at']}: {cp.get('title', '')}")
        for c in cp.get("checks", []):
            t0 = time.perf_counter()
            errs, body = run_check(app, c, at, set(cp.get("unavailable_assets", [])))
            if errs is None:
                continue
            results.append((cp["id"], c.get("label", c["type"]), errs))
            log(f"  {'PASS' if not errs else 'FAIL'}  {c.get('label', c['type'])}  ({time.perf_counter() - t0:.2f}s)")
            for e in errs[:6]:
                log(f"          {e}")
        if dump:
            os.makedirs(dump, exist_ok=True)
            for depot in sorted(app.ref.depots):
                body = app.job_cover({"depot": [depot], "at": [cp["at"]], "hours": ["24"]})
                with open(os.path.join(dump, f"{cp['id']}-job-cover-{depot}.json"), "w", encoding="utf-8") as f:
                    json.dump(body, f, indent=2)
        if on_checkpoint:
            on_checkpoint(app, cp)

    for e in load_events(pack):
        while i < len(cps) and e.get("received_at", "") > cps[i]["at"]:
            flush()
            checkpoint(cps[i])
            i += 1
        batch.append(e)
        if len(batch) >= batch_size:
            flush()
    flush()
    for cp in cps[i:]:
        checkpoint(cp)
    log(f"\nstreamed {sent:,} events; slowest ingest batch {slowest:.2f}s; store: {app.store.stats}")
    return app, results


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    dump = sys.argv[sys.argv.index("--dump") + 1] if "--dump" in sys.argv else None
    t0 = time.time()
    _, results = replay(sys.argv[1], dump=dump)
    failed = [r for r in results if r[2]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed in {time.time() - t0:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()


__all__ = ["replay", "load_events", "check_job_cover_rules", "check_status_shape", "BadRequest", "NotFound"]
