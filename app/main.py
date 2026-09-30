"""Aimsir - a small self-hosted viewer for Met Éireann radar and NWP data."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import nwp, radar
from .config import settings
from .forecast import point_forecast, warnings
from .geo import RADAR_GRID
from .met import MetClient, MetError
from .palettes import RAIN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aimsir")

D = settings.data_dir
RAW_RADAR, RAW_NWP = D / "raw" / "radar", D / "raw" / "nwp"
RADAR_OUT, NWP_OUT = D / "radar", D / "nwp"
for p in (RAW_RADAR, RAW_NWP, RADAR_OUT, NWP_OUT, settings.inbox_dir):
    p.mkdir(parents=True, exist_ok=True)

status: dict[str, dict] = {
    "radar": {"last_poll": None, "last_ok": None, "error": None, "frames": 0},
    "nwp": {"last_poll": None, "last_ok": None, "error": None, "run": None, "busy": False},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _sweep_inbox() -> None:
    """Move files dropped in the inbox to the right raw folder."""
    for f in settings.inbox_dir.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        with open(f, "rb") as fh:
            head = fh.read(8)
        if head.startswith(b"\x89HDF"):
            shutil.move(str(f), RAW_RADAR / f.name)
        elif head.startswith(b"GRIB"):
            shutil.move(str(f), RAW_NWP / f.name)


# ------------------------------------------------------------------ radar

def _render_radar() -> int:
    cutoff = _now() - timedelta(minutes=settings.radar_history_min)
    slots: dict[datetime, list[Path]] = {}
    for f in RAW_RADAR.iterdir():
        if f.name.startswith("."):
            continue
        t = radar.file_time(f.name)
        if t is None:
            try:
                t = radar.read_sweep(f).time
            except Exception:  # noqa: BLE001
                f.unlink(missing_ok=True)
                continue
        if t < cutoff:
            f.unlink(missing_ok=True)
            continue
        slots.setdefault(radar.slot_of(t), []).append(f)

    index = _read_json(RADAR_OUT / "index.json", {"frames": []})
    known = {fr["stamp"]: fr for fr in index.get("frames", [])}
    frames = []
    sites_seen: dict[str, radar.Sweep] = {}
    for slot in sorted(slots):
        files = sorted(p.name for p in slots[slot])
        stamp = slot.strftime("%Y%m%d%H%M")
        png = RADAR_OUT / f"radar_{stamp}.png"
        prev = known.get(stamp)
        if prev and prev.get("files") == files and png.exists():
            frames.append(prev)
            continue
        sweeps = []
        for p in slots[slot]:
            try:
                sweeps.append(radar.read_sweep(p))
            except Exception as e:  # noqa: BLE001
                log.warning("radar %s unreadable: %s", p.name, e)
        if not sweeps:
            continue
        for s in sweeps:
            sites_seen[f"{s.lat:.3f},{s.lon:.3f}"] = s
        png.write_bytes(radar.render_png(radar.composite(sweeps), settings.radar_min_dbz))
        frames.append({"stamp": stamp, "time": slot.isoformat(), "files": files,
                       "sites": len(sweeps)})
    # coverage overlay, rebuilt when the set of radar sites changes
    if sites_seen:
        key = sorted(sites_seen)
        if index.get("coverage_sites") != key or not (RADAR_OUT / "coverage.png").exists():
            (RADAR_OUT / "coverage.png").write_bytes(
                radar.render_coverage_png(radar.coverage_mask(list(sites_seen.values()))))
            index["coverage_sites"] = key
    keep = {f"radar_{fr['stamp']}.png" for fr in frames}
    for f in RADAR_OUT.glob("radar_*.png"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
    index.update({"frames": frames, "bounds": RADAR_GRID.bounds, "legend": RAIN.legend(),
                  "updated": _now().isoformat()})
    _write_json(RADAR_OUT / "index.json", index)
    return len(frames)


async def radar_loop(client: MetClient | None):
    rx = re.compile(settings.radar_file_regex)
    while True:
        st = status["radar"]
        st["last_poll"] = _now().isoformat()
        try:
            _sweep_inbox()
            if client:
                since = _now() - timedelta(minutes=settings.radar_history_min)
                listing = await client.list("radar", since)
                have = {p.name for p in RAW_RADAR.iterdir()}
                for item in listing:
                    name = item["name"]
                    if rx.search(name) and name not in have:
                        await client.download("radar", name, RAW_RADAR)
            st["frames"] = await asyncio.to_thread(_render_radar)
            st["last_ok"], st["error"] = _now().isoformat(), None
        except Exception as e:  # noqa: BLE001
            log.exception("radar poll failed")
            st["error"] = str(e)
        await asyncio.sleep(settings.radar_poll_s)


# ------------------------------------------------------------------ nwp

def _process_nwp(force: bool = False) -> str | None:
    files = sorted(p for p in RAW_NWP.iterdir() if p.is_file() and not p.name.startswith("."))
    sig = [[p.name, p.stat().st_size] for p in files]
    state = _read_json(NWP_OUT / "current.json", {})
    if not files or (not force and state.get("sig") == sig):
        return state.get("run_id")
    refs = nwp.scan(files)
    if not refs:
        raise ValueError(f"{len(files)} NWP file(s) downloaded but none contain recognised fields "
                         "(2 m temperature, 10 m wind, MSLP, precipitation, cloud)")
    found = {r.field for r in refs}
    status["nwp"]["fields_found"] = sorted(found)
    status["nwp"]["fields_missing"] = [f for f in nwp.FIELDS if f not in found]
    index = nwp.process_run(refs, NWP_OUT, settings.nwp_max_hours)
    run_id = index["run_id"]
    run = datetime.fromisoformat(index["run"])
    # raw files that only hold older runs are no longer needed
    newest_in_file: dict[Path, datetime] = {}
    for r in refs:
        newest_in_file[r.path] = max(newest_in_file.get(r.path, r.run), r.run)
    for p in files:
        t = newest_in_file.get(p)
        if (t is not None and t < run) or (t is None and time.time() - p.stat().st_mtime > 12 * 3600):
            p.unlink(missing_ok=True)
    sig = [[p.name, p.stat().st_size] for p in sorted(RAW_NWP.iterdir()) if p.is_file()]
    _write_json(NWP_OUT / "current.json", {"run_id": run_id, "sig": sig})
    for d in NWP_OUT.iterdir():
        if d.is_dir() and d.name < run_id:
            shutil.rmtree(d, ignore_errors=True)
    return run_id


async def nwp_loop(client: MetClient | None):
    rx = re.compile(settings.nwp_file_regex)
    max_bytes = settings.nwp_max_file_mb * 1024 * 1024
    while True:
        st = status["nwp"]
        st["last_poll"] = _now().isoformat()
        try:
            _sweep_inbox()
            if client:
                listing = await client.list("nwp", _now() - timedelta(hours=3))
                have = {p.name for p in RAW_NWP.iterdir()}
                skipped = 0
                for item in listing:
                    name, size = item["name"], item.get("size") or 0
                    if not rx.search(name) or name in have:
                        continue
                    if size and size > max_bytes:
                        skipped += 1
                        continue
                    raw_bytes = sum(p.stat().st_size for p in RAW_NWP.iterdir() if p.is_file())
                    if raw_bytes > settings.nwp_max_raw_gb * 1024 ** 3:
                        log.warning("NWP raw folder over NWP_MAX_RAW_GB - narrow NWP_FILE_REGEX")
                        break
                    try:
                        await client.download("nwp", name, RAW_NWP, max_bytes)
                    except MetError as e:
                        log.warning("nwp %s: %s", name, e)
                if skipped:
                    log.info("skipped %d NWP files over NWP_MAX_FILE_MB", skipped)
            st["busy"] = True
            st["run"] = await asyncio.to_thread(_process_nwp)
            st["last_ok"], st["error"] = _now().isoformat(), None
        except Exception as e:  # noqa: BLE001
            log.exception("nwp poll failed")
            st["error"] = str(e)
        finally:
            st["busy"] = False
        await asyncio.sleep(settings.nwp_poll_s)


# ------------------------------------------------------------------ app

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(headers={"User-Agent": "aimsir-selfhosted/1.0"}, follow_redirects=True)
    app.state.met = MetClient() if settings.api_key else None
    if not settings.api_key:
        log.warning("MET_API_KEY not set: only files dropped in %s will be shown", settings.inbox_dir)
    tasks = [asyncio.create_task(radar_loop(app.state.met)), asyncio.create_task(nwp_loop(app.state.met))]
    yield
    for t in tasks:
        t.cancel()
    await app.state.http.aclose()
    if app.state.met:
        await app.state.met.close()


app = FastAPI(title="Aimsir", lifespan=lifespan, docs_url="/api/docs")
STATIC = Path(__file__).resolve().parent.parent / "static"


@app.get("/api/config")
def config():
    return {
        "home": {"name": settings.home_name, "lat": settings.home_lat, "lon": settings.home_lon},
        "has_key": bool(settings.api_key),
    }


@app.get("/api/status")
def get_status():
    return status


@app.get("/api/radar")
def radar_index():
    idx = _read_json(RADAR_OUT / "index.json", {"frames": [], "bounds": RADAR_GRID.bounds, "legend": RAIN.legend()})
    for fr in idx["frames"]:
        fr.pop("files", None)
        fr["url"] = f"/data/radar/radar_{fr['stamp']}.png"
    idx["coverage"] = "/data/radar/coverage.png" if (RADAR_OUT / "coverage.png").exists() else None
    idx.pop("coverage_sites", None)
    return idx


def _current_run_dir() -> Path | None:
    run_id = _read_json(NWP_OUT / "current.json", {}).get("run_id")
    if not run_id:
        return None
    d = NWP_OUT / run_id
    return d if (d / "index.json").exists() else None


@app.get("/api/nwp")
def nwp_index():
    d = _current_run_dir()
    if not d:
        return {"run": None, "frames": [], "busy": status["nwp"]["busy"], "error": status["nwp"]["error"]}
    idx = _read_json(d / "index.json", {})
    idx["base"] = f"/data/nwp/{d.name}/"
    return idx


@app.get("/api/nwp/inspect")
def nwp_inspect(stamp: str = Query(pattern=r"^\d{12}$"), lat: float = Query(), lon: float = Query()):
    d = _current_run_dir()
    vals = nwp.inspect(d, stamp, lat, lon) if d else None
    if vals is None:
        raise HTTPException(404, "no model data here")
    return vals


@app.get("/api/forecast")
async def forecast(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    try:
        return await point_forecast(app.state.http, lat, lon)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"forecast API: {e}") from e


@app.get("/api/warnings")
async def get_warnings():
    try:
        return await warnings(app.state.http)
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"warnings feed: {e}") from e


@app.get("/api/debug/list/{dataset}")
async def debug_list(dataset: str, hours: float = 3):
    """Raw near-realtime listing - handy for tuning RADAR_FILE_REGEX / NWP_FILE_REGEX."""
    if dataset not in ("radar", "nwp"):
        raise HTTPException(404)
    if not app.state.met:
        raise HTTPException(400, "MET_API_KEY not set")
    try:
        return await app.state.met.list(dataset, _now() - timedelta(hours=hours))
    except (MetError, httpx.HTTPError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/debug/reprocess")
async def reprocess():
    await asyncio.to_thread(_render_radar)
    run = await asyncio.to_thread(_process_nwp, True)
    return {"radar": "ok", "nwp_run": run}


class CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            # frame files are immutable (timestamped names); indexes are not
            immutable = path.endswith(".png") and "coverage" not in path or path.startswith("20") and path.endswith(".json") and "index" not in path
            resp.headers["Cache-Control"] = "public, max-age=86400" if immutable else "no-cache"
        return resp


app.mount("/data/radar", CachedStatic(directory=RADAR_OUT), name="radar-data")
app.mount("/data/nwp", CachedStatic(directory=NWP_OUT), name="nwp-data")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
