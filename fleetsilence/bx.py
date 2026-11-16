"""Decoder for the undocumented BX-series tracker messages.

The mapping was worked out from 300 raw messages and checked against the three
vendor-portal readouts in the data pack (see THRESHOLDS.md and tests/test_bx.py):

    {"u":"BX40471","t":"6ADA16E0","b":3094,"s":8,"p":"5042719,-10456695"}

    u  unit serial, as listed in devices.csv
    t  report time: Unix seconds, UTC, written in hexadecimal
    b  battery: a 12-bit reading, 0-4095 = 0-100 %
    s  status bits: 1 = moving, 2 = low battery, 8 = GPS fix (4 is unexplained)
    p  "latitude,longitude" in degrees x 100000
"""
import json

BIT_MOVING = 1
BIT_LOW_BATTERY = 2
BIT_GPS_FIX = 8
BATTERY_FULL_SCALE = 4095


class BxDecodeError(ValueError):
    pass


def decode(raw):
    """Turn one raw BX message into a plain dict. Raises BxDecodeError if it can't be read."""
    msg = raw
    if isinstance(raw, (str, bytes)):
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise BxDecodeError(f"not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise BxDecodeError("message is not an object")

    serial = msg.get("u")
    if not isinstance(serial, str) or not serial.strip():
        raise BxDecodeError("missing unit serial 'u'")

    t = msg.get("t")
    try:
        device_time = int(t, 16) if isinstance(t, str) else int(t)
    except (TypeError, ValueError) as exc:
        raise BxDecodeError(f"unreadable time 't': {t!r}") from exc

    out = {"serial": serial.strip(), "device_time": device_time,
           "battery_pct": None, "moving": None, "low_battery": None, "gps_fix": None,
           "lat": None, "lng": None, "status_bits": None}

    b = msg.get("b")
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        out["battery_pct"] = max(0, min(100, round(b / BATTERY_FULL_SCALE * 100)))

    s = msg.get("s")
    if isinstance(s, int) and not isinstance(s, bool):
        out["status_bits"] = s
        out["moving"] = bool(s & BIT_MOVING)
        out["low_battery"] = bool(s & BIT_LOW_BATTERY)
        out["gps_fix"] = bool(s & BIT_GPS_FIX)

    p = msg.get("p")
    if isinstance(p, str) and "," in p:
        try:
            lat, lng = (int(x) / 100000 for x in p.split(",", 1))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                out["lat"], out["lng"] = lat, lng
        except ValueError:
            pass
    return out
