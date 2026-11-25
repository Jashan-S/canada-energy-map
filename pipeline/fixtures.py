"""Synthetic stand-in data for offline smoke tests. NOT real infrastructure.

Copies Natural Earth from data/raw if present (real boundary), and fabricates OSM/LIO
files with the same shapes the real downloads have, so build.py runs end-to-end.
"""
from __future__ import annotations

import json
import shutil

import numpy as np
from shapely.geometry import Point, box, mapping

from . import common
from .common import log

rng = np.random.default_rng(7)
CITIES = [(-79.38, 43.65), (-79.87, 43.25), (-80.49, 43.45), (-81.25, 42.98), (-83.0, 42.3),
          (-75.70, 45.42), (-76.48, 44.23), (-78.32, 44.30), (-79.69, 44.39), (-82.40, 42.98)]


def _fc(feats):
    return {"type": "FeatureCollection", "features": feats}


def make(cfg: dict) -> None:
    raw = common.RAW
    raw.mkdir(parents=True, exist_ok=True)
    real = common.ROOT / "data" / "raw"
    ne = cfg["natural_earth"]
    for name in [ne["provinces"], *ne["lakes"]]:
        src = real / f"{name}.geojson"
        if not src.exists():
            raise SystemExit(f"fixtures need real Natural Earth file {src} (run fetch_base once)")
        shutil.copy(src, raw / src.name)

    x0, y0, x1, y1 = cfg["region"]["bbox"]

    def rpt(spread=0.6):
        cx, cy = CITIES[rng.integers(len(CITIES))]
        return cx + rng.normal(0, spread), cy + rng.normal(0, spread * 0.6)

    # power lines: radial lines from cities + random ties
    els = []
    for i in range(2500):
        a = rpt(0.8)
        b = (a[0] + rng.normal(0, 0.15), a[1] + rng.normal(0, 0.1))
        kv = rng.choice(["500000", "230000", "115000", "44000", "27600", None], p=[.03, .12, .15, .3, .3, .1])
        tags = {"power": "line" if kv and int(kv) >= 115000 else "minor_line"}
        if kv:
            tags["voltage"] = kv
        els.append({"type": "way", "id": i, "tags": tags,
                    "geometry": [{"lon": a[0], "lat": a[1]}, {"lon": b[0], "lat": b[1]}]})
    (raw / "osm_power_lines.json").write_text(json.dumps({"elements": els}))

    pts = []
    for i in range(1200):
        x, y = rpt(0.9)
        kv = rng.choice(["230000;44000", "115000;27600", "44000;13800", None], p=[.15, .2, .45, .2])
        t = {"power": "substation", "name": f"Test TS {i}"}
        if kv:
            t["voltage"] = kv
        pts.append({"type": "node", "id": i, "lat": y, "lon": x, "tags": t})
    for i in range(150):
        x, y = rpt(1.2)
        pts.append({"type": "way", "id": 10000 + i, "center": {"lat": y, "lon": x},
                    "tags": {"power": "plant", "name": f"Test plant {i}",
                             "plant:source": str(rng.choice(["wind", "solar", "gas", "hydro"])),
                             "plant:output:electricity": f"{rng.integers(5, 500)} MW"}})
    (raw / "osm_power_points.json").write_text(json.dumps({"elements": pts}))

    ind = []
    for i in range(15000):
        x, y = rpt(0.4)
        d = abs(rng.normal(0.004, 0.003))
        ind.append({"type": "way", "id": i,
                    "bounds": {"minlat": y - d, "minlon": x - d, "maxlat": y + d, "maxlon": x + d}})
    (raw / "osm_industrial.json").write_text(json.dumps({"elements": ind}))

    def blobs(n, r, props=lambda i: {}):
        return _fc([{"type": "Feature", "properties": props(i),
                     "geometry": mapping(Point(rng.uniform(x0, x1), rng.uniform(y0, y1)).buffer(r))}
                    for i in range(n)])

    def write(key, obj):
        (raw / f"lio_{key}.geojson").write_text(json.dumps(obj))

    write("provincial_park", blobs(120, 0.05))
    write("conservation_reserve", blobs(80, 0.03))
    write("national_wildlife", blobs(5, 0.03))
    write("federal_protected", blobs(5, 0.03))
    write("first_nation_reserve", blobs(30, 0.04))
    write("wetland", blobs(4000, 0.01, lambda i: {"WETLAND_SIGNIFICANCE": "Provincially Significant" if i % 3 == 0 else "Non-Provincially Significant"}))
    write("built_up", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(Point(c).buffer(0.12))} for c in CITIES]))
    gb = Point(-79.5, 43.85).buffer(0.55).difference(Point(-79.45, 43.7).buffer(0.32))
    write("greenbelt", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(gb)}]))
    write("specialty_crop", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(box(-79.6, 44.0, -79.45, 44.12))},
                                 {"type": "Feature", "properties": {}, "geometry": mapping(box(-79.6, 43.12, -79.0, 43.22))}]))
    write("oak_ridges_moraine", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(box(-79.9, 43.95, -78.3, 44.1))}]))
    write("niagara_escarpment", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(box(-80.2, 43.3, -79.9, 44.6))}]))
    soil = []
    for gx in np.arange(x0, x1, 0.25):
        for gy in np.arange(y0, y1, 0.25):
            soil.append({"type": "Feature", "properties": {"CLI1": str(rng.choice(["1", "2T", "3W", "4", "5", "6", "7", "O"]))},
                         "geometry": mapping(box(gx, gy, gx + 0.25, gy + 0.25))})
    write("soil", _fc(soil))
    write("airport", _fc([{"type": "Feature", "properties": {}, "geometry": mapping(Point(c[0] + 0.1, c[1] + 0.05))} for c in CITIES]))
    muni = []
    for gx in np.arange(x0, x1, 0.4):
        for gy in np.arange(y0, y1, 0.4):
            muni.append({"type": "Feature", "properties": {"MUNICIPAL_NAME_SHORTFORM": f"TEST TOWNSHIP {gx:.1f} {gy:.1f}"},
                         "geometry": mapping(box(gx, gy, gx + 0.4, gy + 0.4))})
    write("municipality", _fc(muni))
    log.info("fixtures written to %s", raw)
