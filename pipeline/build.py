"""Turn raw downloads into the web map's data files.

  1. Southern Ontario land mask  -> H3 hex grid
  2. Per-hex features: grid distances, load proxy, land constraints, soil class, municipality
  3. Export: web/data/hexes.json (columnar), lines/substations/plants.geojson, meta.json
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
from shapely import STRtree
from shapely.geometry import LineString, Point, box, mapping
from shapely.ops import unary_union

from . import common
from .common import ROOT, log, write_json

WGS = "EPSG:4326"

# Bit flags stored per hex (keep in sync with web/app.js FLAGS)
F_PROTECTED, F_PSW, F_WETLAND, F_GREENBELT, F_SPECIALTY, F_ORM, F_NEP, F_RESERVE, F_BUILTUP, F_NEED = (
    1 << i for i in range(10)
)


# ---------------------------------------------------------------- helpers
def _raw(name: str) -> Path:
    return common.RAW / name


def _kv(v: str | None) -> float:
    """'230000;115000' -> 230.0 ; '27.6 kV' -> 27.6 ; None -> nan"""
    if not v:
        return np.nan
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(v))]
    if not nums:
        return np.nan
    kv = [n / 1000 if n >= 1000 else n for n in nums]
    return max(kv)


def _mw(v: str | None) -> float:
    if not v:
        return np.nan
    m = re.search(r"(\d+(?:\.\d+)?)\s*(gw|mw|kw|w)?", str(v).lower())
    if not m:
        return np.nan
    x = float(m.group(1))
    unit = m.group(2) or "mw"
    return x * {"gw": 1000, "mw": 1, "kw": 1e-3, "w": 1e-6}[unit]


def _read_lio(key: str) -> gpd.GeoDataFrame | None:
    p = _raw(f"lio_{key}.geojson")
    if not p.exists():
        return None
    g = gpd.read_file(p)
    if g.empty:
        return None
    g = g.set_crs(WGS, allow_override=True)
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    if g.geom_type.isin(["Polygon", "MultiPolygon"]).any():
        g["geometry"] = g.geometry.make_valid()
    return g


def _nearest_km(pts: gpd.GeoSeries, targets: gpd.GeoSeries) -> np.ndarray:
    if targets is None or len(targets) == 0:
        return np.full(len(pts), np.nan)
    tree = STRtree(targets.values)
    _, dist = tree.query_nearest(pts.values, return_distance=True, all_matches=False)
    return dist / 1000.0


def _within(pts: gpd.GeoDataFrame, polys: gpd.GeoDataFrame | None) -> np.ndarray:
    """Boolean per point: inside any polygon."""
    if polys is None or polys.empty:
        return np.zeros(len(pts), bool)
    polys = polys[polys.geom_type.isin(["Polygon", "MultiPolygon"])]
    j = gpd.sjoin(pts[["geometry"]], polys[["geometry"]], how="inner", predicate="within")
    out = np.zeros(len(pts), bool)
    out[np.unique(j.index.values)] = True
    return out


def _join_attr(pts: gpd.GeoDataFrame, polys: gpd.GeoDataFrame, col: str) -> pd.Series:
    j = gpd.sjoin(pts[["geometry"]], polys[[col, "geometry"]], how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    return j[col].reindex(pts.index)


# ---------------------------------------------------------------- steps
def jurisdictions(cfg: dict) -> gpd.GeoDataFrame:
    """Province/state polygons touching the download box, with ISO 3166-2 code + scored flag."""
    ne = cfg["natural_earth"]
    prov = gpd.read_file(_raw(f"{ne['provinces']}.geojson"))
    region = box(*cfg["region"]["bbox"]).buffer(1.0)
    j = prov[prov.intersects(region)][["iso_3166_2", "name", "geometry"]].rename(columns={"iso_3166_2": "j"})
    j["scored"] = j.j.isin(cfg["jurisdictions"]["score"])
    if not j.scored.any():
        raise SystemExit(f"no Natural Earth polygon matches jurisdictions.score={cfg['jurisdictions']['score']}")
    return j.reset_index(drop=True)


def land_mask(cfg: dict, juris: gpd.GeoDataFrame):
    ne = cfg["natural_earth"]
    region = box(*cfg["region"]["bbox"])
    land = unary_union(juris[juris.scored].geometry.values).intersection(region)
    lakes = []
    for name in ne["lakes"]:
        lk = gpd.read_file(_raw(f"{name}.geojson"))
        lk = lk[lk.intersects(region)]
        lakes.extend(lk.geometry.make_valid().values)
    if lakes:
        land = land.difference(unary_union(lakes))
    log.info("land mask area ≈ %.0f km²", gpd.GeoSeries([land], crs=WGS).to_crs(cfg["projection"]).area.iloc[0] / 1e6)
    return land


def hex_grid(land, res: int, proj: str) -> gpd.GeoDataFrame:
    cells = sorted(h3.geo_to_cells(mapping(land), res))
    latlon = np.array([h3.cell_to_latlng(c) for c in cells])
    g = gpd.GeoDataFrame(
        {"h3": cells, "lat": latlon[:, 0], "lon": latlon[:, 1]},
        geometry=gpd.points_from_xy(latlon[:, 1], latlon[:, 0]),
        crs=WGS,
    )
    log.info("hex grid: %d cells at res %d", len(g), res)
    return g, g.to_crs(proj)


def tag_jurisdiction(gdf: gpd.GeoDataFrame, juris: gpd.GeoDataFrame, proj: str) -> gpd.GeoDataFrame:
    """Add j (ISO code at the feature's midpoint) and scope (touches a scored jurisdiction, 1 km buffer)."""
    if gdf.empty:
        gdf["j"], gdf["scope"] = [], []
        return gdf
    mid = gdf.geometry.apply(lambda g: g.interpolate(0.5, normalized=True) if g.geom_type == "LineString" else g.representative_point())
    pts = gpd.GeoDataFrame(geometry=mid, crs=WGS)
    gdf["j"] = _join_attr(pts, juris, "j").fillna("").values
    scored = gpd.GeoSeries([unary_union(juris[juris.scored].geometry.values)], crs=WGS).to_crs(proj).buffer(1000).to_crs(WGS).iloc[0]
    gdf["scope"] = gdf.geometry.intersects(scored).values
    return gdf


def load_osm(cfg: dict):
    hv = cfg["features"]["hv_kv"]
    # lines
    lines = []
    for el in json.loads(_raw("osm_power_lines.json").read_text())["elements"]:
        geom = el.get("geometry")
        if el.get("type") != "way" or not geom or len(geom) < 2:
            continue
        t = el.get("tags", {})
        lines.append(
            {
                "power": t.get("power"),
                "kv": _kv(t.get("voltage")),
                "name": t.get("name") or t.get("ref") or "",
                "operator": t.get("operator", ""),
                "geometry": LineString([(p["lon"], p["lat"]) for p in geom]),
            }
        )
    lines = gpd.GeoDataFrame(lines, crs=WGS)
    lines["kv230"] = lines["kv"] >= cfg["features"]["dc_kv"]
    # transmission if tagged >= hv, or tagged power=line with no voltage (OSM convention: 'line' = HV-ish)
    lines["hv"] = (lines["kv"] >= hv) | ((lines["power"] == "line") & lines["kv"].isna())

    pts = []
    for el in json.loads(_raw("osm_power_points.json").read_text())["elements"]:
        c = el if el.get("type") == "node" else el.get("center")
        if not c or "lat" not in c:
            continue
        t = el.get("tags", {})
        pts.append(
            {
                "kind": t.get("power"),
                "name": t.get("name", ""),
                "operator": t.get("operator", ""),
                "kv": _kv(t.get("voltage")),
                "source": t.get("plant:source", ""),
                "mw": _mw(t.get("plant:output:electricity")),
                "geometry": Point(c["lon"], c["lat"]),
            }
        )
    pts = gpd.GeoDataFrame(pts, crs=WGS)

    ind = []
    for el in json.loads(_raw("osm_industrial.json").read_text())["elements"]:
        b = el.get("bounds")
        if not b:
            continue
        c = {"lat": (b["minlat"] + b["maxlat"]) / 2, "lon": (b["minlon"] + b["maxlon"]) / 2}
        dy = (b["maxlat"] - b["minlat"]) * 111.0
        dx = (b["maxlon"] - b["minlon"]) * 111.0 * np.cos(np.radians(c["lat"]))
        ind.append({"area_ha": min(dx * dy * 100 * 0.6, 2000), "lat": c["lat"], "lon": c["lon"]})
    ind = pd.DataFrame(ind)
    log.info("osm: %d lines (%d HV), %d substations, %d plants, %d industrial", len(lines), int(lines.hv.sum()),
             int((pts.kind == "substation").sum()), int((pts.kind == "plant").sum()), len(ind))
    return lines, pts, ind


def industrial_load(cells: list[str], ind: pd.DataFrame, res: int, radius_km: float) -> np.ndarray:
    """Hectares of industrial land within ~radius of each hex (H3 ring sum)."""
    if ind.empty:
        return np.zeros(len(cells))
    agg: dict[str, float] = {}
    for lat, lon, a in ind[["lat", "lon", "area_ha"]].itertuples(index=False):
        c = h3.latlng_to_cell(lat, lon, res)
        agg[c] = agg.get(c, 0.0) + a
    spacing_km = h3.average_hexagon_edge_length(res, "km") * np.sqrt(3)
    k = max(1, int(round(radius_km / spacing_km)))
    tot: dict[str, float] = {}
    for c, a in agg.items():
        for n in h3.grid_disk(c, k):
            tot[n] = tot.get(n, 0.0) + a
    return np.array([tot.get(c, 0.0) for c in cells])


def _is_str(s: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _pick_col(df: pd.DataFrame, prefs: list[str], contains: str) -> str | None:
    cols = {c.upper(): c for c in df.columns}
    for p in prefs:
        if p.upper() in cols:
            return cols[p.upper()]
    for c in df.columns:
        if contains in c.upper() and _is_str(df[c]):
            return c
    return None


def _cli_class(v) -> int:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0
    s = str(v).strip().upper()
    if not s:
        return 0
    if s[0] == "O":
        return 8  # organic soil (e.g. Holland Marsh)
    return int(s[0]) if s[0].isdigit() and 1 <= int(s[0]) <= 7 else 0


def build(cfg: dict) -> dict:
    proj = cfg["projection"]
    res = cfg["region"]["h3_resolution"]
    fcfg = cfg["features"]
    missing = []

    juris = jurisdictions(cfg)
    land = land_mask(cfg, juris)
    hexw, hexp = hex_grid(land, res, proj)
    n = len(hexp)

    # ---- grid (only infrastructure in scored jurisdictions counts toward scores)
    lines, pts, ind = load_osm(cfg)
    lines = tag_jurisdiction(lines, juris, proj)
    pts = tag_jurisdiction(pts, juris, proj)
    lines_p, pts_p = lines.to_crs(proj), pts.to_crs(proj)
    lines_s = lines_p[lines_p.scope]
    hv_lines = lines_s[lines_s.hv]
    l230 = lines_s[lines_s.kv230]
    subs_all = pts_p[pts_p.kind == "substation"].copy()
    near_hv = _nearest_km(subs_all.geometry, lines_p[lines_p.hv].geometry) * 1000
    near_230 = _nearest_km(subs_all.geometry, lines_p[lines_p.kv230].geometry) * 1000
    snap = fcfg["substation_hv_snap_m"]
    subs_all["hv"] = (subs_all.kv >= fcfg["hv_kv"]) | (near_hv <= snap)
    subs_all["kv230"] = (subs_all.kv >= fcfg["dc_kv"]) | (near_230 <= snap)
    subs = subs_all[subs_all.scope]
    plants = pts_p[(pts_p.kind == "plant") & pts_p.scope].copy()
    ind = ind[_within(gpd.GeoDataFrame(geometry=gpd.points_from_xy(ind.lon, ind.lat), crs=WGS),
                      juris[juris.scored])] if len(ind) else ind
    log.info("in-scope: %d/%d lines, %d/%d substations (%d HV, %d >=%d kV), %d industrial",
             len(lines_s), len(lines_p), len(subs), len(subs_all), int(subs.hv.sum()), int(subs.kv230.sum()),
             fcfg["dc_kv"], len(ind))

    feat = pd.DataFrame({"h3": hexw.h3})
    feat["d_sub"] = _nearest_km(hexp.geometry, subs.geometry)
    feat["d_sub_hv"] = _nearest_km(hexp.geometry, subs[subs.hv].geometry)
    feat["d_line_hv"] = _nearest_km(hexp.geometry, hv_lines.geometry)
    feat["d_line"] = _nearest_km(hexp.geometry, lines_s.geometry)
    feat["d_sub_230"] = _nearest_km(hexp.geometry, subs[subs.kv230].geometry)
    feat["d_line_230"] = _nearest_km(hexp.geometry, l230.geometry)
    feat["d_plant"] = _nearest_km(hexp.geometry, plants.geometry)
    feat["ind_ha"] = industrial_load(list(hexw.h3), ind, res, fcfg["load_radius_km"])

    # nearest HV substation name/kv for the detail panel
    if len(subs[subs.hv]):
        hvs = subs[subs.hv].reset_index(drop=True)
        idx = STRtree(hvs.geometry.values).query_nearest(hexp.geometry.values, all_matches=False)[1]
        feat["sub_hv_idx"] = idx
        k230 = np.flatnonzero(hvs.kv230.values)
        if len(k230):
            j = STRtree(hvs.geometry.values[k230]).query_nearest(hexp.geometry.values, all_matches=False)[1]
            feat["sub_230_idx"] = k230[j]
        else:
            feat["sub_230_idx"] = -1
    else:
        hvs = subs.iloc[0:0]
        feat["sub_hv_idx"] = -1
        feat["sub_230_idx"] = -1

    # ---- buildings (coverage % of hex footprint, count)
    bpath = common.RAW / f"buildings_h3r{res}.json"
    if bpath.exists():
        bl = json.loads(bpath.read_text())
        area = np.array([h3.cell_area(c, "m^2") for c in feat.h3])
        ba = np.array([bl.get(c, (0, 0))[0] for c in feat.h3], dtype=float)
        feat["bldg_cov"] = np.minimum(100, ba / area * 100)
        feat["bldg_n"] = [bl.get(c, (0, 0))[1] for c in feat.h3]
    else:
        missing.append("buildings")
        feat["bldg_cov"] = np.nan
        feat["bldg_n"] = -1

    # ---- land constraints
    flags = np.zeros(n, dtype=np.int32)

    def flag(keys, bit):
        nonlocal flags
        hit = np.zeros(n, bool)
        for k in keys:
            g = _read_lio(k)
            if g is None:
                missing.append(k)
                continue
            hit |= _within(hexw, g)
        flags |= np.where(hit, bit, 0).astype(np.int32)
        return hit

    flag(["provincial_park", "conservation_reserve", "national_wildlife", "federal_protected"], F_PROTECTED)
    flag(["greenbelt"], F_GREENBELT)
    flag(["specialty_crop"], F_SPECIALTY)
    flag(["oak_ridges_moraine"], F_ORM)
    flag(["niagara_escarpment"], F_NEP)
    flag(["first_nation_reserve"], F_RESERVE)
    flag(["built_up"], F_BUILTUP)

    wet = _read_lio("wetland")
    if wet is None:
        missing.append("wetland")
    else:
        strcols = [c for c in wet.columns if c != "geometry" and _is_str(wet[c])]
        sig = pd.Series(False, index=wet.index)
        for c in strcols:
            s = wet[c].astype(str).str.strip().str.lower()
            sig |= s.str.startswith("provincially significant") | (s == "psw")
        psw_hit = _within(hexw, wet[sig])
        any_hit = _within(hexw, wet)
        flags |= np.where(psw_hit, F_PSW, 0).astype(np.int32)
        flags |= np.where(any_hit & ~psw_hit, F_WETLAND, 0).astype(np.int32)
        log.info("wetlands: %d polygons, %d PSW", len(wet), int(sig.sum()))

    # optional: grid-need polygons (hand-drawn from IESO regional plans)
    gn_path = ROOT / cfg["manual"]["grid_needs"]
    need_label = np.array([""] * n, dtype=object)
    if gn_path.exists():
        gn = gpd.read_file(gn_path)
        if not gn.empty:
            gn = gn.set_crs(WGS, allow_override=True)
            col = "need" if "need" in gn.columns else gn.columns[0]
            lab = _join_attr(hexw, gn, col).fillna("")
            need_label = lab.astype(str).values
            flags |= np.where(need_label != "", F_NEED, 0).astype(np.int32)
    feat["flags"] = flags

    # soil capability (CLI) – proxy for prime agricultural land (classes 1-3)
    soil = _read_lio("soil")
    if soil is None:
        missing.append("soil")
        feat["cli"] = 0
    else:
        col = _pick_col(soil, ["CLI1", "CLI_1", "CLI", "CLI_CLASS", "CLI1_CLASS"], "CLI")
        if col is None:
            log.warning("soil: no CLI column found in %s", list(soil.columns))
            feat["cli"] = 0
        else:
            log.info("soil: using column %s", col)
            feat["cli"] = _join_attr(hexw, soil, col).map(_cli_class).fillna(0).astype(int).values

    # airports
    ap = _read_lio("airport")
    if ap is None:
        missing.append("airport")
        feat["d_airport"] = np.nan
    else:
        feat["d_airport"] = _nearest_km(hexp.geometry, ap.to_crs(proj).geometry)

    # municipality
    muni = _read_lio("municipality")
    munis: list[dict] = []
    if muni is None:
        missing.append("municipality")
        feat["muni"] = -1
    else:
        col = _pick_col(
            muni,
            ["MUNICIPAL_NAME_SHORTFORM", "MUNICIPAL_NAME", "OFFICIAL_MUNICIPAL_NAME", "LEGAL_NAME", "NAME"],
            "NAME",
        )
        names = _join_attr(hexw, muni, col).fillna("").astype(str).str.strip().str.title()
        uniq = sorted(set(names) - {""})
        lut = {nm: i for i, nm in enumerate(uniq)}
        feat["muni"] = names.map(lambda x: lut.get(x, -1)).values
        stance = _read_stance(ROOT / cfg["manual"]["municipal_stance"])
        munis = [{"name": nm, "stance": stance.get(nm.lower(), "")} for nm in uniq]

    # optional price metric from nodes (e.g. your LMP spread analysis)
    feat["price"] = np.nan
    pn_path = ROOT / cfg["manual"]["price_nodes"]
    price_meta = None
    if pn_path.exists():
        pn = pd.read_csv(pn_path, comment="#")
        if len(pn):
            pg = gpd.GeoDataFrame(pn, geometry=gpd.points_from_xy(pn.lon, pn.lat), crs=WGS).to_crs(proj)
            tree = STRtree(pg.geometry.values)
            i, d = tree.query_nearest(hexp.geometry.values, return_distance=True, all_matches=False)
            vals = pg["metric"].values[i[1]]
            vals = np.where(d / 1000 <= fcfg["price_node_max_km"], vals, np.nan)
            feat["price"] = vals
            price_meta = {"nodes": int(len(pn)), "min": float(np.nanmin(pn.metric)), "max": float(np.nanmax(pn.metric)),
                          "label": str(pn.columns[pn.columns.get_loc("metric")])}

    # ---- export
    def col(name, nd=1):
        a = feat[name].astype(float).values
        return [None if np.isnan(v) else round(float(v), nd) for v in a]

    hexes = {
        "res": res,
        "n": n,
        "cols": {
            "h3": list(feat.h3),
            "d_sub": col("d_sub"),
            "d_sub_hv": col("d_sub_hv"),
            "d_line": col("d_line"),
            "d_line_hv": col("d_line_hv"),
            "d_plant": col("d_plant"),
            "d_sub_230": col("d_sub_230"),
            "d_line_230": col("d_line_230"),
            "bldg_cov": col("bldg_cov"),
            "bldg_n": [int(x) for x in feat["bldg_n"]],
            "d_airport": col("d_airport"),
            "ind_ha": col("ind_ha", 0),
            "price": col("price", 2),
            "flags": [int(x) for x in feat["flags"]],
            "cli": [int(x) for x in feat.cli],
            "muni": [int(x) for x in feat.muni],
            "sub_hv": [int(x) for x in feat.sub_hv_idx],
            "sub_230": [int(x) for x in feat.sub_230_idx],
            "need": [str(x) for x in need_label] if (need_label != "").any() else None,
        },
        "munis": munis,
        "hv_subs": [
            {"name": r.name or "(unnamed)", "kv": None if np.isnan(r.kv) else r.kv}
            for r in hvs.to_crs(WGS).itertuples()
        ],
    }
    write_json(common.WEB_DATA / "hexes.json", hexes)
    _export_overlays(lines, pts, subs_all.to_crs(WGS))

    meta = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "region": cfg["region"],
        "jurisdictions": cfg["jurisdictions"]["score"],
        "hex_count": n,
        "counts": {
            "lines": int(len(lines_s)), "hv_lines": int(len(hv_lines)), "substations": int(len(subs)),
            "hv_substations": int(subs.hv.sum()), "kv230_substations": int(subs.kv230.sum()),
            "plants": int(len(plants)), "industrial": int(len(ind)),
            "outside_scope": {"lines": int((~lines.scope).sum()), "substations": int((~subs_all.scope).sum())},
        },
        "missing_layers": sorted(set(missing)),
        "price": price_meta,
        "fixture": bool(cfg.get("_fixture")),
    }
    write_json(common.WEB_DATA / "meta.json", meta)
    log.info("export done: %s", meta)
    return meta


def _read_stance(path: Path) -> dict:
    out = {}
    if path.exists():
        with open(path) as f:
            for row in csv.DictReader(r for r in f if not r.startswith("#")):
                if row.get("municipality"):
                    out[row["municipality"].strip().lower()] = (row.get("stance") or "").strip().lower()
    return out


def _export_overlays(lines, pts, subs):
    def fc(gdf, props):
        feats = []
        for r in gdf.itertuples():
            p = {}
            for k in props:
                v = getattr(r, k)
                if isinstance(v, float) and np.isnan(v):
                    v = None
                elif isinstance(v, (np.bool_, bool)):
                    v = bool(v)
                elif isinstance(v, np.floating):
                    v = round(float(v), 1)
                p[k] = v
            feats.append({"type": "Feature", "properties": p, "geometry": mapping(r.geometry)})
        return {"type": "FeatureCollection", "features": feats}

    tl = lines[lines.hv | (lines.kv >= 27)].copy()   # skip low-voltage clutter
    tl["geometry"] = tl.geometry.simplify(0.0002)
    tl = tl.round({"kv": 0})
    write_json(common.WEB_DATA / "lines.geojson", _round_coords(fc(tl, ["kv", "name", "operator", "hv", "j", "scope"])))
    write_json(common.WEB_DATA / "substations.geojson", _round_coords(fc(subs, ["name", "kv", "operator", "hv", "j", "scope"])))
    pl = pts[pts.kind == "plant"]
    write_json(common.WEB_DATA / "plants.geojson", _round_coords(fc(pl, ["name", "source", "mw", "operator", "j", "scope"])))


def _round_coords(fc_obj, nd=5):
    def rnd(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rnd(x) for x in c]

    for f in fc_obj["features"]:
        f["geometry"]["coordinates"] = rnd(f["geometry"]["coordinates"])
    return fc_obj
