"""Web-Mercator output grids. Every overlay PNG we produce is laid out on one of
these so Leaflet's imageOverlay lines it up exactly (Leaflet stretches images
linearly in projected space, so rows must be spaced evenly in Mercator y)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

EARTH_R = 6_371_000.0


def merc_y(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


@dataclass(frozen=True)
class MercGrid:
    name: str
    south: float
    west: float
    north: float
    east: float
    width: int

    @cached_property
    def height(self) -> int:
        span_y = merc_y(self.north) - merc_y(self.south)
        span_x = np.radians(self.east - self.west)
        return int(round(self.width * span_y / span_x))

    @cached_property
    def lons(self) -> np.ndarray:
        return self.west + (np.arange(self.width) + 0.5) / self.width * (self.east - self.west)

    @cached_property
    def lats(self) -> np.ndarray:
        y0, y1 = merc_y(self.north), merc_y(self.south)
        ys = y0 + (np.arange(self.height) + 0.5) / self.height * (y1 - y0)
        return inv_merc_y(ys)

    @cached_property
    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        lon2d, lat2d = np.meshgrid(self.lons, self.lats)
        return lat2d, lon2d

    @property
    def bounds(self) -> list[list[float]]:
        return [[self.south, self.west], [self.north, self.east]]

    def pixel_to_lonlat(self, col, row):
        col = np.asarray(col, dtype=float)
        row = np.asarray(row, dtype=float)
        lon = self.west + (col + 0.5) / self.width * (self.east - self.west)
        y0, y1 = merc_y(self.north), merc_y(self.south)
        lat = inv_merc_y(y0 + (row + 0.5) / self.height * (y1 - y0))
        return lon, lat

    def lonlat_to_pixel(self, lon: float, lat: float) -> tuple[int, int] | None:
        col = (lon - self.west) / (self.east - self.west) * self.width - 0.5
        y0, y1 = merc_y(self.north), merc_y(self.south)
        row = (merc_y(lat) - y0) / (y1 - y0) * self.height - 0.5
        c, r = int(round(col)), int(round(row))
        if 0 <= c < self.width and 0 <= r < self.height:
            return c, r
        return None


# Both radars (Dublin, Shannon) reach roughly 240-300 km.
RADAR_GRID = MercGrid("radar", south=50.3, west=-12.9, north=56.7, east=-3.1, width=1100)
# NWP overlay: Ireland + surrounding seas; clipped further to the model domain.
NWP_GRID = MercGrid("nwp", south=48.0, west=-17.5, north=59.5, east=0.5, width=1000)


def great_circle(lat0, lon0, lat, lon):
    """Distance (m) and initial bearing (deg, clockwise from north) from a point."""
    p0, l0 = np.radians(lat0), np.radians(lon0)
    p, l = np.radians(lat), np.radians(lon)
    dl = l - l0
    a = np.sin((p - p0) / 2) ** 2 + np.cos(p0) * np.cos(p) * np.sin(dl / 2) ** 2
    dist = 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    brg = np.degrees(np.arctan2(np.sin(dl) * np.cos(p),
                                np.cos(p0) * np.sin(p) - np.sin(p0) * np.cos(p) * np.cos(dl)))
    return dist, np.mod(brg, 360.0)


def to_xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)
