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

  // ------------------------------------------------------------ settings (saved in this browser)
  const DEFAULTS = {
    theme: "system", home: null, startView: "ireland", startLayer: "last",
    nowMode: "live", speed: "normal", tempUnit: "C", windUnit: "kmh",
  };
  let settings = { ...DEFAULTS, ...store.get("settings", {}) };
  const saveSettings = () => store.set("settings", settings);
  const sysDark = matchMedia("(prefers-color-scheme: dark)");
  const isDark = () => settings.theme === "dark" || (settings.theme === "system" && sysDark.matches);

  const WIND_UNITS = { kmh: [1, "km/h"], mph: [0.621371, "mph"], kt: [0.539957, "kt"], ms: [1 / 3.6, "m/s"] };
  const toT = (c) => (settings.tempUnit === "F" ? c * 9 / 5 + 32 : c);
  const tStr = (c) => (c == null ? "–" : String(Math.round(toT(c))));
  const tUnit = () => (settings.tempUnit === "F" ? "°F" : "°C");
  const toW = (kmh) => kmh * WIND_UNITS[settings.windUnit][0];
  const wStr = (kmh) => (kmh == null ? "–" : String(Math.round(toW(kmh))));
  const wUnit = () => WIND_UNITS[settings.windUnit][1];
  const SPEED = { slow: 1.8, normal: 1, fast: 0.55 };

  // ------------------------------------------------------------ map (MapLibre GL)
  // One GPU-drawn map for everything: basemap, weather tiles, isobars and labels
  // all move together, so zooming is smooth and nothing snaps into place afterwards.
  const OFM = "https://tiles.openfreemap.org";
  const GLYPHS = `${OFM}/fonts/{fontstack}/{range}.pbf`;
  const FONT = ["Noto Sans Regular"];
  const ink = () => (isDark() ? "#e6e3da" : "#1c1f22");
  const halo = () => (isDark() ? "rgba(23,25,27,0.9)" : "rgba(250,249,245,0.92)");
  let map = null;
  let firstSymbol;          // weather goes below the basemap's labels
  let coastAlways = false;

  async function buildStyle(kind) {
    const dark = isDark();
    const HALO = halo();
    coastAlways = false;
    const plain = {
      version: 8, glyphs: GLYPHS, sources: {},
      layers: [{ id: "bg", type: "background", paint: { "background-color": dark ? "#1b1e21" : "#e4e6e3" } }],
    };
    if (kind === "openfreemap") {
      try {
        const r = await fetch(`${OFM}/styles/${dark ? "dark" : "positron"}`);
        if (!r.ok) throw new Error(`style ${r.status}`);
        const style = await r.json();
        // Keep place and water names only (no road shields or POIs), with a firm halo
        // so they stay readable on top of coloured weather layers.
        const clutter = /highway|road|poi|shield|transport|aeroway|airport|rail|housenumber|building/i;
        style.layers = style.layers
          .filter((l) => l.type !== "symbol" || (!clutter.test(l.id) && l.layout?.["text-field"]))
          .map((l) => (l.type === "line" && dark && /road|highway|transport|bridge|tunnel/i.test(l.id)
            ? { ...l, paint: { ...l.paint, "line-opacity": 0.35 } }   // dark roads fight with weather colours
            : l))
          .map((l) => (l.type !== "symbol" ? l : {
            ...l,
            layout: { ...l.layout, "icon-image": "", "text-transform": "none", "text-letter-spacing": 0.02 },
            paint: { ...l.paint, "text-color": dark ? "#e6e3da" : "#2b2e31", "text-halo-color": HALO,
                     "text-halo-width": dark ? 1.1 : 1.4, "text-halo-blur": dark ? 0.6 : 0.2 },
          }));
        return style;
      } catch (e) {
        console.warn("OpenFreeMap basemap unavailable, using the bundled coastline:", e);
        kind = "none";
      }
    }
    if (kind === "osm") {
      plain.sources.osm = { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
                            maxzoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' };
      plain.layers.push({ id: "osm", type: "raster", source: "osm",
                          paint: dark ? { "raster-brightness-min": 0.92, "raster-brightness-max": 0.12, "raster-saturation": -0.7 }
                                      : { "raster-saturation": -0.35 } });
      return plain;
    }
    coastAlways = true; // "none": plain background + bundled coastline, nothing external
    return plain;
  }

  async function initMap(kind, view) {
    const style = await buildStyle(kind);
    map = new maplibregl.Map({
      container: "map", style, center: view?.center || [-7.9, 53.45], zoom: view?.zoom || 6.3,
      minZoom: 4.5, maxZoom: 12.5,
      attributionControl: false, dragRotate: false, pitchWithRotate: false, touchPitch: false,
      fadeDuration: 0,
    });
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    map.addControl(new maplibregl.AttributionControl({
      compact: true, customAttribution: 'Weather © <a href="https://www.met.ie">Met Éireann</a>',
    }), "bottom-right");
    window.aimsirMap = map; // handy from the dev console
    await new Promise((ok) => (map.loaded() ? ok() : map.once("load", ok)));
    addOverlays();
    if (matchMedia("(max-width: 760px)").matches) {
      document.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
    }
    map.on("click", onMapClick);
  }

  // Our own layers on top of whichever basemap style is loaded (re-run after a theme change).
  function addOverlays() {
    const INK = ink(), HALO = halo();
    // Some basemap label layers (water names) sit below the road lines in the style.
    // Lift every label to the top so the weather can go between shapes and labels.
    for (const l of map.getStyle().layers) if (l.type === "symbol") map.moveLayer(l.id);
    firstSymbol = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
    // Invisible marker layer: weather frames are inserted below it, overlays above it.
    map.addLayer({ id: "anchor-wx", type: "background", paint: { "background-opacity": 0 } }, firstSymbol);

    map.addSource("coast", { type: "geojson", data: "/static/coast.json" });
    map.addLayer({ id: "coast", type: "line", source: "coast", layout: { visibility: "none" },
                   paint: { "line-color": INK, "line-width": 0.7, "line-opacity": 0.55 } }, firstSymbol);

    map.addSource("isobars", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "isobars", type: "line", source: "isobars",
                   paint: { "line-color": INK, "line-opacity": 0.6,
                            "line-width": ["case", ["==", ["%", ["get", "hpa"], 20], 0], 1.4, 0.8] } }, firstSymbol);
    map.addLayer({ id: "isobar-labels", type: "symbol", source: "isobars",
                   layout: { "symbol-placement": "line", "symbol-spacing": 320, "text-field": ["to-string", ["get", "hpa"]],
                             "text-font": FONT, "text-size": 10, "text-keep-upright": true },
                   paint: { "text-color": isDark() ? "#b9b6ad" : "#4a4d50", "text-halo-color": HALO, "text-halo-width": 1.5 } });
    syncCoast();
  }

  async function applyTheme() {
    document.documentElement.dataset.theme = isDark() ? "dark" : "light";
    if (!map) return;
    const style = await buildStyle(S.cfg?.basemap || "openfreemap");
    map.setStyle(style, { diff: false });
    map.once("style.load", () => {
      addOverlays();
      S.wxIds.clear();
      S.currentId = null;
      S.wantId = null;
      S.coverageUrl = null;
      ensureCoverage();
      S.isoStamp = null;
      if (S.frames.length) show(S.i);
      else updateIsobars(new Date());
    });
  }
  sysDark.addEventListener("change", () => settings.theme === "system" && applyTheme());

  function syncCoast() {
    if (!map?.getLayer("coast")) return;
    const want = coastAlways || !isRadar(S.mode);
    map.setLayoutProperty("coast", "visibility", want ? "visible" : "none");
  }

  function ensureCoverage() {
    const url = S.radar?.coverage;
    if (!map || !url) return;
    if (map.getSource("coverage") && S.coverageUrl !== url) {
      map.removeLayer("coverage");
      map.removeSource("coverage");
    }
    if (!map.getSource("coverage")) {
      map.addSource("coverage", { type: "raster", tiles: [url], tileSize: 256, minzoom: 3, maxzoom: 10 });
      map.addLayer({ id: "coverage", type: "raster", source: "coverage",
                     paint: { "raster-fade-duration": 0, "raster-resampling": "nearest" } }, "coast");
      S.coverageUrl = url;
    }
    map.setLayoutProperty("coverage", "visibility", S.mode === "radar" ? "visible" : "none");
  }

  // ------------------------------------------------------------ state
  const S = {
    cfg: null, radar: null, nwp: null, mode: store.get("mode", "radar"),
    frames: [], i: 0, playing: false, timer: null, followLatest: true,
    currentId: null, wantId: null, wxIds: new Set(), coverageUrl: null,
    isobars: store.get("isobars", true), isoCache: new Map(), isoStamp: null,
    place: null, homePin: null, status: null, fcHours: undefined, live: undefined,
  };

  const RADAR_MODES = ["radar", "radaracc"];
  const isRadar = (m) => RADAR_MODES.includes(m);
  const MORE_MODES = ["radaracc", "gust", "lightning", "vis", "snow"];

  // ------------------------------------------------------------ frames (raster tile sources)
  function framesFor(mode) {
    const mk = (time, stamp, tpl) => ({ t: new Date(time), stamp, url: tpl.replace("{stamp}", stamp) });
    if (mode === "radar") {
      const r = S.radar;
      return r?.tiles ? r.frames.map((f) => mk(f.time, f.stamp, r.tiles)) : [];
    }
    if (mode === "radaracc") {
      const a = S.radar?.acc;
      return a?.tiles ? a.frames.map((f) => mk(f.time, f.stamp, a.tiles)) : [];
    }
    const n = S.nwp;
    if (!n?.frames || !n.tiles) return [];
    return n.frames.filter((f) => f.layers.includes(mode))
      .map((f) => mk(f.valid, f.stamp, n.tiles.replace("{layer}", mode)));
  }

  function sourceOpts(url) {
    const o = { type: "raster", tiles: [url], tileSize: 256, minzoom: 3, maxzoom: isRadar(S.mode) ? 11 : 10 };
    const bb = S.mode === "radar" ? S.radar?.bbox : isRadar(S.mode) ? null : S.nwp?.bbox;
    if (bb) o.bounds = [Math.max(bb[0], -180), Math.max(bb[1], -85), Math.min(bb[2], 180), Math.min(bb[3], 85)];
    return o;
  }

  function ensureFrame(f) {
    const id = `wx-${S.mode}-${f.stamp}`;
    if (!map.getSource(id)) {
      map.addSource(id, sourceOpts(f.url));
      map.addLayer({ id, type: "raster", source: id,
                     paint: { "raster-opacity": 0, "raster-fade-duration": 0, "raster-resampling": "linear" } }, "anchor-wx");
      S.wxIds.add(id);
    }
    return id;
  }

  function removeFrame(id) {
    if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(id)) map.removeSource(id);
    S.wxIds.delete(id);
  }

  function clearOverlays() {
    if (!map) return;
    for (const id of [...S.wxIds]) removeFrame(id);
    S.currentId = null;
    S.wantId = null;
  }

  function reveal(id) {
    if (S.currentId && S.currentId !== id && map.getLayer(S.currentId)) map.setPaintProperty(S.currentId, "raster-opacity", 0);
    if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", 1);
    S.currentId = id;
  }

  // Swap frames only once the next one's tiles are in, so animation never flashes blank.
  let revealTimer = null;
  function onSourceData(e) {
    if (e.sourceId && e.sourceId === S.wantId && map.isSourceLoaded(e.sourceId)) {
      clearTimeout(revealTimer);
      reveal(e.sourceId);
    }
  }

  function show(i) {
    if (!S.frames.length || !map) return;
    S.i = Math.max(0, Math.min(S.frames.length - 1, i));
    S.followLatest = isRadar(S.mode) && S.i === S.frames.length - 1;
    const f = S.frames[S.i];
    const id = ensureFrame(f);
    S.wantId = id;
    clearTimeout(revealTimer);
    if (map.isSourceLoaded(id)) reveal(id);
    else revealTimer = setTimeout(() => S.wantId === id && reveal(id), S.playing ? 1500 : 250);
    // keep a small window of frames loaded around the current one
    const keep = new Set([id, S.currentId]);
    for (let k = -1; k <= 3; k++) {
      const g = S.frames[(S.i + k + S.frames.length) % S.frames.length];
      if (g) keep.add(ensureFrame(g));
    }
    for (const other of [...S.wxIds]) if (!keep.has(other)) removeFrame(other);
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
    let waitingSince = 0;
    // Advance only when the next frame's tiles are ready (or after a short grace period),
    // so a slow connection slows the loop down instead of showing half-drawn frames.
    const step = () => {
      if (!S.playing) return;
      const next = S.i >= S.frames.length - 1 ? 0 : S.i + 1;
      const id = ensureFrame(S.frames[next]);
      waitingSince ||= Date.now();
      if (!map.isSourceLoaded(id) && Date.now() - waitingSince < 2500) {
        S.timer = setTimeout(step, 60);
        return;
      }
      waitingSince = 0;
      show(next);
      const hold = (S.i === S.frames.length - 1 ? 1400 : isRadar(S.mode) ? 260 : 420) * SPEED[settings.speed];
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
    ensureCoverage();
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
    const conv = lg.key === "temp" ? (v) => Math.round(toT(v))
      : ["wind", "gust"].includes(lg.key) ? (v) => Math.round(toW(v)) : (v) => v;
    const unit = lg.key === "temp" ? tUnit() : ["wind", "gust"].includes(lg.key) ? wUnit() : lg.unit;
    const lbl = (v0) => { const v = conv(v0); return Math.abs(v) < 1 && v !== 0 ? String(v).replace(/^0/, "") : String(v); };
    el.innerHTML =
      `<div class="ttl">${esc(lg.label)} · ${esc(unit)}</div>` +
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
  const EMPTY_FC = { type: "FeatureCollection", features: [] };
  async function updateIsobars(t) {
    document.querySelector("#isobars").checked = S.isobars;
    if (!map?.getSource("isobars")) return;
    const on = S.isobars && S.nwp?.frames?.length;
    if (!on) { map.getSource("isobars").setData(EMPTY_FC); S.isoStamp = null; return; }
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
    if (S.isoStamp === best.stamp) map.getSource("isobars").setData(gj);
  }
  $("#isobars").addEventListener("change", (e) => {
    S.isobars = e.target.checked;
    store.set("isobars", S.isobars);
    S.isoStamp = null;
    updateIsobars(S.frames[S.i]?.t || new Date());
  });

  // ------------------------------------------------------------ map click
  async function onMapClick(e) {
    const { lat, lng } = e.lngLat;
    const head = `<h3>${lat.toFixed(2)}°N ${Math.abs(lng).toFixed(2)}°${lng < 0 ? "W" : "E"}</h3>`;
    const btn = `<button type="button" data-go>Forecast for here</button>`;
    const pop = new maplibregl.Popup({ maxWidth: "260px", closeButton: true, focusAfterOpen: false })
      .setLngLat(e.lngLat).setHTML(`<div class="pop">${head}${btn}</div>`).addTo(map);
    const wire = () => pop.getElement()?.querySelector("[data-go]")?.addEventListener("click", () => {
      setPlace({ name: `${lat.toFixed(2)}, ${lng.toFixed(2)}`, lat, lon: lng, custom: true });
      pop.remove();
    });
    wire();
    if (!S.nwp?.frames?.length) return;
    const t = S.frames[S.i]?.t || new Date();
    let best = S.nwp.frames[0];
    for (const f of S.nwp.frames) if (Math.abs(new Date(f.valid) - t) < Math.abs(new Date(best.valid) - t)) best = f;
    try {
      const v = await getJSON(`/api/nwp/inspect?stamp=${best.stamp}&lat=${lat}&lon=${lng}`);
      const rows = [
        ["Temp", v.temp != null && `${toT(v.temp).toFixed(1)} ${tUnit()}`],
        ["Rain", v.rain != null && `${v.rain.toFixed(1)} mm/h`],
        ["Wind", v.wind != null && `${compass(v.wind_dir)} ${wStr(v.wind)} ${wUnit()}`],
        ["Cloud", v.cloud != null && `${Math.round(v.cloud)} %`],
        ["MSLP", v.msl != null && `${Math.round(v.msl)} hPa`],
        ["Gusts", v.gust != null && `${wStr(v.gust)} ${wUnit()}`],
        ["Visibility", v.vis != null && (v.vis >= 10 ? "10 km+" : `${v.vis < 1 ? Math.round(v.vis * 1000) + " m" : v.vis.toFixed(1) + " km"}`)],
        ["Snow", v.snow != null && v.snow >= 0.5 && `${v.snow.toFixed(0)} mm w.e.`],
        ["Lightning", v.lightning != null && v.lightning >= 0.5 && v.lightning.toFixed(v.lightning < 10 ? 1 : 0)],
      ].filter((r) => r[1]);
      if (!pop.isOpen()) return;
      pop.setHTML(`<div class="pop">${head}<table>${rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("")}</table>` +
        `<h3 style="margin:6px 0 0">model · ${fDate.format(new Date(best.valid))} ${hm(new Date(best.valid))}</h3>${btn}</div>`);
      wire();
    } catch { /* outside model domain */ }
  }

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
    buildPlaces();
    if (S.homePin) S.homePin.remove();
    const pin = Object.assign(document.createElement("div"), { className: "home-pin" });
    S.homePin = new maplibregl.Marker({ element: pin }).setLngLat([p.lon, p.lat]).addTo(map);
    loadForecast();
    loadLive();
  }

  async function loadForecast() {
    const p = S.place;
    try {
      const fc = await getJSON(`/api/forecast?lat=${p.lat}&lon=${p.lon}`);
      if (S.place !== p) return;
      S.fcHours = fc.hours.map((h) => ({ ...h, t: new Date(h.time) }));
    } catch {
      S.fcHours = null;
    }
    renderForecastPanels();
  }

  async function loadLive() {
    const p = S.place;
    try {
      const live = await getJSON(`/api/live?lat=${p.lat}&lon=${p.lon}`);
      if (S.place !== p) return;
      S.live = live;
    } catch {
      S.live = null;
    }
    renderNow();
  }

  function renderForecastPanels() {
    renderNow();
    const hours = S.fcHours;
    if (!hours) { $("#meteogram").innerHTML = ""; $("#days").innerHTML = ""; return; }
    const cur = currentHour();
    if (!cur) return;
    drawMeteogram(hours.filter((h) => h.t >= cur.t && h.t <= cur.t.getTime() + 48 * 3.6e6));
    drawDays(hours);
  }

  const currentHour = () => {
    const hours = S.fcHours;
    if (!hours?.length) return null;
    const now = Date.now();
    return hours.find((h) => h.t.getTime() >= now - 30 * 60e3) || hours[0];
  };

  function renderNow() {
    const mode = settings.nowMode;
    document.querySelectorAll("#now-mode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.v === mode)));
    const sec = $("#now");
    sec.classList.toggle("live", mode === "live");
    sec.classList.toggle("fc", mode !== "live");
    const cell = (k, v, u) => `<div><dt>${k}</dt><dd>${v}${u ? ` <small>${u}</small>` : ""}</dd></div>`;

    if (mode === "live") {
      const st = S.live?.station;
      const rd0 = S.live?.radar;
      const rd = rd0 && Date.now() - new Date(rd0.time) < 30 * 60e3 ? rd0 : null;   // ignore stale radar
      if (!st) {
        $("#now-kicker").textContent = S.live ? "No station observations available right now" : "Loading observations…";
        $("#now-temp").innerHTML = "–";
        $("#now-glyph").innerHTML = "";
        $("#now-desc").textContent = "";
        $("#now-grid").innerHTML = "";
        return;
      }
      const obsT = st.time ? new Date(st.time) : null;
      $("#now-kicker").textContent = `Observed · ${st.label}${st.distance_km > 1 ? ` · ${st.distance_km} km away` : ""}${obsT ? ` · ${hm(obsT)}` : ""}`;
      $("#now-temp").innerHTML = `${tStr(st.temp)}<sup>${tUnit()}</sup>`;
      $("#now-glyph").innerHTML = Glyphs.glyph(st.symbol);
      let radarTxt = "";
      if (rd?.covered) radarTxt = rd.rate_mmh > 0 ? `radar shows ${rd.rate_mmh.toFixed(1)} mm/h here` : "radar shows no rain here";
      $("#now-desc").textContent = [st.weather, radarTxt].filter(Boolean).join(" · ");
      $("#now-grid").innerHTML = [
        cell("Wind", `${st.wind_name || ""} ${wStr(st.wind_kmh)}`, wUnit()),
        cell("Humidity", st.humidity != null ? Math.round(st.humidity) : "–", "%"),
        cell("Pressure", st.pressure != null ? Math.round(st.pressure) : "–", "hPa"),
        cell("Rain, station", st.rain_mmh != null ? st.rain_mmh.toFixed(1) : "–", "mm/h"),
        cell("Radar here", rd?.covered ? (rd.rate_mmh > 0 ? rd.rate_mmh.toFixed(1) : "dry") : "–", rd?.covered && rd.rate_mmh > 0 ? "mm/h" : ""),
        cell("Radar time", rd?.time ? hm(new Date(rd.time)) : "–", ""),
      ].join("");
      return;
    }

    const hours = S.fcHours;
    const cur = currentHour();
    if (!cur) {
      $("#now-kicker").textContent = hours === null ? "Forecast unavailable right now" : "Loading forecast…";
      $("#now-temp").innerHTML = "–";
      $("#now-glyph").innerHTML = "";
      $("#now-desc").textContent = "";
      $("#now-grid").innerHTML = "";
      return;
    }
    const sym = cur.symbol || hours.find((h) => h.symbol && h.t >= cur.t)?.symbol;
    $("#now-kicker").textContent = `Model forecast · for ${hm(cur.t)}`;
    $("#now-temp").innerHTML = `${tStr(cur.temp)}<sup>${tUnit()}</sup>`;
    $("#now-glyph").innerHTML = Glyphs.glyph(sym);
    $("#now-desc").textContent = Glyphs.describe(sym) + (cur.dew != null ? ` · dew point ${tStr(cur.dew)}°` : "");
    const next3 = hours.filter((h) => h.t > cur.t - 1 && h.t <= cur.t.getTime() + 3 * 3.6e6).reduce((a, h) => a + (h.precip || 0), 0);
    $("#now-grid").innerHTML = [
      cell("Wind", `${compass(cur.wind_dir)} ${wStr(cur.wind_kmh)}`, wUnit()),
      cell("Gusts", wStr(cur.gust_kmh), wUnit()),
      cell("Rain 3 h", next3.toFixed(1), "mm"),
      cell("Humidity", cur.humidity != null ? Math.round(cur.humidity) : "–", "%"),
      cell("Pressure", cur.pressure != null ? Math.round(cur.pressure) : "–", "hPa"),
      cell("Cloud", cur.cloud != null ? Math.round(cur.cloud) : "–", "%"),
    ].join("");
  }

  document.querySelectorAll("#now-mode button").forEach((b) => b.addEventListener("click", () => {
    settings.nowMode = b.dataset.v;
    saveSettings();
    renderNow();
  }));

  function drawMeteogram(hs) {
    const el = $("#meteogram");
    if (hs.length < 2) { el.innerHTML = ""; return; }
    const W = 300, top = 16, tH = 70, pTop = top + tH + 6, pH = 30, wY = pTop + pH + 30;
    const t0 = hs[0].t.getTime(), t1 = hs[hs.length - 1].t.getTime();
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    hs = hs.map((h) => ({ ...h, temp: h.temp == null ? null : toT(h.temp) }));
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
      parts.push(`<text x="${X.toFixed(1)}" y="${wY + 16}" text-anchor="middle">${h.wind_kmh == null ? "" : wStr(h.wind_kmh)}</text>`);
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
        <span class="range"><span class="lo">${tStr(mn)}°</span><span class="bar"><i style="left:${pct(mn)}%;right:${100 - pct(mx)}%"></i></span><span>${tStr(mx)}°</span></span>
        <span class="mm ${mm < 0.2 ? "dry" : ""}">${mm < 0.2 ? "dry" : mm.toFixed(1) + " mm"}</span>
        <span class="w">${compass(windy.wind_dir)} ${windy.wind_kmh == null ? "" : wStr(windy.wind_kmh)}</span>
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
      ensureCoverage();
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

  // ------------------------------------------------------------ settings dialog
  function homePlace() {
    if (settings.home) return settings.home;
    const h = S.cfg?.home || { name: "Dublin", lat: 53.3498, lon: -6.2603 };
    return { name: h.name, lat: h.lat, lon: h.lon, custom: !PLACES.some((p) => p[0] === h.name) };
  }

  const dlg = $("#settings");
  function fillSettings() {
    dlg.querySelectorAll(".seg[data-setting]").forEach((seg) => {
      seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(settings[seg.dataset.setting] === b.dataset.v)));
    });
    dlg.querySelector("select[data-setting=startLayer]").value = settings.startLayer;
    const sel = $("#set-home");
    const opts = [`<option value="">Server default (${esc(S.cfg?.home?.name || "Dublin")})</option>`]
      .concat(PLACES.map((p, k) => `<option value="${k}">${p[0]}</option>`));
    if (settings.home?.custom) opts.push(`<option value="c">${esc(settings.home.name)}</option>`);
    sel.innerHTML = opts.join("");
    sel.value = !settings.home ? "" : settings.home.custom ? "c" : String(PLACES.findIndex((p) => p[0] === settings.home.name));
  }
  function applySetting(key) {
    saveSettings();
    if (key === "theme") applyTheme();
    if (key === "tempUnit" || key === "windUnit") { renderForecastPanels(); drawLegend(); }
    if (key === "nowMode") renderNow();
    if (key === "home") setPlace(homePlace());
    fillSettings();
  }
  $("#open-settings").addEventListener("click", () => { fillSettings(); $("#set-home-note").textContent = ""; dlg.showModal(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // click on the backdrop
  dlg.querySelectorAll(".seg[data-setting] button").forEach((b) => b.addEventListener("click", () => {
    const key = b.closest(".seg").dataset.setting;
    settings[key] = b.dataset.v;
    applySetting(key);
  }));
  dlg.querySelector("select[data-setting=startLayer]").addEventListener("change", (e) => {
    settings.startLayer = e.target.value;
    applySetting("startLayer");
  });
  $("#set-home").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "") settings.home = null;
    else if (v !== "c") { const p = PLACES[+v]; settings.home = { name: p[0], lat: p[1], lon: p[2] }; }
    applySetting("home");
  });
  $("#set-home-centre").addEventListener("click", () => {
    const c = map.getCenter();
    settings.home = { name: `${c.lat.toFixed(2)}, ${c.lng.toFixed(2)}`, lat: +c.lat.toFixed(4), lon: +c.lng.toFixed(4), custom: true };
    applySetting("home");
    $("#set-home-note").textContent = "Default set to the centre of the map.";
  });
  $("#set-home-gps").addEventListener("click", () => {
    const note = $("#set-home-note");
    if (!navigator.geolocation || !window.isSecureContext) {
      note.textContent = "Your browser only shares location over HTTPS. Open Aimsir through your HTTPS proxy, or use the map centre instead.";
      return;
    }
    note.textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition((pos) => {
      const { latitude: lat, longitude: lon } = pos.coords;
      settings.home = { name: "My location", lat: +lat.toFixed(4), lon: +lon.toFixed(4), custom: true };
      applySetting("home");
      note.textContent = `Default set to ${lat.toFixed(3)}, ${lon.toFixed(3)}.`;
    }, (err) => { note.textContent = `Couldn't get your location (${err.message}).`; }, { timeout: 10000 });
  });
  $("#settings-reset").addEventListener("click", () => {
    const themeChanged = settings.theme !== DEFAULTS.theme;
    settings = { ...DEFAULTS };
    saveSettings();
    if (themeChanged) applyTheme();
    renderForecastPanels();
    drawLegend();
    setPlace(homePlace());
    fillSettings();
  });

  // ------------------------------------------------------------ boot
  (async function boot() {
    S.cfg = await getJSON("/api/config").catch(() => ({ home: { name: "Dublin", lat: 53.35, lon: -6.26 }, has_key: false, basemap: "openfreemap" }));
    const home = homePlace();
    const view = settings.startView === "home" ? { center: [home.lon, home.lat], zoom: 8.2 } : null;
    await initMap(S.cfg.basemap || "openfreemap", view);
    map.on("sourcedata", onSourceData);
    if (settings.startLayer !== "last") S.mode = settings.startLayer;
    setPlace(home);
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
    setInterval(loadLive, 5 * 60e3);
  })();
})();
