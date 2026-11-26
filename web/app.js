/* Ontario Energy Siting Screener — client-side scoring of pre-computed H3 hex features. */
(() => {
  "use strict";

  // keep in sync with pipeline/build.py
  const FLAGS = { PROTECTED: 1, PSW: 2, WETLAND: 4, GREENBELT: 8, SPECIALTY: 16, ORM: 32, NEP: 64, RESERVE: 128, BUILTUP: 256, NEED: 512 };
  const FLAG_LABEL = {
    PROTECTED: "Park / protected area", PSW: "Provincially significant wetland", WETLAND: "Evaluated wetland",
    GREENBELT: "Greenbelt", SPECIALTY: "Specialty crop area", ORM: "Oak Ridges Moraine", NEP: "Niagara Escarpment Plan",
    RESERVE: "First Nation reserve", BUILTUP: "Built-up area", NEED: "Grid need area",
  };
  const TORONTO = [43.6532, -79.3832];

  // ---- scoring assumptions (documented in README) ----
  // Grid term = Σ share × exp(−distance_km / decay_km): one decay-length away keeps 37 % of the points.
  const PROFILES = {
    dist: {
      label: "BESS · distribution < 10 MW", grid: [["d_sub", 3, 0.65], ["d_line", 1, 0.35]],
      loadLabel: "Load nearby", pen: { primeAg: 0.25, GREENBELT: 0.35 },
    },
    tx: {
      label: "BESS · transmission 20 MW+", grid: [["d_sub_hv", 5, 0.6], ["d_line_hv", 2, 0.4]],
      loadLabel: "Load nearby", pen: { primeAg: 0.25, GREENBELT: 0.35 },
    },
    dc: {
      label: "Data centre (large load)", grid: [["d_sub_230", 6, 0.6], ["d_line_230", 3, 0.4]],
      loadLabel: "Market & zoning", pen: { primeAg: 0.5, GREENBELT: 0.6 },   // DCs need employment-land rezoning
    },
  };
  const PENALTY = { organic: 0.5, ORM: 0.4, NEP: 0.4, WETLAND: 0.3, PSW: 0.7, SPECIALTY: 0.7, PROTECTED: 0.9,
    RESERVE: 0.9, BUILTUP: 0.3, airport: 0.15, buildingsMax: 0.6 };
  const BLDG_FREE_PCT = 5;          // building coverage below this costs nothing
  const LOAD_FULL_HA = 1500;        // industrial ha within 5 km for full load score (log scale)
  const DC_MARKET_KM = 60;          // decay distance from Toronto core for the data-centre market term
  const STANCE = { opposed: 0.4, neutral: 1, supportive: 1.1 };
  const RAMP = ["#f3e79b", "#fac484", "#f8a07e", "#eb7f86", "#ce6693", "#a059a0", "#5c53a5"].map(hex);
  const GREY = [120, 124, 132, 100];

  // hex states / exclusion reasons
  const HARD = ["PROTECTED", "PSW", "SPECIALTY", "RESERVE"];   // reason codes 1-4
  const S_SCORED = 0, S_FAINT = 1, S_EXCL = 2;
  const REASONS = ["", "Park / protected area", "Provincially significant wetland", "Specialty crop area",
    "First Nation reserve", "Greenbelt (excluded by your settings)", "Built-up area (excluded by your settings)",
    "Council marked opposed", "Too built up: building coverage above your limit", "Dense housing: building count above your limit"];

  const state = {
    profile: "dist",
    w: { grid: 50, land: 25, load: 15, price: 10 },
    excl: new Set(["PROTECTED", "PSW", "SPECIALTY", "RESERVE"]),
    minScore: 40, maxCov: 25, maxBldg: 150, sizeMW: 5,
    layers: { hex: true, faint: true, excluded: true, lines: true, subs: true, plants: false, outside: false, capcolor: true },
  };

  let D, N, score, sub, hstate, reason, shown, ranked, overlay, map, meta, selected = -1, centers, dTor;

  const $ = (s) => document.querySelector(s);
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  function hex(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function decay(d, k) { return d == null ? 0 : Math.exp(-d / k); }
  function rampColor(s, alpha) {
    const t = clamp01(s / 100) * (RAMP.length - 1);
    const i = Math.min(RAMP.length - 2, Math.floor(t)), f = t - i, a = RAMP[i], b = RAMP[i + 1];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, alpha];
  }

  // ------------------------------------------------------------ scoring
  function scoreAll() {
    const c = D.cols, P = PROFILES[state.profile], w = state.w;
    const hasPrice = meta.price && meta.price.max > meta.price.min;
    const pMin = hasPrice ? meta.price.min : 0, pRange = hasPrice ? meta.price.max - meta.price.min : 1;
    const wsum = w.grid + w.land + w.load + (hasPrice ? w.price : 0) || 1;
    const ex = state.excl, hasBldg = !meta.missing_layers.includes("buildings"), hasCap = !!meta.capacity;
    const show = [], rank = [];
    for (let i = 0; i < N; i++) {
      const fl = c.flags[i];
      const st = c.muni[i] >= 0 ? D.munis[c.muni[i]].stance : "";
      let r = 0;
      for (let h = 0; h < HARD.length && !r; h++) if (fl & FLAGS[HARD[h]] && ex.has(HARD[h])) r = h + 1;
      if (!r && ex.has("GREENBELT") && fl & FLAGS.GREENBELT) r = 5;
      if (!r && ex.has("BUILTUP") && fl & FLAGS.BUILTUP) r = 6;
      if (!r && ex.has("OPPOSED") && st === "opposed") r = 7;
      if (!r && hasBldg && c.bldg_cov[i] > state.maxCov) r = 8;
      if (!r && hasBldg && c.bldg_n[i] > state.maxBldg) r = 9;
      reason[i] = r;
      if (r) { score[i] = -1; hstate[i] = S_EXCL; if (state.layers.excluded) show.push(i); continue; }

      let g = 0;
      const capK = "d_cap" + state.sizeMW;
      if (state.profile === "dist" && hasCap && c[capK]) {
        // distribution BESS: substation + line proximity + distance to a Hydro One station with room for the project
        g = 0.45 * decay(c.d_sub[i], 3) + 0.2 * decay(c.d_line[i], 1) + 0.35 * decay(c[capK][i], 4);
      } else {
        for (const [k, dk, share] of P.grid) g += share * decay(c[k][i], dk);
      }
      const ci = c.cap ? c.cap[i] : -1;
      if (ci >= 0 && D.cap_subs[ci].tc && state.profile !== "dc") g *= 0.7;   // Hydro One transmission-constrained area
      let land = 1;
      const cli = c.cli[i];
      if (cli >= 1 && cli <= 3) land *= 1 - P.pen.primeAg;
      if (cli === 8) land *= 1 - PENALTY.organic;
      if (fl & FLAGS.GREENBELT) land *= 1 - P.pen.GREENBELT;
      for (const k of ["ORM", "NEP", "WETLAND", "PSW", "SPECIALTY", "PROTECTED", "RESERVE", "BUILTUP"])
        if (fl & FLAGS[k]) land *= 1 - PENALTY[k];
      if (c.d_airport[i] != null && c.d_airport[i] < 3) land *= 1 - PENALTY.airport;
      if (hasBldg && c.bldg_cov[i] > BLDG_FREE_PCT)
        land *= 1 - PENALTY.buildingsMax * clamp01((c.bldg_cov[i] - BLDG_FREE_PCT) / Math.max(1, state.maxCov - BLDG_FREE_PCT));
      let load = Math.min(1, Math.log1p(c.ind_ha[i]) / Math.log1p(LOAD_FULL_HA));
      if (state.profile === "dc") load = 0.7 * Math.exp(-dTor[i] / DC_MARKET_KM) + 0.3 * load;
      if (fl & FLAGS.NEED) load = Math.max(load, 0.9);
      const pv = c.price[i];
      const price = hasPrice && pv != null ? clamp01((pv - pMin) / pRange) : 0.5;
      let s = (w.grid * g + w.land * land + w.load * load + (hasPrice ? w.price * price : 0)) / wsum * 100;
      s = Math.min(100, s * (STANCE[st] || 1));
      score[i] = s;
      sub[i * 4] = g; sub[i * 4 + 1] = land; sub[i * 4 + 2] = load; sub[i * 4 + 3] = price;
      if (s >= state.minScore) { hstate[i] = S_SCORED; rank.push(i); if (state.layers.hex) show.push(i); }
      else { hstate[i] = S_FAINT; if (state.layers.faint) show.push(i); }
    }
    shown = show; ranked = rank;
  }

  function topSites(k = 20, sepKm = 6) {
    const order = ranked.slice().sort((a, b) => score[b] - score[a]);
    const out = [];
    for (const i of order) {
      const [la, lo] = centers[i];
      if (out.every((j) => km(la, lo, centers[j][0], centers[j][1]) > sepKm)) out.push(i);
      if (out.length >= k) break;
    }
    return out;
  }
  function km(a, b, c, d) { const x = (d - b) * 111.32 * Math.cos(((a + c) / 2) * Math.PI / 180), y = (c - a) * 110.57; return Math.hypot(x, y); }

  // ------------------------------------------------------------ rendering
  function fill(i) {
    if (i === selected) return [20, 20, 20, 230];
    const st = hstate[i];
    if (st === S_EXCL) return GREY;
    if (st === S_FAINT) return rampColor(score[i], 55);
    return rampColor(score[i], 200);
  }
  function hexLayers() {
    // one layer per resolution zone (H3 layers draw a single resolution efficiently)
    return D.zones.map((z, k) => {
      const end = z.start + z.n, data = [];
      for (const i of shown) if (i >= z.start && i < end) data.push(i);
      return hexLayer(`hex-${z.zone}`, data);
    });
  }
  function hexLayer(id, data) {
    const common = {
      id, data, pickable: true,
      getFillColor: fill,
      updateTriggers: { getFillColor: [score, selected, shown.length, state.minScore] },
      onClick: (info) => { if (info.object != null && hstate[info.object] !== S_EXCL) showDetail(info.object); },
      beforeId: "lines-other",
    };
    if (deck.H3HexagonLayer) return new deck.H3HexagonLayer({ ...common, getHexagon: (i) => D.cols.h3[i], extruded: false, stroked: false, highPrecision: "auto" });
    return new deck.SolidPolygonLayer({ ...common, getPolygon: (i) => h3.cellToBoundary(D.cols.h3[i], true) });
  }
  function tooltip({ object }) {
    if (object == null) return null;
    const i = object, m = D.cols.muni[i] >= 0 ? D.munis[D.cols.muni[i]].name : "";
    if (hstate[i] === S_EXCL) return { text: `Excluded: ${REASONS[reason[i]]}${m ? "\n" + m : ""}` };
    return { text: `Score ${score[i].toFixed(0)}${hstate[i] === S_FAINT ? " (below your cutoff)" : ""}${m ? " · " + m : ""}\nClick for details` };
  }

  function render() {
    overlay.setProps({ layers: hexLayers(), getTooltip: tooltip });
    const tops = topSites();
    $("#top").innerHTML = tops.map((i) => {
      const m = D.cols.muni[i] >= 0 ? D.munis[D.cols.muni[i]].name : "—";
      return `<li data-i="${i}"><b>${score[i].toFixed(1)}</b> <span>${esc(m)}</span></li>`;
    }).join("") || "<li><span>No sites above threshold</span></li>";
    $("#top").onclick = (e) => { const li = e.target.closest("li[data-i]"); if (li) flyTo(+li.dataset.i); };
    $("#csv").onclick = () => exportCSV(tops);
    $("#leg-min").textContent = state.minScore;
  }

  function update() {
    scoreAll(); render();
    if (selected >= 0) { if (hstate[selected] === S_EXCL) closeDetail(); else showDetail(selected, false); }
  }

  function flyTo(i) {
    const [la, lo] = centers[i];
    map.flyTo({ center: [lo, la], zoom: 12, speed: 1.6 });
    showDetail(i);
  }

  // ------------------------------------------------------------ detail panel
  function closeDetail() { $("#detail").hidden = true; selected = -1; render(); }

  function showDetail(i, rerender) {
    selected = i;
    const c = D.cols, [la, lo] = centers[i], s = score[i], P = PROFILES[state.profile];
    const muni = c.muni[i] >= 0 ? D.munis[c.muni[i]] : null;
    const st = (k) => (c[k] && c[k][i] >= 0 ? D.hv_subs[c[k][i]] : null);
    const hvs = st("sub_hv"), s230 = st("sub_230");
    const tags = Object.keys(FLAGS).filter((k) => c.flags[i] & FLAGS[k]).map((k) => {
      const bad = ["PROTECTED", "PSW", "SPECIALTY", "RESERVE"].includes(k);
      return `<span class="${k === "NEED" ? "good" : bad ? "bad" : ""}">${FLAG_LABEL[k]}${k === "NEED" && c.need ? ": " + esc(c.need[i]) : ""}</span>`;
    });
    const cli = c.cli[i];
    if (cli) tags.push(`<span class="${cli <= 3 ? "bad" : ""}">${cli === 8 ? "Organic soil" : "CLI class " + cli}${cli <= 3 ? " (prime ag – AIA needed)" : ""}</span>`);
    if (c.bldg_cov && c.bldg_cov[i] != null && c.bldg_cov[i] > BLDG_FREE_PCT) tags.push(`<span>${c.bldg_cov[i]}% covered by buildings</span>`);
    const cs = c.cap && c.cap[i] >= 0 ? D.cap_subs[c.cap[i]] : null;
    if (cs && cs.tc) tags.push(`<span class="bad">Transmission-constrained area (Hydro One TC)</span>`);
    if (muni && muni.stance) tags.push(`<span class="${muni.stance === "opposed" ? "bad" : muni.stance === "supportive" ? "good" : ""}">Council: ${esc(muni.stance)}</span>`);
    const bar = (v) => `<div class="bar"><i style="width:${(v * 100).toFixed(0)}%"></i></div>`;
    const f = (v, u = " km") => (v == null || v < 0 ? "—" : v + u);
    const stn = (x) => (x ? esc(x.name) + (x.kv ? " · " + x.kv + " kV" : "") : "—");
    const el = $("#detail");
    el.innerHTML = `
      <h3><span>${esc(muni ? muni.name : "Unknown municipality")}</span><button class="close" aria-label="Close">×</button></h3>
      <div class="big">${s.toFixed(0)} <small>/ 100 · ${P.label}${hstate[i] === S_FAINT ? " · below your cutoff" : ""}</small></div>
      <div class="tags">${tags.join("") || '<span class="good">No mapped constraints</span>'}</div>
      <table>
        <tr><td>Grid access${bar(sub[i * 4])}</td><td>${(sub[i * 4] * 100).toFixed(0)}</td></tr>
        <tr><td>Land suitability${bar(sub[i * 4 + 1])}</td><td>${(sub[i * 4 + 1] * 100).toFixed(0)}</td></tr>
        <tr><td>${P.loadLabel}${bar(sub[i * 4 + 2])}</td><td>${(sub[i * 4 + 2] * 100).toFixed(0)}</td></tr>
        ${meta.price ? `<tr><td>Price signal${bar(sub[i * 4 + 3])}</td><td>${c.price[i] ?? "—"}</td></tr>` : ""}
      </table>
      <table>
        <tr><td>Nearest ≥115 kV station</td><td>${stn(hvs)}</td></tr>
        <tr><td>Distance to ≥115 kV station</td><td>${f(c.d_sub_hv[i])}</td></tr>
        <tr><td>Nearest ≥230 kV station</td><td>${stn(s230)}</td></tr>
        <tr><td>Distance to ≥230 kV station</td><td>${f(c.d_sub_230[i])}</td></tr>
        <tr><td>Distance to any substation</td><td>${f(c.d_sub[i])}</td></tr>
        ${meta.capacity ? `<tr><td>Nearest Hydro One listed station</td><td>${cs ? esc(cs.site) : "none within 15 km"}</td></tr>
        ${cs ? `<tr><td>Its generation room: best bus / station</td><td>${cs.tc ? "TC (none)" : cs.best + " / " + cs.total + " MW"}</td></tr>` : ""}
        <tr><td>Distance to station with ≥${state.sizeMW} MW room</td><td>${f(c["d_cap" + state.sizeMW] && c["d_cap" + state.sizeMW][i])}</td></tr>` : ""}
        <tr><td>Distance to ≥115 / ≥230 kV line</td><td>${f(c.d_line_hv[i], "")} / ${f(c.d_line_230[i])}</td></tr>
        <tr><td>Building coverage · count</td><td>${f(c.bldg_cov[i], "%")} · ${f(c.bldg_n[i], "")}</td></tr>
        <tr><td>Industrial land within 5 km</td><td>${f(c.ind_ha[i], " ha")}</td></tr>
        <tr><td>Distance to downtown Toronto</td><td>${dTor[i].toFixed(0)} km</td></tr>
        <tr><td>Nearest airport</td><td>${f(c.d_airport[i])}</td></tr>
        <tr><td>Hex centre</td><td>${la.toFixed(4)}, ${lo.toFixed(4)}</td></tr>
      </table>
      <div class="links">
        <a target="_blank" rel="noopener" href="https://www.google.com/maps/@${la},${lo},1500m/data=!3m1!1e3">Satellite view</a>
        <a target="_blank" rel="noopener" href="https://www.hydroone.com/business-services/generators/generation-capacity-map">Hydro One capacity map</a>
      </div>
      <p class="note">Screening only. Confirm capacity with the utility (Hydro One Station Capacity Calculator / OEB CCIM), land-use designation in the municipal Official Plan, and parcel ownership before acting.</p>`;
    el.hidden = false;
    el.querySelector(".close").onclick = closeDetail;
    if (rerender !== false) render();
  }

  function exportCSV(tops) {
    const c = D.cols;
    const rows = [["rank", "score", "profile", "municipality", "lat", "lon", "d_sub_hv_km", "d_sub_230_km", "d_line_hv_km", "d_sub_km",
      "building_cov_pct", "industrial_ha_5km", "cli", "flags", "h3"]];
    tops.forEach((i, r) => rows.push([r + 1, score[i].toFixed(1), state.profile, c.muni[i] >= 0 ? D.munis[c.muni[i]].name : "",
      centers[i][0].toFixed(5), centers[i][1].toFixed(5), c.d_sub_hv[i], c.d_sub_230[i], c.d_line_hv[i], c.d_sub[i],
      c.bldg_cov[i], c.ind_ha[i], c.cli[i], Object.keys(FLAGS).filter((k) => c.flags[i] & FLAGS[k]).join("|"), c.h3[i]]));
    const blob = new Blob([rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `top-sites-${state.profile}.csv`; a.click();
  }

  function esc(s) { return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch])); }

  // ------------------------------------------------------------ map + overlays
  const BLANK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#eef0f2" } }] };
  const BASEMAP = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
  const PLANT_COLORS = { wind: "#0e9384", solar: "#f5b400", nuclear: "#7a5af8", gas: "#d92d20", hydro: "#2e90fa", battery: "#9e77ed" };

  function addOverlays() {
    map.addSource("lines", { type: "geojson", data: "data/lines.geojson" });
    map.addSource("subs", { type: "geojson", data: "data/substations.geojson" });
    map.addSource("plants", { type: "geojson", data: "data/plants.geojson" });
    const kv = ["coalesce", ["get", "kv"], 0];
    map.addLayer({ id: "lines-other", type: "line", source: "lines", filter: ["<", kv, 115],
      paint: { "line-color": "#8a8f98", "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.4, 12, 1.5] } });
    map.addLayer({ id: "lines-hv", type: "line", source: "lines", filter: [">=", kv, 115],
      paint: { "line-color": ["step", kv, "#2b6cb0", 200, "#e8710a", 400, "#b42318"],
        "line-width": ["interpolate", ["linear"], ["zoom"], 6, ["step", kv, 0.8, 200, 1.2, 400, 1.8], 12, ["step", kv, 2, 200, 3, 400, 4]] } });
    map.addLayer({ id: "subs", type: "circle", source: "subs",
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, ["case", ["get", "hv"], 2.5, 1.2], 12, ["case", ["get", "hv"], 6, 3.5]],
        "circle-color": ["case", ["get", "hv"], "#111827", "#9aa0a9"], "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
    const pc = ["match", ["get", "source"]];
    for (const [k, v] of Object.entries(PLANT_COLORS)) pc.push(k, v);
    pc.push("#475467");
    map.addLayer({ id: "plants", type: "circle", source: "plants", layout: { visibility: "none" },
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 3, 12, 7], "circle-color": pc,
        "circle-stroke-color": "#fff", "circle-stroke-width": 1.2 } });
    for (const id of ["subs", "plants"]) {
      map.on("click", id, (e) => {
        const p = e.features[0].properties;
        const where = p.j ? ` · ${esc(p.j)}` : "";
        const capTxt = p.cap_tc === true || p.cap_tc === "true" ? "<br>Hydro One: transmission-constrained (TC)"
          : p.cap_best != null && p.cap_best !== "null" ? `<br>Hydro One generation room: ${p.cap_best} MW best bus · ${p.cap_total} MW station` : "";
        const html = id === "subs"
          ? `<b>${esc(p.name || "Substation")}</b><br>${p.kv && p.kv !== "null" ? p.kv + " kV" : "voltage not tagged"}${p.hv === true || p.hv === "true" ? " · treated as ≥115 kV" : ""}${where}${p.operator ? "<br>" + esc(p.operator) : ""}${capTxt}`
          : `<b>${esc(p.name || "Generating station")}</b><br>${esc(p.source || "")} ${p.mw && p.mw !== "null" ? "· " + p.mw + " MW" : ""}${where}`;
        new maplibregl.Popup({ closeButton: false }).setLngLat(e.lngLat).setHTML(html).addTo(map);
      });
      map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
    }
  }

  function setLayerVis() {
    const v = (on) => (on ? "visible" : "none");
    if (!map.getLayer("lines-hv")) return;
    for (const [id, on] of [["lines-hv", state.layers.lines], ["lines-other", state.layers.lines], ["subs", state.layers.subs], ["plants", state.layers.plants]])
      map.setLayoutProperty(id, "visibility", v(on));
    // infrastructure outside the scored jurisdiction(s)
    const scope = state.layers.outside ? null : ["==", ["get", "scope"], true];
    const kv = ["coalesce", ["get", "kv"], 0];
    map.setFilter("lines-other", scope ? ["all", ["<", kv, 115], scope] : ["<", kv, 115]);
    map.setFilter("lines-hv", scope ? ["all", [">=", kv, 115], scope] : [">=", kv, 115]);
    map.setFilter("subs", scope);
    const hv = ["case", ["get", "hv"], "#111827", "#9aa0a9"];
    const room = ["case",
      ["==", ["get", "cap_tc"], true], "#d92d20",
      ["==", ["typeof", ["get", "cap_best"]], "number"],
      ["step", ["get", "cap_best"], "#d92d20", 2, "#f79009", 10, "#12b76a"],
      hv];
    map.setPaintProperty("subs", "circle-color", state.layers.capcolor && meta.capacity ? room : hv);
    map.setFilter("plants", scope);
  }

  // ------------------------------------------------------------ controls
  function bindControls() {
    document.querySelectorAll("#profile button").forEach((b) => (b.onclick = () => {
      document.querySelectorAll("#profile button").forEach((x) => x.classList.toggle("on", x === b));
      state.profile = b.dataset.p;
      $("#load-label").textContent = PROFILES[state.profile].loadLabel;
      update();
    }));
    const bindSlider = (inp, fn) => {
      const out = inp.nextElementSibling;
      out.textContent = inp.value + (inp.dataset.unit || "");
      inp.oninput = () => { out.textContent = inp.value + (inp.dataset.unit || ""); fn(+inp.value); debounced(); };
    };
    document.querySelectorAll("input[data-w]").forEach((inp) => bindSlider(inp, (v) => (state.w[inp.dataset.w] = v)));
    bindSlider($("#minscore"), (v) => (state.minScore = v));
    bindSlider($("#maxcov"), (v) => (state.maxCov = v));
    bindSlider($("#maxbldg"), (v) => (state.maxBldg = v));
    document.querySelectorAll("#size button").forEach((b) => (b.onclick = () => {
      document.querySelectorAll("#size button").forEach((x) => x.classList.toggle("on", x === b));
      state.sizeMW = +b.dataset.mw; update();
    }));
    const syncSize = () => ($("#size-row").hidden = !(meta.capacity && state.profile === "dist"));
    syncSize();
    document.querySelectorAll("#profile button").forEach((b) => b.addEventListener("click", syncSize));
    if (!meta.capacity) $("#cap-leg").hidden = true;
    document.querySelectorAll("#excl input").forEach((cb) => (cb.onchange = () => {
      cb.checked ? state.excl.add(cb.dataset.x) : state.excl.delete(cb.dataset.x); update();
    }));
    document.querySelectorAll("#layers input").forEach((cb) => (cb.onchange = () => {
      state.layers[cb.dataset.l] = cb.checked; setLayerVis();
      if (["hex", "faint", "excluded"].includes(cb.dataset.l)) update(); else render();
    }));
    $("#sheet-toggle").onclick = () => $("#panel").classList.toggle("open");
    if (!meta.price) { const r = $("#price-row"); r.classList.add("disabled"); r.querySelector("input").disabled = true; r.title = "Add data/manual/price_nodes.csv to enable"; }
    if (meta.missing_layers.includes("buildings")) document.querySelectorAll(".bldg-ctl").forEach((e) => e.classList.add("disabled"));
    const juris = (meta.jurisdictions || ["CA-ON"]).join(", ");
    $("#outside-label").textContent = `Show infrastructure outside ${juris === "CA-ON" ? "Ontario" : juris}`;
  }
  let t; function debounced() { clearTimeout(t); t = setTimeout(update, 120); }

  // ------------------------------------------------------------ boot
  async function boot() {
    const qs = new URLSearchParams(location.search);
    meta = await fetch("data/meta.json").then((r) => r.json());
    const files = meta.hex_files || [{ file: "hexes.json", zone: "all" }];
    D = await fetch("data/" + files[0].file).then((r) => r.json());
    D.zones = [{ zone: files[0].zone, start: 0, n: D.n, res: D.res }];
    initArrays();
    if (window.innerWidth < 700) $("#panel").classList.remove("open");

    const notes = [];
    if (meta.fixture) notes.push("<b>TEST DATA.</b> Built from synthetic fixtures — not real infrastructure.");
    if (meta.missing_layers.length) notes.push("Missing layers (download failed): " + meta.missing_layers.join(", "));
    if (notes.length) { $("#banner").innerHTML = notes.join("<br>"); $("#banner").hidden = false; }
    $("#meta").innerHTML = `Built ${meta.built} · ${meta.hex_count.toLocaleString()} hexes · ${meta.counts.hv_substations} ≥115 kV / ${meta.counts.substations} substations · ${meta.counts.plants} plants<br>
      Sources: OpenStreetMap contributors (ODbL), Ontario GeoHub / LIO (Open Government Licence – Ontario), Microsoft Canadian Building Footprints (ODbL), Natural Earth.`;

    map = new maplibregl.Map({
      container: "map", style: qs.get("basemap") === "none" ? BLANK : BASEMAP,
      center: [-79.6, 43.9], zoom: window.innerWidth < 700 ? 6 : 7, attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    let styleFailed = false;
    map.on("error", (e) => {
      if (!styleFailed && !map.isStyleLoaded() && /style|fetch/i.test(String(e.error && e.error.message))) { styleFailed = true; map.setStyle(BLANK); }
    });
    await new Promise((res) => map.once("load", res));
    addOverlays();
    overlay = new deck.MapboxOverlay({ interleaved: true, layers: [] });
    map.addControl(overlay);
    bindControls();
    setLayerVis();
    update();
    $("#loading").remove();
    loadMoreZones(files.slice(1));
    window.__siting = { state, update, score: () => score, ranked: () => ranked, shown: () => shown, topSites, hstate: () => hstate, reason: () => reason };
  }
  function initArrays() {
    N = D.cols.h3.length;
    for (const k of Object.keys(D.cols)) if (D.cols[k] == null && k !== "need") D.cols[k] = new Array(N).fill(null);
    if (!D.cols.bldg_cov) { D.cols.bldg_cov = new Array(N).fill(null); D.cols.bldg_n = new Array(N).fill(-1); }
    for (const k of ["d_sub_230", "d_line_230", "sub_230"]) if (!D.cols[k]) D.cols[k] = new Array(N).fill(null);
    score = new Float32Array(N); sub = new Float32Array(N * 4); hstate = new Uint8Array(N); reason = new Uint8Array(N);
    centers = D.cols.h3.map((h) => h3.cellToLatLng(h));
    dTor = new Float32Array(N);
    for (let i = 0; i < N; i++) dTor[i] = km(centers[i][0], centers[i][1], TORONTO[0], TORONTO[1]);
  }

  // Later zones (e.g. northern Ontario) load after the first render, then get appended.
  async function loadMoreZones(rest) {
    for (const f of rest) {
      const note = $("#zone-note");
      note.textContent = `Loading ${f.zone} Ontario…`; note.hidden = false;
      try {
        const part = await fetch("data/" + f.file).then((r) => r.json());
        const start = D.cols.h3.length;
        for (const k of Object.keys(D.cols)) {
          const add = part.cols[k] || new Array(part.n).fill(k === "need" ? "" : null);
          if (D.cols[k] == null) { if (k === "need") D.cols[k] = new Array(start).fill(""); else continue; }
          D.cols[k] = D.cols[k].concat(add);
        }
        D.zones.push({ zone: f.zone, start, n: part.n, res: part.res });
        initArrays();
        update();
        note.hidden = true;
      } catch (e) { note.textContent = `Couldn't load ${f.zone} Ontario: ${e.message}`; }
    }
  }

  boot().catch((e) => { $("#loading").textContent = "Failed to load: " + e.message; console.error(e); });
})();
