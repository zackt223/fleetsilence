"""Time helpers. Internally every time is an int: seconds since the epoch, UTC."""
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc


def parse_ts(value):
    """Parse a timestamp as leniently as is safe. Returns epoch seconds or None.

    Accepts ISO strings (with Z, an offset, or no zone = UTC; 'T' or a space),
    epoch seconds or milliseconds as numbers, and numbers written as strings.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_number(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return _from_number(float(s))
    except ValueError:
        pass
    s = s.replace(" ", "T", 1)
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    try:
        return int(d.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _from_number(n):
    if n != n or n in (float("inf"), float("-inf")):
        return None
    if n > 1e11:  # milliseconds
        n = n / 1000.0
    if n < 0 or n > 4102444800:  # after the year 2100: not a real timestamp
        return None
    return int(n)


def iso(t):
    if t is None:
        return None
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


class Clock:
    """Depot-local time without a tz database (the service must run offline)."""

    def __init__(self, depots, inferred=None):
        self.depots = depots
        # depot -> sorted [(utc day number, offset hours)] read off the schedule, for depots settings.toml lacks
        self.inferred = inferred or {}

    def offset_hours(self, depot, t):
        if depot not in self.depots and self.inferred.get(depot):
            return self._inferred_offset(depot, t)
        d = self.depots.get(depot) or {}
        base = d.get("utc_offset_h", 0)
        if not d.get("observes_dst"):
            return base
        # US/Canada rule: second Sunday of March 02:00 to first Sunday of November 02:00, local.
        year = datetime.fromtimestamp(t, UTC).year
        start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time(), UTC) + timedelta(hours=2 - base)
        end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time(), UTC) + timedelta(hours=2 - (base + 1))
        return base + 1 if start.timestamp() <= t < end.timestamp() else base

    def _inferred_offset(self, depot, t, half_window_days=3):
        """Median of the per-day estimates within a few days of t, so a daylight-saving change is followed."""
        days = self.inferred[depot]
        day = int(t // 86400)
        near = [o for d, o in days if abs(d - day) <= half_window_days]
        if not near:
            near = [min(days, key=lambda x: abs(x[0] - day))[1]]
        near.sort()
        return near[len(near) // 2]

    @staticmethod
    def infer_offsets(jobs_by_depot, workday_start_h):
        """Estimate each depot's UTC offset, day by day, from when its working day starts.

        The take-home schedule starts every working day at about 07:30 local in both depots (Mississauga
        11:30 UTC in daylight time, Regina 13:30 UTC), so offset = 07:30 - the day's earliest start in UTC,
        rounded to the hour. Used only for depots that settings.toml does not list.
        """
        out = {}
        for depot, jobs in jobs_by_depot.items():
            first = {}
            for j in jobs:
                d = int(j.start // 86400)
                first[d] = min(first.get(d, j.start), j.start)
            est = []
            for d, s in sorted(first.items()):
                h = (s - d * 86400) / 3600
                off = round(workday_start_h - h)
                off = (off + 12) % 24 - 12                 # keep it within -12..+11
                est.append((d, off))
            if est:
                out[depot] = est
        return out

    def local(self, depot, t):
        return datetime.fromtimestamp(t, UTC) + timedelta(hours=self.offset_hours(depot, t))

    def local_date(self, depot, t):
        return self.local(depot, t).strftime("%Y-%m-%d")

    def fmt(self, depot, t, ref=None):
        """'14:05' if on the same local day as ref, else 'Mon Oct 26 14:05'. Always depot-local."""
        if t is None:
            return "never"
        d = self.local(depot, t)
        if ref is not None and self.local(depot, ref).date() == d.date():
            return d.strftime("%H:%M")
        return d.strftime("%a %b ") + str(d.day) + d.strftime(" %H:%M")


def duration(seconds):
    """Human duration: '12 min', '5 h 10 min', '3 days'."""
    seconds = max(0, int(seconds))
    m = seconds // 60
    if m < 90:
        return f"{m} min"
    h = m // 60
    if h < 48:
        return f"{h} h {m % 60} min" if m % 60 else f"{h} h"
    return f"{round(h / 24, 1):g} days"
