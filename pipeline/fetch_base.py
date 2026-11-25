"""Natural Earth province boundary + lakes (for the land mask)."""
from __future__ import annotations

from .common import RAW, http, log


def fetch_all(cfg: dict, force: bool = False) -> None:
    ne = cfg["natural_earth"]
    for name in [ne["provinces"], *ne["lakes"]]:
        out = RAW / f"{name}.geojson"
        if out.exists() and not force:
            log.info("ne  %-22s cached", name)
            continue
        r = http("GET", f"{ne['base_url']}/{name}.geojson", timeout=300)
        out.write_bytes(r.content)
        log.info("ne  %-22s saved (%.1f MB)", name, len(r.content) / 1e6)
