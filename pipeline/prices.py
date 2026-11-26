"""IESO day-ahead nodal LMPs -> a BESS value signal per station.

fetch(): downloads the last N days of PUB_DAHourlyEnergyLMP_YYYYMMDD.csv from IESO's public
         reports site (cached per day in data/raw/da_lmp/).
station_spreads(): per pricing node and day, the 4-hour arbitrage spread
         = mean(top-4 hourly LMPs) - mean(bottom-4 hourly LMPs); averaged over days, then the
         median across all nodes sharing a station prefix (e.g. CHERRYWOOD-LT.xxx -> CHERRYWOOD).
locate(): matches station prefixes to OSM substations/plants by name -> lat/lon points.
"""
from __future__ import annotations

import difflib
import io
import re
from datetime import date, timedelta

import pandas as pd

from . import common
from .common import http, log

BASE = "https://reports-public.ieso.ca/public/DAHourlyEnergyLMP/PUB_DAHourlyEnergyLMP_{d}.csv"

# station prefix clean-up: drop equipment / unit suffixes, expand common IESO abbreviations
_SUFFIX = r"(DESN\d*|BESS\d*|RSVC\d*|\d+KV|WF|CGS|PGS|GS|DS|TS|CTS|SS|HWB|TG|LF|\d+)$"
_ABBR = [(r"VLE$", "VILLE"), (r"VL$", "VILLE"), (r"NGTN", "NGTON"), (r"WATR", "WATER"), (r"MNT$", "MOUNT")]


def _days(cfg) -> list[date]:
    n = cfg.get("prices", {}).get("days", 60)
    today = date.today()
    return [today - timedelta(days=k) for k in range(0, n + 1)]


def fetch(cfg: dict, force: bool = False) -> None:
    d_dir = common.RAW / "da_lmp"
    d_dir.mkdir(parents=True, exist_ok=True)
    got = miss = 0
    for d in _days(cfg):
        out = d_dir / f"{d:%Y%m%d}.csv"
        if out.exists() and not force:
            got += 1
            continue
        try:
            r = http("GET", BASE.format(d=f"{d:%Y%m%d}"), timeout=60, retries=1)
            out.write_bytes(r.content)
            got += 1
        except Exception:
            miss += 1
    log.info("lmp day-ahead files: %d available, %d missing", got, miss)


def _read_day(path) -> pd.DataFrame:
    txt = path.read_text(errors="ignore")
    lines = txt.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Delivery Hour"))
    return pd.read_csv(io.StringIO("\n".join(lines[start:])))


def station_spreads(cfg: dict, files=None) -> pd.DataFrame:
    files = files if files is not None else sorted((common.RAW / "da_lmp").glob("*.csv"))
    per = []
    for p in files:
        try:
            df = _read_day(p)
        except Exception as e:
            log.warning("lmp: skip %s (%s)", p.name, e)
            continue
        df = df.rename(columns={"Pricing Location": "node", "Delivery Hour": "hour"})
        df = df[~df.node.str.contains(":PZ", regex=False)]
        g = df.groupby("node").LMP
        top = g.apply(lambda s: s.nlargest(4).mean())
        bot = g.apply(lambda s: s.nsmallest(4).mean())
        per.append((top - bot).rename(p.stem))
    if not per:
        return pd.DataFrame()
    node = pd.concat(per, axis=1)
    out = pd.DataFrame({"spread": node.mean(axis=1), "days": node.notna().sum(axis=1)})
    out["station"] = out.index.str.extract(r"^([A-Z0-9_]+?)(?:-|:)", expand=False)
    out = out.dropna(subset=["station"])
    st = out.groupby("station").agg(spread=("spread", "median"), nodes=("spread", "size"), days=("days", "max")).reset_index()
    log.info("lmp: %d nodes over %d days -> %d station prefixes", len(out), len(per), len(st))
    return st


def _clean(prefix: str) -> str:
    s = prefix.upper().replace("_", "")
    for _ in range(2):
        s = re.sub(_SUFFIX, "", s)
    for a, b in _ABBR:
        s = re.sub(a, b, s)
    return s.lower()


def _core(name: str) -> str:
    s = re.sub(r"\b(hydro one|transformer station|distribution station|switching station|generating station|"
               r"wind farm|wind energy centre|solar|ts|ds|mts|ss|cts|gs|tps|desn\s*\d+)\b", " ", str(name).lower())
    return re.sub(r"[^a-z0-9]", "", s)


def locate(st: pd.DataFrame, places: pd.DataFrame) -> pd.DataFrame:
    """places: name, lat, lon, kv (OSM substations + plants in scope). Returns matched stations."""
    if st.empty or places.empty:
        return pd.DataFrame(columns=["name", "lat", "lon", "metric", "osm_name", "how"])
    pl = places[places["name"].astype(str).str.len() > 0].copy()
    pl["core"] = pl["name"].map(_core)
    pl = pl[pl.core.str.len() >= 3].sort_values("kv", ascending=False, na_position="last").drop_duplicates("core")
    cores = list(pl.core)
    idx = pl.set_index("core")
    rows = []
    for r in st.itertuples():
        q = _clean(r.station)
        if len(q) < 3:
            continue
        hit, how = None, ""
        if q in idx.index:
            hit, how = q, "exact"
        else:
            pre = [c for c in cores if c.startswith(q) and len(c) - len(q) <= 2] if len(q) >= 5 else []
            if len(pre) == 1:
                hit, how = pre[0], "prefix"
            else:
                cm = [c for c in difflib.get_close_matches(q, cores, n=3, cutoff=0.88) if c[0] == q[0]]
                if cm:
                    hit, how = cm[0], "fuzzy"
        if hit:
            o = idx.loc[hit]
            rows.append({"name": r.station, "lat": o.lat, "lon": o.lon, "metric": round(float(r.spread), 2),
                         "osm_name": o["name"], "how": how})
    out = pd.DataFrame(rows)
    log.info("lmp: located %d of %d station prefixes (%s)", len(out), len(st),
             out.how.value_counts().to_dict() if len(out) else {})
    return out
