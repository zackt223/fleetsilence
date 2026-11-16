# fleetsilence

Fleet devices go quiet all the time. Usually it means nothing: the van is parked, the camera is asleep.
Sometimes it means a truck is about to go out and nobody can track or record it. This service tells the
two apart, says why, and for every job at risk either names another vehicle that can take it or says
plainly that there isn't one.

It is built for one person: the **fleet manager**, who decides which vehicle does which job the night
before and again before the 6 a.m. shift.

## Run it

Needs Python 3.12. No install step: the service uses only the standard library.

```bash
python3 -m fleetsilence
```

Run it from the repository root. It listens on `http://localhost:8080`, keeps everything in memory and
starts empty every time. It makes no network calls.

| Variable | Default | Meaning |
|---|---|---|
| `QF_DATA_DIR` | `./data` | Folder with `assets.csv`, `devices.csv`, `schedule.csv`, `weather.csv`, `plan_changes.csv`, `support_cases.csv` |
| `QF_PORT` | `8080` | Port to listen on |
| `QF_SETTINGS` | `./settings.toml` | Alternative settings file |

On Windows the same command is `py -3.12 -m fleetsilence`.

## What it answers

| Endpoint | Answer |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `POST /ingest` | Takes `{"events": [...]}`; returns `{"accepted": n}`. Events may arrive late, more than once, or out of order. |
| `GET /devices/{serial}/status?at=` | Is the device reporting, quiet as expected, in need of action, or unknown; the most likely cause; the reasons; the event IDs behind the answer. |
| `GET /job-cover?depot=&at=&hours=` | Jobs starting in the window whose vehicle can't be tracked or recorded, each with a swap to another vehicle, or a gap and the reason there is none. |

Shapes and allowed values follow the take-home's `docs/CONTRACT.md` exactly. Bad or missing parameters
return `400`, an unknown serial `404`.

```bash
curl "http://localhost:8080/job-cover?depot=REG&at=2026-10-26T23:30:00Z&hours=24"
```

```json
{
  "depot": "REG", "at": "2026-10-26T23:30:00Z", "hours": 24,
  "jobs": [{
    "job_id": "J-REG-1027-08488", "asset_id": "K-27",
    "problem": "no_location", "decision": "swap", "swap_to": "K-22",
    "manager_action": "Move J-REG-1027-08488 (Tue Oct 27 07:55-09:25, service call) from K-27 to K-22.",
    "reasons": [
      "The tracker sent a power-disconnect alert at 12:10, then a backup-battery low-voltage alert at 12:20. Last heard 12:28 (position).",
      "It is running on (or has drained) its small backup battery; someone has to reconnect it on site.",
      "K-22 is a pickup at Regina, has no other job during Tue Oct 27 07:55-09:25, and its tracker is reporting normally."
    ],
    "evidence": ["e33683614", "e78779377", "e97794714"]
  }]
}
```

Times in `reasons` and `manager_action` are in the depot's local time, because that is how the manager
reads a schedule. Everything else is UTC. The response has the contract's fields and no others.

## How it decides

**The trade-off.** For this manager a missed real problem is worse than a false alarm: a job done blind
is a legal and safety risk, a wrong swap costs a few minutes. So unexplained silence is never called
"expected", and thresholds sit just outside normal behaviour. But a swap is only recommended on strong
evidence, to a vehicle that is itself visibly healthy and free for the whole slot. Where this shows in
the code: `fleetsilence/status.py` (anything silent past its limit with no explanation becomes
`needs_action`), and `fleetsilence/jobcover.py` (`Health.eligible` — a vehicle that is silent,
unexplained or inside an outage is never offered as a replacement).

**Silence, by device** (`fleetsilence/status.py`):

| What is seen | Verdict |
|---|---|
| Tracker: ignition off, heartbeats on time | `quiet_expected` · `parked_normal` |
| Tracker: power-disconnect alert, then low-voltage alert or 35 min without a restore | `needs_action` · `power_loss` |
| Tracker: silent mid-drive, alone, under 100 min | `unknown` · `coverage_gap` (watched, not acted on) |
| Tracker: silent together with most others on the same carrier and depot that were due to report | `needs_action` · `carrier_outage` |
| Tracker: silent, and another unit now reports from the same vehicle | `device_swapped` (the vehicle is still tracked) |
| Camera: last report `sleeping`, or the vehicle is parked | `quiet_expected` · `sleeping_by_design` |
| Camera: `storage_error`, or silent while the vehicle drives | `needs_action` · `camera_fault` |
| Any device: data plan paused or ended | `needs_action` · `plan_paused` |
| Battery tag: missed check-in at 10 % or less, or low-battery flag | `needs_action` · `battery_low` |
| Battery tag: missed check-ins only after nights at −10 °C or colder | `quiet_expected` · `cold_weather` |
| Battery tag: stopped the same day as the rest of its seasonal group | `quiet_expected` · `planned_downtime` |
| Anything silent past its limit with no explanation | `needs_action` · `unknown` |

