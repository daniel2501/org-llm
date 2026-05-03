"""Weather subsystem — open-meteo client + cache + agenda impact.

Phase candidate: weather-aware org-agenda. Combines a free,
keyless weather feed (open-meteo.com — Apache 2.0, FOSS-clean)
with the existing `org_agenda` scan to produce a structured
"what does the forecast do to my plans?" bundle that the agenda
agent can reason over.

Design:
  - Pure deterministic data path: forecast fetch → cache → join
    against agenda items by date + outdoor-keyword detection.
  - LLM does the reasoning: pre-fetch block packages forecast +
    flagged items + vault_profile digest; agent narrates
    recommendations + plan adjustments. We don't try to "decide"
    in code — the user's preferences are what matter and only
    the LLM (with vault_profile context) can read those.
  - Resilient: cached forecast served when offline; explicit
    error when location unconfigured.

Configuration:
  - location_lat / location_lon — set via
    `org-llm config location_lat 40.7128` etc. No default; the
    weather subsystem returns "configure location first" until
    set.
  - weather_enabled (optional) — defaults to true if location is
    set; the user can hard-disable via
    `org-llm config weather_enabled false`.
  - weather_cache_ttl_secs — default 3600 (1h).

API: open-meteo.com /v1/forecast endpoint. Apache 2.0, no key,
no rate limiting on personal use.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date as _date
from pathlib import Path
from typing import Optional

# ── configuration ────────────────────────────────────────────────────────────

_OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,precipitation_sum,wind_speed_10m_max,"
    "wind_gusts_10m_max,uv_index_max,sunrise,sunset"
    "&hourly=weather_code,temperature_2m,precipitation_probability,"
    "precipitation,wind_speed_10m"
    "&timezone=auto"
    "&forecast_days={days}"
)


def _resolve_location() -> Optional[tuple[float, float]]:
    """Return (lat, lon) from db.Config, or None if not set."""
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
        with Session(make_engine()) as s:
            lat_row = s.get(Config, "location_lat")
            lon_row = s.get(Config, "location_lon")
            if lat_row and lon_row and lat_row.value and lon_row.value:
                return (float(lat_row.value), float(lon_row.value))
    except Exception:
        return None
    return None


def _weather_enabled() -> bool:
    """True iff the user has location set AND hasn't disabled
    weather explicitly."""
    if _resolve_location() is None:
        return False
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
        with Session(make_engine()) as s:
            row = s.get(Config, "weather_enabled")
            if row and row.value:
                return row.value.strip().lower() not in ("false", "0", "no", "off")
    except Exception:
        pass
    return True


def _cache_path() -> Path:
    return (Path.home() / ".cache" / "org-llm" / "weather.json")


def _cache_ttl_secs() -> int:
    try:
        from .db import Config, make_engine
        from sqlalchemy.orm import Session
        with Session(make_engine()) as s:
            row = s.get(Config, "weather_cache_ttl_secs")
            if row and row.value:
                return max(60, int(row.value))
    except Exception:
        pass
    return 3600


# ── WMO weather code decoder (open-meteo uses these) ─────────────────────────

_WMO_CODES = {
    0:  ("clear",        "clear sky"),
    1:  ("clear-ish",    "mainly clear"),
    2:  ("partly cloudy", "partly cloudy"),
    3:  ("overcast",     "overcast"),
    45: ("fog",          "fog"),
    48: ("fog",          "freezing fog"),
    51: ("light drizzle", "light drizzle"),
    53: ("drizzle",      "drizzle"),
    55: ("heavy drizzle", "dense drizzle"),
    56: ("freezing drizzle", "light freezing drizzle"),
    57: ("freezing drizzle", "dense freezing drizzle"),
    61: ("light rain",   "slight rain"),
    63: ("rain",         "moderate rain"),
    65: ("heavy rain",   "heavy rain"),
    66: ("freezing rain", "light freezing rain"),
    67: ("freezing rain", "heavy freezing rain"),
    71: ("light snow",   "slight snowfall"),
    73: ("snow",         "moderate snowfall"),
    75: ("heavy snow",   "heavy snowfall"),
    77: ("snow grains",  "snow grains"),
    80: ("rain showers", "slight rain showers"),
    81: ("rain showers", "moderate rain showers"),
    82: ("violent rain", "violent rain showers"),
    85: ("snow showers", "slight snow showers"),
    86: ("snow showers", "heavy snow showers"),
    95: ("thunderstorm", "thunderstorm"),
    96: ("thunderstorm + hail", "thunderstorm with slight hail"),
    99: ("thunderstorm + hail", "thunderstorm with heavy hail"),
}


def decode_wmo(code: int) -> dict:
    """Map a WMO weather code to {short, long} labels."""
    short, long_ = _WMO_CODES.get(int(code), ("unknown", f"code {code}"))
    return {"short": short, "long": long_, "code": int(code)}


# ── outdoor-keyword tagging ──────────────────────────────────────────────────

# Phrases that strongly suggest the heading is a weather-sensitive
# (i.e. outdoor) commitment. Lowercase substring check against the
# heading text. Curated to avoid false positives — `:outdoor:` tag
# is the cleanest signal but not all users tag.
_OUTDOOR_KEYWORDS = {
    "hike", "hiking", "hiked",
    "bbq", "barbecue", "cookout",
    "garden", "gardening", "weed", "weeding", "mow", "mowing",
    "yard", "yardwork", "yard work", "rake", "raking",
    "run", "runs", "running", "jog", "jogging",
    "bike", "biking", "bicycle", "cycling",
    "walk", "walking", "stroll",
    "fish", "fishing",
    "camp", "camping",
    "beach", "swim", "swimming",
    "pool",
    "ski", "skiing", "snowboard", "snowboarding", "sled",
    "kayak", "kayaking", "canoe",
    "sail", "sailing",
    "picnic",
    "wash car", "wash the car",
    "outdoor", "outside",
    "park", "parks",
    "festival",
    "concert",
}


_OUTDOOR_TAG_RE = re.compile(r":outdoor:|:hike:|:bbq:|:garden:|:walking:",
                                  re.IGNORECASE)


# Weather-constraint tag DSL — users mark scheduled items with
# explicit hard requirements that override the heuristic outdoor
# detection. Format: `:cant-rain:`, `:cant-snow:`, `:cant-wind:`,
# `:cant-hot:`, `:cant-cold:`, `:needs-sun:`. Each maps to a
# threshold function that returns a concern string when violated,
# or "" when the forecast satisfies the constraint.
_WEATHER_TAG_RE = re.compile(
    r":(cant-rain|cant-snow|cant-wind|cant-hot|cant-cold|"
    r"needs-sun|weather-sensitive):",
    re.IGNORECASE,
)


def _check_constraint(tag: str, day_fc: dict) -> str:
    """Return a concern string when the explicit user constraint
    `tag` is violated by `day_fc`, or "" when it's fine."""
    pp    = day_fc.get("precip_prob") or 0
    short = (day_fc.get("short") or "").lower()
    wm    = day_fc.get("wind_max")   or 0
    wg    = day_fc.get("wind_gusts") or 0
    t_max = day_fc.get("t_max")
    t_min = day_fc.get("t_min")
    t = tag.lower()
    if t == "cant-rain":
        if pp >= 30 or "rain" in short or "drizzle" in short or "thunder" in short:
            return f":cant-rain: violated — {pp}% precip, {short}"
    elif t == "cant-snow":
        if "snow" in short or "freezing" in short:
            return f":cant-snow: violated — {short}"
    elif t == "cant-wind":
        if wm > 25 or wg > 40:
            return (f":cant-wind: violated — {wm:.0f}km/h sustained"
                    + (f", gusts {wg:.0f}" if wg > 40 else ""))
    elif t == "cant-hot":
        if t_max is not None and t_max > 28:
            return f":cant-hot: violated — {t_max:.0f}°C max"
    elif t == "cant-cold":
        if t_min is not None and t_min < 5:
            return f":cant-cold: violated — {t_min:.0f}°C min"
    elif t == "needs-sun":
        if "rain" in short or "snow" in short or "thunder" in short \
                or "fog" in short or "overcast" in short or pp >= 30:
            return f":needs-sun: violated — {short}, {pp}% precip"
    elif t == "weather-sensitive":
        # Generic — defer to the heuristic concern checker.
        return ""
    return ""


