"""Job cover for the fleet manager.

For every job starting in the window: can its vehicle be seen (tracker) and
recorded (camera)? If not, move the job to another vehicle that can take it,
or say plainly that none can and why.

Rules built in here:
* Missing a real problem is worse than a false alarm, so a confirmed fault is
  flagged for every job in the window, however far ahead.
* A job is moved (`swap`), reported as having no replacement (`gap`), or left
  where it is (`keep`) when the fault is expected to be fixed before it starts:
  a remote reboot within about an hour, or a technician visit the next working
  day. How fast each fix happens is taken from the case history (settings).
* A wrong swap costs the crew time, so a swap is only offered to a vehicle of
  the same type and depot that is free for the job's whole slot and is itself
  visibly healthy - anything silent, unexplained or inside a carrier outage is
  never offered as a replacement.
* When many vehicles are quiet for one outside reason (a carrier outage), no
  jobs are moved; it is said once, in `notices`.
* Replacements are shared out so the most jobs are covered: an earlier job is
  moved to a different free vehicle rather than leave a later one with none.
* A job already under way on a vehicle that can't be seen is said in `notices`
  (the contract's `jobs` list is only for jobs starting in the window).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .status import ACTION
from .timeutil import duration, iso

NOT_THE_VEHICLE = ("carrier_outage",)     # causes that nothing done to the vehicle can fix
BLIND = {"no_location": "untracked", "no_video": "unrecorded", "both": "untracked and unrecorded"}


class Health:
    __slots__ = ("asset_id", "trackers", "cameras", "location_issue", "video_issue", "soft", "outage", "eligible")

    @property
    def problem(self):
        if self.location_issue and self.video_issue:
            return "both"
        return "no_location" if self.location_issue else "no_video" if self.video_issue else None


class JobCover:
    def __init__(self, engine):
        self.engine = engine
        self.cfg, self.ref, self.clock = engine.cfg, engine.ref, engine.clock

    def health(self, snap, asset_id, memo):
        if asset_id in memo:
            return memo[asset_id]
        h = Health()
        h.asset_id = asset_id
        h.trackers = [snap.assess(s) for s in snap.asset_trackers(asset_id)]
        h.cameras = [snap.assess(s) for s in snap.asset_cameras(asset_id)]
        h.location_issue = h.video_issue = h.outage = None
        h.soft = []

        seen = any(t.ok and t.cause != "coverage_gap" for t in h.trackers)
        if not seen:
            hard = [t for t in h.trackers if t.state == ACTION and t.cause not in NOT_THE_VEHICLE]
            if hard:
                h.location_issue = max(hard, key=lambda t: t.last_heard or 0)
            else:
                h.soft += h.trackers
        for c in h.cameras:
            if c.ok and c.cause != "coverage_gap":
                continue
            if c.state == ACTION and c.cause not in NOT_THE_VEHICLE:
                h.video_issue = h.video_issue or c
            else:
                h.soft.append(c)

        outages = snap.outages()
        if outages:
            serials = {d["serial"] for d in self.ref.asset_devices.get(asset_id, []) if d.get("status") == "active"}
            serials.update(t.serial for t in h.trackers)
            for s in sorted(serials):
                o = outages.get(snap.group(s))
                if o:
                    h.outage = o
                    break
        h.eligible = bool(h.trackers) and seen and not h.video_issue and not h.soft and h.outage is None
        memo[asset_id] = h
        return h

    def answer(self, depot, at, hours):
        snap = self.engine.snapshot(at)
        jobs = self.ref.jobs_starting(depot, at, at + hours * 3600)
        memo = {}
        watch = defaultdict(list)
        troubled = []                     # (job, asset row, health) for every job that can't be seen or recorded
        kept = {}                         # job_id -> (fixed by, how) for jobs the fix will reach first
        overdue = {}                      # job_id -> why the expected fix can't be counted on any more
        for job in jobs:
            asset = self.ref.assets.get(job.asset_id)
            if not asset:
                continue
            h = self.health(snap, job.asset_id, memo)
            if h.problem:
                fix = self._fixed_by(depot, at, h)
                if fix and fix[0] is None:
                    overdue[job.job_id] = fix[1]
                elif fix and fix[0] <= job.start:
                    kept[job.job_id] = fix
                troubled.append((job, asset, h))
            elif h.soft:
                watch[job.asset_id].append(job)

        moving = [(job, asset, h) for job, asset, h in troubled if job.job_id not in kept]
        options = {job.job_id: self._candidates(snap, job, asset, h, memo) for job, asset, h in moving}
        placed, load = self._allocate([job for job, _, _ in moving], options)

        items = []
        counts = {"swap": 0, "gap": 0, "keep": 0}
        lead = self.cfg.jobcover.get("recheck_lead_h", 12) * 3600
        for job, asset, h in troubled:
            issues = [i for i in (h.location_issue, h.video_issue) if i]
            reasons = [r for i in issues for r in i.reasons]
            evidence = []
            for i in issues:
                evidence += [x for x in i.evidence if x not in evidence]
            slot = f"{self.clock.fmt(depot, job.start, at)}-{self.clock.fmt(depot, job.end, job.start)}"
            item = {"job_id": job.job_id, "asset_id": job.asset_id, "problem": h.problem, "decision": None,
                    "swap_to": None, "manager_action": "", "reasons": reasons, "evidence": evidence}
            opts, tally = options.get(job.job_id, ({}, {}))
            target = placed.get(job.job_id)
            if job.job_id in overdue:
                item["reasons"].append(overdue[job.job_id])
            if job.job_id in kept:
                when, how = kept[job.job_id]
                item["decision"] = "keep"
                item["reasons"].append(f"{how} That is before this job starts ({slot}), so it stays on {job.asset_id}.")
                item["manager_action"] = (
                    f"Leave {job.job_id} ({slot}) on {job.asset_id}, and make sure the fix happens: it should be done by"
                    f" {self.clock.fmt(depot, when, at)}. If it is not, ask again and the job will be moved.")
            elif target:
                t = self.ref.assets[target]
                item["decision"], item["swap_to"] = "swap", target
                cam = " with a working camera" if snap.asset_cameras(target) else ""
                item["reasons"].append(
                    f"{target} is a {t.get('type')} at {snap.depot_name(depot)}{cam}, has no other job during {slot},"
                    " and its tracker is reporting normally.")
                item["manager_action"] = f"Move {job.job_id} ({slot}, {job.job_type}) from {job.asset_id} to {target}."
                if opts[target][0]:      # second-best: tracked, but not recorded
                    kind, others, detail = self._why(job, asset, opts, tally, load)
                    item["reasons"].append(f"No {kind} with a working camera is free (of the {others}, {detail}),"
                                           f" so the job moves to a {kind} without a camera: tracked, but not recorded.")
                    item["manager_action"] += " It will be tracked but not recorded: no van with a working camera is free."
            else:
                item["decision"] = "gap"
                kind, others, detail = self._why(job, asset, opts, tally, load)
                tail = (" A van without a camera would not fix a recording problem, so none is offered."
                        if tally["no camera"] and not h.location_issue else "")
                item["reasons"].append(f"No {kind} at {snap.depot_name(job.depot)} can take this job: of the {others},"
                                       f" {detail}.{tail}")
                blind = BLIND[h.problem]
                item["manager_action"] = (
                    f"No replacement for {job.job_id} ({slot}). Reschedule it, or get {job.asset_id} fixed before then;"
                    f" otherwise it goes out {blind}.")
            if job.start - at > lead:
                # Both vehicles are judged on what is known now. Nobody can know their state at the job's start.
                item["manager_action"] += (f" The job starts in {duration(job.start - at)}; this is based on what is"
                                           " known now, so ask again nearer the time.")
            counts[item["decision"]] += 1
            items.append(item)

        under_way = self._under_way(snap, depot, at, memo)
        return {
            "depot": depot, "at": iso(at), "hours": hours,
            "jobs": items,
            "notices": under_way + self._notices(snap, depot, watch, memo),
            "summary": {"jobs_in_window": len(jobs), "at_risk": len(items), **counts,
                        "under_way_at_risk": sum(len(n["job_ids"]) for n in under_way)},
        }

    def _fixed_by(self, depot, at, h):
        """When the vehicle's fault should be fixed, going by how such faults were fixed in the case history.

        The expected fix is counted from when the fault began, not from `at`. A fix that should already
        have happened and hasn't is overdue: nobody can count on it, so nothing is kept.

        Returns (time, sentence) when a fix can be counted on, (None, sentence) when the fix is overdue,
        or None when keep is off or the fault has no fix to wait for. With two faults, the later fix counts.
        """
        cfg = self.cfg.jobcover
        if not cfg.get("keep_when_fix_expected", True):
            return None
        fixes = []
        for i in (h.location_issue, h.video_issue):
            if i is None:
                continue
            if i.cause == "plan_paused":
                return None            # an account matter: in the history these took one to three days
            began = i.detail.get("since") or i.last_heard or at
            if i.cause == "camera_fault" and not i.detail.get("failed_reboots"):
                when = began + cfg.get("remote_reboot_fix_min", 90) * 60
                if when < at:
                    return None, (f"A remote reboot usually clears a camera fault within about an hour, but this one"
                                  f" began at {self.clock.fmt(depot, began, at)} and is still not fixed, so the job is"
                                  " not left waiting for it.")
                fixes.append((when, f"A remote reboot usually clears this camera fault within about an hour, so"
                                    f" {i.serial} should be recording again by {self.clock.fmt(depot, when, at)}."))
                continue
            when = self._technician_done(depot, began)
            if when < at:
                return None, (f"A technician visit is normally done by {self.clock.fmt(depot, when, at)} for a fault"
                              f" that began at {self.clock.fmt(depot, began, at)}; it has not happened, so the job is"
                              " not left waiting for it.")
            fixes.append((when, f"{i.serial} needs a technician. Visits happen the next working day and have always"
                                f" finished by {cfg.get('technician_done_by_local', '14:00')}, so it should be fixed by"
                                f" {self.clock.fmt(depot, when, at)}."))
        return max(fixes) if fixes else None

    def _technician_done(self, depot, at):
        """The time on the next working day by which a technician visit has always been finished."""
        cfg = self.cfg.jobcover
        hh, mm = (int(x) for x in str(cfg.get("technician_done_by_local", "14:00")).split(":"))
        day = self.clock.local(depot, at).date() + timedelta(days=1)
        while day.weekday() >= 5 and not cfg.get("technician_works_weekends", False):
            day += timedelta(days=1)
        naive = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc).timestamp()
        return int(naive - self.clock.offset_hours(depot, naive) * 3600)

    def _candidates(self, snap, job, asset, h, memo):
        """Vehicles that could take the job if nothing else were being moved.

        Returns ({asset_id: (tier, other use, jobs that day, asset_id)}, tally of why the rest can't).
        Tier 0 is a full replacement; tier 1 is a van without a camera, offered only when the job's own
        vehicle can't be tracked at all.
        """
        cfg = self.cfg.jobcover
        need_camera = cfg["require_working_camera_for_camera_vans"] and bool(h.cameras)
        allow_camera_less = bool(h.location_issue) and cfg["camera_less_fallback_when_untracked"]
        tally = defaultdict(int)
        opts = {}
        for aid, row in self.ref.assets.items():
            if aid == job.asset_id or row.get("type") != asset.get("type") or row.get("depot") != asset.get("depot"):
                continue
            tally["same type"] += 1
            if self.ref.busy(aid, job.start, job.end):
                tally["busy"] += 1
                continue
            c = self.health(snap, aid, memo)
            if c.outage is not None:
                tally["outage"] += 1
                continue
            if not c.eligible:
                tally["unhealthy"] += 1
                continue
            day = sum(1 for j in self.ref.jobs_by_asset.get(aid, []) if abs(j.start - job.start) < 12 * 3600)
            other_use = bool(cfg["prefer_same_use"] and row.get("use") != asset.get("use"))
            if need_camera and not c.cameras:
                tally["no camera"] += 1
                if allow_camera_less:
                    opts[aid] = (1, other_use, day, aid)
                continue
            opts[aid] = (0, other_use, day, aid)
        return opts, tally

    def _allocate(self, jobs, options):
        """Hand out replacements so that as many jobs as possible are covered.

        Jobs are taken in start order. When every vehicle a job could use has already been given to an
        earlier job in the same slot, the earlier job is moved to another vehicle if it has one, instead
        of leaving this job without cover. Returns ({job_id: asset_id}, {asset_id: [jobs moved onto it]}).
        """
        depth = self.cfg.jobcover.get("reshuffle_depth", 3)
        placed, load = {}, defaultdict(list)

        def clash(a, job):
            return [j for j in load[a] if j.start < job.end and job.start < j.end]

        def order(job, tier_max, locked):
            opts = options[job.job_id][0]
            mine = {a for a, js in load.items() if any(j.asset_id == job.asset_id for j in js)}
            # full replacement first, then the vehicle already covering this asset's other jobs, same use, lightest day
            return sorted((a for a in opts if opts[a][0] <= tier_max and a not in locked),
                          key=lambda a: (opts[a][0], a not in mine, opts[a][1], opts[a][2] + len(load[a]), a))

        def place(job, tier_max, depth, locked):
            cands = order(job, tier_max, locked)
            for a in cands:
                if not clash(a, job):
                    load[a].append(job)
                    placed[job.job_id] = a
                    return True
            if depth <= 0:
                return False
            for a in cands:
                saved = ({k: list(v) for k, v in load.items()}, dict(placed))
                out = clash(a, job)
                for j in out:
                    load[a].remove(j)
                    del placed[j.job_id]
                load[a].append(job)
                placed[job.job_id] = a
                # a job that is pushed out never ends up worse off than it was
                if all(place(j, max(tier_max, options[j.job_id][0][a][0]), depth - 1, locked | {a}) for j in out):
                    return True
                load.clear()
                load.update(saved[0])
                placed.clear()
                placed.update(saved[1])
            return False

        for job in jobs:
            if not place(job, 0, depth, frozenset()):
                place(job, 1, depth, frozenset())
        return placed, load

    def _why(self, job, asset, opts, tally, load):
        """Words for why the other vehicles of this type can't take the job: (kind, 'N other vans', detail)."""
        tally = defaultdict(int, tally)
        for a, rank in opts.items():
            if any(j.job_id != job.job_id and j.start < job.end and job.start < j.end for j in load[a]):
                tally["taken"] += 1
                if rank[0]:
                    tally["no camera"] -= 1
        kind = asset.get("type") or "vehicle"

        def n(key, one, many):
            return f"{tally[key]} {one if tally[key] == 1 else many}"
        parts = []
        if tally["busy"]:
            parts.append(n("busy", "already has", "already have") + " a job in that slot")
        if tally["taken"]:
            parts.append(n("taken", "is", "are") + " covering another moved job in that slot")
        if tally["unhealthy"]:
            parts.append(n("unhealthy", "can't be seen or recorded itself", "can't be seen or recorded themselves"))
        if tally["outage"]:
            parts.append(n("outage", "is", "are") + " on the carrier that is down")
        if tally["no camera"] > 0:
            parts.append(n("no camera", "is free but has", "are free but have") + " no camera")
        detail = "; ".join(parts) if parts else "there are none"
        others = f"{tally['same type']} other {kind}{'' if tally['same type'] == 1 else 's'}"
        return kind, others, detail

    def _under_way(self, snap, depot, at, memo):
        """Jobs that had already started at `at` on a vehicle that can't be seen or recorded.

        The contract's `jobs` list covers only jobs that start in the window, so these are said here:
        it is too late to swap them, but the manager still needs to know.
        """
        by_asset = defaultdict(list)
        for job in self.ref.jobs_under_way(depot, at):
            if job.asset_id in self.ref.assets and self.health(snap, job.asset_id, memo).problem:
                by_asset[job.asset_id].append(job)
        out = []
        for asset_id, jobs in sorted(by_asset.items()):
            h = memo[asset_id]
            issues = [i for i in (h.location_issue, h.video_issue) if i]
            evidence = []
            for i in issues:
                evidence += [x for x in i.evidence if x not in evidence]
            names = ", ".join(f"{j.job_id} (until {self.clock.fmt(depot, j.end, at)})" for j in jobs)
            out.append({
                "kind": "job_under_way", "asset_ids": [asset_id], "job_ids": [j.job_id for j in jobs],
                "problem": h.problem, "likely_cause": issues[0].cause,
                "manager_action": (f"{names} is already under way on {asset_id}, which is {BLIND[h.problem]} right now."
                                   " It is too late to swap: call the crew."),
                "reasons": [r for i in issues for r in i.reasons], "evidence": evidence,
            })
        return out

    def _notices(self, snap, depot, watch, memo):
        """Things the manager should know once, not once per job. No job listed here is moved."""
        out = []
        by_outage = defaultdict(list)
        for asset_id, jobs in sorted(watch.items()):
            h = memo[asset_id]
            causes = {i.cause for i in h.soft}
            if h.outage is not None and "carrier_outage" in causes:
                by_outage[(h.outage.carrier, h.outage.depot)].append((asset_id, jobs))
                continue
            i = h.soft[0]
            out.append({
                "kind": "watch", "asset_ids": [asset_id], "job_ids": [j.job_id for j in jobs],
                "likely_cause": i.cause,
                "manager_action": f"No change for {asset_id} yet. Check again before its job starts.",
                "reasons": list(i.reasons), "evidence": list(i.evidence),
            })
        for key, o in sorted(snap.outages().items()):
            if o.depot != depot:
                continue
            hit = by_outage.get(key, [])
            n = sum(len(j) for _, j in hit)
            out.insert(0, {
                "kind": "carrier_outage", "carrier": o.carrier, "depot": o.depot, "since": iso(o.start),
                "devices_silent": len(o.serials), "asset_ids": [a for a, _ in hit],
                "job_ids": [j.job_id for _, jobs in hit for j in jobs], "likely_cause": "carrier_outage",
                "manager_action": (f"Do not move any jobs for this. {n} job(s) in the window are on affected vehicles; they run as"
                                   f" planned and positions fill in when {o.carrier} is back. Vehicles on {o.carrier} are not"
                                   " offered as replacements until then."),
                "reasons": [snap.outage_reason(o)], "evidence": list(o.evidence),
            })
        return out
