"""Job cover for the fleet manager.

For every job starting in the window: can its vehicle be seen (tracker) and
recorded (camera)? If not, move the job to another vehicle that can take it,
or say plainly that none can and why.

Rules built in here:
* Missing a real problem is worse than a false alarm, so a confirmed fault is
  flagged for every job in the window, however far ahead.
* A job is either moved (`swap`) or reported as having no replacement (`gap`).
* A wrong swap costs the crew time, so a swap is only offered to a vehicle of
  the same type and depot that is free for the job's whole slot and is itself
  visibly healthy - anything silent, unexplained or inside a carrier outage is
  never offered as a replacement.
* When many vehicles are quiet for one outside reason (a carrier outage), no
  jobs are moved and those jobs are not listed: the network is down, not the
  vehicles. Each device's status says so, with the same reason for all of them.
"""
from collections import defaultdict

from .status import ACTION
from .timeutil import iso

NOT_THE_VEHICLE = ("carrier_outage",)     # causes that nothing done to the vehicle can fix


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

        seen = any(t.ok for t in h.trackers)
        if not seen:
            hard = [t for t in h.trackers if t.state == ACTION and t.cause not in NOT_THE_VEHICLE]
            if hard:
                h.location_issue = max(hard, key=lambda t: t.last_heard or 0)
            else:
                h.soft += h.trackers
        for c in h.cameras:
            if c.ok:
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
        assigned = defaultdict(list)      # replacement asset -> [(start, end)] handed out in this answer
        moved_to = {}                     # troubled asset -> replacement already chosen for it
        items = []

        for job in jobs:
            asset = self.ref.assets.get(job.asset_id)
            if not asset:
                continue
            h = self.health(snap, job.asset_id, memo)
            problem = h.problem
            if not problem:
                continue

            issues = [i for i in (h.location_issue, h.video_issue) if i]
            reasons = [r for i in issues for r in i.reasons]
            evidence = []
            for i in issues:
                evidence += [x for x in i.evidence if x not in evidence]
            slot = f"{self.clock.fmt(depot, job.start, at)}-{self.clock.fmt(depot, job.end, job.start)}"
            item = {"job_id": job.job_id, "asset_id": job.asset_id, "problem": problem, "decision": None,
                    "swap_to": None, "manager_action": "", "reasons": reasons, "evidence": evidence}

            target, why_not = self._replacement(snap, job, asset, h, memo, assigned, moved_to.get(job.asset_id))
            if target:
                assigned[target].append((job.start, job.end))
                moved_to[job.asset_id] = target
                t = self.ref.assets[target]
                item["decision"], item["swap_to"] = "swap", target
                cam = " with a working camera" if snap.asset_cameras(target) else ""
                item["reasons"].append(
                    f"{target} is a {t.get('type')} at {snap.depot_name(depot)}{cam}, has no other job during {slot},"
                    " and its tracker is reporting normally.")
                item["manager_action"] = f"Move {job.job_id} ({slot}, {job.job_type}) from {job.asset_id} to {target}."
            else:
                item["decision"] = "gap"
                item["reasons"].append(why_not)
                blind = {"no_location": "untracked", "no_video": "unrecorded", "both": "untracked and unrecorded"}[problem]
                item["manager_action"] = (
                    f"No replacement for {job.job_id} ({slot}). Reschedule it, or get {job.asset_id} fixed before then;"
                    f" otherwise it goes out {blind}.")
            items.append(item)

        return {"depot": depot, "at": iso(at), "hours": hours, "jobs": items}

    def _replacement(self, snap, job, asset, h, memo, assigned, previous):
        """Pick the replacement vehicle, or explain why there is none. Returns (asset_id, reason)."""
        cfg = self.cfg.jobcover
        need_camera = cfg["require_working_camera_for_camera_vans"] and bool(h.cameras)
        tally = defaultdict(int)
        best = None
        for aid, row in self.ref.assets.items():
            if aid == job.asset_id or row.get("type") != asset.get("type") or row.get("depot") != asset.get("depot"):
                continue
            tally["same type"] += 1
            if self.ref.busy(aid, job.start, job.end) or any(s < job.end and job.start < e for s, e in assigned[aid]):
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
            rank = (aid != previous,
                    cfg["prefer_same_use"] and row.get("use") != asset.get("use"),
                    day + len(assigned[aid]), aid)
            if need_camera and not c.cameras:
                tally["no camera"] += 1
                continue
            if best is None or rank < best[0]:
                best = (rank, aid)
        if best:
            return best[1], ""
        kind = asset.get("type") or "vehicle"

        def n(key, one, many):
            return f"{tally[key]} {one if tally[key] == 1 else many}"
        parts = []
        if tally["busy"]:
            parts.append(n("busy", "already has", "already have") + " a job in that slot")
        if tally["unhealthy"]:
            parts.append(n("unhealthy", "can't be seen or recorded itself", "can't be seen or recorded themselves"))
        if tally["outage"]:
            parts.append(n("outage", "is", "are") + " on the carrier that is down")
        if tally["no camera"]:
            parts.append(n("no camera", "is free but has", "are free but have") + " no camera")
        detail = "; ".join(parts) if parts else "there are none"
        others = f"{tally['same type']} other {kind}{'' if tally['same type'] == 1 else 's'}"
        tail = (" A van without a camera can't record the job, so it is not offered as a replacement."
                if tally["no camera"] else "")
        return None, (f"No {kind} at {snap.depot_name(job.depot)} can take this job: of the {others},"
                      f" {detail}.{tail}")
