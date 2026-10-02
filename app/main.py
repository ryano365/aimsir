"""Aimsir - a small self-hosted viewer for Met Éireann radar and NWP data."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import nwp, radar, tiles
from .grids import bilinear
from .config import settings
from .forecast import observations, point_forecast, warnings
from .met import MetClient, MetError
from .palettes import RAIN, RAIN_ACC

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aimsir")

D = settings.data_dir
RAW_RADAR, RAW_NWP = D / "raw" / "radar", D / "raw" / "nwp"
RAW_ACC = D / "raw" / "radar_acc"
RADAR_OUT, NWP_OUT = D / "radar", D / "nwp"
for p in (RAW_RADAR, RAW_ACC, RAW_NWP, RADAR_OUT, NWP_OUT, settings.inbox_dir, D / "tiles"):
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
            acc = re.search(settings.radar_acc_regex, f.name)
            shutil.move(str(f), (RAW_ACC if acc else RAW_RADAR) / f.name)
        elif head.startswith(b"GRIB"):
            shutil.move(str(f), RAW_NWP / f.name)


# ------------------------------------------------------------------ radar

RADAR_FRAMES, ACC_FRAMES = RADAR_OUT / "frames", RADAR_OUT / "acc"
TILE_CACHE = D / "tiles"


def _render_radar() -> int:
    """Decode each 5-minute slot's volume scans and store the lowest sweeps; the map
    tiles are drawn from these on request."""
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
    sites: dict[str, dict] = {}
    for slot in sorted(slots):
        files = sorted(p.name for p in slots[slot])
        stamp = slot.strftime("%Y%m%d%H%M")
        fdir = RADAR_FRAMES / stamp
        prev = known.get(stamp)
        if prev and prev.get("files") == files and (fdir / "meta.json").exists():
            frames.append(prev)
            for m in _read_json(fdir / "meta.json", []):
                sites[f"{m['lat']:.3f},{m['lon']:.3f}"] = m
            continue
        sweeps = []
        for p in slots[slot]:
            try:
                sweeps.append(radar.read_sweep(p))
            except Exception as e:  # noqa: BLE001
                log.warning("radar %s unreadable: %s", p.name, e)
        if not sweeps:
            continue
        if fdir.exists():
            shutil.rmtree(fdir, ignore_errors=True)
            shutil.rmtree(TILE_CACHE / "radar" / stamp, ignore_errors=True)
        fdir.mkdir(parents=True, exist_ok=True)
        metas = []
        for k, sw in enumerate(sweeps):
            np.save(fdir / f"s{k}.npy", sw.dbz.astype(np.float16))
            m = radar.site_meta(sw)
            metas.append(m)
            sites[f"{m['lat']:.3f},{m['lon']:.3f}"] = m
        _write_json(fdir / "meta.json", metas)
        frames.append({"stamp": stamp, "time": slot.isoformat(), "files": files, "sites": len(sweeps)})
    keep = {fr["stamp"] for fr in frames}
    for d in (RADAR_FRAMES.iterdir() if RADAR_FRAMES.exists() else []):
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(TILE_CACHE / "radar" / d.name, ignore_errors=True)
    for f in RADAR_OUT.glob("*.png"):  # images from the pre-tile version
        f.unlink(missing_ok=True)
    site_list = [sites[k] for k in sorted(sites)]
    cov_key = hashlib.sha1(json.dumps(site_list, sort_keys=True).encode()).hexdigest()[:10] if site_list else None
    if cov_key and index.get("coverage_key") != cov_key:
        shutil.rmtree(TILE_CACHE / "coverage", ignore_errors=True)
    bbox = None
    if site_list:
        bbs = [radar.site_bbox(m) for m in site_list]
        bbox = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
    index.update({"frames": frames, "sites": site_list, "coverage_key": cov_key, "bbox": bbox,
                  "legend": RAIN.legend(), "updated": _now().isoformat()})
    _write_json(RADAR_OUT / "index.json", index)
    return len(frames)


def _render_accum() -> int:
    """Hourly radar rainfall totals (ODIM cartesian composite), stored for tiling."""
    cutoff = _now() - timedelta(hours=settings.radar_acc_hours)
    index = _read_json(RADAR_OUT / "acc_index.json", {"frames": []})
    known = {fr["stamp"]: fr for fr in index.get("frames", [])}
    frames = []
    for f in sorted(RAW_ACC.iterdir()):
        if f.name.startswith("."):
            continue
        t = radar.file_time(f.name)
        if t is not None and t < cutoff:
            f.unlink(missing_ok=True)
            continue
        stamp_guess = t.strftime("%Y%m%d%H%M") if t else None
        if stamp_guess and stamp_guess in known and (ACC_FRAMES / stamp_guess / "values.npy").exists():
            frames.append(known[stamp_guess])
            continue
        try:
            c = radar.read_cartesian(f)
        except Exception as e:  # noqa: BLE001
            log.warning("radar accumulation %s unreadable: %s", f.name, e)
            continue
        t = t or c.time
        stamp = t.strftime("%Y%m%d%H%M")
        vals = radar.dbz_to_rate(c.values) if c.quantity in radar.REFLECTIVITY else c.values
        mapping = radar.cart_mapping(c)
        mapping["bbox"] = radar.cart_bbox(mapping, c.corners)
        adir = ACC_FRAMES / stamp
        shutil.rmtree(TILE_CACHE / "radaracc" / stamp, ignore_errors=True)
        adir.mkdir(parents=True, exist_ok=True)
        np.save(adir / "values.npy", vals.astype(np.float16))
        _write_json(adir / "mapping.json", mapping)
        frames.append({"stamp": stamp, "time": t.isoformat(), "quantity": c.quantity, "projdef": c.projdef,
                       "max": round(float(np.nanmax(vals)), 2) if np.isfinite(vals).any() else None})
    keep = {fr["stamp"] for fr in frames}
    for d in (ACC_FRAMES.iterdir() if ACC_FRAMES.exists() else []):
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(TILE_CACHE / "radaracc" / d.name, ignore_errors=True)
    frames.sort(key=lambda fr: fr["stamp"])
    _write_json(RADAR_OUT / "acc_index.json", {"frames": frames, "updated": _now().isoformat()})
    return len(frames)


async def radar_loop(client: MetClient | None):
    rx = re.compile(settings.radar_file_regex)
    rx_acc = re.compile(settings.radar_acc_regex)
    while True:
        st = status["radar"]
        st["last_poll"] = _now().isoformat()
        try:
            _sweep_inbox()
            if client:
                since = _now() - timedelta(minutes=max(settings.radar_history_min, settings.radar_acc_hours * 60))
                listing = await client.list("radar", since)
                recent = _now() - timedelta(minutes=settings.radar_history_min)
                have = {p.name for p in RAW_RADAR.iterdir()} | {p.name for p in RAW_ACC.iterdir()}
                for item in listing:
                    name = item["name"]
                    if name in have:
                        continue
                    if rx.search(name):
                        t = radar.file_time(name)
                        if t is None or t >= recent:
                            await client.download("radar", name, RAW_RADAR)
                    elif rx_acc.search(name):
                        try:
                            await client.download("radar", name, RAW_ACC)
                        except (MetError, httpx.HTTPError) as e:
                            log.warning("radar accumulation %s: %s", name, e)
            st["frames"] = await asyncio.to_thread(_render_radar)
            try:
                st["acc_frames"] = await asyncio.to_thread(_render_accum)
            except Exception as e:  # noqa: BLE001 - accumulation is optional
                log.warning("radar accumulation failed: %s", e)
                st["acc_error"] = str(e)
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
        for k in ("fields_found", "fields_missing"):
            if k in state:
                status["nwp"][k] = state[k]
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
    _write_json(NWP_OUT / "current.json", {"run_id": run_id, "sig": sig,
                                            "fields_found": status["nwp"]["fields_found"],
                                            "fields_missing": status["nwp"]["fields_missing"]})
    for d in NWP_OUT.iterdir():
        if d.is_dir() and d.name < run_id:
            shutil.rmtree(d, ignore_errors=True)
    return run_id


NWP_NAME = re.compile(r"fc(\d{10})\+(\d{3})")  # fc<run yyyymmddhh>+<lead hours>...


def plan_nwp_downloads(listing: list[dict], rx: re.Pattern, have: set[str], current_run: str | None,
                       max_hours: int, run_every: int, keep_runs: int = 2) -> list[dict]:
    """Choose which near-realtime NWP files to fetch: matching the regex, not already here,
    from the newest `keep_runs` runs (every `run_every` hours, never older than the run on
    screen), lead time <= max_hours. Newest run first, then in lead-time order."""
    picked = []
    for it in listing:
        name = str(it.get("name", ""))
        if not rx.search(name) or name in have:
            continue
        m = NWP_NAME.search(name)
        if not m:
            picked.append(("", 0, it))
            continue
        run_id, lead = m.group(1), int(m.group(2))
        if lead > max_hours or int(run_id[-2:]) % max(run_every, 1):
            continue
        if current_run and run_id < current_run:
            continue
        picked.append((run_id, lead, it))
    runs = sorted({r for r, _, _ in picked if r}, reverse=True)[:keep_runs]
    picked = [p for p in picked if not p[0] or p[0] in runs]
    picked.sort(key=lambda p: (p[0], -p[1]), reverse=True)
    return [p[2] for p in picked]


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
                current = _read_json(NWP_OUT / "current.json", {}).get("run_id")
                have = {p.name for p in RAW_NWP.iterdir()}
                todo = plan_nwp_downloads(listing, rx, have, current, settings.nwp_max_hours,
                                          settings.nwp_run_every_hours)
                skipped = 0
                for item in todo:
                    name, size = item["name"], item.get("size") or 0
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
        "basemap": settings.basemap,
    }


@app.get("/api/status")
def get_status():
    return status


@app.get("/api/radar")
def radar_index():
    idx = _read_json(RADAR_OUT / "index.json", {"frames": [], "legend": RAIN.legend()})
    frames = [{"stamp": fr["stamp"], "time": fr["time"], "sites": fr.get("sites")} for fr in idx.get("frames", [])]
    acc = _read_json(RADAR_OUT / "acc_index.json", {"frames": []})
    return {
        "frames": frames,
        "legend": idx.get("legend", RAIN.legend()),
        "bbox": idx.get("bbox"),
        "tiles": "/tiles/radar/{stamp}/{z}/{x}/{y}.png",
        "coverage": f"/tiles/coverage/{idx['coverage_key']}/{{z}}/{{x}}/{{y}}.png" if idx.get("coverage_key") else None,
        "acc": {
            "legend": RAIN_ACC.legend(),
            "tiles": "/tiles/radaracc/{stamp}/{z}/{x}/{y}.png",
            "frames": [{"stamp": fr["stamp"], "time": fr["time"]} for fr in acc.get("frames", [])],
        },
    }


# ------------------------------------------------------------------ tiles

@lru_cache(maxsize=64)
def _radar_frame(stamp: str, mtime: float):
    fdir = RADAR_FRAMES / stamp
    metas = _read_json(fdir / "meta.json", [])
    return [(np.load(fdir / f"s{k}.npy").astype(np.float32), m) for k, m in enumerate(metas)]


def _png(data: bytes) -> Response:
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=604800, immutable"})


def _tile_args_ok(z: int, x: int, y: int) -> bool:
    return 3 <= z <= 14 and 0 <= x < 2 ** z and 0 <= y < 2 ** z


@app.get("/tiles/radar/{stamp}/{z}/{x}/{y}.png")
def radar_tile(stamp: str, z: int, x: int, y: int):
    fdir = RADAR_FRAMES / stamp
    if not re.fullmatch(r"\d{12}", stamp) or not _tile_args_ok(z, x, y) or not (fdir / "meta.json").exists():
        raise HTTPException(404)

    def render():
        data = _radar_frame(stamp, (fdir / "meta.json").stat().st_mtime)
        if not any(tiles.intersects(z, x, y, radar.site_bbox(m)) for _, m in data):
            return tiles.EMPTY
        lat, lon = tiles.tile_latlon(z, x, y)
        dbz = np.full(lat.shape, np.nan, dtype=np.float32)
        for arr, m in data:
            dbz = np.fmax(dbz, radar.sample_polar(arr, m, lat, lon))
        dbz[dbz < settings.radar_min_dbz] = np.nan
        return tiles.encode(RAIN.colorize(radar.dbz_to_rate(dbz)))

    return _png(tiles.cached(TILE_CACHE / "radar" / stamp / str(z) / str(x) / f"{y}.png", render))


@app.get("/tiles/coverage/{key}/{z}/{x}/{y}.png")
def coverage_tile(key: str, z: int, x: int, y: int):
    idx = _read_json(RADAR_OUT / "index.json", {})
    if key != idx.get("coverage_key") or not _tile_args_ok(z, x, y):
        raise HTTPException(404)

    def render():
        lat, lon = tiles.tile_latlon(z, x, y)
        seen = np.zeros(lat.shape, dtype=bool)
        for m in idx.get("sites", []):
            seen |= radar.in_range(m, lat, lon)
        return tiles.encode(tiles.hatch(~seen, z, x, y))

    return _png(tiles.cached(TILE_CACHE / "coverage" / key / str(z) / str(x) / f"{y}.png", render))


@lru_cache(maxsize=24)
def _acc_frame(stamp: str, mtime: float):
    adir = ACC_FRAMES / stamp
    return np.load(adir / "values.npy").astype(np.float32), _read_json(adir / "mapping.json", {})


@app.get("/tiles/radaracc/{stamp}/{z}/{x}/{y}.png")
def acc_tile(stamp: str, z: int, x: int, y: int):
    adir = ACC_FRAMES / stamp
    if not re.fullmatch(r"\d{12}", stamp) or not _tile_args_ok(z, x, y) or not (adir / "values.npy").exists():
        raise HTTPException(404)

    def render():
        vals, m = _acc_frame(stamp, (adir / "values.npy").stat().st_mtime)
        if m.get("bbox") and not tiles.intersects(z, x, y, m["bbox"]):
            return tiles.EMPTY
        lat, lon = tiles.tile_latlon(z, x, y)
        row, col = radar.cart_frac(m, lat, lon)
        return tiles.encode(RAIN_ACC.colorize(bilinear(vals, row, col)))

    return _png(tiles.cached(TILE_CACHE / "radaracc" / stamp / str(z) / str(x) / f"{y}.png", render))


@app.get("/tiles/nwp/{run_id}/{layer}/{stamp}/{z}/{x}/{y}.png")
def nwp_tile(run_id: str, layer: str, stamp: str, z: int, x: int, y: int):
    run_dir = NWP_OUT / run_id
    if (not re.fullmatch(r"\d{10}", run_id) or not re.fullmatch(r"\d{12}", stamp)
            or layer not in nwp.LAYERS or not _tile_args_ok(z, x, y) or not (run_dir / "index.json").exists()):
        raise HTTPException(404)
    index = _nwp_index(run_id, (run_dir / "index.json").stat().st_mtime)
    return _png(tiles.cached(run_dir / "tiles" / layer / stamp / str(z) / str(x) / f"{y}.png",
                             lambda: nwp.render_tile(run_dir, index, layer, stamp, z, x, y)))


@lru_cache(maxsize=4)
def _nwp_index(run_id: str, mtime: float) -> dict:
    return _read_json(NWP_OUT / run_id / "index.json", {})


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
    idx["tiles"] = f"/tiles/nwp/{d.name}/{{layer}}/{{stamp}}/{{z}}/{{x}}/{{y}}.png"
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


@app.get("/api/live")
async def live(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    """What's being measured now near a point: the nearest Met Éireann station's latest
    observation, plus the newest radar frame sampled exactly at the point."""
    out: dict = {"station": None, "radar": None}
    try:
        obs = await observations(app.state.http)
        ranked = sorted(
            ((float(radar.great_circle(lat, lon, st["lat"], st["lon"])[0]) / 1000, st) for st in obs["stations"]),
            key=lambda x: x[0])
        best = next(((d, st) for d, st in ranked if st["temp"] is not None), None)
        if best:
            d0, st0 = best
            station = {**st0, "distance_km": round(d0), "time": obs["time"], "filled": {}}
            # Fill gaps (Met reports some sensors as -99) from the next-nearest station within 30 km.
            groups = {"wind": ("wind_kmh", "wind_dir", "wind_name"), "weather": ("weather", "symbol"),
                      "humidity": ("humidity",), "pressure": ("pressure",), "rain": ("rain_mmh",)}
            for g, keys in groups.items():
                if station[keys[0]] not in (None, ""):
                    continue
                for d, other in ranked:
                    if other is st0 or d > 30:
                        continue
                    if other[keys[0]] not in (None, ""):
                        for k in keys:
                            station[k] = other[k]
                        station["filled"][g] = other["label"]
                        break
            out["station"] = station
    except (httpx.HTTPError, ValueError) as e:
        out["station_error"] = str(e)
    idx = _read_json(RADAR_OUT / "index.json", {})
    frames = idx.get("frames", [])
    if frames:
        fr = frames[-1]
        fdir = RADAR_FRAMES / fr["stamp"]
        if (fdir / "meta.json").exists():
            data = _radar_frame(fr["stamp"], (fdir / "meta.json").stat().st_mtime)
            la, lo = np.array([[lat]]), np.array([[lon]])
            dbz = np.nan
            covered = False
            for arr, m in data:
                v = float(radar.sample_polar(arr, m, la, lo)[0, 0])
                covered |= bool(radar.in_range(m, la, lo)[0, 0])
                dbz = v if not np.isfinite(dbz) else max(dbz, v)
            rate = float(radar.dbz_to_rate(np.array(dbz))) if np.isfinite(dbz) and dbz >= settings.radar_min_dbz else 0.0
            out["radar"] = {"time": fr["time"], "covered": covered, "rate_mmh": round(rate, 1) if covered else None}
    return out


@app.get("/api/warnings")
async def get_warnings():
    try:
        return await warnings(app.state.http)
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"warnings feed: {e}") from e


@app.get("/api/debug/list/{dataset}")
async def debug_list(dataset: str, hours: float = 3, full: bool = False):
    """Near-realtime listing, summarised by file-name pattern (digits -> #).
    Handy for tuning RADAR_FILE_REGEX / NWP_FILE_REGEX. ?full=true for the raw list."""
    if dataset not in ("radar", "nwp"):
        raise HTTPException(404)
    if not app.state.met:
        raise HTTPException(400, "MET_API_KEY not set")
    try:
        items = await app.state.met.list(dataset, _now() - timedelta(hours=hours))
    except (MetError, httpx.HTTPError) as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if full:
        return items
    groups: dict[str, dict] = {}
    for it in items:
        name = str(it.get("name", ""))
        g = groups.setdefault(re.sub(r"\d", "#", name), {"count": 0, "bytes": 0, "examples": []})
        g["count"] += 1
        g["bytes"] += int(it.get("size") or 0)
        if len(g["examples"]) < 3:
            g["examples"].append(name)
    patterns = sorted(groups.items(), key=lambda kv: -kv[1]["count"])
    return {
        "files": len(items),
        "total_mb": round(sum(g["bytes"] for g in groups.values()) / 1e6, 1),
        "patterns": [{"pattern": k, "count": v["count"], "total_mb": round(v["bytes"] / 1e6, 1),
                      "examples": v["examples"]} for k, v in patterns[:40]],
        "sample_item": items[0] if items else None,
    }


@app.get("/api/debug/radar")
def debug_radar(kind: str = "acc", name: str | None = None):
    """ODIM attributes of the newest downloaded radar file (kind=acc or volume)."""
    folder = RAW_ACC if kind == "acc" else RAW_RADAR
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    if name:
        files = [p for p in files if p.name == name]
    if not files:
        raise HTTPException(404, "no radar files of that kind downloaded yet")
    return {"file": files[-1].name, "attributes": radar.odim_summary(files[-1])}


@app.get("/api/debug/grib")
def debug_grib(name: str | None = None, limit: int = 400):
    """Inventory of one downloaded NWP file: which parameters/levels it holds."""
    files = sorted(p for p in RAW_NWP.iterdir() if p.is_file() and not p.name.startswith("."))
    if name:
        files = [p for p in files if p.name == name]
    if not files:
        raise HTTPException(404, "no NWP files downloaded yet")
    return {"file": files[0].name, "messages": nwp.inventory(files[0], limit)}


@app.post("/api/debug/reprocess")
async def reprocess():
    _sweep_inbox()
    await asyncio.to_thread(_render_radar)
    await asyncio.to_thread(_render_accum)
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
