// Small line-drawn weather glyphs, composed from Met Éireann symbol ids
// (Sun, LightCloud, PartlyCloud, Cloud, LightRainSun, RainThunder, Dark_Sun, ...).
(function () {
  const sun = (cx, cy, r) => {
    let rays = "";
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4, r1 = r + 2.2, r2 = r + 4.2;
      rays += `M${(cx + Math.cos(a) * r1).toFixed(1)} ${(cy + Math.sin(a) * r1).toFixed(1)}L${(cx + Math.cos(a) * r2).toFixed(1)} ${(cy + Math.sin(a) * r2).toFixed(1)}`;
    }
    return `<g class="sun"><circle cx="${cx}" cy="${cy}" r="${r}"/><path d="${rays}"/></g>`;
  };
  const moon = (cx, cy, r) =>
    `<path class="sun" d="M${cx + r * 0.35} ${cy - r}a${r} ${r} 0 1 0 ${r * 0.65} ${r * 1.45}a${r * 0.8} ${r * 0.8} 0 0 1 -${r * 0.65} -${r * 1.45}z"/>`;
  const cloud = (dx = 0, dy = 0, s = 1) =>
    `<path class="fill-p" transform="translate(${dx} ${dy}) scale(${s})" d="M6.5 17.5h11a3.6 3.6 0 0 0 .4-7.2 5.2 5.2 0 0 0-10-.9A3.9 3.9 0 0 0 6.5 17.5z"/>`;
  const drops = (n, cls = "drop") => {
    const xs = n === 1 ? [12] : n === 2 ? [10, 14.5] : [8.5, 12, 15.5];
    return `<path class="${cls}" d="${xs.map((x) => `M${x} 19.5l-1 2.8`).join("")}"/>`;
  };
  const flakes = (n) => {
    const xs = n === 1 ? [12] : n === 2 ? [9.5, 14.5] : [8, 12, 16];
    return xs.map((x) => `<g class="drop"><path d="M${x} 19.3v3.4M${x - 1.5} 20.1l3 1.8M${x + 1.5} 20.1l-3 1.8"/></g>`).join("");
  };
  const bolt = `<path class="sun" d="M12.6 17.8l-2 3h2.6l-1.4 2.7"/>`;

  function glyph(id) {
    id = id || "";
    const night = /^Dark_/.test(id) || /Moon/.test(id);
    const s = id.replace(/^Dark_/, "");
    const celestial = (cx, cy, r) => (night ? moon(cx, cy, r) : sun(cx, cy, r));
    let body = "";
    if (s === "Sun" || s === "Clear") {
      body = celestial(12, 12, 4.5);
    } else if (s === "Fog") {
      body = `<path d="M4 9h16M6 12.5h13M4 16h16M7 19.5h10"/>`;
    } else {
      const hasSun = /Sun|LightCloud|PartlyCloud/.test(s);
      if (hasSun) body += night ? moon(8.5, 8, 3.4) : sun(8.5, 8, 3);
      const shift = hasSun ? 1.5 : 0;
      body += cloud(shift, hasSun ? 0.5 : 0, s === "LightCloud" ? 0.92 : 1);
      const heavy = /Heavy|^Rain/.test(s) ? 3 : /Light|Drizzle/.test(s) ? 1 : 2;
      if (/Snow/.test(s)) body += flakes(heavy);
      else if (/Sleet/.test(s)) body += drops(1) + flakes(1).replace(/12/g, "15");
      else if (/Rain|Drizzle/.test(s)) body += drops(/Drizzle/.test(s) ? 1 : heavy);
      if (/Thunder/.test(s)) body += bolt;
    }
    return `<svg viewBox="0 0 24 24" class="g" aria-hidden="true">${body}</svg>`;
  }

  const words = {
    Sun: "Clear", LightCloud: "Mostly clear", PartlyCloud: "Partly cloudy", Cloud: "Cloudy",
    Fog: "Fog", Drizzle: "Drizzle", DrizzleSun: "Drizzle, bright spells", LightRain: "Light rain",
    LightRainSun: "Light showers", Rain: "Rain", RainSun: "Showers", Sleet: "Sleet", SleetSun: "Sleet showers",
    Snow: "Snow", SnowSun: "Snow showers", RainThunder: "Rain and thunder", LightRainThunderSun: "Thundery showers",
    RainThunderSun: "Thundery showers", HeavySnow: "Heavy snow", LightSnow: "Light snow",
  };
  function describe(id) {
    if (!id) return "";
    const s = id.replace(/^Dark_/, "");
    if (words[s]) return words[s];
    return s.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  }

  window.Glyphs = { glyph, describe };
})();
