"""Decode HARMONIE-AROME GRIB output and render map layers.

The near-realtime feed gives us GRIB files (edition 1 or 2 - both handled)
whose exact split (per lead time, per parameter...) we don't depend on: every
file is scanned, messages we care about are recognised by their keys, and the
newest model run is rendered hour by hour."""
from __future__ import annotations

import json
import shutil
import logging
import math
from collections import defaultdict
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import eccodes as ec

from . import tiles
from .grids import GridSampler, bilinear
from .palettes import CLOUD, GUST, LIGHTNING, RAIN, SNOW, TEMP, VIS, WIND

log = logging.getLogger(__name__)

FIELDS = ("t2", "u10", "v10", "msl", "tp", "tcc")
CLOUD_LAYERS = ("lcc", "mcc", "hcc")  # used to build total cloud when tcc is absent
EXTRA_FIELDS = ("gust", "gu", "gv", "vis", "sd", "ltg")  # optional layers


def _get(h, key, default=None):
    try:
        return ec.codes_get(h, key)
    except Exception:  # noqa: BLE001 - eccodes raises a zoo of KeyValueNotFound variants
        return default


def classify(h) -> str | None:
    """Map a GRIB message onto one of FIELDS (or None to ignore it)."""
    ed = _get(h, "edition", 2)
    sn = str(_get(h, "shortName", "")).lower()
    lt = str(_get(h, "typeOfLevel", ""))
    lev = _get(h, "level", -1)
    if ed == 1:
        iop = _get(h, "indicatorOfParameter", -1)
        table = _get(h, "table2Version", 1)
        if table <= 3 or table == 253:  # WMO table / HIRLAM-ALADIN local table
            if iop == 11 and lev == 2 and lt != "isobaricInhPa":
                return "t2"
            if iop == 33 and lev == 10 and lt != "isobaricInhPa":
                return "u10"
            if iop == 34 and lev == 10 and lt != "isobaricInhPa":
                return "v10"
            if iop == 1 and lt in ("meanSea", "heightAboveSea") and lev == 0:
                return "msl"
            if iop == 61 and lev in (0, -1):
                return "tp"
            if iop == 71 and lt not in ("isobaricInhPa", "hybrid"):
                return "tcc"
    if ed == 2 and _get(h, "discipline", -1) == 0:
        # Fall back on the raw WMO codes in case a local table renames things
        cat, num = _get(h, "parameterCategory", -1), _get(h, "parameterNumber", -1)
        if (cat, num) in ((1, 8), (1, 52)) and lt in ("surface", "heightAboveGround", ""):
            return "tp"
        # WMO 6/1 = total cloud; HARMONIE writes it as local 6/192 (and 6/194-196 for
        # low/medium/high, where WMO uses 6/3-5). Met Éireann's DINI files use the local codes.
        if (cat, num) in ((6, 1), (6, 192)) and lt not in ("isobaricInhPa",):
            return "tcc"
        if (cat, num) == (17, 192):          # HARMONIE lightning (local)
            return "ltg"
        if (cat, num) == (19, 0) and lt not in ("isobaricInhPa",):
            return "vis"
        if (cat, num) == (2, 22) and lt not in ("isobaricInhPa", "hybrid"):
            return "gust"
        if (cat, num) in ((2, 23), (2, 24)) and lt not in ("isobaricInhPa", "hybrid"):
            return "gu" if num == 23 else "gv"
        layered = {3: "lcc", 4: "mcc", 5: "hcc", 194: "lcc", 195: "mcc", 196: "hcc"}
        if cat == 6 and num in layered and lt not in ("isobaricInhPa", "hybrid"):
            return layered[num]
    if sn in ("2t", "t2m") or (sn == "t" and lt == "heightAboveGround" and lev == 2):
        return "t2"
    if sn in ("10u",) or (sn == "u" and lt == "heightAboveGround" and lev == 10):
        return "u10"
    if sn in ("10v",) or (sn == "v" and lt == "heightAboveGround" and lev == 10):
        return "v10"
    if sn in ("msl", "prmsl") or (sn == "pres" and lt in ("meanSea", "heightAboveSea")):
        return "msl"
    if sn in ("tp", "tprate", "prate"):
        return "tp"
    if sn == "tcc" and lt not in ("isobaricInhPa", "hybrid"):
        return "tcc"
    if sn in CLOUD_LAYERS and lt not in ("isobaricInhPa", "hybrid"):
        return sn
    if sn in ("max_10efg", "10efg"):
        return "gu"
    if sn in ("max_10nfg", "10nfg"):
        return "gv"
    if sn in ("10fg", "fg10", "max_10fg", "i10fg", "gust"):
        return "gust"
    if sn == "vis":
        return "vis"
    if sn in ("sd", "sde", "sdwe") and lt not in ("isobaricInhPa", "hybrid"):
        return "sd"
    if sn in ("lgt", "ltng", "litoti"):
        return "ltg"
    return None


