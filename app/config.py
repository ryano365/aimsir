"""Runtime settings, all read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Met Éireann open data portal (https://opendata.met.ie -> Profile -> API key)
    api_key: str = field(default_factory=lambda: _env("MET_API_KEY", ""))
    api_base: str = field(default_factory=lambda: _env("MET_API_BASE", "https://api.opendata.met.ie/api"))
    # Point forecast API (HARMONIE/ECMWF blend, no key needed)
    pf_url: str = field(default_factory=lambda: _env(
        "MET_PF_URL", "http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast"))
    warnings_url: str = field(default_factory=lambda: _env(
        "MET_WARNINGS_URL", "https://www.met.ie/Open_Data/json/warning_IRELAND.json"))

    # Map background: openfreemap (free vector tiles, default), osm (openstreetmap.org raster),
    # or none (bundled coastline only - no external requests at all)
    basemap: str = field(default_factory=lambda: _env("BASEMAP", "openfreemap").lower())

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
    # Drop .h5 / .grib files in here to have them processed without the API (testing, FTP sync...)
    inbox_dir: Path = field(default_factory=lambda: Path(_env("INBOX_DIR", "./data/inbox")).resolve())

    # Default location for the forecast panel
    home_name: str = field(default_factory=lambda: _env("HOME_NAME", "Dublin"))
    home_lat: float = field(default_factory=lambda: _float("HOME_LAT", 53.3498))
    home_lon: float = field(default_factory=lambda: _float("HOME_LON", -6.2603))

    # Radar
    radar_poll_s: int = field(default_factory=lambda: _int("RADAR_POLL_SECONDS", 150))
    radar_history_min: int = field(default_factory=lambda: _int("RADAR_HISTORY_MINUTES", 120))
    # Instantaneous volume scans: 40 = Shannon, 41 = Dublin
    radar_file_regex: str = field(default_factory=lambda: _env("RADAR_FILE_REGEX", r"T_PAGZ4[01]_.*\.h5$"))
    # Hourly radar rainfall accumulation: T_PASH21 = Dublin+Shannon composite (T_PASH41 = Dublin only)
    radar_acc_regex: str = field(default_factory=lambda: _env("RADAR_ACC_REGEX", r"T_PASH21_.*\.hdf$"))
    radar_acc_hours: int = field(default_factory=lambda: _int("RADAR_ACC_HOURS", 12))
    radar_min_dbz: float = field(default_factory=lambda: _float("RADAR_MIN_DBZ", 7.0))

    # NWP (HARMONIE-AROME GRIB)
    nwp_poll_s: int = field(default_factory=lambda: _int("NWP_POLL_SECONDS", 900))
    nwp_file_regex: str = field(default_factory=lambda: _env("NWP_FILE_REGEX", r"CONTROL_grib2_ieIoI$"))
    nwp_max_file_mb: int = field(default_factory=lambda: _int("NWP_MAX_FILE_MB", 1500))
    nwp_max_raw_gb: float = field(default_factory=lambda: _float("NWP_MAX_RAW_GB", 8))
    nwp_max_hours: int = field(default_factory=lambda: _int("NWP_MAX_HOURS", 48))
    # DINI runs hourly; only fetch runs starting every N hours (1 = every run, ~0.7 GB each)
    nwp_run_every_hours: int = field(default_factory=lambda: _int("NWP_RUN_EVERY_HOURS", 3))

    forecast_cache_s: int = field(default_factory=lambda: _int("FORECAST_CACHE_SECONDS", 1800))


settings = Settings()
