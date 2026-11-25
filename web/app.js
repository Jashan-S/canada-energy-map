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

  // Scoring assumptions (documented in README). Grid term = weight * exp(-distance_km / decay_km):
  // a site 1 decay-length away keeps 37% of the points, 2 lengths 14%.
  const PROFILES = {
    dist: { label: "Distribution < 10 MW", sub: ["d_sub", 3, 0.65], line: ["d_line", 1, 0.35] },
    tx: { label: "Transmission 20 MW+", sub: ["d_sub_hv", 5, 0.6], line: ["d_line_hv", 2, 0.4] },
  };
  const LOAD_FULL_HA = 1500;  // industrial hectares within 5 km for full load score (log scale)
  const PENALTY = { primeAg: 0.25, organic: 0.5, GREENBELT: 0.35, ORM: 0.4, NEP: 0.4, WETLAND: 0.3, PSW: 0.7,
    SPECIALTY: 0.7, PROTECTED: 0.9, RESERVE: 0.9, BUILTUP: 0.15, airport: 0.15 };
  const STANCE = { opposed: 0.4, neutral: 1, supportive: 1.1 };
  const RAMP = ["#f3e79b", "#fac484", "#f8a07e", "#eb7f86", "#ce6693", "#a059a0", "#5c53a5"].map(hex);

  const state = {
    profile: "dist",
    w: { grid: 50, land: 25, load: 15, price: 10 },
    excl: new Set(["PROTECTED", "PSW", "SPECIALTY", "RESERVE"]),
    minScore: 40,
    layers: { hex: true, lines: true, subs: true, plants: false },
  };

  let D, N, score, sub, visible, overlay, map, meta, selected = -1, centers;

  const $ = (s) => document.querySelector(s);
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  function hex(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function decay(d, k) { return d == null ? 0 : Math.exp(-d / k); }
  function color(s) {
    const t = clamp01((s - state.minScore) / Math.max(1, 100 - state.minScore)) * (RAMP.length - 1);
    const i = Math.min(RAMP.length - 2, Math.floor(t)), f = t - i, a = RAMP[i], b = RAMP[i + 1];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, 190];
  }

  // ------------------------------------------------------------ scoring
  function scoreAll() {
    const c = D.cols, P = PROFILES[state.profile], w = state.w;
    const hasPrice = meta.price && meta.price.max > meta.price.min;
    const pMin = hasPrice ? meta.price.min : 0, pRange = hasPrice ? meta.price.max - meta.price.min : 1;
    const wsum = w.grid + w.land + w.load + (hasPrice ? w.price : 0) || 1;
    const exclMask = [...state.excl].filter((k) => FLAGS[k]).reduce((m, k) => m | FLAGS[k], 0);
    const exclOpposed = state.excl.has("OPPOSED");
    const vis = [];
    for (let i = 0; i < N; i++) {
      const fl = c.flags[i];
      const st = c.muni[i] >= 0 ? D.munis[c.muni[i]].stance : "";
      if (fl & exclMask || (exclOpposed && st === "opposed")) { score[i] = -1; continue; }
      const g = P.sub[2] * decay(c[P.sub[0]][i], P.sub[1]) + P.line[2] * decay(c[P.line[0]][i], P.line[1]);
      let land = 1;
      const cli = c.cli[i];
      if (cli >= 1 && cli <= 3) land *= 1 - PENALTY.primeAg;
      if (cli === 8) land *= 1 - PENALTY.organic;
      for (const k of ["GREENBELT", "ORM", "NEP", "WETLAND", "PSW", "SPECIALTY", "PROTECTED", "RESERVE", "BUILTUP"])
        if (fl & FLAGS[k]) land *= 1 - PENALTY[k];
      if (c.d_airport[i] != null && c.d_airport[i] < 3) land *= 1 - PENALTY.airport;
      let load = Math.min(1, Math.log1p(c.ind_ha[i]) / Math.log1p(LOAD_FULL_HA));
      if (fl & FLAGS.NEED) load = Math.max(load, 0.9);
      const pv = c.price[i];
      const price = hasPrice && pv != null ? clamp01((pv - pMin) / pRange) : 0.5;
      let s = (w.grid * g + w.land * land + w.load * load + (hasPrice ? w.price * price : 0)) / wsum * 100;
      s *= STANCE[st] || 1;
      s = Math.min(100, s);
      score[i] = s;
      sub[i * 4] = g; sub[i * 4 + 1] = land; sub[i * 4 + 2] = load; sub[i * 4 + 3] = price;
      if (s >= state.minScore) vis.push(i);
    }
    visible = vis;
  }

  function topSites(k = 20, sepKm = 6) {
    const order = visible.slice().sort((a, b) => score[b] - score[a]);
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
  function hexLayer() {
    const data = state.layers.hex ? visible : [];
    const common = {
      id: "hex", data, pickable: true,
      getFillColor: (i) => (i === selected ? [20, 20, 20, 230] : color(score[i])),
      updateTriggers: { getFillColor: [score, state.minScore, selected, visible.length] },
      onClick: (info) => info.object != null && showDetail(info.object),
      beforeId: "lines-other",
    };
    if (deck.H3HexagonLayer) {
      return new deck.H3HexagonLayer({ ...common, getHexagon: (i) => D.cols.h3[i], extruded: false, stroked: false, highPrecision: "auto" });
    }
    return new deck.SolidPolygonLayer({ ...common, getPolygon: (i) => h3.cellToBoundary(D.cols.h3[i], true) });
  }

  function render() {
    overlay.setProps({ layers: [hexLayer()] });
    const tops = topSites();
    $("#top").innerHTML = tops.map((i) => {
      const m = D.cols.muni[i] >= 0 ? D.munis[D.cols.muni[i]].name : "—";
      return `<li data-i="${i}"><b>${score[i].toFixed(1)}</b> <span>${esc(m)}</span></li>`;
    }).join("") || "<li><span>No sites above threshold</span></li>";
    $("#top").onclick = (e) => { const li = e.target.closest("li[data-i]"); if (li) flyTo(+li.dataset.i); };
    $("#csv").onclick = () => exportCSV(tops);
    $("#leg-min").textContent = state.minScore;
  }

  function update() { scoreAll(); render(); if (selected >= 0) showDetail(selected, false); }

  function flyTo(i) {
    const [la, lo] = centers[i];
    map.flyTo({ center: [lo, la], zoom: 12, speed: 1.6 });
    showDetail(i);
  }

  // ------------------------------------------------------------ detail panel
  function showDetail(i, recenter) {
    selected = i;
    const c = D.cols, [la, lo] = centers[i], s = score[i];
    const muni = c.muni[i] >= 0 ? D.munis[c.muni[i]] : null;
    const hvs = c.sub_hv[i] >= 0 ? D.hv_subs[c.sub_hv[i]] : null;
    const tags = Object.keys(FLAGS).filter((k) => c.flags[i] & FLAGS[k]).map((k) => {
      const bad = ["PROTECTED", "PSW", "SPECIALTY", "RESERVE"].includes(k);
      return `<span class="${k === "NEED" ? "good" : bad ? "bad" : ""}">${FLAG_LABEL[k]}${k === "NEED" && c.need ? ": " + esc(c.need[i]) : ""}</span>`;
    });
    const cli = c.cli[i];
    if (cli) tags.push(`<span class="${cli <= 3 ? "bad" : ""}">${cli === 8 ? "Organic soil" : "CLI class " + cli}${cli <= 3 ? " (prime ag – AIA needed)" : ""}</span>`);
    if (muni && muni.stance) tags.push(`<span class="${muni.stance === "opposed" ? "bad" : muni.stance === "supportive" ? "good" : ""}">Council: ${esc(muni.stance)}</span>`);
    const bar = (v) => `<div class="bar"><i style="width:${(v * 100).toFixed(0)}%"></i></div>`;
    const f = (v, u = " km") => (v == null ? "—" : v + u);
    const excluded = s < 0;
    const el = $("#detail");
    el.innerHTML = `
      <h3><span>${esc(muni ? muni.name : "Unknown municipality")}</span><button class="close" aria-label="Close">×</button></h3>
      <div class="big">${excluded ? "Excluded" : s.toFixed(0)} <small>${excluded ? "" : "/ 100 · " + PROFILES[state.profile].label}</small></div>
      <div class="tags">${tags.join("") || '<span class="good">No mapped constraints</span>'}</div>
      ${excluded ? "" : `<table>
        <tr><td>Grid access${bar(sub[i * 4])}</td><td>${(sub[i * 4] * 100).toFixed(0)}</td></tr>
        <tr><td>Land suitability${bar(sub[i * 4 + 1])}</td><td>${(sub[i * 4 + 1] * 100).toFixed(0)}</td></tr>
        <tr><td>Load nearby${bar(sub[i * 4 + 2])}</td><td>${(sub[i * 4 + 2] * 100).toFixed(0)}</td></tr>
        ${meta.price ? `<tr><td>Price signal${bar(sub[i * 4 + 3])}</td><td>${c.price[i] ?? "—"}</td></tr>` : ""}
      </table>`}
      <table>
        <tr><td>Nearest HV station</td><td>${hvs ? esc(hvs.name) + (hvs.kv ? " · " + hvs.kv + " kV" : "") : "—"}</td></tr>
        <tr><td>Distance to HV station</td><td>${f(c.d_sub_hv[i])}</td></tr>
        <tr><td>Distance to any substation</td><td>${f(c.d_sub[i])}</td></tr>
        <tr><td>Distance to ≥115 kV line</td><td>${f(c.d_line_hv[i])}</td></tr>
        <tr><td>Distance to any mapped line</td><td>${f(c.d_line[i])}</td></tr>
        <tr><td>Nearest generating station</td><td>${f(c.d_plant[i])}</td></tr>
        <tr><td>Industrial land within 5 km</td><td>${f(c.ind_ha[i], " ha")}</td></tr>
        <tr><td>Nearest airport</td><td>${f(c.d_airport[i])}</td></tr>
        <tr><td>Hex centre</td><td>${la.toFixed(4)}, ${lo.toFixed(4)}</td></tr>
      </table>
      <div class="links">
        <a target="_blank" rel="noopener" href="https://www.google.com/maps/@${la},${lo},1500m/data=!3m1!1e3">Satellite view</a>
        <a target="_blank" rel="noopener" href="https://www.hydroone.com/business-services/generators/generation-capacity-map">Hydro One capacity map</a>
      </div>
      <p class="note">Screening only. Confirm capacity with the utility (Hydro One Station Capacity Calculator / OEB CCIM), land-use designation in the municipal Official Plan, and parcel ownership before acting.</p>`;
    el.hidden = false;
    el.querySelector(".close").onclick = () => { el.hidden = true; selected = -1; render(); };
    if (recenter !== false) render();
  }

  function exportCSV(tops) {
    const c = D.cols;
    const rows = [["rank", "score", "municipality", "lat", "lon", "d_sub_hv_km", "d_line_hv_km", "d_sub_km", "industrial_ha_5km", "cli", "flags", "h3"]];
    tops.forEach((i, r) => rows.push([r + 1, score[i].toFixed(1), c.muni[i] >= 0 ? D.munis[c.muni[i]].name : "",
      centers[i][0].toFixed(5), centers[i][1].toFixed(5), c.d_sub_hv[i], c.d_line_hv[i], c.d_sub[i], c.ind_ha[i], c.cli[i],
      Object.keys(FLAGS).filter((k) => c.flags[i] & FLAGS[k]).join("|"), c.h3[i]]));
    const blob = new Blob([rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `top-sites-${state.profile}.csv`; a.click();
  }

  function esc(s) { return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch])); }

  // ------------------------------------------------------------ map + overlays
  const BLANK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#eef0f2" } }] };
  const BASEMAP = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

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
        "circle-color": ["case", ["get", "hv"], "#111827", "#6b7280"], "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
    map.addLayer({ id: "plants", type: "circle", source: "plants", layout: { visibility: "none" },
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 3, 12, 7],
        "circle-color": ["match", ["get", "source"], "wind", "#0e9384", "solar", "#f5b400", "nuclear", "#7a5af8", "gas", "#d92d20", "hydro", "#2e90fa", "#475467"],
        "circle-stroke-color": "#fff", "circle-stroke-width": 1.2 } });
    for (const id of ["subs", "plants"]) {
      map.on("click", id, (e) => {
        const p = e.features[0].properties;
        const html = id === "subs"
          ? `<b>${esc(p.name || "Substation")}</b><br>${p.kv && p.kv !== "null" ? p.kv + " kV" : "voltage not tagged"}${p.hv === true || p.hv === "true" ? " · HV" : ""}${p.operator ? "<br>" + esc(p.operator) : ""}`
          : `<b>${esc(p.name || "Generating station")}</b><br>${esc(p.source || "")} ${p.mw && p.mw !== "null" ? "· " + p.mw + " MW" : ""}`;
        new maplibregl.Popup({ closeButton: false }).setLngLat(e.lngLat).setHTML(html).addTo(map);
      });
      map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
    }
  }

  function setLayerVis() {
    const v = (on) => (on ? "visible" : "none");
    if (!map.getLayer("lines-hv")) return;
    map.setLayoutProperty("lines-hv", "visibility", v(state.layers.lines));
    map.setLayoutProperty("lines-other", "visibility", v(state.layers.lines));
    map.setLayoutProperty("subs", "visibility", v(state.layers.subs));
    map.setLayoutProperty("plants", "visibility", v(state.layers.plants));
  }

  // ------------------------------------------------------------ controls
  function bindControls() {
    document.querySelectorAll("#profile button").forEach((b) => (b.onclick = () => {
      document.querySelectorAll("#profile button").forEach((x) => x.classList.toggle("on", x === b));
      state.profile = b.dataset.p; update();
    }));
    document.querySelectorAll("input[data-w]").forEach((inp) => {
      const out = inp.nextElementSibling;
      out.textContent = inp.value;
      inp.oninput = () => { out.textContent = inp.value; state.w[inp.dataset.w] = +inp.value; debounced(); };
    });
    const ms = $("#minscore");
    ms.nextElementSibling.textContent = ms.value;
    ms.oninput = () => { ms.nextElementSibling.textContent = ms.value; state.minScore = +ms.value; debounced(); };
    document.querySelectorAll("#excl input").forEach((cb) => (cb.onchange = () => {
      cb.checked ? state.excl.add(cb.dataset.x) : state.excl.delete(cb.dataset.x); update();
    }));
    document.querySelectorAll("#layers input").forEach((cb) => (cb.onchange = () => {
      state.layers[cb.dataset.l] = cb.checked; setLayerVis(); render();
    }));
    $("#sheet-toggle").onclick = () => $("#panel").classList.toggle("open");
    if (!meta.price) { const r = $("#price-row"); r.classList.add("disabled"); r.querySelector("input").disabled = true; r.title = "Add data/manual/price_nodes.csv to enable"; }
  }
  let t; function debounced() { clearTimeout(t); t = setTimeout(update, 120); }

  // ------------------------------------------------------------ boot
  async function boot() {
    const qs = new URLSearchParams(location.search);
    [meta, D] = await Promise.all([fetch("data/meta.json").then((r) => r.json()), fetch("data/hexes.json").then((r) => r.json())]);
    N = D.n;
    if (window.innerWidth < 700) $("#panel").classList.remove("open");
    score = new Float32Array(N); sub = new Float32Array(N * 4);
    centers = D.cols.h3.map((h) => h3.cellToLatLng(h));

    const notes = [];
    if (meta.fixture) notes.push("<b>TEST DATA.</b> Built from synthetic fixtures — not real infrastructure.");
    if (meta.missing_layers.length) notes.push("Missing layers (download failed): " + meta.missing_layers.join(", "));
    if (notes.length) { $("#banner").innerHTML = notes.join("<br>"); $("#banner").hidden = false; }
    $("#meta").innerHTML = `Built ${meta.built} · ${meta.hex_count.toLocaleString()} hexes · ${meta.counts.hv_substations} HV / ${meta.counts.substations} substations · ${meta.counts.plants} plants<br>
      Sources: OpenStreetMap contributors (ODbL), Ontario GeoHub / LIO (Open Government Licence – Ontario), Natural Earth.`;

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
    window.__siting = { state, update, score: () => score, visible: () => visible, topSites };
  }
  boot().catch((e) => { $("#loading").textContent = "Failed to load: " + e.message; console.error(e); });
})();
