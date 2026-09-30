"""Stepped colour scales. Discrete bands read better on a map than smooth
ramps and make the legend honest: one swatch = one range of values."""
from __future__ import annotations

import numpy as np


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


class Scale:
    def __init__(self, key: str, label: str, unit: str, steps: list[tuple[float, str]],
                 alpha: float | list[float] = 0.8, below: bool = False, above: float | None = None):
        """steps: (lower bound, colour). Values under the first bound are
        transparent unless below=True (then they take the first colour)."""
        self.key, self.label, self.unit = key, label, unit
        self.bounds = np.array([s[0] for s in steps], dtype=float)
        self.colours = [s[1] for s in steps]
        alphas = alpha if isinstance(alpha, list) else [alpha] * len(steps)
        rgba = [(*_hex(c), int(round(a * 255))) for c, a in zip(self.colours, alphas)]
        self.lut = np.array([(0, 0, 0, 0)] + rgba, dtype=np.uint8)
        self.below = below
        self.above = above  # values >= this are transparent (e.g. good visibility)

    def colorize(self, values: np.ndarray) -> np.ndarray:
        idx = np.digitize(values, self.bounds)  # 0 = below first bound
        if self.below:
            idx = np.maximum(idx, 1)
        idx[~np.isfinite(values)] = 0
        if self.above is not None:
            idx[values >= self.above] = 0
        return self.lut[idx]

    def legend(self) -> dict:
        out = {
            "key": self.key, "label": self.label, "unit": self.unit,
            "steps": [{"from": float(b), "colour": c} for b, c in zip(self.bounds, self.colours)],
        }
        if self.above is not None:
            out["above"] = self.above
        return out


# mm/h — shared by radar and model rain so the two can be compared by eye.
RAIN = Scale("rain", "Rain", "mm/h", [
    (0.1, "#bcd7ea"), (0.5, "#8cbbdc"), (1, "#5a9bcb"), (2, "#2f74b1"),
    (4, "#174f8c"), (8, "#e9b53f"), (16, "#e07b2a"), (32, "#c2381f"), (64, "#7a1f4f"),
], alpha=[0.55, 0.7, 0.8, 0.85, 0.9, 0.9, 0.92, 0.95, 0.95])

TEMP = Scale("temp", "Temperature", "°C", [
    (-60, "#2b2d6e"), (-6, "#34479a"), (-4, "#3d62b0"), (-2, "#4c7fc0"), (0, "#5f9ccd"),
    (2, "#78b6d4"), (4, "#93c9cf"), (6, "#a9d4bf"), (8, "#c3dca6"), (10, "#dcdf8f"),
    (12, "#eed27b"), (14, "#f2bb67"), (16, "#eea257"), (18, "#e6874a"), (20, "#da6b3f"),
    (22, "#c85036"), (24, "#b13a31"), (26, "#93272c"), (28, "#6f1a2b"),
], alpha=0.72, below=True)

WIND = Scale("wind", "Wind", "km/h", [
    (0, "#e9eef0"), (10, "#cfe0e2"), (20, "#a6cfd0"), (30, "#78b7b3"), (40, "#e8cf7b"),
    (50, "#e3a358"), (65, "#d06a3e"), (80, "#a83a3a"), (100, "#6e2250"),
], alpha=0.6)

CLOUD = Scale("cloud", "Cloud cover", "%", [
    (10, "#8e959b"), (30, "#8e959b"), (50, "#7c848b"), (70, "#6b737a"), (90, "#5c646b"),
], alpha=[0.12, 0.25, 0.38, 0.5, 0.62])

GUST = Scale("gust", "Wind gusts", "km/h", [
    (30, "#cfe0e2"), (40, "#a6cfd0"), (50, "#e8cf7b"), (65, "#e3a358"), (80, "#d06a3e"),
    (100, "#a83a3a"), (120, "#6e2250"),
], alpha=[0.45, 0.55, 0.65, 0.7, 0.75, 0.8, 0.85])

# km; only poor visibility is drawn - everything from 10 km up is clear.
VIS = Scale("vis", "Visibility", "km", [
    (0, "#4b3f63"), (0.2, "#6b5c86"), (1, "#8f84a8"), (2, "#b3abc4"), (5, "#d6d1df"),
], alpha=[0.85, 0.75, 0.62, 0.48, 0.32], above=10)

# mm of water equivalent lying on the ground (roughly 1 mm ≈ 1 cm of fresh snow)
SNOW = Scale("snow", "Lying snow", "mm w.e.", [
    (0.5, "#eef3fa"), (2, "#d7e3f4"), (5, "#b7c9ea"), (10, "#95a9dc"), (25, "#7a83c6"), (50, "#6a5aa8"),
], alpha=[0.7, 0.75, 0.8, 0.85, 0.88, 0.9])

# HARMONIE lightning diagnostic; Met's live runs peak in the low hundreds, so the
# steps are roughly logarithmic to keep both isolated flashes and big cells readable.
LIGHTNING = Scale("lightning", "Lightning", "model flash density", [
    (0.5, "#f6e27a"), (2, "#f2c14e"), (5, "#ee8434"), (15, "#d6452b"), (40, "#a3206d"), (100, "#5e1a8a"),
], alpha=[0.5, 0.65, 0.78, 0.86, 0.92, 0.95])

# mm in the hour, radar-estimated
RAIN_ACC = Scale("radaracc", "Radar rain, last hour", "mm", [
    (0.2, "#bcd7ea"), (0.5, "#8cbbdc"), (1, "#5a9bcb"), (2, "#2f74b1"),
    (4, "#174f8c"), (8, "#e9b53f"), (16, "#e07b2a"), (32, "#c2381f"),
], alpha=[0.55, 0.7, 0.8, 0.85, 0.9, 0.9, 0.92, 0.95])

SCALES = {s.key: s for s in (RAIN, TEMP, WIND, CLOUD, GUST, VIS, SNOW, LIGHTNING, RAIN_ACC)}


def dbz_to_rate(dbz: np.ndarray) -> np.ndarray:
    """Marshall-Palmer, Z = 200 R^1.6."""
    with np.errstate(invalid="ignore", over="ignore"):
        return (np.power(10.0, dbz / 10.0) / 200.0) ** (1 / 1.6)
