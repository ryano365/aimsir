"""Just enough map projection maths to place ODIM cartesian radar products.

ODIM composites/images carry a PROJ string (where/projdef) plus corner
coordinates. We only need the forward projection (lat/lon -> x/y): each output
pixel is projected and looked up in the source grid, relative to the projected
upper-left corner, so constant offsets (x_0, y_0) cancel out. Spherical formulas
are used; over a few hundred km the error is well under a pixel.

Supported: aeqd, stere (polar and oblique), lcc, merc, longlat/latlong/eqc.
Anything else returns None and the caller falls back to corner interpolation."""
from __future__ import annotations

import numpy as np

R_DEFAULT = 6_371_000.0


def parse(projdef: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in projdef.replace("\x00", " ").split():
        tok = tok.lstrip("+")
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
        elif tok:
            out[tok] = ""
    return out


ELLPS = {"WGS84": (6378137.0, 298.257223563), "GRS80": (6378137.0, 298.257222101),
         "intl": (6378388.0, 297.0), "bessel": (6377397.155, 299.1528128)}


def _local_radius(p: dict, lat0_deg: float) -> float:
    """Sphere that best matches the ellipsoid near the projection centre
    (Gaussian mean radius), so small-area errors stay well under a radar pixel."""
    if "a" in p or "ellps" in p or "datum" in p:
        a, rf = ELLPS.get(p.get("ellps", "WGS84"), ELLPS["WGS84"])
        a = float(p.get("a", a))
        if "b" in p:
            b = float(p["b"])
        elif "rf" in p:
            b = a * (1 - 1 / float(p["rf"]))
        elif "a" in p and "ellps" not in p:
            b = a
        else:
            b = a * (1 - 1 / rf)
        e2 = 1 - (b / a) ** 2
        s2 = np.sin(np.radians(lat0_deg)) ** 2
        M = a * (1 - e2) / (1 - e2 * s2) ** 1.5
        N = a / np.sqrt(1 - e2 * s2)
        return float(np.sqrt(M * N))
    return R_DEFAULT


def forward(projdef: str):
    """Return f(lat_deg, lon_deg) -> (x, y) in projection units, or None."""
    p = parse(projdef)
    name = p.get("proj", "")
    f = lambda k, d=0.0: float(p.get(k, d))  # noqa: E731
    lat0_deg = f("lat_0")
    R = f("R", 0) or _local_radius(p, lat0_deg)
    units = {"km": 1000.0, "m": 1.0}.get(p.get("units", "m"), 1.0)
    lat0, lon0 = np.radians(f("lat_0")), np.radians(f("lon_0"))

    if name in ("longlat", "latlong", "lonlat", "latlon"):
        return lambda la, lo: (np.asarray(lo, float), np.asarray(la, float))

    if name == "eqc":
        k = np.cos(np.radians(f("lat_ts")))
        return lambda la, lo: (R * k * (np.radians(lo) - lon0) / units, R * (np.radians(la) - lat0) / units)

    if name == "merc":
        k = np.cos(np.radians(f("lat_ts"))) * f("k_0", 1.0) if "k_0" in p else np.cos(np.radians(f("lat_ts")))
        return lambda la, lo: (R * k * (np.radians(lo) - lon0) / units,
                               R * k * np.log(np.tan(np.pi / 4 + np.radians(la) / 2)) / units)

    if name == "aeqd":
        def aeqd(la, lo):
            phi, dl = np.radians(la), np.radians(lo) - lon0
            cosc = np.sin(lat0) * np.sin(phi) + np.cos(lat0) * np.cos(phi) * np.cos(dl)
            c = np.arccos(np.clip(cosc, -1, 1))
            k = np.where(c > 1e-12, c / np.maximum(np.sin(c), 1e-12), 1.0)
            x = R * k * np.cos(phi) * np.sin(dl)
            y = R * k * (np.cos(lat0) * np.sin(phi) - np.sin(lat0) * np.cos(phi) * np.cos(dl))
            return x / units, y / units
        return aeqd

    if name == "stere":
        if "k_0" in p or "k" in p:
            k0 = f("k_0", 0) or f("k", 1.0)
        elif "lat_ts" in p and abs(abs(np.degrees(lat0)) - 90) < 1e-6:
            k0 = (1 + np.sin(abs(np.radians(f("lat_ts"))))) / 2
        else:
            k0 = 1.0

        def stere(la, lo):
            phi, dl = np.radians(la), np.radians(lo) - lon0
            k = 2 * k0 / (1 + np.sin(lat0) * np.sin(phi) + np.cos(lat0) * np.cos(phi) * np.cos(dl))
            x = R * k * np.cos(phi) * np.sin(dl)
            y = R * k * (np.cos(lat0) * np.sin(phi) - np.sin(lat0) * np.cos(phi) * np.cos(dl))
            return x / units, y / units
        return stere

    if name == "lcc":
        p1 = np.radians(f("lat_1", np.degrees(lat0)))
        p2 = np.radians(f("lat_2", np.degrees(p1)))
        if abs(p1 - p2) < 1e-9:
            n = np.sin(p1)
        else:
            n = np.log(np.cos(p1) / np.cos(p2)) / np.log(np.tan(np.pi / 4 + p2 / 2) / np.tan(np.pi / 4 + p1 / 2))
        F = np.cos(p1) * np.tan(np.pi / 4 + p1 / 2) ** n / n
        rho0 = R * F / np.tan(np.pi / 4 + lat0 / 2) ** n

        def lcc(la, lo):
            phi, dl = np.radians(la), np.radians(lo) - lon0
            rho = R * F / np.tan(np.pi / 4 + phi / 2) ** n
            return rho * np.sin(n * dl) / units, (rho0 - rho * np.cos(n * dl)) / units
        return lcc

    return None