def inventory(path: Path, limit: int = 400) -> list[dict]:
    """Unique parameter/level combinations in a GRIB file (for debugging)."""
    seen: dict[tuple, dict] = {}
    with open(path, "rb") as f:
        while len(seen) < limit:
            h = ec.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                row = {k: _get(h, k) for k in ("shortName", "name", "typeOfLevel", "level", "stepType",
                                                 "units", "discipline", "parameterCategory", "parameterNumber",
                                                 "indicatorOfParameter", "perturbationNumber")}
                row = {k: v for k, v in row.items() if v is not None}
                row["used_as"] = classify(h)
                key = tuple(sorted((k, str(v)) for k, v in row.items()))
                seen.setdefault(key, row)
            finally:
                ec.codes_release(h)
    return list(seen.values())


@dataclass
class MsgRef:
    path: Path
    offset: int
    field: str
    run: datetime
    valid: datetime
    step_type: str
    units: str


def _dt(date: int, time: int) -> datetime:
    return datetime.strptime(f"{date:08d}{time:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def scan(paths: list[Path]) -> list[MsgRef]:
    refs: list[MsgRef] = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                while True:
                    h = ec.codes_grib_new_from_file(f)
                    if h is None:
                        break
                    try:
                        # DINI-EPS ships 1 control + 30 perturbed members; only the control is drawn.
                        member = _get(h, "perturbationNumber", _get(h, "number", 0)) or 0
                        fld = classify(h) if member == 0 else None
                        if fld:
                            refs.append(MsgRef(
                                path=p, offset=int(_get(h, "offset", 0)), field=fld,
                                run=_dt(_get(h, "dataDate"), _get(h, "dataTime")),
                                valid=_dt(_get(h, "validityDate"), _get(h, "validityTime")),
                                step_type=str(_get(h, "stepType", "instant")),
                                units=str(_get(h, "units", "")),
                            ))
                    finally:
                        ec.codes_release(h)
        except Exception as e:  # noqa: BLE001
            log.warning("could not scan %s: %s", p.name, e)
    return refs


def _read(ref: MsgRef):
    with open(ref.path, "rb") as f:
        f.seek(ref.offset)
        h = ec.codes_grib_new_from_file(f)
        try:
            ni, nj = _get(h, "Ni"), _get(h, "Nj")
            vals = ec.codes_get_values(h).astype(np.float32)
            missing = _get(h, "missingValue", 9999)
            if _get(h, "bitmapPresent", 0):
                vals[vals == missing] = np.nan
            geo = {
                "Ni": ni, "Nj": nj, "gridType": _get(h, "gridType"),
                "iScansNegatively": _get(h, "iScansNegatively", 0),
                "uvRelativeToGrid": _get(h, "uvRelativeToGrid", 0),
                "sig": (_get(h, "gridType"), ni, nj,
                        round(float(_get(h, "latitudeOfFirstGridPointInDegrees", 0)), 4),
                        round(float(_get(h, "longitudeOfFirstGridPointInDegrees", 0)), 4)),
            }
            return vals, geo, h
        except Exception:
            ec.codes_release(h)
            raise


LAYERS = {
    "rain": {"scale": RAIN},
    "temp": {"scale": TEMP},
    "wind": {"scale": WIND},
    "cloud": {"scale": CLOUD},
    "gust": {"scale": GUST},
    "vis": {"scale": VIS},
    "snow": {"scale": SNOW},
    "lightning": {"scale": LIGHTNING},
}


def pick_run(refs: list[MsgRef], max_hours: int) -> datetime:
    """DINI runs every hour and its files may still be arriving. Use the newest run
    that is (nearly) as long as the most complete one we have, so the map doesn't
    flip to a run with only a handful of hours in it."""
    hours: dict[datetime, set] = defaultdict(set)
    for r in refs:
        if r.field in ("t2", "tp") and r.valid <= r.run + timedelta(hours=max_hours):
            hours[r.run].add(r.valid)
    if not hours:
        return max(r.run for r in refs)
    best = max(len(v) for v in hours.values())
    return max(run for run, v in hours.items() if len(v) >= 0.9 * best)



# ---------------------------------------------------------------- pipeline

STORED = ("rain", "temp", "cloud", "wind", "u", "v", "msl", "gust", "vis", "snow", "lightning")


def _grid_keys(h) -> dict:
    keys = {}
    for k in ("Latin1InDegrees", "Latin2InDegrees", "LaDInDegrees", "LoVInDegrees", "radius",
              "orientationOfTheGridInDegrees"):
        v = _get(h, k)
        if v is not None:
            keys[k] = float(v)
    return keys


def _isobars(msl: np.ndarray, lats: np.ndarray, lons: np.ndarray, interval: int = 4) -> dict:
    """Contour MSLP on the model's own grid, then place the lines by the grid's lat/lon."""
    import contourpy

    feats = []
    finite = msl[np.isfinite(msl)]
    if finite.size == 0:
        return {"type": "FeatureCollection", "features": []}
    lo = int(math.floor(finite.min() / interval) * interval)
    hi = int(math.ceil(finite.max() / interval) * interval)
    gen = contourpy.contour_generator(z=np.ma.masked_invalid(msl), name="serial")
    for level in range(lo, hi + 1, interval):
        for line in gen.lines(level):
            if len(line) < 5:
                continue
            la = bilinear(lats, line[:, 1], line[:, 0])
            lo_ = bilinear(lons, line[:, 1], line[:, 0])
            ok = np.isfinite(la) & np.isfinite(lo_)
            if ok.sum() < 5:
                continue
            coords = np.round(np.column_stack([lo_[ok], la[ok]]), 4).tolist()
            feats.append({"type": "Feature", "properties": {"hpa": level},
                          "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats}


def process_run(refs: list[MsgRef], out_dir: Path, max_hours: int) -> dict:
    """Decode every hour of the newest run and store the fields on the model's own
    grid (float16 .npy), ready to be drawn as map tiles at any zoom."""
    if not refs:
        raise ValueError("no recognised NWP fields in the downloaded files")
    run = pick_run(refs, max_hours)
    refs = [r for r in refs if r.run == run and r.valid <= run + timedelta(hours=max_hours)]
    by_time: dict[datetime, dict[str, MsgRef]] = defaultdict(dict)
    for r in refs:
        by_time[r.valid][r.field] = r
    times = sorted(by_time)
    run_dir = out_dir / run.strftime("%Y%m%d%H")
    shutil.rmtree(run_dir / "tiles", ignore_errors=True)   # re-rendered from the new fields
    fdir = run_dir / "fields"
    fdir.mkdir(parents=True, exist_ok=True)

    grids: dict[tuple, dict] = {}        # signature -> {gid, sampler, lats, lons, alpha}
    layer_grid: dict[str, str] = {}
    prev_tp: tuple[datetime, np.ndarray] | None = None
    frames = []
    ranges: dict[str, list[float]] = {}
    for t in times:
        vals: dict[str, tuple[str, np.ndarray]] = {}
        uv: dict[str, np.ndarray] = {}
        uv_geo = None
        for name, ref in by_time[t].items():
            try:
                raw, geo, h = _read(ref)
            except Exception as e:  # noqa: BLE001
                log.warning("skip %s @ %s: %s", name, t, e)
                continue
            try:
                g = grids.get(geo["sig"])
                if g is None:
                    nj, ni = geo["Nj"], geo["Ni"]
                    lats = ec.codes_get_array(h, "latitudes").reshape(nj, ni)
                    lons = ec.codes_get_array(h, "longitudes").reshape(nj, ni)
                    lons = np.where(lons > 180, lons - 360, lons)
                    sampler = GridSampler.from_latlon(lats, lons, str(geo["gridType"]), _grid_keys(h))
                    gid = f"g{len(grids)}"
                    sampler.save(run_dir, gid)
                    dlat = np.gradient(lats, axis=1)
                    dlon = np.gradient(lons, axis=1) * np.cos(np.radians(lats))
                    g = grids[geo["sig"]] = {"gid": gid, "sampler": sampler, "lats": lats, "lons": lons,
                                             "alpha": np.arctan2(dlat, dlon)}
            finally:
                ec.codes_release(h)
            arr = raw.reshape(g["lats"].shape)
            gid = g["gid"]
            if name in ("u10", "v10"):
                uv[name] = arr
                uv_geo = (geo, g)
                continue
            if name == "tp":
                if ref.step_type == "accum" or ref.units in ("kg m**-2", "kg m-2", "mm"):
                    acc = arr
                    if prev_tp is not None and t > prev_tp[0]:
                        hours = (t - prev_tp[0]).total_seconds() / 3600
                        rate = np.clip(acc - prev_tp[1], 0, None) / hours
                    elif t == run:
                        rate = np.zeros_like(acc)
                    else:
                        rate = acc / max((t - run).total_seconds() / 3600, 1)
                    prev_tp = (t, acc)
                    arr = rate
                else:
                    arr = arr * 3600.0
                vals["rain"] = (gid, arr)
            elif name == "t2":
                vals["temp"] = (gid, arr - 273.15 if np.nanmean(arr) > 150 else arr)
            elif name == "msl":
                vals["msl"] = (gid, arr / 100.0 if np.nanmean(arr) > 2000 else arr)
            elif name == "tcc":
                vals["cloud"] = (gid, arr * 100.0 if np.nanmax(arr) <= 1.01 else arr)
            elif name in CLOUD_LAYERS:
                c = arr * 100.0 if np.nanmax(arr) <= 1.01 else arr
                prev = vals.get("_layers")
                vals["_layers"] = (gid, c if prev is None else np.fmax(prev[1], c))
            elif name == "gust":
                vals["gust"] = (gid, arr * 3.6)
            elif name in ("gu", "gv"):
                vals[name] = (gid, arr)
            elif name == "vis":
                vals["vis"] = (gid, arr / 1000.0 if np.nanmax(arr) > 200 else arr)
            elif name == "sd":
                vals["snow"] = (gid, arr * 1000.0 if ref.units.strip() == "m" else arr)
            elif name == "ltg":
                vals["lightning"] = (gid, arr)
        if "cloud" not in vals and "_layers" in vals:
            vals["cloud"] = vals["_layers"]
        vals.pop("_layers", None)
        if "gust" not in vals and "gu" in vals and "gv" in vals:
            vals["gust"] = (vals["gu"][0], np.hypot(vals["gu"][1], vals["gv"][1]) * 3.6)
        vals.pop("gu", None)
        vals.pop("gv", None)
        if "u10" in uv and "v10" in uv and uv_geo is not None:
            geo, g = uv_geo
            u, v = uv["u10"], uv["v10"]
            if geo["uvRelativeToGrid"]:
                a = g["alpha"] + (np.pi if geo["iScansNegatively"] else 0.0)
                ca, sa = np.cos(a), np.sin(a)
                u, v = u * ca - v * sa, u * sa + v * ca
            vals["u"], vals["v"] = (g["gid"], u), (g["gid"], v)
            vals["wind"] = (g["gid"], np.hypot(u, v) * 3.6)

        stamp = t.strftime("%Y%m%d%H%M")
        for key, (gid, arr) in vals.items():
            np.save(fdir / f"{key}_{stamp}.npy", arr.astype(np.float16))
            layer_grid[key] = gid
            if key in ("gust", "vis", "snow", "lightning") and np.isfinite(arr).any():
                lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
                r = ranges.setdefault(key, [lo, hi])
                r[0], r[1] = min(r[0], lo), max(r[1], hi)
        if "msl" in vals:
            g = next(g for g in grids.values() if g["gid"] == vals["msl"][0])
            (run_dir / f"msl_{stamp}.json").write_text(json.dumps(_isobars(vals["msl"][1], g["lats"], g["lons"])))
        layers = [k for k in ("rain", "temp", "cloud", "wind", "gust", "vis", "snow", "lightning", "msl") if k in vals]
        if layers:
            frames.append({"valid": t.isoformat(), "stamp": stamp, "layers": layers})

    bboxes = {g["gid"]: g["sampler"].bbox for g in grids.values()}
    union = [min(b[0] for b in bboxes.values()), min(b[1] for b in bboxes.values()),
             max(b[2] for b in bboxes.values()), max(b[3] for b in bboxes.values())] if bboxes else None
    index = {
        "run": run.isoformat(),
        "run_id": run.strftime("%Y%m%d%H"),
        "bbox": union,
        "grids": bboxes,
        "layer_grid": layer_grid,
        "frames": frames,
        "legends": {k: v["scale"].legend() for k, v in LAYERS.items()},
        "ranges": {k: [round(v[0], 4), round(v[1], 4)] for k, v in ranges.items()},
        "generated": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "index.json").write_text(json.dumps(index))
    return index


# ---------------------------------------------------------------- reading back

@lru_cache(maxsize=8)
def _grid(run_dir: str, gid: str) -> GridSampler:
    return GridSampler.load(Path(run_dir), gid)


@lru_cache(maxsize=96)
def _field(path: str) -> np.ndarray:
    return np.load(path).astype(np.float32)


def field(run_dir: Path, key: str, stamp: str) -> np.ndarray | None:
    f = run_dir / "fields" / f"{key}_{stamp}.npy"
    return _field(str(f)) if f.exists() else None


def render_tile(run_dir: Path, index: dict, layer: str, stamp: str, z: int, x: int, y: int) -> bytes:
    gid = index.get("layer_grid", {}).get(layer)
    if gid is None or layer not in LAYERS:
        return tiles.EMPTY
    g = _grid(str(run_dir), gid)
    if not tiles.intersects(z, x, y, g.bbox):
        return tiles.EMPTY
    lat, lon = tiles.tile_latlon(z, x, y)
    fj, fi = g.frac(lat, lon)
    arr = field(run_dir, layer, stamp)
    if arr is None:
        return tiles.EMPTY
    vals = bilinear(arr, fj, fi)
    rgba = LAYERS[layer]["scale"].colorize(vals)
    if layer == "wind":
        u, v = field(run_dir, "u", stamp), field(run_dir, "v", stamp)
        if u is not None and v is not None:
            rgba = tiles.draw_arrows(np.ascontiguousarray(rgba), vals, bilinear(u, fj, fi), bilinear(v, fj, fi))
    return tiles.encode(rgba)


def inspect(run_dir: Path, stamp: str, lat: float, lon: float) -> dict | None:
    idx_path = run_dir / "index.json"
    if not idx_path.exists():
        return None
    index = json.loads(idx_path.read_text())
    out = {}
    for key in ("rain", "temp", "cloud", "wind", "u", "v", "msl", "gust", "vis", "snow", "lightning"):
        gid = index.get("layer_grid", {}).get(key)
        arr = field(run_dir, key, stamp) if gid else None
        if arr is None:
            continue
        v = float(_grid(str(run_dir), gid).sample(arr, np.array([lat]), np.array([lon]))[0])
        out[key] = None if not math.isfinite(v) else round(v, 1)
    if not any(v is not None for v in out.values()):
        return None
    u, v = out.pop("u", None), out.pop("v", None)
    if u is not None and v is not None:
        out["wind_dir"] = round(math.degrees(math.atan2(-u, -v)) % 360)
    return out
