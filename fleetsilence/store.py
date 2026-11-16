"""In-memory event store.

Handles the three ways incoming data lies about order:

* duplicates  - an event is identified by what it says (serial, type, device
                time, payload), not by its event_id, because retries can carry
                a new id. The extra ids are remembered as aliases so evidence
                lists still contain whichever id the reader knows.
* late        - every event keeps two times: when the device says it happened
                (t) and when the platform received it (r). Questions "as of
                `at`" only look at events with r <= at, then order them by t.
* out of order - per-device lists are kept sorted by r whatever order batches
                arrive in.
"""
import bisect
import json
import threading

from . import bx
from .timeutil import parse_ts

PLATFORM_TYPES = {"command", "plan_change"}        # sent by the platform, not proof the device is alive
RARE_TYPES = {"command", "plan_change", "power_disconnect", "power_restore", "low_voltage"}
MOTION_TYPES = {"position", "ignition_on", "ignition_off", "heartbeat"}
VIN_TYPES = {"heartbeat", "ignition_on"}
TAG_TYPES = {"check_in", "bx_report"}


class Event:
    __slots__ = ("ids", "serial", "type", "t", "r", "payload", "clock_suspect")

    def __init__(self, event_id, serial, type_, t, r, payload, clock_suspect=False):
        self.ids = [(r, event_id)]   # (received, event_id) for the original and every duplicate
        self.serial, self.type, self.t, self.r = serial, type_, t, r
        self.payload = payload
        self.clock_suspect = clock_suspect

    def ids_at(self, at):
        return [i for r, i in self.ids if r <= at]

    @property
    def event_id(self):
        return self.ids[0][1]


def _newer(e, cur):
    """Is e at least as recent as cur? Device time first, then arrival; ties are broken by
    event id so the answer never depends on the order batches happened to arrive in."""
    if cur is None:
        return True
    a, b = (e.t, e.r), (cur.t, cur.r)
    return a > b or (a == b and e.event_id >= cur.event_id)


class Summary:
    """What is known about one device from its first n events (in received order)."""
    __slots__ = ("n", "last_contact", "last", "first", "first_position", "live_since", "latest", "rare")
    contact_break_s = 360      # set from settings by Store: a gap this long ends a run of live contact
    backfill_lateness_s = 120  # set from settings by Store: events this late are buffered, not live

    def __init__(self):
        self.n = 0
        self.last_contact = None   # latest received time of anything the device itself sent
        self.last = None           # the device's own latest event, by device time
        self.first = None          # the device's own earliest event, by device time
        self.first_position = None
        self.live_since = None     # start of the current unbroken run of on-time contact (received time)
        self.latest = {}           # key -> latest event of that kind, by device time
        self.rare = []             # all power, command, plan and storage-error events

    def add(self, e):
        self.n += 1
        latest = self.latest
        key = e.type
        if _newer(e, latest.get(key)):
            latest[key] = e
        if e.type in PLATFORM_TYPES:
            self.rare.append(e)
            return
        if (self.last_contact is None or e.r - self.last_contact > self.contact_break_s
                or e.r - e.t > self.backfill_lateness_s):
            self.live_since = e.r
        if self.last_contact is None or e.r > self.last_contact:
            self.last_contact = e.r
        if _newer(e, self.last):
            self.last = e
        if self.first is None or e.t < self.first.t:
            self.first = e
        if e.type == "position" and (self.first_position is None or e.t < self.first_position.t):
            self.first_position = e
        if e.type in MOTION_TYPES:
            if _newer(e, latest.get("motion")):
                latest["motion"] = e
            if e.type in VIN_TYPES and e.payload.get("vin") and _newer(e, latest.get("vin")):
                latest["vin"] = e
        elif e.type == "camera_status":
            if e.payload.get("status") == "storage_error":
                self.rare.append(e)
        elif e.type in RARE_TYPES:
            self.rare.append(e)
        elif e.type in TAG_TYPES:
            if _newer(e, latest.get("tag")):
                latest["tag"] = e


