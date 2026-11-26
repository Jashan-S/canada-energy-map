"""Hydro One "List of Station Capacity": generation connection room at distribution level.

The PDF lists, for every Hydro One-owned station bus, the estimated thermal and short-circuit
capacity for new distribution-connected generation (incl. exporting BESS), *before* subtracting
existing/allocated projects. "TC" = transmission-constrained (no exporting DER).

  fetch()  – download the latest PDF and parse it to data/raw/hydroone_lsc.csv (falls back to the
             committed copy in data/manual/ if Hydro One stops publishing it)
  sites()  – one row per station site: best single-bus MW, station total MW, TC flag
  match()  – attach those values to OSM substations by normalized name
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import common
from .common import ROOT, http, log

URL = "https://www.hydroone.com/businessservices_/generators_/Documents/honi_lsc.pdf"
COLS = ["station", "bus", "feeders", "kv", "min_load", "sc_mva", "thermal_mw", "upstream_ts", "upstream_feeder"]
SC_PER_MW = 1.2   # inverter short-circuit contribution, MVA per MW (assumption)
TYPES = {"ts", "ds", "mts", "pds", "cts", "ss", "hvds", "tps"}


# ------------------------------------------------------------------ fetch / parse
def parse_pdf(path: Path) -> list[list[str]]:
    import pdfplumber

    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for r in table:
                    if not r or len(r) != 9 or not r[0] or r[0].startswith("Station"):
                        continue
                    rows.append([(c or "").replace("\n", " ").strip() for c in r])
    return rows


def fetch(cfg: dict, force: bool = False) -> None:
    out = common.RAW / "hydroone_lsc.csv"
    if out.exists() and not force:
        log.info("cap hydroone_lsc.csv         cached")
        return
    try:
        pdf = common.RAW / "honi_lsc.pdf"
        pdf.write_bytes(http("GET", URL, timeout=120, retries=2).content)
        rows = parse_pdf(pdf)
        if len(rows) < 500:
            raise RuntimeError(f"only {len(rows)} rows parsed; layout may have changed")
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            w.writerows(rows)
        log.info("cap parsed %d station-bus rows from Hydro One PDF", len(rows))
    except Exception as e:
        log.warning("cap download/parse failed (%s); using committed copy in data/manual/", e)


def _source(cfg: dict) -> Path | None:
    for p in (common.RAW / "hydroone_lsc.csv", ROOT / cfg["capacity"]["fallback_csv"]):
        if p.exists():
            return p
    return None


# ------------------------------------------------------------------ station sites
def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def sites(cfg: dict) -> pd.DataFrame:
    src = _source(cfg)
    if src is None:
        return pd.DataFrame()
    df = pd.read_csv(src, dtype=str).fillna("")
    recs = []
    for r in df.itertuples():
        th, sc = _num(r.thermal_mw), _num(r.sc_mva)
        tc = r.thermal_mw.strip().upper() == "TC"
        if th is None and not tc:
            continue   # "Sum of Buses" / N/A rows: recomputed below
        eff = 0.0 if tc else th if sc is None else min(th, sc / SC_PER_MW)
        name = re.sub(r"\s+", " ", r.station.strip())
        recs.append({"desn": name, "site": re.sub(r"\s+(DESN\s*\d+|#\s*\d+)$", "", name, flags=re.I),
                     "is_total": r.bus.strip().lower() == "total", "eff": eff, "tc": tc})
    b = pd.DataFrame(recs)
    out = []
    for site, g in b.groupby("site"):
        tot, best = 0.0, 0.0
        for _, d in g.groupby("desn"):
            buses, totals = d[~d.is_total], d[d.is_total]
            tot += totals.eff.sum() if len(totals) else buses.eff.sum()
            best = max(best, buses.eff.max() if len(buses) else totals.eff.max())
        out.append({"site": site, "best_mw": round(best, 1), "total_mw": round(tot, 1), "tc": bool(g.tc.any())})
    s = pd.DataFrame(out)
    s["key"], s["core"] = zip(*s.site.map(norm))
    log.info("cap: %d Hydro One station sites (%d TC) from %s", len(s), int(s.tc.sum()), src.name)
    return s


# ------------------------------------------------------------------ name matching
_REPL = [
    (r"\bhydro one( networks)?( inc\.?)?\b", " "),
    (r"\bmunicipal transformer station\b", " mts "),
    (r"\btransformer station\b", " ts "),
    (r"\bdistribution station\b", " ds "),
    (r"\bswitching station\b", " ss "),
    (r"\bdesn\s*\d+\b", " "),
    (r"#\s*\d+", " "),
    (r"\bst\.", "st"),
    (r"[^a-z0-9 ]", " "),
]


def norm(name: str) -> tuple[str, str]:
    """'Hydro One Agincourt Transformer Station' -> ('agincourt ts', 'agincourt')"""
    s = f" {str(name).lower()} "
    for a, b in _REPL:
        s = re.sub(a, b, s)
    toks = s.split()
    typ = toks[-1] if toks and toks[-1] in TYPES else ""
    core = " ".join(toks[:-1] if typ else toks)
    return (f"{core} {typ}".strip(), core)


def match(subs: pd.DataFrame, s: pd.DataFrame) -> pd.DataFrame:
    """Add cap_best, cap_total, cap_tc, cap_site to OSM substations (NaN when unmatched)."""
    subs = subs.copy()
    for c in ("cap_best", "cap_total"):
        subs[c] = np.nan
    subs["cap_tc"], subs["cap_site"] = False, ""
    if s.empty:
        return subs
    by_key = s.set_index("key")
    core_counts = s.core.value_counts()
    by_core = s[s.core.map(core_counts) == 1].set_index("core")
    hits = 0
    for i, nm in zip(subs.index, subs["name"]):
        if not nm:
            continue
        key, core = norm(nm)
        row = None
        if key in by_key.index:
            row = by_key.loc[key]
        elif core and core in by_core.index and not key.endswith((" ss", " cts")):
            row = by_core.loc[core]
        if row is None:
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        subs.loc[i, ["cap_best", "cap_total", "cap_tc", "cap_site"]] = [row.best_mw, row.total_mw, bool(row.tc), row.site]
        hits += 1
    log.info("cap: matched %d OSM substations to %d of %d Hydro One sites", hits,
             subs.loc[subs.cap_site != "", "cap_site"].nunique(), len(s))
    return subs
