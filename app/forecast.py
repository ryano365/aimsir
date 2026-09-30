"""Point forecast (Met Éireann WDB API, XML) and national warnings feed."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from .config import settings

_cache: dict[tuple, tuple[float, dict]] = {}


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _f(el, attr):
    if el is None:
        return None
    try:
        return float(el.get(attr))
    except (TypeError, ValueError):
        return None


def parse_pf(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    instants: dict[datetime, dict] = {}
    intervals: dict[datetime, tuple[float, dict]] = {}
    for tel in root.iter("time"):
        t0, t1 = _t(tel.get("from")), _t(tel.get("to"))
        loc = tel.find("location")
        if loc is None:
            continue
        if t0 == t1:
            wd, ws, wg = loc.find("windDirection"), loc.find("windSpeed"), loc.find("windGust")
            instants[t0] = {
                "time": t0.isoformat(),
                "temp": _f(loc.find("temperature"), "value"),
                "dew": _f(loc.find("dewpointTemperature"), "value"),
                "wind_dir": _f(wd, "deg"),
                "wind_name": wd.get("name") if wd is not None else None,
                "wind_kmh": None if _f(ws, "mps") is None else round(_f(ws, "mps") * 3.6),
                "gust_kmh": None if _f(wg, "mps") is None else round(_f(wg, "mps") * 3.6),
                "humidity": _f(loc.find("humidity"), "value"),
                "pressure": _f(loc.find("pressure"), "value"),
                "cloud": _f(loc.find("cloudiness"), "percent"),
            }
        else:
            dur = (t1 - t0).total_seconds() / 3600
            p, sym = loc.find("precipitation"), loc.find("symbol")
            item = {
                "precip": _f(p, "value"),
                "precip_max": _f(p, "maxvalue"),
                "prob": _f(p, "probability"),
                "symbol": sym.get("id") if sym is not None else None,
                "period_h": dur,
            }
            if t1 not in intervals or dur < intervals[t1][0]:
                intervals[t1] = (dur, item)
    hours = []
    for t in sorted(instants):
        row = dict(instants[t])
        if t in intervals:
            row.update(intervals[t][1])
        hours.append(row)
    return {"hours": hours}


async def point_forecast(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    key = (round(lat, 2), round(lon, 2))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < settings.forecast_cache_s:
        return hit[1]
    url = f"{settings.pf_url}?lat={key[0]};long={key[1]}"
    r = await client.get(url, timeout=20)
    r.raise_for_status()
    data = parse_pf(r.text)
    data.update({"lat": key[0], "lon": key[1], "fetched": datetime.now(timezone.utc).isoformat()})
    _cache[key] = (time.time(), data)
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return data


_warn_cache: tuple[float, list] | None = None


async def warnings(client: httpx.AsyncClient) -> list[dict]:
    global _warn_cache
    if _warn_cache and time.time() - _warn_cache[0] < 300:
        return _warn_cache[1]
    r = await client.get(settings.warnings_url, timeout=15)
    r.raise_for_status()
    now = datetime.now(timezone.utc)
    items = []
    for w in r.json() or []:
        try:
            if _t(w["expiry"]) < now:
                continue
        except (KeyError, ValueError):
            pass
        items.append({k: w.get(k) for k in
                      ("level", "headline", "description", "onset", "expiry", "regions", "type", "severity")})
    _warn_cache = (time.time(), items)
    return items


# ---------------------------------------------------------------- live observations

# Met Éireann synoptic stations reported in obs_present.xml (approximate positions).
STATIONS = {
    "Athenry": (53.289, -8.786), "Ballyhaise": (54.051, -7.310), "Belmullet": (54.228, -10.007),
    "Casement": (53.306, -6.439), "Claremorris": (53.711, -8.993), "Cork": (51.847, -8.486),
    "Dublin": (53.428, -6.241), "Dunsany": (53.516, -6.660), "Finner": (54.494, -8.243),
    "Gurteen": (53.052, -8.009), "Johnstown Castle": (52.298, -6.497), "Knock": (53.906, -8.817),
    "Mace Head": (53.326, -9.901), "Malin Head": (55.372, -7.339), "Markree Castle": (54.175, -8.456),
    "Moore Park": (52.164, -8.264), "Mt Dillon": (53.727, -7.981), "Mullingar": (53.537, -7.362),
    "NewportMayo": (53.883, -9.546), "Oak Park": (52.861, -6.915), "Phoenix Park": (53.364, -6.350),
    "Roche's Point": (51.793, -8.244), "Shannon": (52.690, -8.918), "Sherkin Island": (51.476, -9.428),
    "Valentia": (51.938, -10.241), "Carlow": (52.861, -6.915), "Belfast": (54.664, -6.216),
}
LABELS = {"Dublin": "Dublin Airport", "Cork": "Cork Airport", "Shannon": "Shannon Airport",
          "Knock": "Ireland West Airport", "Casement": "Casement Aerodrome", "NewportMayo": "Newport, Mayo"}
_obs_cache: tuple[float, dict] | None = None
KTS_TO_KMH = 1.852
COMPASS = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
           "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}


def _num(el):
    try:
        return float(el.text.strip())
    except (AttributeError, ValueError):
        return None


def _obs_symbol(symbol: str, text: str) -> str:
    """Map Met's observation icon / text to the glyph ids the frontend already draws."""
    s = f"{symbol} {text}".lower()
    night = "night" in s
    if "thunder" in s:
        base = "RainThunder"
    elif "snow" in s:
        base = "Snow"
    elif "sleet" in s:
        base = "Sleet"
    elif "drizzle" in s:
        base = "Drizzle"
    elif "shower" in s:
        base = "LightRainSun"
    elif "rain" in s:
        base = "Rain"
    elif "fog" in s or "mist" in s or "haze" in s:
        base = "Fog"
    elif "overcast" in s or "broken" in s or "cloudy" in s:
        base = "Cloud"
    elif "scattered" in s or "few" in s or "partly" in s or "fair" in s:
        base = "PartlyCloud"
    elif "clear" in s or "sun" in s:
        base = "Sun"
    else:
        base = "Cloud"
    return ("Dark_" + base) if night and ("Sun" in base or base == "PartlyCloud") else base


async def observations(client: httpx.AsyncClient) -> dict:
    global _obs_cache
    if _obs_cache and time.time() - _obs_cache[0] < 600:
        return _obs_cache[1]
    r = await client.get(settings.obs_url, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = {"time": root.get("time"), "stations": []}
    for st in root.iter("station"):
        name = st.get("name", "").strip()
        if name not in STATIONS:
            continue
        wd = (st.findtext("wind_direction") or "").strip().upper()
        kts = _num(st.find("wind_speed"))
        out["stations"].append({
            "name": name, "label": LABELS.get(name, name),
            "lat": STATIONS[name][0], "lon": STATIONS[name][1],
            "temp": _num(st.find("temp")),
            "weather": (st.findtext("weather_text") or "").strip().capitalize(),
            "symbol": _obs_symbol(st.findtext("symbol") or "", st.findtext("weather_text") or ""),
            "wind_kmh": None if kts is None else round(kts * KTS_TO_KMH),
            "wind_dir": COMPASS.get(wd),
            "wind_name": wd or None,
            "humidity": _num(st.find("humidity")),
            "rain_mmh": _num(st.find("rainfall")),
            "pressure": _num(st.find("pressure")),
        })
    _obs_cache = (time.time(), out)
    return out
