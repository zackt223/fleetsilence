"""Decides what a device's silence means, as of a given moment.

Everything here answers one question: is this device quiet because that is
normal, or because something is wrong - and what is the most likely reason?
The trade-off for the fleet manager is that a missed real problem is worse
than a false alarm, so unexplained silence is never called "expected".
"""
from collections import defaultdict
from statistics import median

from .timeutil import duration, iso

REPORTING, QUIET, ACTION, UNKNOWN = "reporting", "quiet_expected", "needs_action", "unknown"


class Assessment:
    __slots__ = ("serial", "asset_id", "depot", "family", "state", "cause", "confidence",
                 "last_heard", "reasons", "evidence", "detail")

    def __init__(self, serial, asset_id, depot, family):
        self.serial, self.asset_id, self.depot, self.family = serial, asset_id, depot, family
        self.state, self.cause, self.confidence = UNKNOWN, "unknown", 0.3
        self.last_heard = None
        self.reasons, self.evidence = [], []
        self.detail = {}     # extra facts for job cover (failed reboots, when the fault began, ...)

    def set(self, state, cause, confidence, *reasons):
        self.state, self.cause, self.confidence = state, cause, confidence
        self.reasons.extend(r for r in reasons if r)
        return self

    @property
    def ok(self):
        """True if the device gives no reason to doubt the asset can be seen or recorded."""
        return self.state in (REPORTING, QUIET)

    def to_json(self, at):
        return {
            "serial": self.serial,
            "asset_id": self.asset_id or None,
            "at": iso(at),
            "state": self.state,
            "likely_cause": self.cause,
            "confidence": round(self.confidence, 2),
            "last_heard_at": iso(self.last_heard),
            "reasons": self.reasons or ["No evidence either way yet."],
            "evidence": self.evidence,
        }


class Outage:
    __slots__ = ("carrier", "depot", "start", "serials", "driving", "evidence")

    def __init__(self, carrier, depot, start, serials, driving, evidence):
        self.carrier, self.depot, self.start = carrier, depot, start
        self.serials, self.driving, self.evidence = serials, driving, evidence


class Vehicle:
    """What the asset's tracker says the vehicle is doing."""
    __slots__ = ("motion", "in_contact", "since", "tracker", "event", "live_since")

    def __init__(self, motion=None, in_contact=False, since=None, tracker=None, event=None, live_since=None):
        self.motion, self.in_contact, self.since, self.tracker, self.event = motion, in_contact, since, tracker, event
        self.live_since = live_since   # since when the tracker has been in unbroken, on-time contact


class Engine:
    def __init__(self, cfg, ref, store, clock):
        self.cfg, self.ref, self.store, self.clock = cfg, ref, store, clock

    def family(self, serial):
        dev = self.ref.devices.get(serial)
        if dev and dev.get("family"):
            return dev["family"]
        s = serial.upper()
        if s.startswith("BX"):
            return "BX"
        return s[:1] if s[:1] in ("T", "C", "A") else ""

    def known(self, serial):
        return serial in self.ref.devices or self.store.knows(serial)

    def snapshot(self, at):
        return Snapshot(self, at)


