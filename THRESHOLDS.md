# Thresholds, and the evidence behind each

Every cut-off lives in [`settings.toml`](settings.toml). This page says where each number came from.

- **Given** values are stated in the candidate brief.
- **Calibrated** values were measured on the Oct 4–31 history (640,419 events, 179 support cases).
  `python3 -I tools/calibrate.py <data pack>` reprints every figure quoted here.

The trade-off that decides the close calls: for the fleet manager **a missed real problem is worse than a
false alarm**, so thresholds sit just outside what normal behaviour looked like in the history, not far
outside it. The one place this is deliberately softened is swaps: a swap is only offered when the evidence
is strong, because a wrong swap wastes a crew's time and erodes trust in the next recommendation.

## Vehicle trackers (T-series)

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `tracker.heartbeat_interval_min` | 240 | Given | "A heartbeat every 4 hours while it's off." |
| `tracker.heartbeat_late_min` | 270 | Given | "Late after 4 hours 30 minutes with nothing." In the history parked heartbeats arrive exactly 240 min apart. |
| `tracker.drive_quiet_min` | 6 | Calibrated | Positions while driving are 2.0 min apart (99.99th percentile 2.05). Six minutes is three missed in a row. |
| `tracker.coverage_gap_max_min` | 100 | Calibrated | 221 mid-drive silences with no power alert: shortest 21 min, median 54, longest 91; each ended with the vehicle reporting again and its buffered positions arriving. Silence beyond 100 min is outside anything a dead zone produced, so it becomes `needs_action`. |
| `power.confirm_after_min` | 35 | Calibrated | 25 power-disconnect alerts. 15 were real: each sent a backup-battery low-voltage alert 8–14 min later. 10 were harmless: each was restored after 18–32 min with no low-voltage alert. So: low-voltage alert, or 35 min without a restore, confirms a power loss. Waiting costs at most 14 minutes on a real one. |
| `power.backup_window_min` | 60 | Calibrated | After a real disconnect the unit kept talking for 9–34 min on its backup battery. A unit still reporting an hour later has power again even if the restore message was lost (seen once). |
| `swap.handover_window_min` / `handover_distance_km` | 10 / 3 | Calibrated | 7 unit swaps. In all 5 done mid-shift, the new unit's first position came exactly 2 min after the old unit's last, 0.01–0.55 km away. Its first VIN-bearing message only came 5–10 h later, and without this rule the van looks untracked for that long. |

## Two status verdicts the references leave open

| Setting | Value | Why this value |
|---|---|---|
| `status.parked_state` | `quiet_expected` | The contract defines `quiet_expected` as "quiet and that's normal" and lists `parked_normal` as a cause, which can only be reported with a non-`reporting` state. All 26 `parked_normal` support cases were closed "heartbeats on time; vehicle parked", no fault found. |
| `status.coverage_gap_state` | `unknown` | All 14 `coverage_gap` cases resolved by themselves, but while the silence is in progress it looks exactly like the first 100 minutes of a fault. The contract's `unknown` is "not enough evidence yet". The practice exam accepts `unknown` as the cautious answer for the other self-resolving cause it tests (carrier outage). |

Whatever word is chosen, a vehicle in a dead zone is not offered as a replacement and its own jobs are not moved.

## Carrier outages

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `outage.min_devices` | 4 | Calibrated | Counting driving trackers silent together per carrier and depot: the two real outages peaked at 39 (Lakeline, Mississauga, Oct 12) and 31 (Northwave, Regina, Oct 27). On every other occasion in 28 days the most was 3. |
| `outage.min_fraction` | 0.6 | Calibrated | In both outages 100 % of the driving trackers on that carrier at that depot went silent, while the other carrier and the other depot were untouched. 60 % leaves room for a vehicle that happens to be parked or on a roaming SIM. |
| `outage.cluster_window_min` | 30 | Calibrated | On Oct 12 every affected tracker's last message arrived between 14:00 and 14:02 UTC. 30 min tolerates a ragged start. |

Parked trackers whose heartbeat is overdue count as silent too, so an outage that starts when nobody is
driving is still recognised. In the history no parked tracker was ever overdue without a power alert or a
unit swap, so this adds no false positives there.

Grouping is by carrier **and** depot because both outages were regional: Lakeline devices in Regina kept
reporting while Lakeline was down in Mississauga.

## Dash cameras (C-series)

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `camera.sleep_after_ignition_off_min` | 15 | Given | "Goes to sleep 15 minutes after the ignition turns off." Measured: exactly 15.0 min. |
| `camera.quiet_min` | 12 | Calibrated | A recording camera reports every 5.0 min (1st percentile 4.4). Twelve minutes is two missed in a row. |
| `camera.wake_grace_min` | 10 | Calibrated | After a sleep, cameras report "booting" a median 5 s after ignition on (2,723 wake-ups). |
| `camera.reboot_settle_min` | 15 | Calibrated | When a remote reboot worked, "booting" followed the command within a minute (median 40 s). A reboot older than 15 min with no recovery has failed. |

Also measured, and not turned into a rule: of 40 camera-fault cases, 28 were cleared by the first remote
reboot and 12 needed a technician after two failed reboots. The service swaps every job on a van with a
faulted camera regardless.

