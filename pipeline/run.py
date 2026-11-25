"""Entry point.

  python -m pipeline.run                 # fetch (cached) + build
  python -m pipeline.run --force         # re-download everything
  python -m pipeline.run --steps build   # rebuild from cached raw data
  python -m pipeline.run --fixtures      # offline smoke test with synthetic data (never deploy)
"""
from __future__ import annotations

import argparse
import sys

from . import common
from .common import ensure_dirs, load_config, log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="fetch,build")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    steps = set(a.steps.split(","))

    if a.fixtures:
        from . import fixtures

        common.RAW = common.ROOT / "data" / "raw_fixture"
        cfg["_fixture"] = True
        fixtures.make(cfg)
        steps.discard("fetch")
    ensure_dirs()

    if "fetch" in steps:
        from . import fetch_arcgis, fetch_base, fetch_buildings, fetch_osm

        fetch_base.fetch_all(cfg, a.force)
        fetch_osm.fetch_all(cfg, a.force)
        failed = fetch_arcgis.fetch_all(cfg, a.force)
        try:
            fetch_buildings.fetch(cfg, a.force)
        except Exception as e:  # degrade gracefully: map shows buildings as missing
            log.error("buildings FAILED: %s", e)
        if failed:
            log.warning("continuing without LIO layers: %s", failed)
    if "build" in steps:
        from . import build

        build.build(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