class Snapshot:
    """All questions about one moment. Results are memoised, so a job-cover
    request that looks at hundreds of devices does each piece of work once."""

    def __init__(self, engine, at):
        self.e, self.at = engine, at
        self.cfg, self.ref, self.store, self.clock = engine.cfg, engine.ref, engine.store, engine.clock
        self._summ, self._assess, self._vehicle = {}, {}, {}
        self._outages = self._vin_map = self._trackers = None
        self._claimed = {}
        self._earlier = None

    # ------------------------------------------------------------ basics
    def summ(self, serial):
        if serial not in self._summ:
            self._summ[serial] = self.store.summary(serial, self.at)
        return self._summ[serial]

    def ids(self, *events, limit=12):
        out = []
        for ev in events:
            if ev is None:
                continue
            for i in ev.ids_at(self.at):
                if i not in out:
                    out.append(i)
        return out[:limit]

    def when(self, depot, t):
        return self.clock.fmt(depot, t, self.at)

    def trackers(self):
        """Every tracker serial we know of, from the records or from the events."""
        if self._trackers is None:
            found = {s for s, d in self.ref.devices.items() if d.get("family") == "T"}
            found |= {s for s in self.store.logs if self.e.family(s) == "T"}
            self._trackers = sorted(found)
        return self._trackers

    def vin_map(self):
        """asset_id -> trackers whose own messages carry that asset's VIN.
        The records are not always what is on the vehicle; the VIN is."""
        if self._vin_map is None:
            m, rev = defaultdict(list), {}
            for serial in self.trackers():
                s = self.summ(serial)
                v = s.latest.get("vin") if s else None
                asset = self.ref.vin_asset.get(v.payload.get("vin")) if v else None
                if asset:
                    m[asset].append(serial)
                    rev[serial] = asset
            self._vin_map, self._claimed = m, rev
        return self._vin_map

    def asset_of(self, serial):
        """The asset the device is really on: by its own evidence if there is any, else by the records."""
        if self.e.family(serial) == "T":
            self.vin_map()
            if serial in self._claimed:
                return self._claimed[serial]
        return (self.ref.devices.get(serial) or {}).get("asset_id", "")

    def group(self, serial):
        carrier = (self.ref.devices.get(serial) or {}).get("carrier", "")
        depot = self.ref.depot_of(self.asset_of(serial))
        return (carrier, depot) if carrier and depot else None

    def asset_trackers(self, asset_id):
        out = [d["serial"] for d in self.ref.asset_devices.get(asset_id, [])
               if d.get("family") == "T" and d.get("status") != "stock"]
        for s in self.vin_map().get(asset_id, []):
            if s not in out:
                out.append(s)
        return out

    def asset_cameras(self, asset_id):
        return [d["serial"] for d in self.ref.asset_devices.get(asset_id, [])
                if d.get("family") == "C" and d.get("status") == "active"]

    # ------------------------------------------------------------ tracker facts
    @staticmethod
    def motion(s):
        m = s.latest.get("motion") if s else None
        if m is None:
            return None
        if m.type == "ignition_on" or (m.type == "position" and m.payload.get("ignition")):
            return "driving"
        return "parked"

    def power(self, s):
        """Unresolved power loss on a wired tracker, or None.
        Returns (first alert, low-voltage alert or None, confirmed)."""
        alerts = [x for x in s.rare if x.type == "power_disconnect"
                  or (x.type == "low_voltage" and x.payload.get("source", "backup_battery") == "backup_battery")]
        if not alerts:
            return None
        pds = [x for x in alerts if x.type == "power_disconnect"]
        start = max(pds, key=lambda x: x.t) if pds else max(alerts, key=lambda x: x.t)
        if any(x.type == "power_restore" and x.t >= start.t for x in s.rare):
            return None
        if s.last and s.last.t > start.t + self.cfg.power["backup_window_min"] * 60:
            return None      # still talking long after the backup battery would have died: power is back
        lvs = [x for x in alerts if x.type == "low_voltage" and x.t >= start.t]
        lv = max(lvs, key=lambda x: x.t) if lvs else None
        confirmed = lv is not None or self.at - start.t >= self.cfg.power["confirm_after_min"] * 60
        return start, lv, confirmed

    def tracker_silent(self, s):
        """(motion, silent?) by the tracker's own schedule."""
        mo = self.motion(s)
        gap = self.at - s.last_contact
        if mo == "driving":
            return mo, gap > self.cfg.tracker["drive_quiet_min"] * 60
        return mo, gap > self.cfg.tracker["heartbeat_late_min"] * 60

    # ------------------------------------------------------------ carrier outages
    def outages(self):
        """Carrier outages in progress: {(carrier, depot): Outage}.

        An outage is called when most trackers that were mid-drive on one
        carrier at one depot went silent at about the same time. One vehicle in
        a dead zone never qualifies; it takes a crowd."""
        if self._outages is not None:
            return self._outages
        cfg = self.cfg.outage
        quiet = self.cfg.tracker["drive_quiet_min"] * 60
        window = cfg["cluster_window_min"] * 60
        driving, silent = defaultdict(int), defaultdict(list)
        for serial in self.trackers():
            s = self.summ(serial)
            if not s or s.last_contact is None or s.last_contact < self.at - cfg["lookback_h"] * 3600:
                continue
            if self.power(s):
                continue
            g = self.group(serial)
            if not g:
                continue
            if self.motion(s) != "driving":
                continue
            driving[g] += 1
            if self.at - s.last_contact > quiet:
                silent[g].append((s.last_contact, serial))
        found = {}
        for g, members in silent.items():
            mid = median(t for t, _ in members)
            cluster = sorted((t, sn) for t, sn in members if abs(t - mid) <= window)
            # Vehicles silent since long before this cluster are not part of it.
            base = driving[g] - (len(members) - len(cluster))
            if len(cluster) >= cfg["min_devices"] and len(cluster) / max(base, 1) >= cfg["min_fraction"]:
                serials = [sn for _, sn in cluster]
                evidence = self.ids(*[self.summ(sn).last for sn in serials[:6]])
                found[g] = Outage(g[0], g[1], cluster[0][0], serials, base, evidence)
        self._outages = found
        return found

    def outage_for(self, serial, expected_by):
        """The outage that explains this device's silence, if the device was
        due to report at or after the outage began."""
        g = self.group(serial)
        o = self.outages().get(g) if g else None
        if o and expected_by >= o.start - self.cfg.outage["cluster_window_min"] * 60:
            return o
        return None

    def outage_just_ended(self, serial, expected_by):
        """An outage on this device's carrier that was still running one camera report cycle ago.
        Trackers flush their buffers the moment service returns; cameras need a cycle to catch up."""
        if self._earlier is None:
            self._earlier = Snapshot(self.e, self.at - self.cfg.camera["quiet_min"] * 60)
        return self._earlier.outage_for(serial, expected_by)

    def outage_reason(self, o):
        return (f"{len(o.serials)} of {o.driving} {o.carrier} trackers that were driving out of "
                f"{self.depot_name(o.depot)} went silent together around {self.when(o.depot, o.start)}; "
                f"other carriers kept reporting. The network is down, not the vehicles.")

    def depot_name(self, depot):
        return (self.cfg.depots.get(depot) or {}).get("name", depot)

    # ------------------------------------------------------------ vehicle
    def vehicle(self, asset_id):
        if asset_id in self._vehicle:
            return self._vehicle[asset_id]
        best = None
        for serial in self.asset_trackers(asset_id):
            s = self.summ(serial)
            if s and s.last and s.latest.get("motion") and (best is None or s.last.t > best[1].last.t):
                best = (serial, s)
        v = Vehicle()
        if best:
            serial, s = best
            mo, silent = self.tracker_silent(s)
            # When the current stint began: the ignition event that started it, if we have it.
            key = "ignition_on" if mo == "driving" else "ignition_off"
            other = "ignition_off" if mo == "driving" else "ignition_on"
            k, o = s.latest.get(key), s.latest.get(other)
            since, ev = None, s.latest["motion"]
            if k and (o is None or k.t >= o.t):
                since, ev = k.t, k
            v = Vehicle(mo, not silent and not self.power(s), since, serial, ev, s.live_since)
        self._vehicle[asset_id] = v
        return v

    # ------------------------------------------------------------ assessments
    def assess(self, serial):
        if serial in self._assess:
            return self._assess[serial]
        fam = self.e.family(serial)
        asset = self.asset_of(serial)
        a = Assessment(serial, asset, self.ref.depot_of(asset), fam)
        s = self.summ(serial)
        if s and s.last:
            a.last_heard = s.last.t
        self._assess[serial] = a   # set before the work: camera and tracker look at each other
        dev = self.ref.devices.get(serial) or {}
        if not s or s.last is None:
            if not (s and self._plan_paused(a, s)):
                what = {"stock": "It is a spare in stock.", "removed": "The records show it was removed."}
                a.set(UNKNOWN, "unknown", 0.3, "Nothing has ever been received from this device. "
                      + what.get(dev.get("status"), "There is not enough evidence to say why."))
        elif self._plan_paused(a, s):
            pass
        elif fam == "T":
            self._tracker(a, s, dev)
        elif fam == "C":
            self._camera(a, s, dev)
        elif fam in ("A", "BX"):
            self._tag(a, s, dev)
        else:
            self._generic(a, s)
        return a

    def _plan_paused(self, a, s):
        p = s.latest.get("plan_change")
        if p is None or p.payload.get("status") not in ("paused", "ended"):
            return False
        st = p.payload.get("status")
        a.set(ACTION, "plan_paused", 0.9,
              f"The device's {p.payload.get('plan') or 'data'} plan was {st} on {self.when(a.depot, p.t)}"
              f" ({p.payload.get('reason') or 'no reason given'}). The device works but is not allowed to send;"
              f" this is fixed on the account, not on the vehicle.")
        a.evidence = self.ids(p, s.last)
        a.detail["since"] = p.t
        return True

    def _replacement(self, serial, asset, s):
        """Another tracker now reporting from this asset (same VIN), newer than this one."""
        for other in self.vin_map().get(asset, []):
            if other == serial:
                continue
            o = self.summ(other)
            if o and o.last and s.first and o.last.t > s.last.t and o.first.t > s.first.t:
                return other, o
        return None

    def _tracker(self, a, s, dev):
        cfg, at, depot = self.cfg.tracker, self.at, a.depot
        last = s.last
        heard = f"Last heard {self.when(depot, last.t)} ({last.type.replace('_', ' ')})"

        pw = self.power(s)
        if pw:
            start, lv, confirmed = pw
            a.evidence = self.ids(start, lv, last)
            a.detail["since"] = start.t
            what = "sent a power-disconnect alert" if start.type == "power_disconnect" else "reported low backup voltage"
            if confirmed:
                extra = (f"then a backup-battery low-voltage alert at {self.when(depot, lv.t)}" if lv else
                         f"and power has not come back in {duration(at - start.t)}")
                a.set(ACTION, "power_loss", 0.9 if lv else 0.75,
                      f"The tracker {what} at {self.when(depot, start.t)}, {extra}. {heard}.",
                      "It is running on (or has drained) its small backup battery; someone has to reconnect it on site.")
            else:
                a.set(UNKNOWN, "power_loss", 0.4,
                      f"The tracker {what} at {self.when(depot, start.t)}, {duration(at - start.t)} ago.",
                      f"Short disconnects usually restore themselves within {self.cfg.power['confirm_after_min']} min;"
                      " too early to call it a fault.")
            return

        mo, silent = self.tracker_silent(s)
        gap = at - s.last_contact
        if not silent:
            a.evidence = self.ids(last)
            if mo == "driving":
                a.set(REPORTING, "none", 0.95, f"Ignition is on and positions are arriving on schedule. {heard}.")
            elif mo == "parked":
                m = s.latest.get("ignition_off")
                since = f" since {self.when(depot, m.t)}" if m else ""
                a.set(QUIET, "parked_normal", 0.9,
                      f"The vehicle is parked with the ignition off{since}; heartbeats are on time. {heard}.")
                a.evidence = self.ids(last, m)
            else:
                a.set(REPORTING, "none", 0.6, f"{heard}.")
            return

        # Silent beyond its own schedule. Work through the explanations, most specific first.
        a.evidence = self.ids(last)
        rep = self._replacement(a.serial, a.asset_id, s)
        if rep:
            other, o = rep
            fixed = (self.ref.devices.get(other) or {}).get("asset_id") == a.asset_id and \
                (self.ref.devices.get(other) or {}).get("status") == "active"
            a.evidence = self.ids(last, o.latest.get("vin"), o.first)
            a.detail["replaced_by"] = other
            if fixed:
                a.set(QUIET, "device_swapped", 0.8,
                      f"{heard}. {other} has been reporting from {a.asset_id} since {self.when(depot, o.first.t)}"
                      " and the records already list it; this unit was replaced.")
            else:
                a.set(ACTION, "device_swapped", 0.85,
                      f"{heard}. A different tracker, {other}, has been reporting {a.asset_id}'s VIN since"
                      f" {self.when(depot, o.first.t)}: the unit was swapped but the records still point here.",
                      f"The vehicle itself is visible through {other}; support should correct the device record.")
            return
        expected_by = s.last_contact if mo == "driving" else s.last_contact + cfg["heartbeat_interval_min"] * 60
        o = self.outage_for(a.serial, expected_by)
        if o:
            a.evidence = self.ids(last, limit=2) + [i for i in o.evidence if i not in a.evidence][:8]
            a.detail["outage"] = o
            a.set(ACTION, "carrier_outage", 0.85, f"{heard}. " + self.outage_reason(o),
                  "Nothing to fix on the vehicle; follow up with the carrier. Buffered positions should arrive when service returns.")
            return

        if dev.get("status") == "removed":
            a.set(QUIET, "device_swapped", 0.7, f"{heard}. The records show this unit was removed from {a.asset_id or 'its asset'}.")
            return

        if mo == "driving":
            if gap <= cfg["coverage_gap_max_min"] * 60:
                a.set(UNKNOWN, "coverage_gap", 0.6,
                      f"{heard} while driving, {duration(gap)} ago, with no power alert and no wider outage.",
                      "Most likely a dead zone: in the history these lasted up to an hour and a half and the buffered"
                      f" positions arrived afterwards. It becomes a fault after {cfg['coverage_gap_max_min']} min.")
            else:
                a.set(ACTION, "unknown", 0.5,
                      f"{heard} while driving and nothing for {duration(gap)}: longer than any coverage gap in the history"
                      f" ({cfg['coverage_gap_max_min']} min limit), with no power alert, no outage and no replacement unit seen.",
                      "Treat the vehicle as not trackable until someone checks it.")
        else:
            a.set(ACTION, "unknown", 0.5,
                  f"{heard}; a parked tracker should send a heartbeat every {duration(cfg['heartbeat_interval_min'] * 60)}"
                  f" and it is now {duration(gap)} since the last one"
                  f" (late after {duration(cfg['heartbeat_late_min'] * 60)}).",
                  "No power alert, outage or replacement unit explains it. Treat the vehicle as not trackable until someone checks it.")

    def _camera(self, a, s, dev):
        cfg, at, depot = self.cfg.camera, self.at, a.depot
        cs = s.latest.get("camera_status")
        if cs is None:
            return self._generic(a, s)
        status = cs.payload.get("status")
        heard = f"Last camera report {self.when(depot, cs.t)} ({status})"
        veh = self.vehicle(a.asset_id)
        gap = at - s.last_contact
        quiet = cfg["quiet_min"] * 60
        # Only trust "the vehicle is driving" as a reason to expect the camera once the tracker has been
        # live for a while: right after an outage or dead zone the camera needs a report cycle to catch up.
        driving_live = (veh.motion == "driving" and veh.in_contact and veh.live_since is not None
                        and at - veh.live_since >= quiet)

        if status == "storage_error":
            reboots = sorted((x for x in s.rare if x.type == "command" and x.payload.get("command") == "reboot" and x.t >= cs.t),
                             key=lambda x: x.t)
            failed = [x for x in reboots if at - x.t >= cfg["reboot_settle_min"] * 60]
            a.evidence = self.ids(cs, *reboots)
            a.detail.update(since=cs.t, reboots=len(reboots), failed_reboots=len(failed))
            code = f" ({cs.payload['code']})" if cs.payload.get("code") else ""
            moving = " while the vehicle kept driving" if veh.motion == "driving" and veh.in_contact else ""
            if not reboots:
                tail = "No remote reboot has been tried yet."
            elif failed:
                tail = (f"{len(failed)} remote reboot{'s' if len(failed) > 1 else ''} since then did not bring it back;"
                        " it most likely needs a technician (storage card).")
            else:
                tail = f"A remote reboot was sent at {self.when(depot, reboots[-1].t)}; waiting to see if recording resumes."
            a.set(ACTION, "camera_fault", 0.9 if failed else 0.8,
                  f"Camera reported storage_error{code} at {self.when(depot, cs.t)}{moving} and has recorded nothing since. {tail}")
            return

        def outage_or(expected_by, fallback):
            o = self.outage_for(a.serial, expected_by)
            if o:
                a.detail["outage"] = o
                a.evidence = self.ids(cs, limit=2) + o.evidence[:8]
                a.set(ACTION, "carrier_outage", 0.8, f"{heard}. " + self.outage_reason(o))
                return
            o = self.outage_just_ended(a.serial, expected_by)
            if o:
                a.evidence = self.ids(cs, limit=2) + self.ids(*[self.summ(sn).last for sn in o.serials[:4]])
                a.set(UNKNOWN, "carrier_outage", 0.5,
                      f"{heard}. {o.carrier} service at {self.depot_name(o.depot)} has only just come back after an outage"
                      f" that began around {self.when(o.depot, o.start)}; the camera should report again within a few minutes.")
                return
            fallback()

        if status == "sleeping":
            woke_late = (driving_live and veh.since and veh.since > cs.t
                         and at - veh.since > cfg["wake_grace_min"] * 60)
            if woke_late:
                def fault():
                    a.evidence = self.ids(cs, veh.event)
                    a.detail["since"] = veh.since
                    a.set(ACTION, "camera_fault", 0.6,
                          f"{heard}, but the ignition has been on since {self.when(depot, veh.since)} and the camera"
                          f" has not woken up ({duration(at - veh.since)}). It normally boots within a minute.")
                outage_or(veh.since, fault)
            else:
                a.evidence = self.ids(cs, veh.event if veh.motion == "parked" else None)
                off = f" Ignition went off at {self.when(depot, veh.since)}." if veh.motion == "parked" and veh.since else ""
                a.set(QUIET, "sleeping_by_design", 0.9,
                      f"Camera went to sleep at {self.when(depot, cs.t)}, {cfg['sleep_after_ignition_off_min']} min after"
                      f" ignition off, as designed.{off} It wakes with the ignition.")
            return

        if gap <= quiet:
            a.evidence = self.ids(cs)
            a.set(REPORTING, "none", 0.95, f"Camera is {status}; reports are arriving on schedule. {heard}.")
            return

        # Was recording, now silent.
        if veh.motion == "parked":
            # A parked vehicle's camera is asleep whether or not its "sleeping" message got through.
            a.evidence = self.ids(cs, veh.event)
            off = f"the ignition went off at {self.when(depot, veh.since)}" if veh.since else "the vehicle is parked"
            clean = veh.since is not None and veh.since - cs.t <= quiet
            note = "" if clean else (" Its reports stopped before the ignition went off (a paused plan or a network gap"
                                     " would do that), so this is confirmed only when it wakes with the next ignition.")
            a.set(QUIET, "sleeping_by_design", 0.8 if clean else 0.55,
                  f"{heard}; {off} and the camera sleeps"
                  f" {cfg['sleep_after_ignition_off_min']} min after ignition off, by design.{note}")
        elif driving_live:
            def fault():
                a.evidence = self.ids(cs, veh.event)
                a.detail["since"] = cs.t
                a.set(ACTION, "camera_fault", 0.6,
                      f"{heard}, then nothing for {duration(gap)} while the tracker shows the vehicle still driving."
                      " A recording camera reports every 5 min; it has stopped without an error message.")
            outage_or(s.last_contact, fault)
        else:
            def unknown():
                ta = self.assess(veh.tracker) if veh.tracker else None
                a.evidence = self.ids(cs)
                if ta and ta.cause == "coverage_gap":
                    a.set(UNKNOWN, "coverage_gap", 0.5,
                          f"{heard}; the vehicle's tracker went quiet at about the same time, most likely a dead zone.")
                else:
                    a.set(UNKNOWN, "unknown", 0.3,
                          f"{heard}, nothing for {duration(gap)}, and the vehicle's tracker can't confirm whether the"
                          " ignition is on. Not enough evidence yet.")
            outage_or(s.last_contact, unknown)

    def _tag(self, a, s, dev):
        cfg, at, depot = self.cfg.tag, self.at, a.depot
        tg = s.latest.get("tag") or s.last
        batt = tg.payload.get("battery_pct")
        low = (isinstance(batt, (int, float)) and batt <= cfg["battery_low_pct"]) or bool(tg.payload.get("low_battery"))
        gap = at - s.last_contact
        bt = f"battery {batt:g} %" if isinstance(batt, (int, float)) else "battery level unknown"
        heard = f"Last check-in {self.when(depot, tg.t)} ({bt})"
        a.evidence = self.ids(tg)
        late = gap > cfg["late_after_h"] * 3600
        if low:
            flag = " and the device raised its low-battery flag" if tg.payload.get("low_battery") else ""
            if late:
                a.set(ACTION, "battery_low", 0.85,
                      f"{heard}{flag}, then nothing for {duration(gap)}. In the history every tag that went quiet at"
                      f" {cfg['battery_low_pct']} % or less needed a new battery pack.")
            else:
                a.set(ACTION, "battery_low", 0.75,
                      f"{heard}{flag}. It is still checking in but will go quiet soon; plan a battery replacement.")
            return
        if not late:
            a.set(REPORTING, "none", 0.9, f"Daily check-ins are on time. {heard}.")
            return

        o = self.outage_for(a.serial, tg.t + 86400)
        if o:
            a.detail["outage"] = o
            a.evidence = self.ids(tg) + o.evidence[:8]
            a.set(ACTION, "carrier_outage", 0.6, f"{heard}; its next check-in was due during a carrier outage. " + self.outage_reason(o))
            return

        missed = []
        k = 1
        slack = cfg["late_after_h"] * 3600 - 86400     # the same slack that made it "late" in the first place
        while tg.t + k * 86400 + slack <= at:
            missed.append(tg.t + k * 86400)
            k += 1
        lows = [self.ref.weather.get((depot, self.clock.local_date(depot, t))) for t in missed]
        if missed and len(missed) <= cfg["cold_max_missed_days"] and all(x is not None and x <= cfg["cold_low_c"] for x in lows):
            nights = ", ".join(f"{x:g} C" for x in lows)
            a.set(QUIET, "cold_weather", 0.75,
                  f"{heard}; it has missed {len(missed)} daily check-in{'s' if len(missed) > 1 else ''}, each after a night at"
                  f" {cfg['cold_low_c']} C or colder ({nights}). Batteries lose capacity in the cold; in the history these tags"
                  " checked in again by themselves once it warmed up.")
            return

        why = self._downtime(a, tg)
        if why:
            a.set(QUIET, "planned_downtime", 0.65, f"{heard}, nothing for {duration(gap)}. {why}")
            return
        cold = "" if not missed else f" The nights since were not cold enough to explain it ({cfg['cold_low_c']} C or colder)."
        a.set(ACTION, "unknown", 0.5,
              f"{heard}, then nothing for {duration(gap)}; it should check in once a day.{cold}"
              " The battery was healthy and nothing else explains the silence, so someone should look at it.")

    def _downtime(self, a, tg):
        """A reason to believe the asset was taken out of use on purpose, or None."""
        asset = self.ref.assets.get(a.asset_id) or {}
        use, depot = asset.get("use", ""), a.depot
        cfg = self.cfg.tag
        peers = [d["serial"] for aid, row in self.ref.assets.items() if row.get("use") == use and row.get("depot") == depot
                 for d in self.ref.asset_devices.get(aid, []) if d.get("family") in ("A", "BX") and d.get("status") == "active"]
        day = self.clock.local_date(depot, tg.t)
        same = 0
        for p in peers:
            ps = self.summ(p)
            pt = ps.latest.get("tag") if ps else None
            if pt and self.at - ps.last_contact > cfg["late_after_h"] * 3600 and self.clock.local_date(depot, pt.t) == day:
                same += 1
        if use and same >= cfg["downtime_peer_min"] and same / max(len(peers), 1) >= cfg["downtime_peer_fraction"]:
            return (f"{same} of the {len(peers)} {use} assets at {self.depot_name(depot)} stopped checking in on the same day"
                    f" ({day}) with healthy batteries, which points to the equipment being put away together, not to faults.")
        season = (self.cfg.get("seasonal_downtime") or {}).get(use)
        if season:
            md = self.clock.local(depot, self.at).strftime("%m-%d")
            lo, hi = season["from"], season["to"]
            if (lo <= md <= hi) if lo <= hi else (md >= lo or md <= hi):
                return f"{use.capitalize()} equipment is out of season ({lo} to {hi}), so it is expected to be stored."
        return None

    def _generic(self, a, s):
        gap = self.at - s.last_contact
        a.evidence = self.ids(s.last)
        if gap <= self.cfg.tracker["heartbeat_late_min"] * 60:
            a.set(REPORTING, "none", 0.5, f"Last heard {self.when(a.depot, s.last.t)}.")
        else:
            a.set(UNKNOWN, "unknown", 0.3,
                  f"Last heard {self.when(a.depot, s.last.t)}, {duration(gap)} ago; not enough is known about this"
                  " kind of device to say whether that is normal.")