## Battery trackers (A-series and BX-series)

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `tag.late_after_h` | 25 | Calibrated | Daily check-ins are 23.3–24.7 h apart. Customers raised "missed today's check-in" cases at about 25 h. |
| `tag.battery_low_pct` | 10 | Calibrated | 11 silences ended with a new battery pack; the last reading before each was 8 % or 10 %. 33 silences ended by themselves; the lowest reading before those was 17 %. |
| `tag.cold_low_c` | −10 | Calibrated | All 33 self-ending silences fell on nights at −10 °C or colder (Regina). No healthy tag missed a check-in on a warmer night. |
| `tag.cold_max_missed_days` | 5 | Calibrated | The longest cold silence was 3 missed check-ins in a row. |
| `tag.downtime_peer_fraction` / `seasonal_downtime` | 0.5 / landscaping Oct 15–Apr 15 | Calibrated | All 12 landscaping trailers stopped checking in on the same day (Oct 23) with healthy batteries (40–84 %) in mild weather: equipment put away for the season, not twelve faults. |

## Job cover: when a job is kept on its vehicle

**Switched off by the ops lead (2026-11-18):** jobs are planned with what is known when the decision is made,
so `jobcover.keep_when_fix_expected = false` and these figures are not used. They are kept for the record.
The same answer turned off `jobcover.camera_less_fallback_when_untracked`: a replacement must be seen and
recorded.

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `jobcover.remote_reboot_fix_min` | 90 | Calibrated | 40 `camera_fault` cases: 28 fixed by a remote reboot, all closed within 1.1 h of opening (median 0.5 h); 12 needed a technician after the reboot failed twice. Used only while no reboot has failed yet. |
| `jobcover.technician_done_by_local` | 14:00 | Calibrated | 26 technician visits to vehicles (14 `power_loss`, 12 `camera_fault`): every one on the next working day after the case opened, finished between 08:00 and 13:45 local. |
| `jobcover.technician_works_weekends` | false | Calibrated | No vehicle visit in the history happened on a Saturday or Sunday; Friday cases were all closed on Monday. |
| (overdue fix) | nothing kept | Both times are counted from when the fault began. If that time has passed and the fault is still there, the history's pattern did not hold for this vehicle (no reboot sent, or no visit booked), so no job waits for it. Example: V-145's camera failed Thu Oct 29; a visit was due by Fri Oct 30 14:00 and never came, so on Oct 31 none of its jobs are kept. |
| (plan paused) | never kept | Calibrated | 11 `plan_paused` cases took 30–62 h to resolve, by an account action nobody can schedule. |

## Ingest

| Setting | Value | Source | Evidence |
|---|---|---|---|
| `ingest.backfill_lateness_s` | 120 | Calibrated | 90 % of events arrive within 5 s and 98.8 % within 2 min. Anything later came out of a device's buffer. |
| `ingest.clock_future_tolerance_s` | 600 | Calibrated | No device clock in the history is ahead of the platform clock at all. |
| `ingest.max_trusted_lateness_h` | 24 | Calibrated | The latest buffered event arrived 90 min late. A device time more than 24 h behind arrival is treated as a wrong clock and the event is ordered by arrival time. |

Also measured, and handled in code rather than by a threshold:

- 7,609 duplicate events: 5,353 repeat the same `event_id`, 2,256 carry a **new** one. Events are therefore
  identified by content (serial, type, device time, payload), and every id is kept for evidence lists.
- 247 events from A-series firmware 1.9.0 send `device_time` as Unix seconds instead of a string.

## How well the calibrated rules fit the history

`python3 -I tools/evaluate.py <data pack>` asks the service about each support case at the moment the
case was opened, and compares the answer with the case's labelled root cause.

- **165 of 179 cases agree.**
- The 14 that don't are label timing, not logic: 12 cases were opened minutes *after* the device had already
  reported again (the service correctly says `reporting`), and for 2 tags a `battery_low` case and a
  `cold_weather` case were opened on the same device the same morning (the service says `battery_low`).
- At the three practice checkpoints, the assets the service refuses to offer as replacements are exactly
  the assets the exam lists as unavailable (99 of 99, 3 of 3, 3 of 3).
- An hourly-then-20-minute sweep over the whole month found no carrier outage called outside the two real
  ones, and no camera fault flagged that the case history or a `storage_error` event doesn't back up.

## The BX-series mapping

Worked out from the 300 raw messages and checked against all three portal readouts
(`tests/test_bx.py` asserts each one):

| Field | Meaning | How it was found |
|---|---|---|
| `u` | Unit serial, as in `devices.csv` | Direct match. |
| `t` | Report time, Unix seconds in hexadecimal, UTC | `6ADA16E0` = 2026-10-22 14:00:00 UTC = 08:00 Regina time, the portal's value. |
| `b` | Battery, 12-bit reading: percent = `b / 4095 × 100` | 3094 → 76 %, 2583 → 63 %, 573 → 14 %: all three readouts. |
| `s` | Status bits: 1 = moving, 2 = low battery, 8 = GPS fix | 8 → fix only; 9 → moving + fix; 10 → low battery + fix. |
| `p` | `"lat,lng"` in degrees × 100,000 | `5042719,-10456695` → 50.42719, −104.56695. |

Not explained: status bit 4 (6 of 300 messages have it set). It is decoded and ignored.
