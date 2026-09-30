(() => {
  "use strict";
  const TZ = "Europe/Dublin";
  const $ = (s) => document.querySelector(s);
  const fmt = (o) => new Intl.DateTimeFormat("en-IE", { timeZone: TZ, ...o });
  const fHM = fmt({ hour: "2-digit", minute: "2-digit", hour12: false });
  const fH = fmt({ hour: "2-digit", hour12: false });
  const fDay = fmt({ weekday: "short" });
  const fDate = fmt({ weekday: "short", day: "numeric", month: "short" });
  const fKey = fmt({ year: "numeric", month: "2-digit", day: "2-digit" });
  const hm = (d) => fHM.format(d);
  const dayKey = (d) => fKey.format(d);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const getJSON = async (u) => {
    const r = await fetch(u, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${u}: ${r.status}`);
    return r.json();
  };

  const PLACES = [
    ["Dublin", 53.3498, -6.2603], ["Cork", 51.8985, -8.4756], ["Galway", 53.2707, -9.0568],
    ["Limerick", 52.6638, -8.6267], ["Waterford", 52.2593, -7.1101], ["Belfast", 54.5973, -5.9301],
    ["Sligo", 54.2766, -8.4761], ["Athlone", 53.4239, -7.9407], ["Killarney", 52.0599, -9.5044],
    ["Letterkenny", 54.9558, -7.7342], ["Wexford", 52.3369, -6.4633], ["Westport", 53.8008, -9.5186],
  ];
  const REGIONS = {
    EI01: "Carlow", EI02: "Cavan", EI03: "Clare", EI04: "Cork", EI06: "Donegal", EI07: "Dublin", EI10: "Galway",
    EI11: "Kerry", EI12: "Kildare", EI13: "Kilkenny", EI14: "Leitrim", EI15: "Laois", EI16: "Limerick", EI18: "Longford",
    EI19: "Louth", EI20: "Mayo", EI21: "Meath", EI22: "Monaghan", EI23: "Offaly", EI24: "Roscommon", EI25: "Sligo",
    EI26: "Tipperary", EI27: "Waterford", EI29: "Westmeath", EI30: "Wexford", EI31: "Wicklow",
  };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("aimsir." + k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("aimsir." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  // ------------------------------------------------------------ map
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const map = L.map("map", { zoomControl: false, attributionControl: true, minZoom: 5, maxZoom: 11, zoomSnap: 0.5 })
    .setView([53.45, -7.9], 7);
  L.control.zoom({ position: "topleft" }).addTo(map);
  map.createPane("labels").style.zIndex = 450;
  map.getPane("labels").style.pointerEvents = "none";
  map.createPane("isobars").style.zIndex = 430;
  map.createPane("coast").style.zIndex = 425;
  map.getPane("coast").style.pointerEvents = "none";
  map.attributionControl.setPrefix(false);
  map.attributionControl.addAttribution('Weather © <a href="https://www.met.ie">Met Éireann</a>');
  window.aimsirMap = map; // handy from the dev console

  // Natural Earth coastline (bundled). Drawn over model layers, and always when there is no basemap.
  let coast = null;
  let coastAlways = false;
  const syncCoast = () => {
    if (!coast) return;
    const want = coastAlways || !isRadar(S.mode);
    if (want && !map.hasLayer(coast)) coast.addTo(map);
    if (!want && map.hasLayer(coast)) map.removeLayer(coast);
  };
  fetch("/static/coast.json").then((r) => r.json()).then((gj) => {
    coast = L.geoJSON(gj, { pane: "coast", interactive: false, style: { color: dark ? "#e6e3da" : "#1c1f22", weight: 0.7, opacity: 0.55 } });
    syncCoast();
  }).catch(() => {});

  const loadAsset = (src) => new Promise((ok, fail) => {
    const el = src.endsWith(".css") ? Object.assign(document.createElement("link"), { rel: "stylesheet", href: src })
      : Object.assign(document.createElement("script"), { src });
    el.onload = ok;
    el.onerror = () => fail(new Error("failed to load " + src));
    document.head.appendChild(el);
  });

  // Basemap: OpenFreeMap vector tiles (free, no key) by default. The style is split in two so
  // place names sit above the weather overlays: shapes in the tile pane, labels in the labels pane.
  async function setupBasemap(kind) {
    if (kind === "openfreemap") {
      try {
        await loadAsset("/static/vendor/maplibre/maplibre-gl.css");
        await loadAsset("/static/vendor/maplibre/maplibre-gl.js");
        await loadAsset("/static/vendor/maplibre/leaflet-maplibre-gl.js");
        const r = await fetch(`https://tiles.openfreemap.org/styles/${dark ? "dark" : "positron"}`);
        if (!r.ok) throw new Error(`style ${r.status}`);
        const style = await r.json();
        const part = (keep) => ({ ...style, layers: style.layers.filter(keep) });
        // Labels sit on top of coloured weather layers, so keep only place/water names
        // (no road shields or POIs) and give them a firm halo that reads on any overlay.
        const clutter = /highway|road|poi|shield|transport|aeroway|airport|rail|housenumber|building/i;
        const labelLayers = style.layers
          .filter((l) => l.type === "symbol" && !clutter.test(l.id) && l.layout?.["text-field"])
          .map((l) => ({
            ...l,
            layout: { ...l.layout, "icon-image": "", "text-transform": "none", "text-letter-spacing": 0.02 },
            paint: {
              ...l.paint,
              "text-color": dark ? "#e6e3da" : "#2b2e31",
              "text-halo-color": dark ? "rgba(23,25,27,0.9)" : "rgba(250,249,245,0.92)",
              "text-halo-width": dark ? 1.1 : 1.4,
              "text-halo-blur": dark ? 0.6 : 0.2,
            },
          }));
        L.maplibreGL({ style: part((l) => l.type !== "symbol"), interactive: false }).addTo(map);
        L.maplibreGL({ style: { ...style, layers: labelLayers }, pane: "labels", interactive: false }).addTo(map);
        return;
      } catch (e) {
        console.warn("OpenFreeMap basemap unavailable, falling back to coastline only:", e);
        kind = "none";
      }
    }
    if (kind === "osm") {
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, className: dark ? "osm-dark" : "",
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(map);
      return;
    }
    coastAlways = true; // "none": sea-coloured background + bundled coastline, nothing external
    syncCoast();
  }

  // ------------------------------------------------------------ state
  const S = {
    cfg: null, radar: null, nwp: null, mode: store.get("mode", "radar"),
    frames: [], i: 0, playing: false, timer: null, followLatest: true,
    overlays: new Map(), current: null, want: null, coverage: null,
    isobars: store.get("isobars", true), isoLayer: null, isoCache: new Map(), isoStamp: null,
    place: store.get("place", null), homePin: null, status: null,
  };

  const RADAR_MODES = ["radar", "radaracc"];
  const isRadar = (m) => RADAR_MODES.includes(m);
  const MORE_MODES = ["radaracc", "gust", "lightning", "vis", "snow"];

  // ------------------------------------------------------------ frames / overlays
  function framesFor(mode) {
    if (mode === "radar") {
      const r = S.radar;
      return r ? r.frames.map((f) => ({ t: new Date(f.time), url: f.url, stamp: f.stamp })) : [];
    }
    if (mode === "radaracc") {
      const a = S.radar?.acc;
      return a ? a.frames.map((f) => ({ t: new Date(f.time), url: f.url, stamp: f.stamp })) : [];
    }
    const n = S.nwp;
    if (!n || !n.frames) return [];
    return n.frames.filter((f) => f.layers.includes(mode))
      .map((f) => ({ t: new Date(f.valid), url: `${n.base}${mode}_${f.stamp}.png`, stamp: f.stamp }));
  }
  const boundsFor = (mode) => (isRadar(mode) ? S.radar?.bounds : S.nwp?.bounds);

  function clearOverlays() {
    for (const ov of S.overlays.values()) map.removeLayer(ov);
    S.overlays.clear();
    S.current = null;
  }

  function overlay(url) {
    let ov = S.overlays.get(url);
    if (!ov) {
      ov = L.imageOverlay(url, boundsFor(S.mode), { opacity: 0, className: "wx", interactive: false }).addTo(map);
      ov._ready = false;
      ov.on("load", () => { ov._ready = true; if (S.want === url && S.overlays.get(url) === ov) swap(ov); });
      S.overlays.set(url, ov);
    }
    return ov;
  }
  function swap(ov) {
    if (S.current && S.current !== ov) S.current.setOpacity(0);
    ov.setOpacity(1);
    S.current = ov;
  }

  function show(i) {
    if (!S.frames.length) return;
    S.i = Math.max(0, Math.min(S.frames.length - 1, i));
    S.followLatest = isRadar(S.mode) && S.i === S.frames.length - 1;
    const f = S.frames[S.i];
    S.want = f.url;
    const ov = overlay(f.url);
    if (ov._ready) swap(ov);
    for (let k = 1; k <= 3; k++) if (S.frames[S.i + k]) overlay(S.frames[S.i + k].url);
    drawClock(f.t);
    $("#head").style.left = pos(f.t) * 100 + "%";
    updateIsobars(f.t);
  }

  // ------------------------------------------------------------ timeline
  let span = [0, 1];
  const pos = (t) => (span[1] > span[0] ? (t - span[0]) / (span[1] - span[0]) : 0);

  function drawTicks() {
    const el = $("#ticks");
    el.innerHTML = "";
    if (!S.frames.length) return;
    span = [S.frames[0].t.getTime(), S.frames[S.frames.length - 1].t.getTime()];
    const out = [];
    const radar = S.mode === "radar";
    const width = $("#track").clientWidth || 600;
    const hours = (span[1] - span[0]) / 3.6e6;
    const labelEvery = radar ? 30 : hours > 36 ? (width < 500 ? 12 : 6) : 3; // minutes (radar) / hours (model)
    for (const f of S.frames) {
      const x = pos(f.t) * 100;
      const parts = hm(f.t).split(":");
      const mins = +parts[0] * 60 + +parts[1];
      let major = false;
      if (radar) major = mins % labelEvery === 0;
      else major = +parts[0] % labelEvery === 0;
      out.push(`<i class="${major ? "major" : ""}" style="left:${x}%"></i>`);
      if (!radar && parts[0] === "00") {
        out.push(`<b class="day" style="left:calc(${x}% + 3px)">${fDay.format(f.t)}</b>`);
      } else if (major) {
        out.push(`<b style="left:${x}%">${radar ? hm(f.t) : parts[0]}</b>`);
      }
    }
    const now = Date.now();
    if (!radar && now > span[0] && now < span[1]) out.push(`<i class="now" style="left:${pos(now) * 100}%"></i>`);
    el.innerHTML = out.join("");
  }

  function drawClock(t) {
    const today = dayKey(new Date()) === dayKey(t);
    const kind = S.mode === "radar" ? "radar" : S.mode === "radaracc" ? "radar total" : "model";
    $("#t-day").textContent = `${today ? "Today" : fDate.format(t)} · ${kind}`;
    $("#t-time").textContent = hm(t);
    $(".clock").classList.toggle("future", t.getTime() > Date.now() + 5 * 60e3);
  }

  function seekFromEvent(e) {
    const r = $("#track").getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const target = span[0] + frac * (span[1] - span[0]);
    let best = 0;
    S.frames.forEach((f, k) => { if (Math.abs(f.t - target) < Math.abs(S.frames[best].t - target)) best = k; });
    show(best);
  }
  $("#track").addEventListener("pointerdown", (e) => {
    stop();
    seekFromEvent(e);
    const move = (ev) => seekFromEvent(ev);
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  });

  function play() {
    if (S.frames.length < 2) return;
    S.playing = true;
    $("#play").classList.add("on");
    const step = () => {
      const last = S.i >= S.frames.length - 1;
      show(last ? 0 : S.i + 1);
      const hold = S.i === S.frames.length - 1 ? 1400 : S.mode === "radar" ? 280 : 450;
      S.timer = setTimeout(step, hold);
    };
    if (S.i >= S.frames.length - 1) show(0);
    S.timer = setTimeout(step, 300);
  }
  function stop() {
    S.playing = false;
    clearTimeout(S.timer);
    $("#play").classList.remove("on");
  }
  $("#play").addEventListener("click", () => (S.playing ? stop() : play()));
  addEventListener("keydown", (e) => {
    if (e.target.closest("select, input")) return;
    if (e.code === "Space") { e.preventDefault(); S.playing ? stop() : play(); }
    if (e.code === "ArrowRight") { stop(); show(S.i + 1); }
    if (e.code === "ArrowLeft") { stop(); show(S.i - 1); }
  });
  addEventListener("resize", () => { drawTicks(); if (S.frames[S.i]) $("#head").style.left = pos(S.frames[S.i].t) * 100 + "%"; });

  // ------------------------------------------------------------ mode / legend
  function setMode(mode, keepTime) {
    const prevT = keepTime && S.frames[S.i] ? S.frames[S.i].t : null;
    S.mode = mode;
    store.set("mode", mode);
    stop();
    clearOverlays();
    document.querySelector(`input[name=layer][value=${mode}]`).checked = true;
    S.frames = framesFor(mode);
    drawTicks();
    drawLegend();
    if (S.coverage) S.coverage.setOpacity(mode === "radar" ? 1 : 0);
    syncCoast();
    emptyState();
    if (!S.frames.length) { $("#head").style.left = "0"; updateIsobars(new Date()); return; }
    let target = S.frames.length - 1;
    const ref = prevT ? prevT.getTime() : Date.now();
    if (mode !== "radar" || prevT) {
      target = 0;
      S.frames.forEach((f, k) => { if (Math.abs(f.t - ref) < Math.abs(S.frames[target].t - ref)) target = k; });
    }
    show(target);
  }
  document.querySelectorAll("input[name=layer]").forEach((r) =>
    r.addEventListener("change", () => setMode(r.value, true)));
  const more = $("#more");
  more.open = store.get("moreOpen", false) || MORE_MODES.includes(S.mode);
  more.addEventListener("toggle", () => store.set("moreOpen", more.open));

  function drawLegend() {
    const lg = S.mode === "radar" ? S.radar?.legend : S.mode === "radaracc" ? S.radar?.acc?.legend : S.nwp?.legends?.[S.mode];
    const el = $("#legend");
    if (!lg || !S.frames.length) { el.innerHTML = ""; return; }
    const steps = lg.steps;
    el.classList.toggle("dense", steps.length > 12);
    const lbl = (v) => (Math.abs(v) < 1 && v !== 0 ? String(v).replace(/^0/, "") : String(v));
    el.innerHTML =
      `<div class="ttl">${esc(lg.label)} · ${esc(lg.unit)}</div>` +
      `<div class="ramp">${steps.map((s) => `<span style="background:${s.colour}"></span>`).join("")}</div>` +
      `<div class="lbls">${steps.map((s, k) => `<span>${(k === 0 && lg.key === "temp") || (steps.length > 12 && k % 2) ? "" : lbl(s.from)}</span>`).join("")}${lg.above != null ? `<span>${lbl(lg.above)}+</span>` : ""}</div>`;
  }

  function emptyState() {
    const el = $("#empty");
    if (S.frames.length) { el.hidden = true; return; }
    const st = S.status?.[isRadar(S.mode) ? "radar" : "nwp"] || {};
    let msg;
    if (!S.cfg?.has_key) {
      msg = `<b>No API key configured</b>Set <code>MET_API_KEY</code> (from your profile on opendata.met.ie) and restart, or drop radar/GRIB files into the inbox folder.`;
    } else if (st.error) {
      msg = `<b>${isRadar(S.mode) ? "Radar" : "Model"} data unavailable</b>${esc(st.error)}`;
    } else if (S.mode === "radaracc") {
      msg = `<b>No hourly radar totals yet</b>${st.acc_error ? esc(st.acc_error) : "Met publishes these once an hour; the first one should appear within the hour."}`;
    } else if (!isRadar(S.mode) && S.nwp?.frames?.length) {
      msg = `<b>Not in this model run</b>This field wasn't found in the downloaded model files.`;
    } else if (!isRadar(S.mode) && (st.busy || S.nwp?.busy)) {
      msg = `<b>Processing model run…</b>The newest HARMONIE run is being decoded. This can take a few minutes.`;
    } else {
      msg = `<b>Waiting for data</b>Nothing has been downloaded yet. The first files usually arrive within a few minutes.`;
    }
    el.innerHTML = msg;
    el.hidden = false;
  }

  // ------------------------------------------------------------ isobars
  async function updateIsobars(t) {
    const on = S.isobars && S.nwp?.frames?.length;
    document.querySelector("#isobars").checked = S.isobars;
    if (!on) { if (S.isoLayer) { map.removeLayer(S.isoLayer); S.isoLayer = null; S.isoStamp = null; } return; }
    const withMsl = S.nwp.frames.filter((f) => f.layers.includes("msl"));
    if (!withMsl.length) return;
    let best = withMsl[0];
    for (const f of withMsl) if (Math.abs(new Date(f.valid) - t) < Math.abs(new Date(best.valid) - t)) best = f;
    if (best.stamp === S.isoStamp) return;
    S.isoStamp = best.stamp;
    let gj = S.isoCache.get(best.stamp);
    if (!gj) {
      try { gj = await getJSON(`${S.nwp.base}msl_${best.stamp}.json`); } catch { return; }
      S.isoCache.set(best.stamp, gj);
    }
    if (S.isoStamp !== best.stamp) return;
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim();
    const layer = L.layerGroup();
    L.geoJSON(gj, {
      pane: "isobars", interactive: false,
      style: (f) => ({ color: ink, weight: f.properties.hpa % 20 === 0 ? 1.4 : 0.8, opacity: 0.6 }),
    }).addTo(layer);
    for (const f of gj.features) {
      const c = f.geometry.coordinates;
      if (c.length < 14) continue;
      const m = c[Math.floor(c.length / 2)];
      L.marker([m[1], m[0]], {
        pane: "isobars", interactive: false,
        icon: L.divIcon({ className: "isobar-label", html: f.properties.hpa, iconSize: [30, 12], iconAnchor: [15, 6] }),
      }).addTo(layer);
    }
    if (S.isoLayer) map.removeLayer(S.isoLayer);
    S.isoLayer = layer.addTo(map);
  }
  $("#isobars").addEventListener("change", (e) => {
    S.isobars = e.target.checked;
    store.set("isobars", S.isobars);
    S.isoStamp = null;
    updateIsobars(S.frames[S.i]?.t || new Date());
  });

  // ------------------------------------------------------------ map click
  map.on("click", async (e) => {
    const { lat, lng } = e.latlng;
    const pop = L.popup({ maxWidth: 260 }).setLatLng(e.latlng);
    const head = `<h3>${lat.toFixed(2)}°N ${Math.abs(lng).toFixed(2)}°${lng < 0 ? "W" : "E"}</h3>`;
    const btn = `<button type="button" data-go>Forecast for here</button>`;
    pop.setContent(`<div class="pop">${head}${btn}</div>`).openOn(map);
    const wire = () => pop.getElement()?.querySelector("[data-go]")?.addEventListener("click", () => {
      setPlace({ name: `${lat.toFixed(2)}, ${lng.toFixed(2)}`, lat, lon: lng, custom: true });
      map.closePopup();
    });
    wire();
    if (!S.nwp?.frames?.length) return;
    const t = S.frames[S.i]?.t || new Date();
    let best = S.nwp.frames[0];
    for (const f of S.nwp.frames) if (Math.abs(new Date(f.valid) - t) < Math.abs(new Date(best.valid) - t)) best = f;
    try {
      const v = await getJSON(`/api/nwp/inspect?stamp=${best.stamp}&lat=${lat}&lon=${lng}`);
      const rows = [
        ["Temp", v.temp != null && `${v.temp.toFixed(1)} °C`],
        ["Rain", v.rain != null && `${v.rain.toFixed(1)} mm/h`],
        ["Wind", v.wind != null && `${compass(v.wind_dir)} ${Math.round(v.wind)} km/h`],
        ["Cloud", v.cloud != null && `${Math.round(v.cloud)} %`],
        ["MSLP", v.msl != null && `${Math.round(v.msl)} hPa`],
        ["Gusts", v.gust != null && `${Math.round(v.gust)} km/h`],
        ["Visibility", v.vis != null && (v.vis >= 10 ? "10 km+" : `${v.vis < 1 ? Math.round(v.vis * 1000) + " m" : v.vis.toFixed(1) + " km"}`)],
        ["Snow", v.snow != null && v.snow >= 0.5 && `${v.snow.toFixed(0)} mm w.e.`],
        ["Lightning", v.lightning != null && v.lightning >= 0.01 && v.lightning.toFixed(2)],
      ].filter((r) => r[1]);
      pop.setContent(`<div class="pop">${head}<table>${rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("")}</table>` +
        `<h3 style="margin:6px 0 0">model · ${fDate.format(new Date(best.valid))} ${hm(new Date(best.valid))}</h3>${btn}</div>`);
      wire();
    } catch { /* outside model domain */ }
  });

  const compass = (deg) => (deg == null ? "" : ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8]);

  // ------------------------------------------------------------ forecast panel
  function buildPlaces() {
    const sel = $("#place");
    const opts = PLACES.map((p, k) => `<option value="${k}">${p[0]}</option>`);
    if (S.place?.custom) opts.unshift(`<option value="c">${esc(S.place.name)}</option>`);
    sel.innerHTML = opts.join("");
    const k = PLACES.findIndex((p) => p[0] === S.place?.name);
    sel.value = S.place?.custom ? "c" : String(Math.max(0, k));
  }
  $("#place").addEventListener("change", (e) => {
    const p = PLACES[+e.target.value];
    if (p) setPlace({ name: p[0], lat: p[1], lon: p[2] });
  });

  function setPlace(p) {
    S.place = p;
    store.set("place", p);
    buildPlaces();
    if (S.homePin) map.removeLayer(S.homePin);
    S.homePin = L.marker([p.lat, p.lon], {
      interactive: false, icon: L.divIcon({ className: "", html: '<div class="home-pin"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }),
    }).addTo(map);
    loadForecast();
  }

  async function loadForecast() {
    const p = S.place;
    let fc;
    try { fc = await getJSON(`/api/forecast?lat=${p.lat}&lon=${p.lon}`); } catch (e) {
      $("#now-desc").textContent = "Forecast unavailable right now.";
      return;
    }
    const hours = fc.hours.map((h) => ({ ...h, t: new Date(h.time) }));
    const now = Date.now();
    const cur = hours.find((h) => h.t.getTime() >= now - 30 * 60e3) || hours[0];
    if (!cur) return;
    const sym = cur.symbol || hours.find((h) => h.symbol && h.t >= cur.t)?.symbol;
    $("#now-temp").innerHTML = `${cur.temp != null ? Math.round(cur.temp) : "–"}<sup>°C</sup>`;
    $("#now-glyph").innerHTML = Glyphs.glyph(sym);
    $("#now-desc").textContent = Glyphs.describe(sym) + (cur.dew != null ? ` · dew point ${Math.round(cur.dew)}°` : "");
    const next3 = hours.filter((h) => h.t > cur.t - 1 && h.t <= cur.t.getTime() + 3 * 3.6e6).reduce((a, h) => a + (h.precip || 0), 0);
    const cells = [
      ["Wind", `${compass(cur.wind_dir)} ${cur.wind_kmh ?? "–"}`, "km/h"],
      ["Gusts", `${cur.gust_kmh ?? "–"}`, "km/h"],
      ["Rain 3 h", next3.toFixed(1), "mm"],
      ["Humidity", `${cur.humidity != null ? Math.round(cur.humidity) : "–"}`, "%"],
      ["Pressure", `${cur.pressure != null ? Math.round(cur.pressure) : "–"}`, "hPa"],
      ["Cloud", `${cur.cloud != null ? Math.round(cur.cloud) : "–"}`, "%"],
    ];
    $("#now-grid").innerHTML = cells.map(([k, v, u]) => `<div><dt>${k}</dt><dd>${v} <small>${u}</small></dd></div>`).join("");
    drawMeteogram(hours.filter((h) => h.t >= cur.t && h.t <= cur.t.getTime() + 48 * 3.6e6));
    drawDays(hours);
  }

  function drawMeteogram(hs) {
    const el = $("#meteogram");
    if (hs.length < 2) { el.innerHTML = ""; return; }
    const W = 300, top = 16, tH = 70, pTop = top + tH + 6, pH = 30, wY = pTop + pH + 30;
    const t0 = hs[0].t.getTime(), t1 = hs[hs.length - 1].t.getTime();
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    const temps = hs.map((h) => h.temp).filter((v) => v != null);
    const tmin = Math.floor(Math.min(...temps)) - 1, tmax = Math.ceil(Math.max(...temps)) + 1;
    const y = (v) => top + tH - ((v - tmin) / (tmax - tmin)) * tH;
    const pmax = Math.max(2, ...hs.map((h) => (h.precip || 0) / (h.period_h || 1)));
    const parts = [];
    // hour grid + day separators
    for (const h of hs) {
      const hh = +fH.format(h.t);
      const X = x(h.t).toFixed(1);
      if (hh === 0) {
        parts.push(`<line class="day" x1="${X}" x2="${X}" y1="${top - 12}" y2="${pTop + pH}"/>`);
        parts.push(`<text x="${+X + 3}" y="${top - 4}">${fDay.format(h.t)}</text>`);
      } else if (hh % 6 === 0) {
        parts.push(`<line class="grid" x1="${X}" x2="${X}" y1="${pTop + pH}" y2="${pTop + pH + 3}"/>`);
        parts.push(`<text x="${X}" y="${pTop + pH + 12}" text-anchor="middle">${String(hh).padStart(2, "0")}</text>`);
      }
    }
    parts.push(`<line class="grid" x1="0" x2="${W}" y1="${pTop + pH}" y2="${pTop + pH}"/>`);
    // precip bars (rate, so 1 h and 3 h periods compare)
    for (const h of hs) {
      if (!h.precip) continue;
      const rate = h.precip / (h.period_h || 1);
      const bh = Math.max(1, (rate / pmax) * pH);
      const w = Math.max(2, (x(h.t) - x(h.t - (h.period_h || 1) * 3.6e6)) - 1);
      parts.push(`<rect class="p-bar" x="${(x(h.t) - w).toFixed(1)}" y="${(pTop + pH - bh).toFixed(1)}" width="${w.toFixed(1)}" height="${bh.toFixed(1)}"/>`);
    }
    // temperature line + extremes
    const pts = hs.filter((h) => h.temp != null).map((h) => `${x(h.t).toFixed(1)},${y(h.temp).toFixed(1)}`);
    parts.push(`<polyline class="t-line" points="${pts.join(" ")}"/>`);
    const withT = hs.filter((h) => h.temp != null);
    const hi = withT.reduce((a, b) => (b.temp > a.temp ? b : a));
    const lo = withT.reduce((a, b) => (b.temp < a.temp ? b : a));
    const anchor = (X) => (X < 16 ? "start" : X > W - 16 ? "end" : "middle");
    parts.push(`<text class="t-lbl" x="${x(hi.t)}" y="${y(hi.temp) - 5}" text-anchor="${anchor(x(hi.t))}">${Math.round(hi.temp)}°</text>`);
    parts.push(`<text class="t-lbl" x="${x(lo.t)}" y="${y(lo.temp) + 13}" text-anchor="${anchor(x(lo.t))}">${Math.round(lo.temp)}°</text>`);
    parts.push(`<text x="0" y="${pTop + 8}">${pmax.toFixed(0)} mm/h</text>`);
    // wind row every 3 h
    for (const h of hs) {
      if (+fH.format(h.t) % 3 !== 0 || h.wind_dir == null) continue;
      const X = x(h.t);
      if (X < 6 || X > W - 6) continue;
      const rot = (h.wind_dir + 180) % 360; // arrow points where wind goes
      parts.push(`<g transform="translate(${X.toFixed(1)} ${wY}) rotate(${rot})"><path class="wind" d="M0 5V-5M-3 -2L0 -5L3 -2"/></g>`);
      parts.push(`<text x="${X.toFixed(1)}" y="${wY + 16}" text-anchor="middle">${h.wind_kmh ?? ""}</text>`);
    }
    el.innerHTML = `<svg viewBox="0 -2 ${W} ${wY + 20}" role="img" aria-label="Temperature, rain and wind for the next 48 hours">${parts.join("")}</svg>`;
  }

  function drawDays(hours) {
    const days = new Map();
    for (const h of hours) {
      const k = dayKey(h.t);
      if (!days.has(k)) days.set(k, []);
      days.get(k).push(h);
    }
    const rows = [...days.values()].filter((d) => d.length >= 3).slice(0, 8);
    const all = rows.flatMap((d) => d.map((h) => h.temp).filter((v) => v != null));
    const gmin = Math.min(...all), gmax = Math.max(...all);
    const pct = (v) => ((v - gmin) / Math.max(1, gmax - gmin)) * 100;
    $("#days").innerHTML = rows.map((d, k) => {
      const temps = d.map((h) => h.temp).filter((v) => v != null);
      const mn = Math.min(...temps), mx = Math.max(...temps);
      const mm = d.reduce((a, h) => a + (h.precip || 0), 0);
      const mid = d.reduce((a, b) => (Math.abs(+fH.format(b.t) - 13) < Math.abs(+fH.format(a.t) - 13) && b.symbol ? b : a), d[0]);
      const sym = mid.symbol || d.find((h) => h.symbol)?.symbol;
      const windy = d.reduce((a, b) => ((b.wind_kmh || 0) > (a.wind_kmh || 0) ? b : a), d[0]);
      return `<li>
        <span class="d">${k === 0 ? "Today" : fDay.format(d[0].t)}</span>
        ${Glyphs.glyph(sym)}
        <span class="range"><span class="lo">${Math.round(mn)}°</span><span class="bar"><i style="left:${pct(mn)}%;right:${100 - pct(mx)}%"></i></span><span>${Math.round(mx)}°</span></span>
        <span class="mm ${mm < 0.2 ? "dry" : ""}">${mm < 0.2 ? "dry" : mm.toFixed(1) + " mm"}</span>
        <span class="w">${compass(windy.wind_dir)} ${windy.wind_kmh ?? ""}</span>
      </li>`;
    }).join("");
  }

  // ------------------------------------------------------------ warnings / status
  async function loadWarnings() {
    let ws = [];
    try { ws = await getJSON("/api/warnings"); } catch { return; }
    const fT = fmt({ weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    $("#warnings").innerHTML = ws.map((w) => {
      const lvl = (w.level || "").toLowerCase();
      const advisory = /blight|advisory/i.test((w.type || "") + (w.headline || ""));
      const regions = w.regions || [];
      const counties = regions.filter((r) => REGIONS[r]);
      const where = counties.length >= 20 ? "Ireland" : counties.map((r) => REGIONS[r]).join(", ") || "Marine areas";
      return `<div class="warning ${advisory ? "advisory" : lvl}">
        <b>${esc(w.headline)}</b>
        <small>${esc(where)} · until ${fT.format(new Date(w.expiry))}</small>
        ${advisory ? "" : `<p>${esc(w.description)}</p>`}
      </div>`;
    }).join("");
  }

  function drawStatus() {
    const bits = [];
    const lastRadar = S.radar?.frames?.at(-1);
    if (lastRadar) bits.push(`radar ${hm(new Date(lastRadar.time))}`);
    if (S.nwp?.run) {
      const r = new Date(S.nwp.run);
      bits.push(`model ${fmt({ hour: "2-digit", hour12: false, timeZone: "UTC" }).format(r)}Z`);
      $("#run-label").textContent = `· ${fmt({ hour: "2-digit", hour12: false, timeZone: "UTC" }).format(r)}Z run`;
    }
    const err = !S.cfg?.has_key ? "no API key" : S.status?.radar?.error || S.status?.nwp?.error ? "feed error" : "";
    $("#status").innerHTML = bits.join(" · ") + (err ? `<br><span class="bad" title="${esc(S.status?.radar?.error || S.status?.nwp?.error || "")}">${err}</span>` : "");
    document.querySelectorAll("input[name=layer]").forEach((r) => {
      if (r.value === "radar") return;
      const has = r.value === "radaracc" ? !!S.radar?.acc?.frames?.length
        : !!S.nwp?.frames?.some((f) => f.layers.includes(r.value));
      r.closest("label").classList.toggle("off", !has);
    });
  }

  // ------------------------------------------------------------ data refresh
  async function refreshRadar() {
    try {
      const r = await getJSON("/api/radar");
      const sig = (x) => JSON.stringify([x?.frames?.map((f) => f.stamp), x?.acc?.frames?.map((f) => f.stamp)]);
      const changed = sig(r) !== sig(S.radar);
      S.radar = r;
      if (r.coverage && !S.coverage) {
        S.coverage = L.imageOverlay(r.coverage + "?v=" + Date.now(), r.bounds, { interactive: false, opacity: S.mode === "radar" ? 1 : 0 }).addTo(map);
      }
      if (changed && S.booted && isRadar(S.mode)) {
        const follow = S.followLatest || !S.frames.length;
        const cur = S.frames[S.i]?.stamp;
        S.frames = framesFor(S.mode);
        drawTicks();
        drawLegend();
        emptyState();
        if (!S.playing) {
          const k = follow ? S.frames.length - 1 : Math.max(0, S.frames.findIndex((f) => f.stamp === cur));
          show(k);
        }
      }
    } catch (e) { console.warn(e); }
    drawStatus();
  }

  async function refreshNwp() {
    try {
      const n = await getJSON("/api/nwp");
      const changed = n.run_id !== S.nwp?.run_id || (n.frames?.length || 0) !== (S.nwp?.frames?.length || 0);
      S.nwp = n;
      if (changed) {
        S.isoCache.clear();
        S.isoStamp = null;
        if (S.booted && S.mode !== "radar") setMode(S.mode, true);
        else updateIsobars(S.frames[S.i]?.t || new Date());
      }
    } catch (e) { console.warn(e); }
    drawStatus();
  }

  async function refreshStatus() {
    try { S.status = await getJSON("/api/status"); } catch { /* ignore */ }
    drawStatus();
    if (!S.frames.length) emptyState();
  }

  // ------------------------------------------------------------ boot
  (async function boot() {
    S.cfg = await getJSON("/api/config").catch(() => ({ home: { name: "Dublin", lat: 53.35, lon: -6.26 }, has_key: false, basemap: "openfreemap" }));
    setupBasemap(S.cfg.basemap || "openfreemap");
    if (!S.place) S.place = { name: S.cfg.home.name, lat: S.cfg.home.lat, lon: S.cfg.home.lon, custom: !PLACES.some((p) => p[0] === S.cfg.home.name) };
    setPlace(S.place);
    await Promise.all([refreshStatus(), refreshRadar(), refreshNwp()]);
    const avail = (m) => m === "radar" || (m === "radaracc" ? !!S.radar?.acc?.frames?.length
      : (S.nwp?.frames || []).some((f) => f.layers.includes(m)));
    const initial = avail(S.mode) ? S.mode : "radar";
    S.booted = true;
    setMode(initial, false);
    loadWarnings();
    setInterval(refreshRadar, 60e3);
    setInterval(refreshNwp, 5 * 60e3);
    setInterval(refreshStatus, 60e3);
    setInterval(loadWarnings, 10 * 60e3);
    setInterval(loadForecast, 30 * 60e3);
  })();
})();
