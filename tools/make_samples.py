"""Generate synthetic Met Éireann-style files for testing without an API key.

    python -m tools.make_samples [--inbox ./data/inbox]

Writes ODIM-HDF5 polar volumes for the Dublin and Shannon radars (last 2 h,
every 5 min) and a HARMONIE-like GRIB1 run on a Lambert conformal grid with
grid-relative winds - the awkward cases the decoders need to get right.
The weather is made up: a low tracking north-east with a frontal rain band."""
from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import numpy as np

SITES = {  # code, lat, lon
    "41": ("Dublin", 53.4284, -6.2435),
    "40": ("Shannon", 52.7020, -8.9250),
}


def rain_field(lat, lon, t_hours):
    """mm/h: a frontal band sweeping east plus a few showers."""
    front_lon = -11.5 + 1.3 * t_hours + 0.9 * (lat - 53.5)
    d = lon - front_lon
    band = 6.0 * np.exp(-(d / 0.45) ** 2) * (0.6 + 0.4 * np.sin(lat * 7.0 + t_hours))
    showers = np.zeros_like(lat)
    for i, (la, lo) in enumerate([(54.6, -9.8), (52.1, -9.7), (53.9, -8.2), (51.9, -8.4), (54.3, -7.1)]):
        lo2 = lo + 0.5 * t_hours
        showers += (3 + 9 * (i % 2)) * np.exp(-(((lat - la) / 0.12) ** 2 + ((lon - lo2) / 0.2) ** 2))
    return band * (d > -1.2) + showers * (d < -0.4)


def rate_to_dbz(r):
    with np.errstate(divide="ignore"):
        return 10 * np.log10(200 * np.power(np.maximum(r, 1e-6), 1.6))


