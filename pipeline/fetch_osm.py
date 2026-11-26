"""OpenStreetMap power infrastructure + industrial land.

Ontario: Geofabrik's daily Ontario extract (.osm.pbf), filtered locally with pyosmium. This avoids
public Overpass servers, which time out on province-wide queries.
Border (US / Quebec, display only): small Overpass box queries; failure is non-fatal.

Outputs (data/raw/), in Overpass-like element form:
  osm_power_lines.json   power=line|minor_line|cable ways with geometry + tags
  osm_power_points.json  substations and plants (centre point + tags)
  osm_industrial.json    landuse=industrial (bounding box)
"""
from __future__ import annotations

import json

from . import common
from .common import http, log

LINE_KINDS = {"line", "minor_line", "cable"}
POINT_KINDS = {"substation", "plant"}

Q_BORDER = """[out:json][timeout:{t}];
(
  way["power"~"^(line|cable)$"]({b});
  nwr["power"~"^(substation|plant)$"]({b});
);
out tags geom;"""


# ------------------------------------------------------------------ Geofabrik extract
def _download_pbf(cfg: dict):
    url = cfg["osm"]["pbf_url"]
    pbf = common.RAW / "ontario-latest.osm.pbf"
    log.info("osm downloading %s", url)
    r = http("GET", url, timeout=1800, stream=True)
    with open(pbf, "wb") as f:
        for chunk in r.iter_content(1 << 22):
            f.write(chunk)
    log.info("osm saved %.0f MB", pbf.stat().st_size / 1e6)
    return pbf


def extract(pbf) -> tuple[list, list, list]:
    """Filter a .osm.pbf to power lines, substations/plants and industrial land."""
    import osmium

    lines, pts, ind = [], [], []

    def bounds(coords):
        la = [c[0] for c in coords]
        lo = [c[1] for c in coords]
        return {"minlat": min(la), "minlon": min(lo), "maxlat": max(la), "maxlon": max(lo)}

    fp = (osmium.FileProcessor(str(pbf))
          .with_locations()
          .with_areas(osmium.filter.KeyFilter("landuse", "power"))
          .with_filter(osmium.filter.KeyFilter("power", "landuse")))
    n = 0
    for o in fp:
        tags = dict(o.tags)
        power, landuse = tags.get("power"), tags.get("landuse")
        if o.is_node():
            if power in POINT_KINDS:
                pts.append({"type": "node", "id": o.id, "lat": o.location.lat, "lon": o.location.lon, "tags": tags})
        elif o.is_way():
            try:
                coords = [(nd.location.lat, nd.location.lon) for nd in o.nodes if nd.location.valid()]
            except osmium.InvalidLocationError:
                continue
            if len(coords) < 2:
                continue
            if power in LINE_KINDS:
                lines.append({"type": "way", "id": o.id, "tags": tags,
                              "geometry": [{"lat": a, "lon": b} for a, b in coords]})
            elif power in POINT_KINDS:
                b = bounds(coords)
                pts.append({"type": "way", "id": o.id, "tags": tags,
                            "center": {"lat": (b["minlat"] + b["maxlat"]) / 2, "lon": (b["minlon"] + b["maxlon"]) / 2}})
            elif landuse == "industrial":
                ind.append({"type": "way", "id": o.id, "tags": tags, "bounds": bounds(coords)})
        elif o.is_area() and not o.from_way():   # multipolygon relations (closed ways handled above)
            coords = [(nd.lat, nd.lon) for ring in o.outer_rings() for nd in ring if nd.location.valid()]
            if not coords:
                continue
            b = bounds(coords)
            if power in POINT_KINDS:
                pts.append({"type": "relation", "id": o.orig_id(), "tags": tags,
                            "center": {"lat": (b["minlat"] + b["maxlat"]) / 2, "lon": (b["minlon"] + b["maxlon"]) / 2}})
            elif landuse == "industrial":
                ind.append({"type": "relation", "id": o.orig_id(), "tags": tags, "bounds": b})
        n += 1
        if n % 200_000 == 0:
            log.info("   osm extract: %d objects", n)
    log.info("osm extract: %d lines, %d substations/plants, %d industrial", len(lines), len(pts), len(ind))
    return lines, pts, ind


# ------------------------------------------------------------------ border (display only)
def _border(cfg: dict) -> tuple[list, list]:
    bb = cfg["region"].get("border_bbox")
    if not bb:
        return [], []
    x0, y0, x1, y1 = bb
    t = cfg["osm"]["timeout_s"]
    q = Q_BORDER.format(t=t, b=f"{y0},{x0},{y1},{x1}")
    for ep in cfg["osm"]["endpoints"]:
        try:
            els = http("POST", ep, data={"data": q}, timeout=t + 60, retries=1).json().get("elements", [])
            lines, pts = [], []
            for e in els:
                p = e.get("tags", {}).get("power")
                if p in LINE_KINDS and e.get("geometry"):
                    lines.append(e)
                elif p in POINT_KINDS:
                    if e["type"] != "node":
                        b = e.get("bounds")
                        if not b:
                            continue
                        e["center"] = {"lat": (b["minlat"] + b["maxlat"]) / 2, "lon": (b["minlon"] + b["maxlon"]) / 2}
                    pts.append(e)
            log.info("osm border: %d lines, %d points from %s", len(lines), len(pts), ep)
            return lines, pts
        except Exception as e:
            log.warning("osm border query failed on %s: %s", ep, e)
    log.warning("osm border data unavailable; neighbouring infrastructure won't be shown")
    return [], []


def fetch_all(cfg: dict, force: bool = False) -> None:
    outs = {k: common.RAW / f"osm_{k}.json" for k in ("power_lines", "power_points", "industrial")}
    if all(p.exists() for p in outs.values()) and not force:
        log.info("osm extract                 cached")
        return
    pbf = _download_pbf(cfg)
    lines, pts, ind = extract(pbf)
    pbf.unlink()   # ~1 GB; keep the build cache small
    bl, bp = _border(cfg)
    seen = {(e["type"], e["id"]) for e in lines + pts}
    lines += [e for e in bl if (e["type"], e["id"]) not in seen]
    pts += [e for e in bp if (e["type"], e["id"]) not in seen]
    for key, els in (("power_lines", lines), ("power_points", pts), ("industrial", ind)):
        outs[key].write_text(json.dumps({"elements": els}))
