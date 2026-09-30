"""Grid-based area discovery (PROJECT.md §7b).

Overcomes the Places API per-query result cap by tiling the requested area into
overlapping circles, running one search per tile, and de-duplicating results by
`place_id`.

Places API (New) Nearby Search returns at most 20 results per call and provides
no page token, so each tile is capped at 20; a tile that hits that cap is
recursively subdivided into four smaller tiles (up to `max_recursion_depth`) so
dense blocks are still covered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

# search_fn(lat, lng, radius_m) -> list of place dicts
SearchFn = Callable[[float, float, float], list[dict[str, Any]]]

# Local flat-earth approximation is fine at the scale of a city district.
_M_PER_DEG_LAT = 111_320.0


def _deg_lat(meters: float) -> float:
    return meters / _M_PER_DEG_LAT


def _deg_lng(meters: float, lat: float) -> float:
    denom = _M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6)
    return meters / denom


@dataclass
class Tile:
    lat: float
    lng: float
    radius_m: float
    depth: int = 0


@dataclass
class DiscoveryConfig:
    block_size_m: float = 500.0
    tile_overlap_factor: float = 0.6  # tile radius = block_size * this
    max_recursion_depth: int = 2
    saturation_threshold: int = 20  # New Nearby Search hard cap per call

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DiscoveryConfig":
        d = d or {}
        return cls(
            block_size_m=float(d.get("block_size_m", 500.0)),
            tile_overlap_factor=float(d.get("tile_overlap_factor", 0.6)),
            max_recursion_depth=int(d.get("max_recursion_depth", 2)),
            saturation_threshold=int(d.get("saturation_threshold", 20)),
        )


@dataclass
class DiscoveryResult:
    places: dict[str, dict[str, Any]] = field(default_factory=dict)
    tiles_queried: int = 0
    saturated_tiles: int = 0
    # place_id -> tile centers (lat, lng) it was found in (grid-coverage debugging)
    place_tiles: dict[str, list[tuple[float, float]]] = field(default_factory=dict)


def plan_tiles(
    center_lat: float, center_lng: float, radius_m: float, cfg: DiscoveryConfig
) -> list[Tile]:
    """Square grid of tile centers spaced `block_size_m` apart covering the
    requested circle. Tiles whose center falls outside the circle by more than
    one tile radius are dropped."""
    block = cfg.block_size_m
    tile_radius = block * cfg.tile_overlap_factor
    n = max(0, math.ceil(radius_m / block)) if block > 0 else 0
    tiles: list[Tile] = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dist = math.hypot(i * block, j * block)
            if dist > radius_m + tile_radius:
                continue
            tlat = center_lat + _deg_lat(i * block)
            tlng = center_lng + _deg_lng(j * block, center_lat)
            tiles.append(Tile(tlat, tlng, tile_radius))
    return tiles


def estimate(
    center_lat: float, center_lng: float, radius_m: float, cfg: DiscoveryConfig
) -> dict[str, int]:
    """Pre-flight estimate so `add-area` can print cost before executing.
    `min_api_calls` excludes recursive re-splits (best case)."""
    tiles = plan_tiles(center_lat, center_lng, radius_m, cfg)
    return {"tiles": len(tiles), "min_api_calls": len(tiles)}


def _split_tile(tile: Tile) -> list[Tile]:
    """Split a saturated tile into four half-radius children offset diagonally."""
    half = tile.radius_m / 2.0
    off = tile.radius_m / 2.0
    children: list[Tile] = []
    for si in (-1, 1):
        for sj in (-1, 1):
            clat = tile.lat + _deg_lat(si * off)
            clng = tile.lng + _deg_lng(sj * off, tile.lat)
            children.append(Tile(clat, clng, half, tile.depth + 1))
    return children


def _query_tile(
    search_fn: SearchFn,
    tile: Tile,
    cfg: DiscoveryConfig,
    result: DiscoveryResult,
    on_tile: Callable[[Tile, int], None] | None,
) -> None:
    places = search_fn(tile.lat, tile.lng, tile.radius_m)
    result.tiles_queried += 1
    if on_tile is not None:
        on_tile(tile, len(places))
    for p in places:
        pid = p.get("id")
        if not pid:
            continue
        result.places.setdefault(pid, p)
        result.place_tiles.setdefault(pid, []).append(
            (round(tile.lat, 5), round(tile.lng, 5))
        )
    if len(places) >= cfg.saturation_threshold and tile.depth < cfg.max_recursion_depth:
        result.saturated_tiles += 1
        for sub in _split_tile(tile):
            _query_tile(search_fn, sub, cfg, result, on_tile)


def discover_area(
    search_fn: SearchFn,
    center_lat: float,
    center_lng: float,
    radius_m: float,
    cfg: DiscoveryConfig,
    on_tile: Callable[[Tile, int], None] | None = None,
) -> DiscoveryResult:
    """Run the full grid search and return merged, de-duplicated results."""
    result = DiscoveryResult()
    for tile in plan_tiles(center_lat, center_lng, radius_m, cfg):
        _query_tile(search_fn, tile, cfg, result, on_tile)
    return result
