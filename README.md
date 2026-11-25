# Ontario Energy Siting Screener

Map-based screening of Southern Ontario for **battery energy storage (BESS)** and **data-centre** sites, built only from public data.
Every ~0.74 km² hexagon (H3 resolution 8, ~160k cells) gets a 0–100 score from grid access, land suitability, nearby load or market, and an optional price signal. Weights, exclusions, building limits and project type can all be changed live in the browser.

**Hexagon states:** coloured = at or above your score cutoff · faint = below the cutoff (still clickable) · grey = excluded (park, provincially significant wetland, specialty crop, reserve, too built up, or anything you've ticked under Exclude; hover to see why).

> Screening only. Before acting on a site, confirm capacity with the utility (Hydro One Station Capacity Calculator / OEB Centralized Capacity Information Map), the land-use designation in the municipal Official Plan, and who owns the parcel.

## Deploy (GitHub Actions + Pages, free)

1. Create a new GitHub repo (e.g. `ontario-energy-siting`) and upload this folder's contents, `.github/` included.
2. **Settings → Pages → Source: "GitHub Actions".**
3. **Actions → "Build data and deploy map" → Run workflow.** The first run downloads everything (~30–60 min). Later runs reuse that month's cache and take a few minutes.
4. The site goes live at `https://<user>.github.io/<repo>/`. The data refreshes on the 3rd of every month.

Free GitHub Pages needs a **public** repo, and the site URL is public but unlisted. All inputs are public data, so the only thing it reveals is your weighting. If that matters, run it locally (below) instead.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pipeline.run            # fetch (cached in data/raw) + build -> web/data/
python -m http.server -d web 8000 # open http://localhost:8000
```

`python -m pipeline.run --steps build` rebuilds from cached downloads. `--force` re-downloads everything. `--fixtures` builds from synthetic data for an offline smoke test. A fixture build shows a "TEST DATA" banner, so never deploy it.

## Data sources

| Layer | Source | Used for |
|---|---|---|
| Transmission and distribution lines (with voltage), substations, generating stations | OpenStreetMap via Overpass (ODbL) | Grid distances, HV station inference, overlays |
| Industrial land | OpenStreetMap `landuse=industrial` | Load proxy (hectares within 5 km) |
| Parks, conservation reserves, national wildlife and federal protected areas, First Nation reserves | Ontario GeoHub / LIO (OGL-Ontario) | Exclusions |
| Wetlands (provincially significant vs other) | LIO "Wetland With Significance" | PSW exclusion, other wetland penalty |
| Greenbelt, Greenbelt specialty crop, Oak Ridges Moraine, Niagara Escarpment Plan | LIO | Exclusions and penalties |
| Soil Survey Complex (CLI class) | LIO | Proxy for prime agricultural land (CLI 1–3) |
| Built-up area, airports, lower/single-tier municipalities | LIO | Penalties, municipality name |
| Building footprints (~3.8M) | Microsoft Canadian Building Footprints (ODbL) | Building coverage % and count per hex: penalty, or exclusion above your limits |
| Province outline and lakes | Natural Earth | Land mask |

Layers are looked up by **name** on the LIO MapServers, so a renumbered layer won't break the build. If a download fails, the build continues and the map shows which layers are missing.

## Jurisdictions

`config.yaml → jurisdictions.score` lists the provinces/states whose land is scored (now `[CA-ON]`). OSM infrastructure is tagged with its ISO 3166-2 code. Anything outside the scored jurisdictions (New York, Michigan, Quebec…) is **never used in scores**; you can switch it on in the map with "Show infrastructure outside Ontario". To expand, add a code and supply that jurisdiction's land-constraint layers, because the LIO layers only cover Ontario.

## Your own inputs (`data/manual/`)

| File | What to put in it | Effect |
|---|---|---|
| `price_nodes.csv` | `name,lat,lon,metric`, e.g. average daily real-time LMP spread ($/MWh) per node from your MRP analysis | Turns on the **Price signal** weight. Each hex takes the nearest node within 60 km. |
| `municipal_stance.csv` | `municipality,stance,note` with stance `supportive`, `neutral` or `opposed` (e.g. from LT2/LLT support resolutions or moratoria) | Score ×1.1 for supportive and ×0.4 for opposed; can also be set as an exclusion |
| `grid_needs.geojson` | Polygons with a `need` property, hand-drawn from IESO regional plans (IRRPs) | Load term set to at least 0.9, and the area is labelled in the detail panel |

Push the changes and the workflow rebuilds the map.

## Scoring assumptions (edit in `web/app.js`)

`score = Σ(weight × subscore) / Σweights × 100 × municipal stance factor`

| Subscore | BESS distribution < 10 MW | BESS transmission 20 MW+ | Data centre |
|---|---|---|---|
| Grid access | 0.65·e^(−d_substation/3 km) + 0.35·e^(−d_any_line/1 km) | 0.6·e^(−d_≥115 kV station/5 km) + 0.4·e^(−d_≥115 kV line/2 km) | 0.6·e^(−d_≥230 kV station/6 km) + 0.4·e^(−d_≥230 kV line/3 km) |
| Land | Product of (1 − penalty): CLI 1–3 0.25 · organic soil 0.5 · Greenbelt 0.35 · ORM/NEP 0.4 · other wetland 0.3 · built-up 0.3 · airport < 3 km 0.15 · building coverage above 5% up to 0.6 at your max | same | Same, but CLI 1–3 0.5 and Greenbelt 0.6 (needs employment-land rezoning) |
| Load / market | log(1 + industrial ha within 5 km) / log(1501), capped at 1 | same | 0.7·e^(−km to downtown Toronto/60) + 0.3·industrial-land term |
| Price | Min-max normalized node metric (0.5 if no node within range) | same | same |

Building limits (defaults: 25% coverage, 150 buildings per hex) exclude dense urban and residential hexagons. A 20 MW / 4 h BESS needs roughly 0.5–1.5 ha, so a single farmhouse in a 74 ha hexagon isn't penalized.

A substation counts as HV (≥ 115 kV) if it's tagged that way in OSM, or if a ≥ 115 kV line ends within 300 m of it.

Default hard exclusions: parks/protected areas, provincially significant wetlands, specialty crop areas, First Nation reserves. Greenbelt, built-up areas and opposed municipalities can be switched on as exclusions.

## Known limitations

- **OSM coverage**: transmission is mapped well; distribution feeders and substation voltages are patchy. That's why the distribution profile leans on substations, and why utility capacity maps are the real test.
- **No hosting capacity data yet**: Hydro One's map and the OEB CCIM can't be bulk-downloaded. Use the links in the detail panel.
- **Prime agricultural areas** are designated by municipalities and aren't mapped province-wide. CLI class 1–3 is a proxy, and many Southwestern Ontario municipalities designate all rural land as prime.
- **No parcel data**: the provincial parcel fabric is licensed (Teranet/MPAC). Use the hex location to look up parcels in GeoWarehouse or municipal open data.
- Constraint tests use the **hex centre**, so a hex can straddle a boundary.
- **Data centres**: the binding constraint is large-load connection capacity, which isn't published in bulk. Fibre routes and water aren't included either. Treat this profile as a first screen only.
- **Building footprints** come from imagery of varying age, so recent construction may be missing.

## Roadmap

1. Wind and solar profiles (Global Wind Atlas, Global Solar Atlas, slope, 550 m receptor setbacks)
2. Nodal LMP spread layer from IESO public reports
3. Parcel-level lookups for shortlisted hexes
4. IESO contracted-facility list and connection queue overlay
