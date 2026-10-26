#!/usr/bin/env python3
"""
Builds ocean_beach_tides.ics: every high AND low tide at Ocean Beach, San Francisco
(NOAA station 9414275) that falls between sunrise and sunset. Each event's
description lists:
  - the 3 forecast swells (height, period, direction) at the SF Bar buoy (CDIP 142)
  - the forecast wind speed and direction at Ocean Beach

Data sources (all free, no API keys):
  tides  - NOAA CO-OPS            api.tidesandcurrents.noaa.gov
  swells - Open-Meteo Marine      marine-api.open-meteo.com  (model forecast at buoy 142's coordinates)
  wind   - National Weather Service api.weather.gov          (hourly forecast at Ocean Beach)

Usage:  pip install requests astral
        python ob_tides.py
"""
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import sun

TZ = ZoneInfo("America/Los_Angeles")
TIDE_STATION = "9414275"          # Ocean Beach, outer coast
OB = (37.775, -122.513)           # Ocean Beach (wind + sunrise/sunset)
BUOY_142 = (37.78, -122.59)       # CDIP 142 San Francisco Bar
OUT_FILE = "ocean_beach_tides.ics"
UA = {"User-Agent": "ocean-beach-tide-calendar (personal use)", "Accept": "application/geo+json"}
M_TO_FT = 3.28084
LOC = LocationInfo("Ocean Beach", "USA", "America/Los_Angeles", *OB)

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def clock(dt):
    """'7:05 AM' without platform-specific strftime flags."""
    return dt.strftime("%I:%M %p").lstrip("0")


def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    """RFC 5545 line folding at 75 octets."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur)
            cur = b
        else:
            cur += b
    parts.append(cur)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


# ---------------------------------------------------------------- data fetchers
def get_tides(start, end):
    r = requests.get(
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
        params={
            "product": "predictions", "application": "ob_tide_calendar",
            "station": TIDE_STATION,
            "begin_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d"),
            "datum": "MLLW", "time_zone": "lst_ldt", "interval": "hilo",
            "units": "english", "format": "json",
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if "predictions" not in data:
        raise RuntimeError(f"NOAA tide error: {data}")
    return data["predictions"]


def get_swells():
    """{'YYYY-MM-DDTHH:00': [(ft, period_s, deg), ...up to 3]} for the buoy 142 location."""
    names = ["swell_wave", "secondary_swell_wave", "tertiary_swell_wave"]
    hourly = [f"{n}_{v}" for n in names for v in ("height", "period", "direction")]
    r = requests.get(
        "https://marine-api.open-meteo.com/v1/marine",
        params={
            "latitude": BUOY_142[0], "longitude": BUOY_142[1],
            "hourly": ",".join(hourly), "timezone": "America/Los_Angeles",
            "forecast_days": 16,
        },
        timeout=60,
    )
    r.raise_for_status()
    h = r.json()["hourly"]
    out = {}
    for i, t in enumerate(h["time"]):
        swells = []
        for n in names:
            ht, per, d = h[f"{n}_height"][i], h[f"{n}_period"][i], h[f"{n}_direction"][i]
            if ht is not None and per is not None and d is not None:
                swells.append((ht * M_TO_FT, per, d))
        out[t] = swells
    return out


def get_wind():
    """List of (start, end, speed_text, direction_text) from the NWS hourly forecast."""
    p = requests.get(f"https://api.weather.gov/points/{OB[0]},{OB[1]}", headers=UA, timeout=60)
    p.raise_for_status()
    url = p.json()["properties"]["forecastHourly"]
    f = requests.get(url, headers=UA, timeout=60)
    f.raise_for_status()
    periods = []
    for per in f.json()["properties"]["periods"]:
        periods.append((
            datetime.fromisoformat(per["startTime"]),
            datetime.fromisoformat(per["endTime"]),
            per.get("windSpeed", ""), per.get("windDirection", ""),
        ))
    return periods


def safe(fn, label, *args):
    try:
        return fn(*args)
    except Exception as e:  # keep the calendar building even if one source is down
        print(f"WARNING: {label} unavailable ({e})", file=sys.stderr)
        return None


# ---------------------------------------------------------------- text builders
def swell_text(swells_by_hour, t):
    if swells_by_hour is None:
        return "Swell forecast unavailable."
    nearest = t.replace(minute=0, second=0, microsecond=0)
    if t.minute >= 30:
        nearest += timedelta(hours=1)
    swells = swells_by_hour.get(nearest.strftime("%Y-%m-%dT%H:00"))
    if not swells:
        return "Swell forecast not yet available for this date."
    labels = ["Primary", "Secondary", "Tertiary"]
    return "\n".join(
        f"{labels[i]}: {ft:.1f} ft @ {per:.0f}s from {compass(d)} ({d:.0f}\u00b0)"
        for i, (ft, per, d) in enumerate(swells)
    )


def wind_text(periods, t):
    if periods is None:
        return "Wind forecast unavailable."
    for start, end, speed, direction in periods:
        if start <= t < end:
            return f"{speed} from {direction}"
    return "Wind forecast not yet available for this date."


# ---------------------------------------------------------------- main
def main():
    start = date.today()
    end = start + timedelta(days=364)

    tides = get_tides(start, end)
    swells = safe(get_swells, "swell forecast")
    wind = safe(get_wind, "wind forecast")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Ocean Beach Daylight Tides//EN", "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Ocean Beach Tides", "X-WR-TIMEZONE:America/Los_Angeles",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]

    count = 0
    for p in tides:
        kind = "High" if p["type"] == "H" else "Low"
        t = datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        s = sun(LOC.observer, date=t.date(), tzinfo=TZ)
        if not (s["sunrise"] <= t <= s["sunset"]):
            continue
        ft = float(p["v"])
        t_utc = t.astimezone(timezone.utc)
        desc = (
            f"{kind} tide {ft:.2f} ft (MLLW) at {clock(t)}\n"
            f"Sunrise {clock(s['sunrise'])} / sunset {clock(s['sunset'])}\n\n"
            f"SWELLS at SF Bar buoy (142), forecast\n{swell_text(swells, t)}\n\n"
            f"WIND at Ocean Beach, forecast\n{wind_text(wind, t)}"
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:ob-{kind.lower()}-{t_utc.strftime('%Y%m%dT%H%M')}@ob-tides",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            f"DTSTART:{t_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{(t_utc + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{esc(f'{kind} tide {ft:.1f} ft')}",
            "LOCATION:Ocean Beach\\, San Francisco\\, CA",
            f"DESCRIPTION:{esc(desc)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        count += 1

    lines.append("END:VCALENDAR")
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")
    print(f"Wrote {count} daylight tide events to {OUT_FILE}")


if __name__ == "__main__":
    main()