**Job cover** (`fleetsilence/jobcover.py`):

- A job is listed when its vehicle's tracker (`no_location`), camera (`no_video`) or both have a confirmed
  fault. Vans without a camera are not flagged for video.
- `swap`: to a vehicle of the same type and depot, with no other job at any point in the slot, that is
  itself tracked and (for a camera van) recorded. One replacement is never given overlapping jobs, and all
  the jobs of one troubled vehicle go to the same replacement where possible, so the crew changes vehicle once.
- `gap`: no vehicle qualifies. The reason counts why: how many are busy, unhealthy, on a dead carrier, or
  have no camera. A job on a camera van is never moved to a van without one.
- The contract also allows `keep`. This service never answers it: a job is either moved or reported as
  having no replacement.
- Carrier outage: nothing is moved, and jobs on affected vehicles are not listed (the network is down, not
  the vehicles). Vehicles on the affected carrier are not offered as replacements until it is back. The
  one shared explanation is on each affected device's status.

**Messy data, on purpose** (`fleetsilence/store.py`):

- Duplicates are recognised by content, not by `event_id` (2,256 retries in the history carry a new id).
  All ids are kept, so evidence lists contain whichever one the reader has.
- Every event keeps the device's time and the platform's arrival time. "As of `at`" uses only events
  that had arrived by `at`, then orders them by device time. A burst of buffered positions therefore
  doesn't rewrite what was knowable earlier.
- Device clocks that are impossible (in the future, or more than a day behind arrival) are not believed.
  Unix-seconds timestamps from old A-series firmware are read.
- The records are not trusted about which tracker is on which vehicle: a tracker's own VIN wins.
- One bad event never fails a batch.

**BX-series trackers** are decoded in `fleetsilence/bx.py` and then treated like any other battery
tracker. The mapping and how it was checked are in [THRESHOLDS.md](THRESHOLDS.md).

## Settings

Every time cut-off and threshold is in [`settings.toml`](settings.toml), each marked GIVEN (from the
brief) or CALIBRATED (measured on the history). [THRESHOLDS.md](THRESHOLDS.md) gives the evidence.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

58 tests, standard library only:

- `test_bx.py` — the decoder against the vendor portal's three readouts; garbled messages.
- `test_ingest.py` — duplicates (same id and new id), the same batch twice, shuffled batches, late
  bursts, wrong clocks, malformed events.
- `test_status.py` — each cause in the table above, and that only events received by `at` count.
- `test_jobcover.py` — swap, gap (busy / unhealthy / no camera), no double-booking, mass-silence grouping, and
  that nothing is moved for an outage.
- `test_http.py` — starts the real service with the start command and calls every endpoint over HTTP.
- `test_practice_replay.py` — streams the take-home's full telemetry through the service and runs its
  three practice checkpoints. The data pack is not in this repository; the test runs when the pack is at
  `./data-pack/quiet-fleet-data-pack` or `QF_PACK_DIR` points to it, and is skipped otherwise.

Tools for working with the data pack:

```bash
python3 -I tools/calibrate.py PATH/TO/quiet-fleet-data-pack   # the measurements behind settings.toml
python3 -I tools/evaluate.py  PATH/TO/quiet-fleet-data-pack   # score against the 179 labelled support cases
python3 -I tools/replay.py    PATH/TO/quiet-fleet-data-pack --url http://localhost:8080   # practice checkpoints over HTTP
```

## Layout

```
fleetsilence/
  __main__.py   start command
  server.py     HTTP routes, parameter checks
  store.py      ingest: de-duplication, device time vs arrival time
  status.py     what a device's silence means; carrier-outage detection
  jobcover.py   at-risk jobs: swap or gap
  bx.py         BX-series decoder
  refdata.py    reference CSVs
  timeutil.py   timestamps and depot-local time (no tz database needed)
  config.py     loads settings.toml
settings.toml   every threshold
THRESHOLDS.md   the evidence for each threshold
tests/  tools/
```

## Not built, deliberately

Screens, logins, a database, the support agent's `/triage` and the scheduler's `/visits`, and anything
that needs the network.

## Known limits

- Carrier outages are detected from trackers: within 6 minutes when vehicles are on the road, but an
  outage that starts when everything is parked is only noticed once heartbeats come due (up to 4.5 h).
  Cameras and battery tags are attributed to an outage, but do not trigger one.
- A job more than a few hours ahead is judged on the vehicle's state now. A power loss or paused plan is
  assumed to still be there when the job starts unless a restore arrives first.
- Planned downtime is inferred (a seasonal group going quiet together); there is no explicit "in the
  shop" flag in the data.
- State is in memory. A restart forgets everything, by design.