class DeviceLog:
    __slots__ = ("serial", "events", "rs", "by_sig", "gen", "_cache")

    def __init__(self, serial):
        self.serial = serial
        self.events = []     # sorted by received time
        self.rs = []         # parallel list of received times, for bisect
        self.by_sig = {}
        self.gen = 0         # bumped whenever the list changes other than by appending
        self._cache = None   # (gen, Summary) for the longest prefix asked about so far

    def insert(self, e):
        if not self.rs or e.r >= self.rs[-1]:
            self.events.append(e)
            self.rs.append(e.r)
        else:
            i = bisect.bisect_right(self.rs, e.r)
            self.events.insert(i, e)
            self.rs.insert(i, e.r)
            self.gen += 1

    def remove(self, e):
        i = bisect.bisect_left(self.rs, e.r)
        while i < len(self.events) and self.events[i] is not e:
            i += 1
        if i < len(self.events):
            del self.events[i]
            del self.rs[i]
            self.gen += 1

    def summary(self, at):
        """Summary of everything received at or before `at`. Treat the result as read-only."""
        n = bisect.bisect_right(self.rs, at)
        c = self._cache
        if c and c[0] == self.gen and c[1].n <= n:
            s = c[1]
        else:
            s = Summary()
            if c and c[0] == self.gen and n < c[1].n:
                # A question about an earlier moment: answer it without throwing away the newer summary.
                for e in self.events[:n]:
                    s.add(e)
                return s
        for e in self.events[s.n:n]:
            s.add(e)
        self._cache = (self.gen, s)
        return s


def _signature(serial, type_, device_time, payload):
    try:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        body = repr(payload)
    return (type_, repr(device_time), body)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.logs = {}               # serial -> DeviceLog
        self.stats = {"received": 0, "stored": 0, "duplicates": 0, "rejected": 0, "bx_undecodable": 0}
        self._future = cfg.ingest["clock_future_tolerance_s"]
        self._late = cfg.ingest["max_trusted_lateness_h"] * 3600
        Summary.contact_break_s = cfg.tracker["drive_quiet_min"] * 60
        Summary.backfill_lateness_s = cfg.ingest["backfill_lateness_s"]

    # ---------------------------------------------------------------- ingest
    def ingest(self, events):
        """Store a batch. Returns how many events were accepted (duplicates count as accepted).
        One bad event never fails the batch."""
        accepted = 0
        with self.lock:
            for raw in events:
                self.stats["received"] += 1
                try:
                    ok = self._ingest_one(raw)
                except Exception:   # noqa: BLE001 - one bad event must never fail a batch
                    ok = False
                if ok:
                    accepted += 1
                else:
                    self.stats["rejected"] += 1
        return accepted

    def _ingest_one(self, raw):
        if not isinstance(raw, dict):
            return False
        event_id = raw.get("event_id")
        if isinstance(event_id, (int, float)) and not isinstance(event_id, bool):
            event_id = str(event_id)
        if not isinstance(event_id, str) or not event_id:
            return False
        r = parse_ts(raw.get("received_at"))
        if r is None:
            return False
        type_ = raw.get("type")
        if not isinstance(type_, str) or not type_:
            return False

        if type_ == "bx_raw":
            try:
                d = bx.decode(raw.get("raw"))
            except bx.BxDecodeError:
                self.stats["bx_undecodable"] += 1
                return False
            serial, device_time = d.pop("serial"), d.pop("device_time")
            type_, payload = "bx_report", d
        else:
            serial = raw.get("serial")
            if not isinstance(serial, str) or not serial.strip():
                return False
            serial = serial.strip()
            device_time = raw.get("device_time")
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                payload = {}

        t = parse_ts(device_time)
        suspect = False
        if t is None or t > r + self._future or t < r - self._late:
            t, suspect = r, True     # the device clock can't be trusted: order by arrival instead

        log = self.logs.get(serial)
        if log is None:
            log = self.logs[serial] = DeviceLog(serial)
        sig = _signature(serial, type_, device_time, payload)
        old = log.by_sig.get(sig)
        if old is not None:
            self.stats["duplicates"] += 1
            if not any(i == event_id for _, i in old.ids):
                old.ids.append((r, event_id))
                old.ids.sort()
            elif r < old.ids[0][0]:
                old.ids = sorted([(r, event_id)] + [x for x in old.ids if x[1] != event_id])
            if r < old.r:
                # The copy we already hold turns out to be the later one: move it.
                log.remove(old)
                old.r = r
                if suspect:
                    old.t = r
                log.insert(old)
            return True
        e = Event(event_id, serial, type_, t, r, payload, suspect)
        log.by_sig[sig] = e
        log.insert(e)
        self.stats["stored"] += 1
        return True

    # ---------------------------------------------------------------- queries
    def summary(self, serial, at):
        log = self.logs.get(serial)
        return log.summary(at) if log else None

    def knows(self, serial):
        return serial in self.logs