def extract_weather_tags(text: str) -> list[str]:
    """Pull weather-constraint tags out of an agenda item's heading
    text. Returns lowercase tag names without colons."""
    return [m.group(1).lower() for m in _WEATHER_TAG_RE.finditer(text or "")]


_OUTDOOR_KEYWORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in sorted(
        _OUTDOOR_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def is_outdoor_item(text: str, tags: str = "") -> bool:
    """Heuristic: does this agenda item happen outdoors? Reads
    text for outdoor keywords (with word boundaries — 'fish'
    won't match 'selfish') + the :outdoor: (or sibling) tag.

    False positives are recoverable (the agent can override based
    on vault_profile or context); false negatives lose weather
    awareness for that item, so the bias is moderately generous
    while avoiding mid-word substring traps."""
    if _OUTDOOR_KEYWORDS_RE.search(text or ""):
        return True
    if _OUTDOOR_TAG_RE.search(tags or ""):
        return True
    return False


# ── forecast fetch + cache ───────────────────────────────────────────────────

def get_forecast(*, days: int = 7,
                   force: bool = False) -> dict:
    """Return a 7-day forecast dict (open-meteo daily + hourly)
    for the configured location. Cached at ~/.cache/org-llm/weather.json
    for `weather_cache_ttl_secs` (default 3600s).

    Shape:
      {
        "lat": float, "lon": float,
        "fetched_at": iso,
        "stale": bool,                      # true if served from cache after a fetch failure
        "daily": [{date, code_short, code_long, t_max, t_min,
                    precip_prob, precip_mm, wind_max, wind_gusts,
                    uv_max, sunrise, sunset}, ...],
        "hourly_summary": [{date, am_code, pm_code,
                            am_precip_prob, pm_precip_prob}, ...],
        "error": str | None,
      }
    """
    loc = _resolve_location()
    if loc is None:
        return {"error": "location not configured — set "
                          "`org-llm config location_lat <N>` and "
                          "`org-llm config location_lon <N>` to enable "
                          "weather"}
    if not _weather_enabled():
        return {"error": "weather is disabled — re-enable with "
                          "`org-llm config weather_enabled true`"}
    lat, lon = loc
    cache = _cache_path()
    cached: Optional[dict] = None
    cache_age = float("inf")
    try:
        if cache.is_file():
            cached = json.loads(cache.read_text())
            cache_age = time.time() - cache.stat().st_mtime
            if cached.get("lat") == lat and cached.get("lon") == lon:
                if not force and cache_age < _cache_ttl_secs():
                    cached["stale"] = False
                    return cached
    except Exception:
        cached = None

    url = _OPEN_METEO_URL.format(lat=lat, lon=lon, days=max(1, min(16, days)))
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "org-llm/1.0 (+vault tooling)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
              ValueError, TimeoutError) as err:
        # Offline / API down — serve stale cache with a flag.
        if cached and cached.get("lat") == lat and cached.get("lon") == lon:
            cached["stale"] = True
            cached["staleness_secs"] = int(cache_age)
            cached["fetch_error"] = str(err)[:200]
            return cached
        return {"error": f"weather fetch failed: {err}",
                 "lat": lat, "lon": lon}

    # Project the open-meteo response into our compact shape.
    daily = raw.get("daily") or {}
    hourly = raw.get("hourly") or {}
    days_out: list[dict] = []
    dates = daily.get("time") or []
    for i, dstr in enumerate(dates):
        code = (daily.get("weather_code") or [None])[i]
        decoded = (decode_wmo(int(code)) if code is not None
                   else {"short": "?", "long": "?", "code": -1})
        days_out.append({
            "date":        dstr,
            **decoded,
            "t_max":       (daily.get("temperature_2m_max") or [None])[i],
            "t_min":       (daily.get("temperature_2m_min") or [None])[i],
            "precip_prob": (daily.get("precipitation_probability_max") or [None])[i],
            "precip_mm":   (daily.get("precipitation_sum") or [None])[i],
            "wind_max":    (daily.get("wind_speed_10m_max") or [None])[i],
            "wind_gusts":  (daily.get("wind_gusts_10m_max") or [None])[i],
            "uv_max":      (daily.get("uv_index_max") or [None])[i],
            "sunrise":     (daily.get("sunrise") or [""])[i],
            "sunset":      (daily.get("sunset") or [""])[i],
        })
    # Hourly summary — collapse 24 hours into am/pm halves per day.
    hourly_summary: list[dict] = []
    h_times = hourly.get("time") or []
    h_codes = hourly.get("weather_code") or []
    h_pp    = hourly.get("precipitation_probability") or []
    by_date: dict[str, dict] = {}
    for ts, code, pp in zip(h_times, h_codes, h_pp):
        dstr = ts[:10]
        try:
            hour = int(ts[11:13])
        except ValueError:
            continue
        bucket = by_date.setdefault(dstr,
            {"am_codes": [], "pm_codes": [], "am_pp": [], "pm_pp": []})
        if hour < 12:
            bucket["am_codes"].append(code)
            bucket["am_pp"].append(pp or 0)
        else:
            bucket["pm_codes"].append(code)
            bucket["pm_pp"].append(pp or 0)
    for dstr, b in sorted(by_date.items())[:days]:
        def _max_code(codes):
            if not codes: return None
            return max(int(c) for c in codes if c is not None)
        am_code = _max_code(b["am_codes"])
        pm_code = _max_code(b["pm_codes"])
        hourly_summary.append({
            "date":         dstr,
            "am_short":     decode_wmo(am_code)["short"] if am_code is not None else "?",
            "pm_short":     decode_wmo(pm_code)["short"] if pm_code is not None else "?",
            "am_precip_prob_max": max(b["am_pp"]) if b["am_pp"] else 0,
            "pm_precip_prob_max": max(b["pm_pp"]) if b["pm_pp"] else 0,
        })

    out = {
        "lat":         lat,
        "lon":         lon,
        "fetched_at":  datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "stale":       False,
        "daily":       days_out,
        "hourly_summary": hourly_summary,
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


# ── agenda × weather join ────────────────────────────────────────────────────

def _flag_outdoor_concerns(item: dict, day_forecast: dict) -> list[str]:
    """Return a list of human-readable concern phrases when the
    forecast intersects an outdoor item meaningfully. Empty list
    when conditions are fine.

    Conservative thresholds tuned for "would a reasonable person
    want to know?":
      - precip_prob >= 50% on any half-day → flag
      - precip_mm > 5 → "significant rain"
      - wind_max > 35 km/h → "windy"
      - wind_gusts > 50 km/h → "strong gusts"
      - t_max < 0 °C → "freezing"
      - t_max > 32 °C → "very hot"
      - uv_max > 8 → "high UV"
      - WMO code in {61-67, 80-82, 95-99} → name the condition
    """
    concerns: list[str] = []
    pp = day_forecast.get("precip_prob") or 0
    pm = day_forecast.get("precip_mm")  or 0
    wm = day_forecast.get("wind_max")   or 0
    wg = day_forecast.get("wind_gusts") or 0
    t_max = day_forecast.get("t_max")
    uv  = day_forecast.get("uv_max")    or 0
    short = day_forecast.get("short")   or ""
    if pp >= 50:
        concerns.append(f"{pp}% chance of precipitation")
    if pm and pm > 5:
        concerns.append(f"{pm:.1f}mm rain expected")
    if "rain" in short or "drizzle" in short:
        if f"{pp}% chance of precipitation" not in concerns:
            concerns.append(short)
    if "snow" in short:
        concerns.append(short)
    if "thunder" in short:
        concerns.append(short)
    if wm > 35:
        concerns.append(f"sustained wind {wm:.0f} km/h")
    if wg > 50:
        concerns.append(f"gusts to {wg:.0f} km/h")
    if t_max is not None and t_max < 0:
        concerns.append(f"freezing ({t_max:.0f}°C max)")
    if t_max is not None and t_max > 32:
        concerns.append(f"very hot ({t_max:.0f}°C max)")
    if uv > 8:
        concerns.append(f"high UV ({uv:.0f})")
    return concerns


def _day_score(day_fc: dict, weather_tags: list[str]) -> tuple[int, str]:
    """Return (score, label) for how good this day is for an
    outdoor item. Lower score = better. Score 0 = ideal.

    Weather tags are HARD constraints; if any is violated the day
    is disqualified (returns score=999)."""
    for tag in weather_tags or []:
        if _check_constraint(tag, day_fc):
            return (999, "constraint violated")
    pp    = day_fc.get("precip_prob") or 0
    short = (day_fc.get("short") or "").lower()
    wm    = day_fc.get("wind_max") or 0
    t_max = day_fc.get("t_max") or 20
    score = 0
    score += int(pp)                                 # 0-100
    if "rain" in short or "drizzle" in short:  score += 50
    if "snow" in short:                        score += 80
    if "thunder" in short:                     score += 100
    if wm > 25: score += int(wm - 25) * 2
    if t_max > 30: score += int(t_max - 30) * 5
    if t_max < 5:  score += int(5 - t_max) * 5
    if "clear" in short:        label = "clear"
    elif "partly" in short:     label = "partly cloudy"
    elif "overcast" in short:   label = "overcast"
    else:                        label = short or "?"
    return (score, label)


def _suggest_alternatives(item_date: str, weather_tags: list[str],
                            forecast_daily: list[dict],
                            *, limit: int = 2) -> list[dict]:
    """Find up to `limit` alternative dates within the forecast
    window whose forecast scores better than the original item's
    date AND satisfies any explicit weather-tag constraints."""
    by_date = {d.get("date"): d for d in forecast_daily}
    if item_date not in by_date:
        return []
    orig_score, _ = _day_score(by_date[item_date], weather_tags)
    suggestions: list[tuple[int, dict]] = []
    try:
        orig_d = datetime.strptime(item_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    today = _date.today()
    for dstr, fc in by_date.items():
        if dstr == item_date:
            continue
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today:           # don't suggest the past
            continue
        s, label = _day_score(fc, weather_tags)
        if s >= orig_score:     # only suggest if strictly better
            continue
        if s >= 999:            # constraint violated — skip
            continue
        # Prefer same day-of-week (weekends stay weekends, etc.).
        dow_bonus = 0 if d.weekday() == orig_d.weekday() else 10
        suggestions.append((s + dow_bonus, {
            "date":         dstr,
            "label":        label,
            "score":        s,
            "t_min":        fc.get("t_min"),
            "t_max":        fc.get("t_max"),
            "precip_prob":  fc.get("precip_prob"),
            "wind_max":     fc.get("wind_max"),
            "delta_days":   (d - orig_d).days,
            "same_dow":     d.weekday() == orig_d.weekday(),
        }))
    suggestions.sort(key=lambda kv: kv[0])
    return [s for _, s in suggestions[:limit]]


def weather_for_agenda(*, days: int = 7) -> dict:
    """Fetch the forecast and join it against the upcoming agenda
    window. Returns a structured bundle the agenda agent can
    narrate over.

    Shape:
      {
        "forecast":      <get_forecast() body>,
        "agenda":        {today, upcoming, overdue, stale_todo},
        "outdoor_items": [{date, item, concerns: [...],
                           weather_tags: [...],
                           suggestions: [{date, label, ...}, ...]},
                           ...],
        "summary":       short-string headline (e.g. "rain Sat 9am-noon").
      }
    """
    forecast = get_forecast(days=days)
    if forecast.get("error"):
        return {"error": forecast["error"]}
    try:
        from . import org_tools as _ot
        agenda = _ot.org_agenda(window_days=days)
    except Exception:
        agenda = {"today": [], "upcoming": [], "overdue": [],
                   "stale_todo": []}
    forecast_by_date = {d["date"]: d for d in (forecast.get("daily") or [])}
    flagged: list[dict] = []
    candidates = (
        list(agenda.get("today")    or [])
        + list(agenda.get("upcoming") or [])
        + list(agenda.get("overdue")  or [])
    )
    for item in candidates:
        item_date = item.get("scheduled") or item.get("deadline")
        if not item_date or item_date not in forecast_by_date:
            continue
        text = item.get("text", "")
        # Explicit user tags ALWAYS qualify, even when the
        # outdoor heuristic wouldn't fire — the user is overriding.
        weather_tags = extract_weather_tags(text)
        if not (weather_tags or is_outdoor_item(text)):
            continue
        day_fc = forecast_by_date[item_date]
        concerns = _flag_outdoor_concerns(item, day_fc)
        # Hard-constraint violations from explicit tags are added
        # to concerns regardless of heuristic thresholds.
        for tag in weather_tags:
            v = _check_constraint(tag, day_fc)
            if v and v not in concerns:
                concerns.append(v)
        suggestions = (
            _suggest_alternatives(item_date, weather_tags,
                                    forecast.get("daily") or [],
                                    limit=2)
            if concerns else []
        )
        flagged.append({
            "date":         item_date,
            "item":         text,
            "state":        item.get("state", ""),
            "file":         item.get("file", ""),
            "line":         item.get("line", 0),
            "concerns":     concerns,
            "weather_tags": weather_tags,
            "suggestions":  suggestions,
            "forecast": {
                "short":       day_fc.get("short"),
                "t_max":       day_fc.get("t_max"),
                "t_min":       day_fc.get("t_min"),
                "precip_prob": day_fc.get("precip_prob"),
                "wind_max":    day_fc.get("wind_max"),
            },
        })
    # Overall headline summary — first concerning day.
    summary = ""
    today_str = _date.today().isoformat()
    for d in (forecast.get("daily") or []):
        if d.get("date", "") < today_str:
            continue
        if (d.get("precip_prob") or 0) >= 50 or "rain" in (d.get("short") or "") \
                or "snow" in (d.get("short") or "") or "thunder" in (d.get("short") or ""):
            summary = (f"{d.get('date')}: {d.get('short')} "
                        f"({d.get('precip_prob')}% precip, "
                        f"{d.get('t_min','?')}-{d.get('t_max','?')}°C)")
            break
    if not summary and forecast.get("daily"):
        d = forecast["daily"][0]
        summary = (f"{d.get('date')}: {d.get('short')} "
                    f"({d.get('t_min','?')}-{d.get('t_max','?')}°C, "
                    f"{d.get('precip_prob',0)}% precip)")
    return {
        "forecast":       forecast,
        "agenda":         agenda,
        "outdoor_items":  flagged,
        "summary":        summary,
    }


# ── proactive weather-tag suggestion ─────────────────────────────────────────

def weather_tag_suggest(*, window_days: int = 14,
                          max_results: int = 50) -> list[dict]:
    """Scan agenda + scheduled items in the vault for headings
    whose text suggests an outdoor / weather-sensitive activity
    but which DON'T already carry an explicit weather-constraint
    tag. Returns suggestions the user (or the agent) can apply.

    Each row:
      {
        "file":            absolute path,
        "line":            heading line number,
        "heading":         heading text,
        "scheduled":       date or None,
        "current_tags":    list of existing weather-constraint tags,
        "suggested_tags":  list of tags to add,
        "reason":          one-line rationale ("contains 'hike'"),
      }

    The agent should NEVER auto-apply — surface for the user to
    confirm. False-positive suggestions are cheap; false-negatives
    cost the user a missed weather impact later.
    """
    try:
        from . import org_tools as _ot
    except Exception:
        return []
    agenda = _ot.org_agenda(window_days=window_days)
    candidates = (
        list(agenda.get("today")    or [])
        + list(agenda.get("upcoming") or [])
        + list(agenda.get("overdue")  or [])
    )
    out: list[dict] = []
    for item in candidates:
        text = item.get("text") or ""
        existing = extract_weather_tags(text)
        if existing:
            continue   # user already opined
        suggested: list[str] = []
        reasons:   list[str] = []
        text_l = text.lower()
        # Strong outdoor activity → :cant-rain:
        if any(re.search(r"\b" + re.escape(kw) + r"\b", text_l)
                 for kw in (
                "hike", "bbq", "barbecue", "cookout", "picnic",
                "garden", "mow", "yard", "yardwork",
                "wash car", "wash the car", "festival",
                "outdoor", "outside")):
            suggested.append("cant-rain")
            kw_hit = next(k for k in (
                "hike", "bbq", "barbecue", "cookout", "picnic",
                "garden", "mow", "yard", "yardwork",
                "wash car", "wash the car", "festival",
                "outdoor", "outside")
                if re.search(r"\b" + re.escape(k) + r"\b", text_l))
            reasons.append(f"contains '{kw_hit}'")
        # Strong activity but tolerates light rain → :weather-sensitive:
        elif any(re.search(r"\b" + re.escape(kw) + r"\b", text_l)
                  for kw in (
                "walk", "run", "jog", "bike", "cycling",
                "fish", "park", "concert", "beach")):
            suggested.append("weather-sensitive")
            kw_hit = next(k for k in (
                "walk", "run", "jog", "bike", "cycling",
                "fish", "park", "concert", "beach")
                if re.search(r"\b" + re.escape(k) + r"\b", text_l))
            reasons.append(f"contains '{kw_hit}'")
        # Snow-impacted activities
        if any(kw in text_l for kw in ("ski", "skiing", "snowboard",
                                            "sled", "snowman")):
            suggested.append("needs-sun" if "ski" in text_l else "weather-sensitive")
            reasons.append("snow-conditional activity")
        # Heat-impacted
        if any(kw in text_l for kw in ("ac repair", "outside work",
                                            "moving day", "load truck")):
            if "cant-hot" not in suggested:
                suggested.append("cant-hot")
            reasons.append("heat-sensitive task")
        # Wind-impacted
        if any(kw in text_l for kw in ("kite", "sail", "drone",
                                            "paint outside", "paint exterior")):
            if "cant-wind" not in suggested:
                suggested.append("cant-wind")
            reasons.append("wind-sensitive task")
        if not suggested:
            continue
        out.append({
            "file":           item.get("file", ""),
            "line":           item.get("line", 0),
            "heading":        text,
            "scheduled":      item.get("scheduled"),
            "current_tags":   existing,
            "suggested_tags": suggested,
            "reason":         "; ".join(reasons),
        })
        if len(out) >= max_results:
            break
    return out
