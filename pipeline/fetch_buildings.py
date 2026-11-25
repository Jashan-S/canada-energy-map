"""Building footprints -> per-hex building coverage and count.

Source: Microsoft Canadian Building Footprints (ODbL), ~3.8M Ontario buildings.
The zip is downloaded once (cached), then streamed feature-by-feature: each building
is reduced to (centroid cell, footprint m²). Output: data/raw/buildings_h3r<res>.json
  {cell: [footprint_m2, count]}
"""
from __future__ import annotations

import io
import json
import math
import zipfile

import h3

from . import common
from .common import http, log


def _ring_area_m2(ring) -> tuple[float, float, float]:
    """Shoelace area in m² on a local equirectangular projection; also centroid lon/lat."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_540
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        a += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    lon = sum(p[0] for p in ring) / len(ring)
    return abs(a) / 2, lon, lat0


def _feature_area(geom) -> tuple[float, float, float] | None:
    t, c = geom.get("type"), geom.get("coordinates")
    if not c:
        return None
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else []
    tot, lon, lat = 0.0, None, None
    for poly in polys:
        outer = [p[:2] for p in poly[0]]
        a, x, y = _ring_area_m2(outer)
        for hole in poly[1:]:
            a -= _ring_area_m2([p[:2] for p in hole])[0]
        tot += max(a, 0)
        if lon is None:
            lon, lat = x, y
    return (tot, lon, lat) if lon is not None else None


def _iter_features(fh):
    """Yield geometries from a GeoJSON FeatureCollection, line-delimited or not."""
    head = fh.read(1 << 16)
    rest = fh
    buf = io.StringIO(head)
    lines_mode = any(l.lstrip().startswith('{"type"') and '"Feature"' in l and l.rstrip().rstrip(",").endswith("}")
                     for l in head.splitlines()[1:6])
    if lines_mode:
        def lines():
            yield from buf
            yield from rest
        carry = ""
        for line in lines():
            if not line.endswith("\n"):
                carry += line
                continue
            line, carry = carry + line, ""
            s = line.strip().rstrip(",")
            if s.startswith("{") and '"geometry"' in s:
                try:
                    yield json.loads(s)["geometry"]
                except json.JSONDecodeError:
                    continue
        if carry.strip().rstrip(",").startswith("{"):
            try:
                yield json.loads(carry.strip().rstrip(","))["geometry"]
            except json.JSONDecodeError:
                pass
    else:
        import ijson  # streaming parser for single-line files

        class _Chain(io.RawIOBase):
            def __init__(self):
                self.parts = [head.encode(), None]

            def readable(self):
                return True

            def readinto(self, b):
                while self.parts:
                    if self.parts[0] is None:
                        chunk = rest.read(len(b)).encode()
                        if not chunk:
                            self.parts.pop(0)
                            continue
                    else:
                        chunk = self.parts.pop(0)
                    n = min(len(b), len(chunk))
                    b[:n] = chunk[:n]
                    if n < len(chunk):
                        self.parts.insert(0, chunk[n:])
                    return n
                return 0

        for g in ijson.items(io.BufferedReader(_Chain()), "features.item.geometry", use_float=True):
            yield g


def aggregate(zip_path, res: int) -> dict:
    agg: dict[str, list] = {}
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        name = next(x for x in z.namelist() if x.lower().endswith((".geojson", ".json")))
        with z.open(name) as raw:
            fh = io.TextIOWrapper(raw, encoding="utf-8")
            for geom in _iter_features(fh):
                r = _feature_area(geom)
                if not r:
                    continue
                a, lon, lat = r
                c = h3.latlng_to_cell(lat, lon, res)
                v = agg.get(c)
                if v is None:
                    agg[c] = [a, 1]
                else:
                    v[0] += a
                    v[1] += 1
                n += 1
                if n % 500_000 == 0:
                    log.info("   buildings: %d processed", n)
    log.info("buildings: %d footprints into %d hexes", n, len(agg))
    return {k: [round(v[0]), v[1]] for k, v in agg.items()}


def fetch(cfg: dict, force: bool = False) -> None:
    b = cfg.get("buildings")
    if not b:
        return
    res = cfg["region"]["h3_resolution"]
    out = common.RAW / f"buildings_h3r{res}.json"
    zp = common.RAW / "buildings_ON.zip"
    if out.exists() and not force:
        log.info("bld %-22s cached", out.name)
        return
    if not zp.exists() or force:
        log.info("bld downloading %s", b["url"])
        r = http("GET", b["url"], timeout=1800, stream=True)
        with open(zp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        log.info("bld saved %.0f MB", zp.stat().st_size / 1e6)
    out.write_text(json.dumps(aggregate(zp, res)))
