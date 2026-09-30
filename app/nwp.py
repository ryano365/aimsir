"""Decode HARMONIE-AROME GRIB output and render map layers.

The near-realtime feed gives us GRIB files (edition 1 or 2 - both handled)
whose exact split (per lead time, per parameter...) we don't depend on: every
file is scanned, messages we care about are recognised by their keys, and the
newest model run is rendered hour by hour."""
from __future__ import annotations

import io
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree

import eccodes as ec

from .geo import NWP_GRID, MercGrid, to_xyz
from .palettes import CLOUD, RAIN, TEMP, WIND

log = logging.getLogger(__name__)

FIELDS = ("t2", "u10", "v10", "msl", "tp", "tcc")
CLOUD_LAYERS = ("lcc", "mcc", "hcc")  # used to build total cloud when tcc is absent


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


class Regridder:
    """Nearest-neighbour lookup from any GRIB grid onto the Mercator grid."""

    def __init__(self, h, grid: MercGrid):
        lats = ec.codes_get_array(h, "latitudes")
        lons = ec.codes_get_array(h, "longitudes")
        lons = np.where(lons > 180, lons - 360, lons)
        self.ni, self.nj = _get(h, "Ni"), _get(h, "Nj")
        tree = cKDTree(to_xyz(lats, lons))
        # typical spacing, to blank pixels outside the model domain
        sample = to_xyz(lats[:: max(1, lats.size // 2000)], lons[:: max(1, lats.size // 2000)])
        d, _ = tree.query(sample, k=2)
        spacing = float(np.median(d[:, 1]))
        lat2d, lon2d = grid.mesh
        dist, idx = tree.query(to_xyz(lat2d.ravel(), lon2d.ravel()))
        self.idx = idx.reshape(lat2d.shape)
        self.inside = (dist < spacing * 1.6).reshape(lat2d.shape)
        self.grid = grid
        # Grid x-axis angle vs east, for rotating grid-relative winds.
        self.alpha = None
        if self.ni and self.nj and self.ni * self.nj == lats.size:
            la2, lo2 = lats.reshape(self.nj, self.ni), lons.reshape(self.nj, self.ni)
            dlat = np.gradient(la2, axis=1)
            dlon = np.gradient(lo2, axis=1) * np.cos(np.radians(la2))
            self.alpha = np.arctan2(dlat, dlon).ravel().astype(np.float32)

    def __call__(self, values: np.ndarray) -> np.ndarray:
        out = values[self.idx]
        out[~self.inside] = np.nan
        return out

    def earth_winds(self, u, v, relative: bool, i_neg: bool):
        if not relative or self.alpha is None:
            return u, v
        a = self.alpha + (np.pi if i_neg else 0.0)
        ca, sa = np.cos(a), np.sin(a)
        return u * ca - v * sa, u * sa + v * ca


# ---------------------------------------------------------------- rendering

def _png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def wind_png(speed_kmh: np.ndarray, u: np.ndarray, v: np.ndarray, spacing: int = 34) -> bytes:
    img = Image.fromarray(WIND.colorize(speed_kmh), "RGBA")
    draw = ImageDraw.Draw(img)
    h, w = speed_kmh.shape
    for r in range(spacing // 2, h, spacing):
        for c in range(spacing // 2, w, spacing):
            s = speed_kmh[r, c]
            if not np.isfinite(s) or s < 3:
                continue
            # screen angle of the direction the wind blows towards (image y points down)
            th = math.atan2(-v[r, c], u[r, c])
            length = 7 + min(s, 90) / 90 * 11
            dx, dy = math.cos(th) * length / 2, math.sin(th) * length / 2
            x0, y0, x1, y1 = c - dx, r - dy, c + dx, r + dy
            col = (38, 44, 50, 200)
            draw.line([(x0, y0), (x1, y1)], fill=col, width=1)
            for side in (0.5, -0.5):
                draw.line([(x1, y1), (x1 - math.cos(th + side) * 4.5, y1 - math.sin(th + side) * 4.5)],
                          fill=col, width=1)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def isobars(msl_hpa: np.ndarray, grid: MercGrid, interval: int = 4) -> dict:
    import contourpy

    feats = []
    finite = msl_hpa[np.isfinite(msl_hpa)]
    if finite.size == 0:
        return {"type": "FeatureCollection", "features": []}
    lo = int(math.floor(finite.min() / interval) * interval)
    hi = int(math.ceil(finite.max() / interval) * interval)
    z = np.ma.masked_invalid(msl_hpa)
    gen = contourpy.contour_generator(z=z, name="serial")
    for level in range(lo, hi + 1, interval):
        for line in gen.lines(level):
            if len(line) < 6:
                continue
            line = line[::2] if len(line) > 40 else line
            lon, lat = grid.pixel_to_lonlat(line[:, 0], line[:, 1])
            coords = np.round(np.column_stack([lon, lat]), 3).tolist()
            feats.append({"type": "Feature", "properties": {"hpa": level},
                          "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats}


# ---------------------------------------------------------------- pipeline

LAYERS = {
    "rain": {"scale": RAIN},
    "temp": {"scale": TEMP},
    "wind": {"scale": WIND},
    "cloud": {"scale": CLOUD},
}
INSPECT_STEP = 3  # decimation for the click-to-inspect value store


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


def process_run(refs: list[MsgRef], out_dir: Path, max_hours: int, grid: MercGrid = NWP_GRID) -> dict:
    """Render every hour of the newest run found in refs. Returns the index."""
    if not refs:
        raise ValueError("no recognised NWP fields in the downloaded files")
    run = pick_run(refs, max_hours)
    refs = [r for r in refs if r.run == run and r.valid <= run + timedelta(hours=max_hours)]
    by_time: dict[datetime, dict[str, MsgRef]] = defaultdict(dict)
    for r in refs:
        by_time[r.valid][r.field] = r
    times = sorted(by_time)
    run_dir = out_dir / run.strftime("%Y%m%d%H")
    run_dir.mkdir(parents=True, exist_ok=True)

    regridders: dict[tuple, Regridder] = {}
    prev_tp: tuple[datetime, np.ndarray] | None = None
    frames = []
    for t in times:
        fields = by_time[t]
        vals: dict[str, np.ndarray] = {}
        geo_uv = None
        for name, ref in fields.items():
            try:
                raw, geo, h = _read(ref)
            except Exception as e:  # noqa: BLE001
                log.warning("skip %s @ %s: %s", name, t, e)
                continue
            try:
                rg = regridders.get(geo["sig"])
                if rg is None:
                    rg = regridders[geo["sig"]] = Regridder(h, grid)
            finally:
                ec.codes_release(h)
            if name in ("u10", "v10"):
                vals[name + "_raw"] = raw
                geo_uv = geo
                continue
            if name == "tp":
                if ref.step_type == "accum" or ref.units in ("kg m**-2", "kg m-2", "mm"):
                    acc = raw
                    if prev_tp is not None and t > prev_tp[0]:
                        hours = (t - prev_tp[0]).total_seconds() / 3600
                        rate = np.clip(acc - prev_tp[1], 0, None) / hours
                    elif t == run:
                        rate = np.zeros_like(acc)
                    else:  # first accum field after start: average since run start
                        rate = acc / max((t - run).total_seconds() / 3600, 1)
                    prev_tp = (t, acc)
                    raw = rate
                else:  # rate in kg m-2 s-1
                    raw = raw * 3600.0
                vals["rain"] = rg(raw)
            elif name == "t2":
                vals["temp"] = rg(raw - 273.15 if np.nanmean(raw) > 150 else raw)
            elif name == "msl":
                vals["msl"] = rg(raw / 100.0 if np.nanmean(raw) > 2000 else raw)
            elif name == "tcc":
                vals["cloud"] = rg(raw * 100.0 if np.nanmax(raw) <= 1.01 else raw)
            elif name in CLOUD_LAYERS:
                c = rg(raw * 100.0 if np.nanmax(raw) <= 1.01 else raw)
                vals["_layers"] = c if "_layers" not in vals else np.fmax(vals["_layers"], c)
        if "cloud" not in vals and "_layers" in vals:
            vals["cloud"] = vals["_layers"]  # maximum-overlap estimate of total cloud
        vals.pop("_layers", None)
        if "u10_raw" in vals and "v10_raw" in vals and geo_uv is not None:
            rg = regridders[geo_uv["sig"]]
            u, v = rg.earth_winds(vals.pop("u10_raw"), vals.pop("v10_raw"),
                                  bool(geo_uv["uvRelativeToGrid"]), bool(geo_uv["iScansNegatively"]))
            u, v = rg(u), rg(v)
            vals["wind"] = np.hypot(u, v) * 3.6
            vals["wind_dir"] = np.mod(np.degrees(np.arctan2(-u, -v)), 360)  # meteorological "from"
            vals["_u"], vals["_v"] = u, v
        vals.pop("u10_raw", None)
        vals.pop("v10_raw", None)

        stamp = t.strftime("%Y%m%d%H%M")
        layers = []
        for key in ("rain", "temp", "cloud"):
            if key in vals:
                (run_dir / f"{key}_{stamp}.png").write_bytes(_png(LAYERS[key]["scale"].colorize(vals[key])))
                layers.append(key)
        if "wind" in vals:
            (run_dir / f"wind_{stamp}.png").write_bytes(wind_png(vals["wind"], vals["_u"], vals["_v"]))
            layers.append("wind")
        if "msl" in vals:
            (run_dir / f"msl_{stamp}.json").write_text(json.dumps(isobars(vals["msl"], grid)))
            layers.append("msl")
        store = {k: vals[k][::INSPECT_STEP, ::INSPECT_STEP].astype(np.float16)
                 for k in ("rain", "temp", "cloud", "wind", "wind_dir", "msl") if k in vals}
        if store:
            np.savez_compressed(run_dir / f"values_{stamp}.npz", **store)
        if layers:
            frames.append({"valid": t.isoformat(), "stamp": stamp, "layers": layers})

    index = {
        "run": run.isoformat(),
        "run_id": run.strftime("%Y%m%d%H"),
        "bounds": grid.bounds,
        "frames": frames,
        "legends": {k: v["scale"].legend() for k, v in LAYERS.items()},
        "generated": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "index.json").write_text(json.dumps(index))
    return index


def inspect(run_dir: Path, stamp: str, lat: float, lon: float, grid: MercGrid = NWP_GRID) -> dict | None:
    f = run_dir / f"values_{stamp}.npz"
    px = grid.lonlat_to_pixel(lon, lat)
    if not f.exists() or px is None:
        return None
    c, r = px[0] // INSPECT_STEP, px[1] // INSPECT_STEP
    out = {}
    with np.load(f) as z:
        for k in z.files:
            a = z[k]
            if r < a.shape[0] and c < a.shape[1]:
                val = float(a[r, c])
                out[k] = None if not math.isfinite(val) else round(val, 1)
    return out
