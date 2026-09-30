"""Decode Met Éireann ODIM-HDF5 radar volumes and render composite frames.

Met publishes polar volume scans (object=PVOL) from Dublin and Shannon every
5 minutes. For a rain map we take the lowest elevation sweep of reflectivity
from each site, resample both onto the Mercator output grid, keep the
stronger echo where they overlap and convert dBZ to rain rate."""
from __future__ import annotations

import io
import math
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

from .geo import RADAR_GRID, MercGrid, great_circle
from .palettes import RAIN, dbz_to_rate

log = logging.getLogger(__name__)

TS_RE = re.compile(r"(\d{14})")
REFLECTIVITY = ("DBZH", "TH", "DBZ", "DBZV", "TV")


@dataclass
class Sweep:
    source: str
    lat: float
    lon: float
    time: datetime
    elangle: float
    rscale: float      # m per bin
    rstart: float      # m to start of first bin
    dbz: np.ndarray    # (nrays, nbins), NaN = no data

    @property
    def nrays(self) -> int:
        return self.dbz.shape[0]

    @property
    def nbins(self) -> int:
        return self.dbz.shape[1]


def _attr(group, name, default=None):
    if group is None or name not in group.attrs:
        return default
    v = group.attrs[name]
    if isinstance(v, bytes):
        return v.decode("ascii", "replace")
    if isinstance(v, np.ndarray) and v.size == 1:
        v = v.item()
        if isinstance(v, bytes):
            return v.decode("ascii", "replace")
    return v


