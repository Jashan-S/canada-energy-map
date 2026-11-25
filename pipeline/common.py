"""Shared helpers: config, paths, HTTP with retries, logging."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
WORK = ROOT / "data" / "work"
WEB_DATA = ROOT / "web" / "data"

log = logging.getLogger("siting")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

UA = {"User-Agent": "ontario-energy-siting/0.1 (public-data screening tool)"}


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def ensure_dirs() -> None:
    for d in (RAW, WORK, WEB_DATA):
        d.mkdir(parents=True, exist_ok=True)


def http(method: str, url: str, *, retries: int = 4, timeout: int = 120, **kw) -> requests.Response:
    """HTTP call with exponential backoff. Raises on final failure."""
    kw.setdefault("headers", {}).update(UA)
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # noqa: PERF203
            last = e
            wait = 5 * 2**attempt
            log.warning("%s %s failed (%s); retry in %ss", method, url[:90], e, wait)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {last}")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
