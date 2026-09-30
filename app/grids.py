"""Sampling model fields at arbitrary lat/lon with bilinear interpolation.

The map tiles are drawn straight from the model's own grid, so we need a fast
lat/lon -> fractional (row, col) mapping. For projected grids (Lambert, which
DINI uses, plus regular lat/lon, Mercator and polar stereographic) we project
with our own maths and fit an affine transform to the grid's lat/lon arrays;
this absorbs grid spacing, origin and scanning direction without having to trust
every GRIB key. If the fit isn't within a fraction of a cell we fall back to a
nearest-neighbour KD-tree, which works for any grid."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from . import proj
from .geo import to_xyz

log = logging.getLogger(__name__)


def bilinear(arr: np.ndarray, fj: np.ndarray, fi: np.ndarray) -> np.ndarray:
    """NaN-aware bilinear sampling of arr (nj, ni) at fractional indices."""
    nj, ni = arr.shape
    out = np.full(fj.shape, np.nan, dtype=np.float32)
    ok = np.isfinite(fj) & np.isfinite(fi) & (fj >= -0.5) & (fj <= nj - 0.5) & (fi >= -0.5) & (fi <= ni - 0.5)
    if not ok.any():
        return out
    y, x = np.clip(fj[ok], 0, nj - 1), np.clip(fi[ok], 0, ni - 1)
    j0 = np.minimum(np.floor(y).astype(np.int64), max(nj - 2, 0))
    i0 = np.minimum(np.floor(x).astype(np.int64), max(ni - 2, 0))
    j1, i1 = np.minimum(j0 + 1, nj - 1), np.minimum(i0 + 1, ni - 1)
    wy, wx = y - j0, x - i0
    acc = np.zeros(y.shape, dtype=np.float64)
    wsum = np.zeros(y.shape, dtype=np.float64)
    for jj, ii, w in ((j0, i0, (1 - wy) * (1 - wx)), (j0, i1, (1 - wy) * wx),
                      (j1, i0, wy * (1 - wx)), (j1, i1, wy * wx)):
        v = arr[jj, ii].astype(np.float64)
        good = np.isfinite(v)
        acc += np.where(good, v * w, 0.0)
        wsum += np.where(good, w, 0.0)
    res = np.where(wsum > 0.3, acc / np.maximum(wsum, 1e-9), np.nan)
    out[ok] = res
    return out


class GridSampler:
    def __init__(self, desc: dict, lats: np.ndarray | None = None, lons: np.ndarray | None = None):
        self.desc = desc
        self.shape = tuple(desc["shape"])
        self.kind = desc["kind"]
        self._fwd = proj.forward(desc["projdef"]) if self.kind == "affine" else None
        self._coef = np.array(desc.get("coef", []), dtype=float)
        self._tree = None
        self._lats, self._lons = lats, lons
        if self.kind == "kd":
            if lats is None or lons is None:
                raise ValueError("kd grid needs lat/lon arrays")
            self._tree = cKDTree(to_xyz(lats.ravel(), lons.ravel()))
            self._spacing = float(desc.get("spacing", 0.001))

    # -- construction ---------------------------------------------------------------
    @classmethod
    def from_latlon(cls, lats: np.ndarray, lons: np.ndarray, grid_type: str, keys: dict) -> "GridSampler":
        lons = np.where(lons > 180, lons - 360, lons)
        nj, ni = lats.shape
        candidates = []
        if grid_type == "lambert":
            R = keys.get("radius") or 6371229.0
            candidates.append(f"+proj=lcc +lat_1={keys['Latin1InDegrees']} +lat_2={keys['Latin2InDegrees']} "
                              f"+lat_0={keys.get('LaDInDegrees', keys['Latin1InDegrees'])} "
                              f"+lon_0={keys['LoVInDegrees']} +R={R}")
        elif grid_type in ("regular_ll", "regular_gg"):
            candidates.append("+proj=longlat")
        elif grid_type == "mercator":
            candidates.append(f"+proj=merc +lat_ts={keys.get('LaDInDegrees', 0)} +R={keys.get('radius') or 6371229.0}")
        elif grid_type == "polar_stereographic":
            candidates.append(f"+proj=stere +lat_0=90 +lat_ts={keys.get('LaDInDegrees', 60)} "
                              f"+lon_0={keys.get('orientationOfTheGridInDegrees', 0)} +R={keys.get('radius') or 6371229.0}")
        jj, ii = np.mgrid[0:nj, 0:ni]
        step = max(1, int(np.sqrt(lats.size / 4000)))
        sl = (slice(None, None, step), slice(None, None, step))
        for pdef in candidates:
            fwd = proj.forward(pdef)
            x, y = fwd(lats[sl].ravel(), lons[sl].ravel())
            A = np.c_[x, y, np.ones_like(x)]
            B = np.c_[jj[sl].ravel(), ii[sl].ravel()].astype(float)
            coef, *_ = np.linalg.lstsq(A, B, rcond=None)
            err = np.abs(A @ coef - B).max()
            if err < 0.25:
                log.info("grid %s %dx%d mapped by %s (max err %.3f cells)", grid_type, ni, nj, pdef.split()[0], err)
                return cls({"kind": "affine", "projdef": pdef, "coef": coef.tolist(), "shape": [nj, ni],
                            "bbox": _bbox(lats, lons)})
            log.warning("grid %s: %s fit off by %.2f cells, using nearest-neighbour", grid_type, pdef, err)
        sample = to_xyz(lats[sl].ravel(), lons[sl].ravel())
        d, _ = cKDTree(sample).query(sample, k=2)
        spacing = float(np.median(d[:, 1])) / step
        return cls({"kind": "kd", "shape": [nj, ni], "spacing": spacing, "bbox": _bbox(lats, lons)},
                   lats.astype(np.float32), lons.astype(np.float32))

    def save(self, folder: Path, gid: str) -> None:
        (folder / f"grid_{gid}.json").write_text(json.dumps(self.desc))
        if self.kind == "kd":
            np.save(folder / f"grid_{gid}_lats.npy", self._lats)
            np.save(folder / f"grid_{gid}_lons.npy", self._lons)

    @classmethod
    def load(cls, folder: Path, gid: str) -> "GridSampler":
        desc = json.loads((folder / f"grid_{gid}.json").read_text())
        if desc["kind"] == "kd":
            return cls(desc, np.load(folder / f"grid_{gid}_lats.npy"), np.load(folder / f"grid_{gid}_lons.npy"))
        return cls(desc)

    # -- use --------------------------------------------------------------------------
    @property
    def bbox(self) -> list[float]:
        return self.desc["bbox"]  # [west, south, east, north]

    def frac(self, lat: np.ndarray, lon: np.ndarray):
        if self.kind == "affine":
            x, y = self._fwd(lat, lon)
            c = self._coef
            return x * c[0, 0] + y * c[1, 0] + c[2, 0], x * c[0, 1] + y * c[1, 1] + c[2, 1]
        d, idx = self._tree.query(to_xyz(np.ravel(lat), np.ravel(lon)))
        nj, ni = self.shape
        fj, fi = (idx // ni).astype(float), (idx % ni).astype(float)
        far = d > self._spacing * 1.6
        fj[far] = np.nan
        fi[far] = np.nan
        return fj.reshape(np.shape(lat)), fi.reshape(np.shape(lat))

    def sample(self, arr: np.ndarray, lat, lon) -> np.ndarray:
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        fj, fi = self.frac(lat, lon)
        return bilinear(arr, fj, fi)

    def index_to_latlon(self, lats: np.ndarray, lons: np.ndarray, fj: np.ndarray, fi: np.ndarray):
        """Grid coordinates -> lat/lon, via the grid's own lat/lon arrays (for contours)."""
        return bilinear(lats, fj, fi), bilinear(lons, fj, fi)


def _bbox(lats, lons) -> list[float]:
    return [round(float(np.nanmin(lons)), 4), round(float(np.nanmin(lats)), 4),
            round(float(np.nanmax(lons)), 4), round(float(np.nanmax(lats)), 4)]