def _odim_time(date, time) -> datetime | None:
    try:
        return datetime.strptime(f"{date}{str(time)[:6]}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def file_time(name: str) -> datetime | None:
    m = TS_RE.search(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def read_sweep(src: str | Path | bytes) -> Sweep:
    """Lowest-elevation reflectivity sweep from an ODIM PVOL/SCAN file."""
    fh = h5py.File(io.BytesIO(src) if isinstance(src, bytes) else src, "r")
    with fh as f:
        what, where = f.get("what"), f.get("where")
        obj = str(_attr(what, "object", "PVOL")).upper()
        if obj not in ("PVOL", "SCAN"):
            raise ValueError(f"unsupported ODIM object {obj!r} (only polar volumes/scans are rendered)")
        lat, lon = float(_attr(where, "lat")), float(_attr(where, "lon"))
        source = str(_attr(what, "source", ""))
        root_time = _odim_time(_attr(what, "date"), _attr(what, "time"))

        best = None  # (elangle, dataset, data group, quantity rank)
        for ds_name in sorted(k for k in f.keys() if k.startswith("dataset")):
            ds = f[ds_name]
            ds_what, ds_where = ds.get("what"), ds.get("where")
            el = _attr(ds_where, "elangle")
            if el is None:
                continue
            for d_name in sorted(k for k in ds.keys() if k.startswith("data")):
                d = ds[d_name]
                q = str(_attr(d.get("what"), "quantity", _attr(ds_what, "quantity", ""))).upper()
                if q not in REFLECTIVITY or "data" not in d:
                    continue
                rank = REFLECTIVITY.index(q)
                key = (float(el), rank)
                if best is None or key < (best[0], best[3]):
                    best = (float(el), ds, d, rank)
        if best is None:
            raise ValueError("no reflectivity quantity found in file")

        el, ds, d, _ = best
        ds_what, ds_where, d_what = ds.get("what"), ds.get("where"), d.get("what")

        def pick(name, default):
            v = _attr(d_what, name)
            if v is None:
                v = _attr(ds_what, name, default)
            return v

        gain, offset = float(pick("gain", 1.0)), float(pick("offset", 0.0))
        nodata, undetect = pick("nodata", None), pick("undetect", None)
        raw = d["data"][()]
        dbz = raw.astype(np.float32) * gain + offset
        if nodata is not None:
            dbz[raw == nodata] = np.nan
        if undetect is not None:
            dbz[raw == undetect] = -32.0   # scanned, no echo (keeps edges crisp when interpolating)

        rscale = float(_attr(ds_where, "rscale", 1000.0))
        rstart = float(_attr(ds_where, "rstart", 0.0)) * 1000.0  # km -> m
        t = _odim_time(_attr(ds_what, "startdate"), _attr(ds_what, "starttime")) or root_time
        return Sweep(source=source, lat=lat, lon=lon, time=t or datetime.now(timezone.utc),
                     elangle=el, rscale=rscale, rstart=rstart, dbz=dbz)


@lru_cache(maxsize=8)
def _polar_lookup(lat: float, lon: float, nrays: int, nbins: int, rscale: float, rstart: float,
                  grid: MercGrid = RADAR_GRID):
    """For each output pixel: (ray index, bin index, inside-range mask)."""
    lat2d, lon2d = grid.mesh
    dist, brg = great_circle(lat, lon, lat2d, lon2d)
    ray = np.floor(brg / 360.0 * nrays).astype(np.int32) % nrays
    b = np.floor((dist - rstart) / rscale).astype(np.int32)
    inside = (b >= 0) & (b < nbins)
    b = np.clip(b, 0, nbins - 1)
    return ray, b, inside


def resample(sweep: Sweep, grid: MercGrid = RADAR_GRID) -> np.ndarray:
    ray, b, inside = _polar_lookup(round(sweep.lat, 5), round(sweep.lon, 5), sweep.nrays, sweep.nbins,
                                   sweep.rscale, sweep.rstart, grid)
    out = sweep.dbz[ray, b]
    out[~inside] = np.nan
    return out


def composite(sweeps: list[Sweep], grid: MercGrid = RADAR_GRID) -> np.ndarray:
    out = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    for s in sweeps:
        out = np.fmax(out, resample(s, grid))
    return out


def coverage_mask(sweeps: list[Sweep], grid: MercGrid = RADAR_GRID) -> np.ndarray:
    """True where at least one radar can see (used to shade 'no coverage')."""
    m = np.zeros((grid.height, grid.width), dtype=bool)
    for s in sweeps:
        m |= _polar_lookup(round(s.lat, 5), round(s.lon, 5), s.nrays, s.nbins, s.rscale, s.rstart, grid)[2]
    return m


def render_png(dbz: np.ndarray, min_dbz: float) -> bytes:
    d = np.where(dbz >= min_dbz, dbz, np.nan)
    rgba = RAIN.colorize(dbz_to_rate(d))
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_coverage_png(mask: np.ndarray) -> bytes:
    """Faint hatch outside radar range so 'no rain' and 'no data' differ."""
    h, w = mask.shape
    yy, xx = np.mgrid[0:h, 0:w]
    hatch = ((xx + yy) % 9 == 0)
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    out = ~mask
    rgba[out] = (60, 64, 70, 28)
    rgba[out & hatch] = (60, 64, 70, 70)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def site_meta(s: Sweep) -> dict:
    return {"lat": s.lat, "lon": s.lon, "rscale": s.rscale, "rstart": s.rstart,
            "nrays": s.nrays, "nbins": s.nbins, "source": s.source}


def sample_polar(dbz: np.ndarray, m: dict, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Bilinear sample of a polar sweep (rays x bins) at lat/lon; NaN outside range."""
    nrays, nbins = dbz.shape
    dist, brg = great_circle(m["lat"], m["lon"], lat, lon)
    fr = brg / 360.0 * nrays - 0.5
    fb = (dist - m["rstart"]) / m["rscale"] - 0.5
    inside = (dist >= m["rstart"]) & (dist < m["rstart"] + nbins * m["rscale"])
    r0 = np.floor(fr).astype(np.int64)
    wr = fr - r0
    r0 %= nrays
    r1 = (r0 + 1) % nrays
    fbc = np.clip(fb, 0, nbins - 1)
    b0 = np.minimum(np.floor(fbc).astype(np.int64), nbins - 2)
    wb = fbc - b0
    b1 = b0 + 1
    acc = np.zeros(lat.shape)
    wsum = np.zeros(lat.shape)
    for rr, bb, w in ((r0, b0, (1 - wr) * (1 - wb)), (r1, b0, wr * (1 - wb)),
                      (r0, b1, (1 - wr) * wb), (r1, b1, wr * wb)):
        v = dbz[rr, bb].astype(np.float64)
        good = np.isfinite(v)
        acc += np.where(good, v * w, 0)
        wsum += np.where(good, w, 0)
    out = np.where((wsum > 0.3) & inside, acc / np.maximum(wsum, 1e-9), np.nan)
    return out.astype(np.float32)


def in_range(m: dict, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    dist, _ = great_circle(m["lat"], m["lon"], lat, lon)
    return (dist >= m["rstart"]) & (dist < m["rstart"] + m["nbins"] * m["rscale"])


def site_bbox(m: dict) -> list[float]:
    r_deg = (m["rstart"] + m["nbins"] * m["rscale"]) / 111_000
    return [m["lon"] - r_deg / math.cos(math.radians(m["lat"])), m["lat"] - r_deg,
            m["lon"] + r_deg / math.cos(math.radians(m["lat"])), m["lat"] + r_deg]


def slot_of(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


# ---------------------------------------------------------------- cartesian products (hourly accumulation)

@dataclass
class Cartesian:
    time: datetime
    values: np.ndarray       # (ysize, xsize), NaN = no data
    quantity: str
    projdef: str
    corners: dict            # UL/UR/LL/LR lat/lon
    xscale: float
    yscale: float


def read_cartesian(src: str | Path | bytes) -> Cartesian:
    """ODIM COMP/IMAGE product, e.g. Met's T_PASH21 hourly accumulation composite."""
    fh = h5py.File(io.BytesIO(src) if isinstance(src, bytes) else src, "r")
    with fh as f:
        what, where = f.get("what"), f.get("where")
        obj = str(_attr(what, "object", "")).upper()
        if obj in ("PVOL", "SCAN"):
            raise ValueError("polar product, not cartesian")
        ds_name = sorted(k for k in f.keys() if k.startswith("dataset"))[0]
        ds = f[ds_name]
        ds_what, ds_where = ds.get("what"), ds.get("where")
        where = ds_where if ds_where is not None and "xsize" in ds_where.attrs else where
        d_name = sorted(k for k in ds.keys() if k.startswith("data"))[0]
        d = ds[d_name]
        d_what = d.get("what")

        def pick(name, default=None):
            v = _attr(d_what, name)
            return _attr(ds_what, name, default) if v is None else v

        raw = d["data"][()]
        gain, offset = float(pick("gain", 1.0)), float(pick("offset", 0.0))
        vals = raw.astype(np.float32) * gain + offset
        nodata, undetect = pick("nodata"), pick("undetect")
        if nodata is not None:
            vals[raw == nodata] = np.nan
        if undetect is not None:
            vals[raw == undetect] = 0.0   # "measured, nothing there" -> zero rain
        corners = {k: float(_attr(where, k)) for k in
                   ("UL_lat", "UL_lon", "UR_lat", "UR_lon", "LL_lat", "LL_lon", "LR_lat", "LR_lon")
                   if _attr(where, k) is not None}
        t = (_odim_time(_attr(ds_what, "enddate"), _attr(ds_what, "endtime"))
             or _odim_time(_attr(what, "date"), _attr(what, "time")) or datetime.now(timezone.utc))
        return Cartesian(time=t, values=vals, quantity=str(pick("quantity", "")).upper(),
                         projdef=str(_attr(where, "projdef", "")), corners=corners,
                         xscale=float(_attr(where, "xscale", 1.0)), yscale=float(_attr(where, "yscale", 1.0)))


def cart_mapping(c: Cartesian) -> dict:
    """How to turn lat/lon into fractional (row, col) in a cartesian product."""
    cn = c.corners
    h, w = c.values.shape
    from .proj import forward

    fwd = forward(c.projdef) if c.projdef else None
    if fwd is not None and all(f"{k}_lat" in cn for k in ("UL", "UR", "LL", "LR")):
        # Fit projected corner positions to pixel edges. This absorbs the small
        # sphere-vs-ellipsoid error of our projection maths (~0.1 px vs pyproj).
        src = np.array([fwd(cn[f"{k}_lat"], cn[f"{k}_lon"]) for k in ("UL", "UR", "LL", "LR")], dtype=float)
        dst = np.array([[0, 0], [0, w], [h, 0], [h, w]], dtype=float)  # (row, col) of pixel edges
        coef, *_ = np.linalg.lstsq(np.c_[src, np.ones(4)], dst, rcond=None)
        return {"kind": "affine", "projdef": c.projdef, "coef": coef.tolist(), "shape": [h, w]}
    log.warning("radar product projection %r not supported, using corner interpolation", c.projdef)
    return {"kind": "corners", "corners": cn, "shape": [h, w]}


def cart_frac(m: dict, lat, lon):
    """Fractional (row, col) at pixel centres convention (0 = centre of first pixel)."""
    if m["kind"] == "affine":
        from .proj import forward

        x, y = forward(m["projdef"])(lat, lon)
        c = np.array(m["coef"])
        row = x * c[0, 0] + y * c[1, 0] + c[2, 0]
        col = x * c[0, 1] + y * c[1, 1] + c[2, 1]
    else:
        cn, (h, w) = m["corners"], m["shape"]
        col = (lon - cn["UL_lon"]) / (cn["UR_lon"] - cn["UL_lon"]) * w
        row = (cn["UL_lat"] - lat) / (cn["UL_lat"] - cn["LL_lat"]) * h
    return row - 0.5, col - 0.5


def cart_bbox(m: dict, corners: dict) -> list[float]:
    lats = [corners[f"{k}_lat"] for k in ("UL", "UR", "LL", "LR") if f"{k}_lat" in corners]
    lons = [corners[f"{k}_lon"] for k in ("UL", "UR", "LL", "LR") if f"{k}_lon" in corners]
    return [min(lons) - 0.5, min(lats) - 0.3, max(lons) + 0.5, max(lats) + 0.3]


def resample_cartesian(c: Cartesian, grid: MercGrid = RADAR_GRID) -> np.ndarray:
    from .grids import bilinear

    lat2d, lon2d = grid.mesh
    row, col = cart_frac(cart_mapping(c), lat2d, lon2d)
    out = bilinear(c.values, row, col)
    if c.quantity in REFLECTIVITY:
        out = dbz_to_rate(out)
    return out


def render_accum_png(mm: np.ndarray) -> bytes:
    from .palettes import RAIN_ACC

    buf = io.BytesIO()
    Image.fromarray(RAIN_ACC.colorize(mm), "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def odim_summary(path: Path) -> dict:
    """Attributes of an ODIM file (debugging)."""
    out: dict = {}
    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if len(out) > 60:
                return
            attrs = {k: (v.decode() if isinstance(v, bytes) else (v.tolist() if hasattr(v, "tolist") else v))
                     for k, v in obj.attrs.items()}
            entry = attrs
            if isinstance(obj, h5py.Dataset):
                entry = {"shape": list(obj.shape), "dtype": str(obj.dtype), **attrs}
            if entry:
                out[name] = entry
        for k in ("what", "where", "how"):
            if k in f:
                visit(k, f[k])
        f.visititems(visit)
    return out
