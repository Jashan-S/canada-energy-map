"""Download polygon/point layers from Land Information Ontario's open ArcGIS MapServers.

Layers are looked up by name (not id), then fetched in object-id chunks so we never
hit the server's max-record limit. Output: data/raw/lio_<key>.geojson
"""
from __future__ import annotations

import json

from .common import RAW, http, log


def _layer_id(base: str, service: str, name: str) -> int:
    meta = http("GET", f"{base}/{service}/MapServer", params={"f": "json"}).json()
    layers = meta.get("layers", [])
    want = name.strip().lower()
    for lyr in layers:
        if lyr["name"].strip().lower() == want:
            return lyr["id"]
    for lyr in layers:  # fallback: contains
        if want in lyr["name"].strip().lower():
            log.warning("layer '%s' matched loosely to '%s'", name, lyr["name"])
            return lyr["id"]
    names = ", ".join(l["name"] for l in layers)
    raise KeyError(f"layer '{name}' not found in {service}. Available: {names}")


def fetch_layer(cfg: dict, key: str, force: bool = False) -> None:
    lio = cfg["lio"]
    spec = lio["layers"][key]
    out = RAW / f"lio_{key}.geojson"
    if out.exists() and not force:
        log.info("lio %-22s cached", key)
        return
    base, svc = lio["base_url"], spec["service"]
    lid = _layer_id(base, svc, spec["name"])
    url = f"{base}/{svc}/MapServer/{lid}/query"
    x0, y0, x1, y1 = cfg["region"]["bbox"]
    common = {
        "geometry": f"{x0},{y0},{x1},{y1}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": spec.get("where", "1=1"),
    }
    ids = http("POST", url, data={**common, "returnIdsOnly": "true", "f": "json"}).json().get("objectIds") or []
    ids.sort()
    log.info("lio %-22s %s/%s id=%s -> %d features", key, svc, spec["name"], lid, len(ids))

    feats = []
    step = lio.get("chunk", 500)
    for i in range(0, len(ids), step):
        chunk = ids[i : i + step]
        r = http(
            "POST",
            url,
            timeout=300,
            data={
                "objectIds": ",".join(map(str, chunk)),
                "outFields": spec.get("fields", "*"),
                "outSR": 4326,
                "maxAllowableOffset": lio.get("generalize_deg", 0),
                "returnGeometry": "true",
                "f": "geojson",
            },
        ).json()
        if "error" in r:
            raise RuntimeError(f"{key}: server error {r['error']}")
        feats.extend(f for f in r.get("features", []) if f.get("geometry"))
        if (i // step) % 20 == 0 and i:
            log.info("   %s: %d/%d", key, i + len(chunk), len(ids))
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    log.info("lio %-22s saved %d features", key, len(feats))


def fetch_all(cfg: dict, force: bool = False) -> list[str]:
    failed = []
    for key in cfg["lio"]["layers"]:
        try:
            fetch_layer(cfg, key, force)
        except Exception as e:  # keep going; a missing layer degrades, not breaks, the build
            log.error("lio %s FAILED: %s", key, e)
            failed.append(key)
    return failed
