"""Stand-in for the Met Éireann point-forecast API and warnings feed, for
offline development:

    uvicorn tools.mock_upstream:app --port 8099
    MET_PF_URL=http://127.0.0.1:8099/locationforecast \
    MET_WARNINGS_URL=http://127.0.0.1:8099/warnings.json uvicorn app.main:app
"""
import math
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI()
SYMS = ["Cloud", "LightRainSun", "PartlyCloud", "Rain", "LightCloud", "Sun", "Drizzle", "RainSun"]


@app.get("/locationforecast")
def pf(lat: str = "53.35"):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = ['<?xml version="1.0" encoding="UTF-8"?><weatherdata><product class="pointData">']
    t = now
    step = 1
    for k in range(0, 200):
        if k > 90:
            step = 3
        h = (t.hour + 1) % 24
        temp = 11 + 4 * math.sin((h - 9) / 24 * 2 * math.pi) - 0.05 * k + math.sin(k / 17)
        wd = (200 + 40 * math.sin(k / 20)) % 360
        ws = 6 + 4 * math.sin(k / 11)
        z = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(f'<time datatype="forecast" from="{z}" to="{z}"><location altitude="10" latitude="{lat}" longitude="-6.26">'
                   f'<temperature id="TTT" unit="celsius" value="{temp:.1f}"/><windDirection id="dd" deg="{wd:.1f}" name="SW"/>'
                   f'<windSpeed id="ff" mps="{ws:.1f}" beaufort="4" name="Moderate"/><windGust id="ff_gust" mps="{ws * 1.6:.1f}"/>'
                   f'<humidity value="{80 + 10 * math.sin(k / 7):.1f}" unit="percent"/><pressure id="pr" unit="hPa" value="{1008 + 6 * math.sin(k / 30):.1f}"/>'
                   f'<cloudiness id="NN" percent="{60 + 35 * math.sin(k / 9):.1f}"/><dewpointTemperature id="TD" unit="celsius" value="{temp - 2.5:.1f}"/>'
                   f'</location></time>')
        t2 = t + timedelta(hours=step)
        rain = max(0.0, 1.6 * math.sin(k / 5.5) + 0.6 * math.sin(k / 2.1) - 0.5) * step
        sym = SYMS[(k // 6) % len(SYMS)] if rain < 0.1 else ("Rain" if rain / step > 1 else "LightRainSun")
        if h < 6 or h > 20:
            sym = "Dark_" + sym if "Sun" in sym else sym
        out.append(f'<time datatype="forecast" from="{z}" to="{t2.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
                   f'<location altitude="10" latitude="{lat}" longitude="-6.26"><precipitation unit="mm" value="{rain:.1f}" minvalue="0" maxvalue="{rain * 1.5:.1f}" probability="{min(90, int(rain * 40))}"/>'
                   f'<symbol id="{sym}" number="3"/></location></time>')
        t = t2
    out.append("</product></weatherdata>")
    return Response("".join(out), media_type="application/xml")


@app.get("/warnings.json")
def warnings():
    now = datetime.now(timezone.utc)
    return [{
        "level": "Yellow", "type": "Wind", "severity": "Moderate",
        "headline": "Status Yellow - Wind warning for Galway, Mayo",
        "description": "Southwest winds will reach mean speeds of 50 to 65 km/h with gusts of 90 to 110 km/h, higher in exposed areas.",
        "onset": now.isoformat(), "expiry": (now + timedelta(hours=9)).isoformat(),
        "regions": ["EI10", "EI20"],
    }]


# ---- fake open-data portal (near-realtime listing + download) -----------------
# Serves whatever is in ./data/mock_portal/{radar,nwp}; requires any api-key header.
import io as _io
import os as _os
import zipfile as _zip
from pathlib import Path as _P

from fastapi import Header, HTTPException

_PORTAL = _P(_os.environ.get("MOCK_PORTAL_DIR", "./data/mock_portal"))


@app.get("/api/near-realtime/{dataset}")
def nrt_list(dataset: str, api_key: str = Header(None, alias="api-key")):
    if not api_key:
        raise HTTPException(401)
    d = _PORTAL / dataset
    return [{"name": f.name, "size": f.stat().st_size,
             "timestamp": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()}
            for f in sorted(d.iterdir())] if d.exists() else []


@app.get("/api/near-realtime/download/{dataset}")
def nrt_download(dataset: str, files: str, api_key: str = Header(None, alias="api-key")):
    if not api_key:
        raise HTTPException(401)
    names = files.split(",")
    if dataset == "nwp":  # exercise the zip path for NWP
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            for n in names:
                z.write(_PORTAL / dataset / n, n)
        return Response(buf.getvalue(), media_type="application/octet-stream")
    return Response((_PORTAL / dataset / names[0]).read_bytes(), media_type="application/octet-stream")


@app.get("/obs_present.xml")
def obs():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = [("Dublin", 13, "scattered_clouds-night.png", "FAIR", "07", "SW", 78, "0.0", 1008),
            ("Phoenix Park", 17, "--", "--", "-99", "-99", 80, "0.1", 1019),
            ("Casement", 12, "light_rain.png", "LIGHT RAIN", "09", "SSW", 88, "0.4", 1007),
            ("Shannon", 14, "rain.png", "RAIN", "14", "SW", 93, "1.8", 1004),
            ("Cork", 15, "few_clouds-night.png", "FAIR", "10", "WSW", 80, "0.0", 1005),
            ("Malin Head", 11, "showers.png", "RAIN SHOWER", "22", "W", 85, "2.2", 1003)]
    body = "".join(
        f'<station name="{n}"><temp unit="C">{t}</temp><symbol>{sym}</symbol><weather_text>{wt}</weather_text>'
        f'<wind_speed unit="kts">{ws} </wind_speed><wind_direction> {wd} </wind_direction>'
        f'<humidity unit="%"> {h} </humidity><rainfall unit="mm/h"> {r} </rainfall><pressure unit="hPa">{p}</pressure></station>'
        for n, t, sym, wt, ws, wd, h, r, p in rows)
    return Response(f'<?xml version="1.0"?><observations time="{now.isoformat()}">{body}</observations>',
                    media_type="application/xml")
