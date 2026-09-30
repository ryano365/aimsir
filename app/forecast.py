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
