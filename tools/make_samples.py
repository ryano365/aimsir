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


def _aeqd_inverse(x, y, lat0, lon0, R=6371000.0):
    rho = np.hypot(x, y)
    c = rho / R
    p0 = np.radians(lat0)
    with np.errstate(invalid="ignore", divide="ignore"):
        lat = np.arcsin(np.cos(c) * np.sin(p0) + np.where(rho > 0, y * np.sin(c) * np.cos(p0) / rho, 0))
        lon = np.radians(lon0) + np.arctan2(x * np.sin(c), rho * np.cos(p0) * np.cos(c) - y * np.sin(p0) * np.sin(c))
    return np.degrees(lat), np.degrees(lon)


def make_radar_acc(inbox: Path, now: datetime, hours: int = 6):
    """ODIM COMP files like Met's T_PASH21 hourly composite accumulation (ACRR, mm)."""
    lat0, lon0, size, scale = 53.2, -7.6, 520, 1000.0
    xs = (np.arange(size) + 0.5 - size / 2) * scale
    ys = (size / 2 - np.arange(size) - 0.5) * scale
    X, Y = np.meshgrid(xs, ys)
    lat, lon = _aeqd_inverse(X, Y, lat0, lon0)
    half = size / 2 * scale
    corners = {}
    for k, (cx, cy) in {"UL": (-half, half), "UR": (half, half), "LL": (-half, -half), "LR": (half, -half)}.items():
        la, lo = _aeqd_inverse(np.array(cx), np.array(cy), lat0, lon0)
        corners[f"{k}_lat"], corners[f"{k}_lon"] = float(la), float(lo)
    end = now.replace(minute=0, second=0, microsecond=0)
    for k in range(hours):
        t = end - timedelta(hours=k)
        th = 2.0 - k  # line up with the synthetic volumes' timeline
        mm = np.mean([rain_field(lat, lon, th - 1 + j / 6) for j in range(6)], axis=0)
        gain = 0.1
        raw = np.clip(np.round(mm / gain), 0, 65534).astype(np.uint16)
        raw[np.hypot(X, Y) > 250_000] = 65535  # outside radar reach: nodata
        with h5py.File(inbox / f"T_PASH21_C_EIDB_{t:%Y%m%d%H%M}00.hdf", "w") as f:
            f.create_group("what").attrs.update({"object": np.bytes_("COMP"), "date": np.bytes_(f"{t:%Y%m%d}"),
                                                 "time": np.bytes_(f"{t:%H%M%S}"), "source": np.bytes_("NOD:iecomp")})
            w = f.create_group("where")
            w.attrs.update({"projdef": np.bytes_(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +R=6371000 +units=m"),
                            "xsize": size, "ysize": size, "xscale": scale, "yscale": scale, **corners})
            ds = f.create_group("dataset1")
            ds.create_group("what").attrs.update({"product": np.bytes_("COMP"),
                                                  "startdate": np.bytes_(f"{t - timedelta(hours=1):%Y%m%d}"),
                                                  "starttime": np.bytes_(f"{t - timedelta(hours=1):%H%M%S}"),
                                                  "enddate": np.bytes_(f"{t:%Y%m%d}"), "endtime": np.bytes_(f"{t:%H%M%S}")})
            d = ds.create_group("data1")
            d.create_dataset("data", data=raw, compression="gzip")
            d.create_group("what").attrs.update({"quantity": np.bytes_("ACRR"), "gain": gain, "offset": 0.0,
                                                 "nodata": 65535.0, "undetect": 0.0})


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


def make_grib2_extras(inbox: Path, now: datetime, hours: int = 36):
    """GRIB2 extras coded the way Met's DINI 'ieIoI' bundle has them: gust components
    (0/2/23-24, max), visibility (0/19/0), snow (sd), lightning (HARMONIE local 0/17/192)."""
    import eccodes as ec

    run = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=(now.hour % 3) + 3)
    nx, ny = 260, 280
    h0 = ec.codes_grib_new_from_samples("GRIB2")
    ec.codes_set_key_vals(h0, {"gridType": "lambert", "Nx": nx, "Ny": ny, "DxInMetres": 5000, "DyInMetres": 5000,
                               "latitudeOfFirstGridPointInDegrees": 48.5, "longitudeOfFirstGridPointInDegrees": 345.0,
                               "LoVInDegrees": 352.0, "Latin1InDegrees": 53.5, "Latin2InDegrees": 53.5,
                               "LaDInDegrees": 53.5, "dataDate": int(run.strftime("%Y%m%d")), "dataTime": run.hour * 100})
    ec.codes_set_values(h0, np.zeros(nx * ny))
    lats = ec.codes_get_array(h0, "latitudes").reshape(ny, nx)
    lons = ec.codes_get_array(h0, "longitudes").reshape(ny, nx)
    lons = np.where(lons > 180, lons - 360, lons)
    out = open(inbox / f"fc{run:%Y%m%d%H}+extrasCONTROL_grib2_ieIoI_synthetic", "wb")

    def put(step, disc_cat_num, vals, level=10, stat=None):
        h = ec.codes_clone(h0)
        if stat is not None:
            ec.codes_set(h, "productDefinitionTemplateNumber", 8)
        d, c, n = disc_cat_num
        ec.codes_set_key_vals(h, {"discipline": d, "parameterCategory": c, "parameterNumber": n,
                                  "typeOfFirstFixedSurface": 103, "scaledValueOfFirstFixedSurface": level,
                                  "scaleFactorOfFirstFixedSurface": 0})
        if stat is not None:
            ec.codes_set_key_vals(h, {"typeOfStatisticalProcessing": stat, "startStep": max(step - 1, 0), "endStep": step})
        else:
            ec.codes_set(h, "forecastTime", step)
        ec.codes_set(h, "bitsPerValue", 16)
        ec.codes_set(h, "packingType", "grid_ccsds")
        ec.codes_set_values(h, vals.ravel())
        ec.codes_write(h, out)
        ec.codes_release(h)

    for step in range(0, hours + 1):
        th = step + 1.5
        rate = rain_field(lats, lons, th)
        clat, clon = 55.0 + 0.08 * step, -14.0 + 0.25 * step
        r = np.hypot((lats - clat) / 3.0, (lons - clon) * math.cos(math.radians(55)) / 3.0)
        gust = 34 * np.exp(-((r - 1.1) ** 2) / 0.5) + 6
        put(step, (0, 2, 23), gust * 0.8, stat=2)
        put(step, (0, 2, 24), gust * 0.6, stat=2)
        fog = 30000 * (1 - 0.97 * np.exp(-(((lats - 52.3) / 0.35) ** 2 + ((lons + 7.8) / 0.6) ** 2)))
        put(step, (0, 19, 0), np.minimum(fog, 30000 / (1 + rate)), level=0)
        snow = np.clip((lats - 54.6) * 30 + np.sin(lons * 3) * 5, 0, None) * (lons > -8.5)
        put(step, (0, 1, 60), snow, level=0)
        put(step, (0, 17, 192), np.clip(rate - 3, 0, None) * 0.8, level=0)
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
    make_radar_acc(inbox, now)
    make_grib(inbox, now, a.hours)
    make_grib2_extras(inbox, now, a.hours)
    print(f"wrote synthetic radar + NWP files to {inbox}")
