"""XYZ web-map tiles (256 px, Web Mercator) drawn on demand from stored fields.

Values are interpolated from the source data for every screen pixel, so layers
stay sharp at any zoom, and results are cached on disk (frames are immutable)."""
from __future__ import annotations

import io
import math
import os
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

TILE = 256


def _empty_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


EMPTY = _empty_png()


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees."""
    n = 2 ** z
    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def tile_latlon(z: int, x: int, y: int, size: int = TILE):
    n = 2 ** z
    px = (np.arange(size) + 0.5) / size
    gx = (x + px) / n
    gy = (y + px) / n
    lon = gx * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * gy))))
    lon2d, lat2d = np.meshgrid(lon, lat)
    return lat2d, lon2d


def intersects(z, x, y, bbox) -> bool:
    """bbox = [west, south, east, north]"""
    w, s, e, n = tile_bounds(z, x, y)
    return not (e < bbox[0] or w > bbox[2] or n < bbox[1] or s > bbox[3])


def encode(rgba: np.ndarray) -> bytes:
    if not rgba[..., 3].any():
        return EMPTY
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def cached(path: Path, render: Callable[[], bytes]) -> bytes:
    if path.exists():
        return path.read_bytes()
    data = render()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def draw_arrows(rgba: np.ndarray, speed_kmh: np.ndarray, u: np.ndarray, v: np.ndarray,
                cell: int = 64, colour=(38, 44, 50, 205)) -> np.ndarray:
    """Wind arrows at the centre of each cell; cells tile seamlessly across tiles,
    so arrows keep a constant screen spacing at every zoom and never get clipped."""
    img = Image.fromarray(rgba, "RGBA")
    draw = ImageDraw.Draw(img)
    for r in range(cell // 2, TILE, cell):
        for c in range(cell // 2, TILE, cell):
            s = speed_kmh[r, c]
            if not np.isfinite(s) or s < 3:
                continue
            th = math.atan2(-v[r, c], u[r, c])       # screen angle the wind blows towards
            length = 10 + min(s, 90) / 90 * 16
            dx, dy = math.cos(th) * length / 2, math.sin(th) * length / 2
            x0, y0, x1, y1 = c - dx, r - dy, c + dx, r + dy
            draw.line([(x0, y0), (x1, y1)], fill=colour, width=1)
            for side in (0.45, -0.45):
                draw.line([(x1, y1), (x1 - math.cos(th + side) * 6, y1 - math.sin(th + side) * 6)],
                          fill=colour, width=1)
    return np.asarray(img)


def hatch(outside: np.ndarray, z: int, x: int, y: int) -> np.ndarray:
    """Faint diagonal hatch for 'no radar coverage', continuous across tiles."""
    gy, gx = np.mgrid[0:TILE, 0:TILE]
    gx = gx + x * TILE
    gy = gy + y * TILE
    lines = ((gx + gy) % 9) == 0
    rgba = np.zeros((TILE, TILE, 4), dtype=np.uint8)
    rgba[outside] = (60, 64, 70, 26)
    rgba[outside & lines] = (60, 64, 70, 66)
    return rgba
