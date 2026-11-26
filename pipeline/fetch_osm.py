"""Download power infrastructure + industrial land from OpenStreetMap (Overpass API).

Outputs (data/raw/):
  osm_power_lines.json   power=line|minor_line|cable ways with geometry + tags
  osm_power_points.json  substations and plants (centre point + tags)
  osm_industrial.json    landuse=industrial (bounding box)
"""
from __future__ import annotations

import json

from .common import RAW, http, log

# {area} = the province (by ISO code), {b} = border box for neighbouring infrastructure
AREA = 'area["ISO3166-2"="CA-ON"]["admin_level"="4"]->.on;'

Q_LINES = """[out:json][timeout:{t}];
{area}
(
  way["power"~"^(line|minor_line|cable)$"](area.on);
  way["power"~"^(line|cable)$"]({b});
);
out tags geom;"""

Q_POINTS = """[out:json][timeout:{t}];
{area}
(
  nwr["power"="substation"](area.on);
  nwr["power"="plant"](area.on);
  nwr["power"="substation"]({b});
  nwr["power"="plant"]({b});
);
out tags center;"""

Q_INDUSTRIAL = """[out:json][timeout:{t}];
{area}
(
  way["landuse"="industrial"](area.on);
  relation["landuse"="industrial"](area.on);
);
out tags bb;"""


def _run(cfg: dict, query: str, out_name: str, force: bool) -> None:
    out = RAW / out_name
    if out.exists() and not force:
        log.info("osm %-22s cached", out_name)
        return
    x0, y0, x1, y1 = cfg["region"].get("border_bbox") or cfg["region"]["bbox"]
    t = cfg["osm"]["timeout_s"]
    q = query.format(t=t, b=f"{y0},{x0},{y1},{x1}", area=AREA)
    last = None
    for ep in cfg["osm"]["endpoints"]:
        try:
            log.info("osm %-22s querying %s", out_name, ep)
            r = http("POST", ep, data={"data": q}, timeout=t + 60, retries=2)
            data = r.json()
            out.write_text(json.dumps(data))
            log.info("osm %-22s saved %d elements", out_name, len(data.get("elements", [])))
            return
        except Exception as e:  # try next mirror
            last = e
            log.warning("overpass endpoint failed: %s", e)
    raise RuntimeError(f"all Overpass endpoints failed for {out_name}: {last}")


def fetch_all(cfg: dict, force: bool = False) -> None:
    _run(cfg, Q_LINES, "osm_power_lines.json", force)
    _run(cfg, Q_POINTS, "osm_power_points.json", force)
    _run(cfg, Q_INDUSTRIAL, "osm_industrial.json", force)