def write_pvol(path: Path, site_lat, site_lon, t: datetime, t_hours: float):
    nrays, nbins, rscale = 360, 480, 500.0
    az = np.radians((np.arange(nrays) + 0.5))[:, None]
    rng = (np.arange(nbins) + 0.5) * rscale
    ang = rng[None, :] / 6371000.0
    p0, l0 = math.radians(site_lat), math.radians(site_lon)
    lat = np.degrees(np.arcsin(np.sin(p0) * np.cos(ang) + np.cos(p0) * np.sin(ang) * np.cos(az)))
    lon = np.degrees(l0 + np.arctan2(np.sin(az) * np.sin(ang) * np.cos(p0),
                                     np.cos(ang) - np.sin(p0) * np.sin(np.radians(lat))))
    dbz = rate_to_dbz(rain_field(lat, lon, t_hours))
    dbz += np.random.default_rng(int(t.timestamp()) + int(site_lon * 100)).normal(0, 1.2, dbz.shape)
    gain, offset = 0.5, -32.0
    raw = np.clip(np.round((dbz - offset) / gain), 0, 254).astype(np.uint8)
    raw[dbz < -10] = 0  # undetect
    with h5py.File(path, "w") as f:
        f.attrs["Conventions"] = np.bytes_("ODIM_H5/V2_2")
        w = f.create_group("what")
        w.attrs.update({"object": np.bytes_("PVOL"), "version": np.bytes_("H5rad 2.2"),
                        "date": np.bytes_(t.strftime("%Y%m%d")), "time": np.bytes_(t.strftime("%H%M%S")),
                        "source": np.bytes_("NOD:iesyn,PLC:synthetic")})
        g = f.create_group("where")
        g.attrs.update({"lat": site_lat, "lon": site_lon, "height": 60.0})
        for i, el in enumerate([1.5, 0.5, 3.0], start=1):  # deliberately unordered
            ds = f.create_group(f"dataset{i}")
            dw = ds.create_group("where")
            dw.attrs.update({"elangle": el, "nbins": nbins, "nrays": nrays, "rscale": rscale,
                             "rstart": 0.0, "a1gate": 0})
            dwh = ds.create_group("what")
            dwh.attrs.update({"product": np.bytes_("SCAN"), "startdate": np.bytes_(t.strftime("%Y%m%d")),
                              "starttime": np.bytes_(t.strftime("%H%M%S"))})
            for j, (q, arr) in enumerate([("TH", raw), ("DBZH", raw if el == 0.5 else raw // 2)], start=1):
                d = ds.create_group(f"data{j}")
                d.create_dataset("data", data=arr, compression="gzip")
                d.create_group("what").attrs.update({"quantity": np.bytes_(q), "gain": gain, "offset": offset,
                                                     "nodata": 255.0, "undetect": 0.0})


def make_radar(inbox: Path, now: datetime):
    end = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    for k in range(24):
        t = end - timedelta(minutes=5 * (23 - k))
        th = k * 5 / 60
        for code, (_, la, lo) in SITES.items():
            write_pvol(inbox / f"T_PAGZ{code}_C_EIDB_{t:%Y%m%d%H%M}00.h5", la, lo, t, th)


def make_grib(inbox: Path, now: datetime, hours: int = 36):
    import eccodes as ec

    run = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=(now.hour % 3) + 3)
    nx, ny, dx = 300, 320, 5000.0
    # Build lats/lons of a Lambert grid by asking eccodes once.
    h0 = ec.codes_grib_new_from_samples("GRIB1")
    ec.codes_set_key_vals(h0, {
        "gridType": "lambert", "Nx": nx, "Ny": ny, "DxInMetres": dx, "DyInMetres": dx,
        "latitudeOfFirstGridPointInDegrees": 47.2, "longitudeOfFirstGridPointInDegrees": -17.0,
        "LoVInDegrees": 5.0, "Latin1InDegrees": 53.5, "Latin2InDegrees": 53.5,
        "uvRelativeToGrid": 1, "table2Version": 253, "centre": 233,
    })
    ec.codes_set_values(h0, np.zeros(nx * ny))
    lats = ec.codes_get_array(h0, "latitudes").reshape(ny, nx)
    lons = ec.codes_get_array(h0, "longitudes").reshape(ny, nx)
    lons = np.where(lons > 180, lons - 360, lons)
    # grid x-axis angle vs east (for writing grid-relative winds)
    alpha = np.arctan2(np.gradient(lats, axis=1), np.gradient(lons, axis=1) * np.cos(np.radians(lats)))

    out = open(inbox / f"harmonie_synthetic_{run:%Y%m%d%H}.grb", "wb")
    acc = np.zeros_like(lats)
    for step in range(0, hours + 1):
        th = step + 1.5
        clat, clon = 55.0 + 0.08 * step, -14.0 + 0.25 * step
        dist2 = ((lats - clat) / 3.0) ** 2 + ((lons - clon) * math.cos(math.radians(55)) / 3.0) ** 2
        msl = 101600 - 2800 * np.exp(-dist2) + 250 * (lats - 53)
        # cyclonic flow around the low
        dy, dx_ = lats - clat, (lons - clon) * math.cos(math.radians(55))
        r = np.hypot(dx_, dy) + 0.3
        speed = 22 * (r / 3.0) * np.exp(1 - r / 3.0)
        ue, vn = -speed * dy / r + 2, speed * dx_ / r + 1
        ug = ue * np.cos(alpha) + vn * np.sin(alpha)
        vg = -ue * np.sin(alpha) + vn * np.cos(alpha)
        t2 = 285.0 + 0.45 * (lons + 8) - 0.6 * (lats - 53) + 3 * np.sin((step - 9) / 24 * 2 * math.pi)
        rate = rain_field(lats, lons, th)
        if step:
            acc = acc + rate
        tcc = np.clip(rate / 2 + 0.4 * np.exp(-dist2 / 2), 0, 1)
        for iop, lev_type, lev, vals in [(11, 105, 2, t2), (33, 105, 10, ug), (34, 105, 10, vg),
                                         (1, 103, 0, msl), (61, 105, 0, acc), (71, 105, 0, tcc)]:
            h = ec.codes_clone(h0)
            ec.codes_set_key_vals(h, {
                "dataDate": int(run.strftime("%Y%m%d")), "dataTime": run.hour * 100,
                "indicatorOfParameter": iop, "indicatorOfTypeOfLevel": lev_type, "level": lev,
                "stepUnits": 1, "timeRangeIndicator": 4 if iop == 61 else 0,
            })
            if iop == 61:
                ec.codes_set_key_vals(h, {"startStep": 0, "endStep": step})
            else:
                ec.codes_set(h, "P1", step)
            ec.codes_set(h, "bitsPerValue", 12)
            ec.codes_set_values(h, vals.ravel())
            ec.codes_write(h, out)
            ec.codes_release(h)
    out.close()
    ec.codes_release(h0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="./data/inbox")
    ap.add_argument("--hours", type=int, default=36)
    a = ap.parse_args()
    inbox = Path(a.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    make_radar(inbox, now)
    make_grib(inbox, now, a.hours)
    print(f"wrote synthetic radar + NWP files to {inbox}")
