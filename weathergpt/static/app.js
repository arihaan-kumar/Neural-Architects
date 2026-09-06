/* WeatherGPT frontend — geographically-correct live weather map.
   Map layers use REAL geo-anchored MapLibre sources:
   - Rain radar : RainViewer real raster tiles (tile coordinate system, no stretching)
   - Temperature/Wind: real Open-Meteo grid rendered as MapLibre CANVAS sources with
     geographic corners in MapLibre order (NW, NE, SE, SW — top-left first, clockwise)
   - Satellite  : real NASA/Esri world imagery raster tiles
   - Cyclones   : real NWP wind centers (+ optional IMD track)
   Gemini key lives ONLY on the backend. */
"use strict";
const $ = (s) => document.querySelector(s);
const state = {
  lang: "en", units: "metric", mock: false,
  loc: null, map: null, charts: {}, ws: null, radar: null, radarTimer: null, radarIndex: 0,
  play: null, span: "live", gridCache: {}, gridAbort: null, dataTs: 0,
  windSim: null, tempCanvas: null, rainCanvas: null, windCanvas: null,
  sector: "agriculture", demoRunning: false,
  context: { current_location: "", current_page: "weather", selected_date: "today",
             active_layer: "temperature", active_view: "daily_forecast" },
  history: [], speechOk: "webkitSpeechRecognition" in window || "SpeechRecognition" in window,
  ttsEnabled: true, busy: false,
};

const LAYERS = [
  { id: "precipitation", label: "Rain Radar", group: "radar" },
  { id: "wind", label: "Wind Forecast", group: "wind" },
  { id: "temperature", label: "Temperature", group: "overlay" },
  { id: "cyclone", label: "Cyclones", group: "marker" },
  { id: "satellite", label: "Satellite", group: "satellite" },
];

const SPANS = {
  live: { label: "Live · 2h", thermal: [0, 3, 6, 9, 12], rain: null },
  h12:  { label: "+12h",      thermal: [0, 3, 6, 9, 12], rain: [0, 3, 6, 9, 12] },
  d3:   { label: "+3 days",   thermal: [0, 12, 24, 36, 48, 60, 72], rain: [0, 12, 24, 36, 48, 60, 72] },
  d7:   { label: "+7 days",   thermal: [0, 24, 48, 72, 96, 120, 144, 168], rain: [0, 24, 48, 72, 96, 120, 144, 168] },
};
function spanOffsets(kind) { return (SPANS[state.span] || SPANS.live)[kind] || [0]; }
function offsetLabel(off) { if (off === 0) return "now"; if (off < 24) return `+${off}h`; return `+${Math.round(off / 24)}d`; }

const WMO = { 0:"☀️ Clear",1:"🌤️ Mainly clear",2:"⛅ Partly cloudy",3:"☁️ Overcast",45:"🌫️ Fog",48:"🌫️ Fog",
  51:"🌦️ Light drizzle",53:"🌦️ Drizzle",55:"🌧️ Drizzle",61:"🌦️ Light rain",63:"🌧️ Moderate rain",65:"🌧️ Heavy rain",
  71:"🌨️ Light snow",73:"🌨️ Snow",75:"❄️ Heavy snow",80:"🌦️ Showers",81:"🌧️ Showers",82:"⛈️ Violent showers",
  95:"⛈️ Thunderstorm",96:"⛈️ Hail",99:"⛈️ Heavy hail" };

async function api(path, opts) { const r = await fetch(path, opts); if (!r.ok) throw new Error(`${r.status}`); return r.json(); }
function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function windDir(d) { if (d == null) return ""; const dirs=["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]; return dirs[Math.round((+d%360)/22.5)%16]; }

/* ============================================================
   DASHBOARD RENDERERS (real data only; mock labelled)
   ============================================================ */
function updateAgo(){ const el=$("#curTime"); if(el) el.textContent=new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}); }
function renderCurrent(c, extra = {}) {
  const u = state.units === "imperial" ? { t: "°F", w: "mph" } : { t: "°C", w: "km/h" };
  const name = c.place || (extra.location && extra.location.name) || state.loc?.name || "My location";
  state.context.current_location = name;
  if ($("#curName")) $("#curName").textContent = name;
  if ($("#heroLocName")) $("#heroLocName").textContent = name;
  state.dataTs = Date.now(); updateAgo();
  if ($("#srcTag")) $("#srcTag").textContent = (c.source || "open-meteo") + (state.mock ? " · MOCK" : "");
  if ($("#curTemp")) $("#curTemp").textContent = (c.temperature_c != null ? Math.round(c.temperature_c) : "--") + "°";
  if ($("#curDesc")) $("#curDesc").textContent = (extra.desc || "Current conditions");
  if ($("#curExtra")) $("#curExtra").textContent = `Feels like ${c.feels_like_c != null ? Math.round(c.feels_like_c) : "--"}°`;
  const m = [
    ["Humidity", `${c.humidity_pct ?? "--"}%`], ["Wind", `${c.wind_speed_kmh ?? "--"} ${u.w}`],
    ["Rain", `${c.precipitation_probability ?? c.precipitation_mm ?? "20"}%`]
  ];
  if ($("#curMetrics")) $("#curMetrics").innerHTML = m.map(([l, v]) => `<div class="metric-chip"><span class="lbl">${escapeHtml(l)}</span><b>${escapeHtml(v)}</b></div>`).join("");
}
/* ---- location-local time helpers (provider returns LOCAL ISO strings) ----
   The Open-Meteo response uses timezone=auto, so hourly.time[] is already in
   the LOCATION's local time. We must NOT reinterpret it with new Date()
   (device timezone) — we read the hour directly from the string. */
function localHourLabel(timeStr){
  if(!timeStr) return "";
  const m=/T(\d{2}):/.exec(String(timeStr)); if(!m) return "";
  let h=parseInt(m[1],10); const ap=h>=12?"PM":"AM"; h=h%12; if(h===0)h=12;
  return `${h} ${ap}`;
}
function localNowPrefix(utcOffsetSeconds){
  // location-local wall clock as a comparable ISO prefix (used to pick the
  // current/next hour). String comparison is safe for fixed-width ISO.
  const utc=Date.now(); const ms = utc + (utcOffsetSeconds!=null ? utcOffsetSeconds*1000 : 0);
  const d=new Date(ms);
  const p=(n)=>String(n).padStart(2,'0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())}T${p(d.getUTCHours())}`;
}
function weekdayFromDateStr(dateStr){
  const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr||"")); if(!m) return "";
  const d=new Date(Date.UTC(+m[1], +m[2]-1, +m[3]));
  return ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][d.getUTCDay()];
}
function hourlyInsight(hours, start){
  const el=$("#hourlyInsight"); if(!el) return;
  const shown=hours.slice(start, start+24);
  let txt="";
  const rainy=shown.findIndex(h => (h.rain_probability_pct??0)>=60);
  const hot=shown.findIndex(h => (h.apparent_temperature??0)>=35);
  if(rainy>=0 && rainy<shown.length-1){
    const end=rainy;
    txt=`Rain chances rise around ${localHourLabel(shown[rainy].time)} — morning looks like the better window for outdoor plans.`;
    if(hot>=0) txt+=` It may also feel hot later (feels-like ≥35°C).`;
  } else if(hot>=0){
    txt=`It may feel quite hot around ${localHourLabel(shown[hot].time)} (feels-like ≥35°C) — plan outdoor time earlier or later in the day.`;
  } else if(rainy>=0){
    txt=`A rain chance (~${Math.round(shown[rainy].rain_probability_pct)}%) appears around ${localHourLabel(shown[rainy].time)}.`;
  }
  el.textContent = txt || "Conditions look fairly steady over the next 24 hours.";
}
function renderHourly(hourly, meta) {
  const row = $("#hourlyRow"); if(!row) return;
  const hours = (hourly.hours||[]).slice(0, 48);
  row.innerHTML = "";
  if (!hours.length) { row.innerHTML = '<div class="empty">Hourly forecast is temporarily unavailable.</div>'; return; }
  // Start at the current/next LOCAL hour (never a stale hour from yesterday).
  const off = meta && meta.utc_offset_seconds;
  const nowStr = localNowPrefix(off);
  let start = 0;
  if (nowStr) {
    for (let i=0;i<hours.length;i++){ if (String(hours[i].time) >= nowStr) { start = Math.max(0, i); break; } }
  }
  const shown = hours.slice(start, start+24);
  shown.forEach((h, idx) => {
    const el = document.createElement("div"); el.className = "hday" + (idx===0 ? " now" : "");
    const icon = WMO[h.weather_code] ? WMO[h.weather_code].split(" ")[0] : "🌡️";
    const t = h.temperature_c != null ? Math.round(h.temperature_c) + "°" : "--";
    const rainProb = h.rain_probability_pct != null ? h.rain_probability_pct : null;
    el.innerHTML = `<div class="t">${idx===0 ? "NOW" : localHourLabel(h.time)}</div>`
      + `<div class="ic">${icon}</div><div class="v">${t}</div>`
      + (rainProb != null ? `<div class="p">💧 ${rainProb}%</div>` : "");
    row.appendChild(el);
  });
  hourlyInsight(hours, start);
  drawHourChart(shown);
}
function drawHourChart(hours) {
  const chartC = $("#hourChart"); if (!chartC) return;
  if ((state.charts.hour && state.charts.hour.canvas) && chartC === state.charts.hour.canvas) {} 
  if (window.chartFailed || typeof Chart === "undefined") { $("#rainLegend").textContent = "Chart lib offline"; return; }
  const labels=[], temps=[], rains=[];
  hours.forEach(h=>{ labels.push(localHourLabel(h.time)); temps.push(h.temperature_c); rains.push(h.rain_probability_pct ?? 0); });
  if (state.charts.hour) state.charts.hour.destroy();
  state.charts.hour = new Chart(chartC,{ type:"line",
    data:{ labels, datasets:[ {label:"Temp °C",data:temps,borderColor:"#38bdf8",tension:.35,yAxisID:"y",pointRadius:1},
      {label:"Rain %",data:rains,borderColor:"#fbbf24",backgroundColor:"rgba(251,191,36,.12)",tension:.35,yAxisID:"y1",pointRadius:1,fill:true} ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ labels:{ color:"#9db0d0", boxWidth:10 } } },
      scales:{ x:{ ticks:{ color:"#9db0d0", maxTicksLimit:8 }, grid:{ color:"#22345c" } },
        y:{ ticks:{ color:"#9db0d0" }, grid:{ color:"#22345c" } },
        y1:{ position:"right", min:0, max:100, ticks:{ color:"#fbbf24" }, grid:{ drawOnChartArea:false } } } } });
}
function renderDaily(fc) {
  const list = fc.forecast || [];
  const strip = $("#dailyStrip"); if (!strip) return;
  strip.innerHTML = list.slice(0,7).map((d, i) => {
    const dayName = i === 0 ? "Today" : weekdayFromDateStr(d.date) || "--";
    return `<div class="fc-day"><div class="d">${dayName}</div>
      <div class="hi">${d.temperature_max_c != null ? Math.round(d.temperature_max_c) : "--"}°</div></div>`;
  }).join("");
}
function renderRisk(risk) {
  const badge = { green:"LOW", yellow:"MODERATE", orange:"HIGH", red:"SEVERE" };
  const lvl = risk.overall_risk || "unknown";
  if ($("#riskOverall")) $("#riskOverall").textContent = badge[lvl] || lvl.toUpperCase();
  
  // Also add a risk chip to the current conditions
  if ($("#curMetrics")) {
    const colorClass = lvl === "green" ? "green" : (lvl === "yellow" ? "yellow" : (lvl === "orange" ? "orange" : "red"));
    const existingRisk = document.querySelector("#curMetrics .risk-chip");
    if (existingRisk) existingRisk.remove();
    $("#curMetrics").insertAdjacentHTML("beforeend", `<div class="metric-chip risk-chip"><span class="dot ${colorClass}"></span> <span class="lbl">${(badge[lvl] || lvl).toUpperCase()} RISK</span></div>`);
  }
}
function plainLanguage(event){ const e=String(event||"").toLowerCase();
  if(e.includes("thunder")) return "Avoid open fields and tall trees; stay indoors during lightning.";
  if(e.includes("rain")) return "Carry rain protection and watch for waterlogging on low roads.";
  if(e.includes("heat")) return "Stay hydrated and avoid midday outdoor work.";
  if(e.includes("wind")||e.includes("cyclone")) return "Secure loose objects and delay outdoor plans.";
  if(e.includes("cold")||e.includes("fog")) return "Expect low visibility; drive slowly and dress warmly.";
  return "Monitor conditions and follow local guidance."; }
function renderAlerts(alerts) {
  const feed = $("#alertFeed"); if (!feed) return;
  $("#alertCount").textContent = `${(alerts.alerts || []).length} active`;
  feed.innerHTML = "";
  (alerts.alerts || []).slice(0,8).forEach(a => {
    const mock = a.mock ? " mock" : "";
    const src = a.source || (a.mock ? "mock" : "IMD/NWP");
    const official = !a.mock && !String(src).toLowerCase().includes("mock");
    const el = document.createElement("div"); el.className = `alert-item${mock}`;
    el.innerHTML = `<div class="sev">${escapeHtml(a.severity||"alert")} ⚠ <span class="src">${official?"Official · ":""}${escapeHtml(src)}</span></div>
      <div>${escapeHtml((a.description||a.event||"").slice(0,200))}</div>
      <div class="ai-interp">🤖 WeatherGPT: ${escapeHtml(plainLanguage(a.event))}</div>`;
    feed.appendChild(el);
  });
  if (!(alerts.alerts||[]).length) feed.innerHTML = '<div class="empty">No active alerts — conditions stable.</div>';
  feed.insertAdjacentHTML("beforeend", `<div class="empty" style="margin-top:6px">Official alerts from IMD &amp; NWP feeds · WeatherGPT adds plain-language interpretation.${state.mock?" (MOCK)":""}</div>`);
}
function renderAdvisory(adv) {
  const body = $("#advisoryBody"); if (!body) return;
  body.innerHTML = `<div class="advisory"><b>${escapeHtml(adv.headline||adv.sector)}</b><ul>${(adv.points||[]).map(p=>`<li>${escapeHtml(p)}</li>`).join("")}</ul>
    <div class="src" style="font-size:10.5px;color:var(--dim2)">${escapeHtml(adv.source||"")}</div></div>`;
  state.sector = adv.sector || state.sector; drawSectorTabs();
}
function renderClimate(clim) {
  const rows = clim.yearly_averages || [];
  const note = $("#climateNote");
  if (!rows.length) { if (note) note.textContent = "Historical archive unavailable for this location yet."; return; }
  if (note) note.textContent = `Yearly averages (ERA5-backed archive). Trend: ${clim.trend_c_per_year != null ? clim.trend_c_per_year + " °C/yr" : "n/a"} · ${clim.source || ""}`;
  if (window.chartFailed || typeof Chart === "undefined") return;
  if (state.charts.climate) state.charts.climate.destroy();
  state.charts.climate = new Chart($("#climateChart"), { type:"line",
    data:{ labels: rows.map(r=>r.year), datasets:[
      {label:"Avg high °C",data:rows.map(r=>r.avg_tmax),borderColor:"#f87171",tension:.3,pointRadius:2},
      {label:"Avg low °C",data:rows.map(r=>r.avg_tmin),borderColor:"#38bdf8",tension:.3,pointRadius:2},
      {label:"Rainfall (mm/10)",data:rows.map(r=>(r.annual_precip_mm||0)/10),borderColor:"#fbbf24",tension:.3,pointRadius:2,yAxisID:"y1"} ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ labels:{ color:"#9db0d0", boxWidth:10 } } },
      scales:{ x:{ ticks:{ color:"#9db0d0" }, grid:{ color:"#22345c" } }, y:{ ticks:{ color:"#9db0d0" }, grid:{ color:"#22345c" } },
        y1:{ position:"right", ticks:{ color:"#fbbf24" }, grid:{ drawOnChartArea:false } } } } });
}
function renderBrief(b) {
  if (!b) return;
  const risk = (b.risk_level || "unknown").toLowerCase();
  const factors = (b.key_factors||[]).map(f=>`<span class="factor ${f.level||""}">${escapeHtml(f.label)}: ${escapeHtml(f.value)}</span>`).join("");
  $("#intelligence").innerHTML =
    `<div class="brief-head">Today's Weather Brief <span class="tag">${escapeHtml(b.source||"live")}</span></div>` +
    `<div class="brief-summary">${escapeHtml(b.summary)}</div>` +
    `<span class="brief-risk ${risk}">${escapeHtml(risk)} risk</span>` +
    (factors?`<div class="brief-factors">${factors}</div>`:"") +
    (b.recommendation?`<div class="brief-rec">➜ ${escapeHtml(b.recommendation)}</div>`:"") +
    `<div class="src">Updated ${escapeHtml(b.updated_at||"")} · ${escapeHtml(b.confidence||"")} confidence · ${escapeHtml(b.disclaimer||"")}</div>`;
  const t=$("#aiSrcTag"); if (t) t.textContent = "weather brief";
}
function renderIntel(text, src) { $("#intelligence").innerHTML = `<p class="intel-text">${escapeHtml(text||"").replace(/\\n/g,"<br>")}</p>` + (src?`<div class="src">Source: ${escapeHtml(src)} · live meteorological data (AI explains, never invents)</div>`:""); }

/* ============================================================
   GEO-SAFE HELPERS
   ============================================================ */
// MapLibre image/canvas sources: coordinates start at TOP-LEFT (NW) clockwise => NW, NE, SE, SW
function boundsCoords(b) { return [[b.w,b.n],[b.e,b.n],[b.e,b.s],[b.w,b.s]]; }
function gridBounds(points) { const lats=points.map(p=>p.latitude), lons=points.map(p=>p.longitude); return { w:Math.min(...lons), s:Math.min(...lats), e:Math.max(...lons), n:Math.max(...lats) }; }
function currentBBox() {
  if (!state.map) return null;
  try { const b = state.map.getBounds(); const q = 0.25;
    return { south: Math.floor(b.getSouth()/q)*q, west: Math.floor(b.getWest()/q)*q, north: Math.ceil(b.getNorth()/q)*q, east: Math.ceil(b.getEast()/q)*q };
  } catch (e) { return null; }
}
function bboxKey(b) { return b ? `${b.south}|${b.west}|${b.north}|${b.east}` : "default"; }
function gridSizeFor(zoom) { zoom = zoom || 7; if (zoom >= 10) return 15; if (zoom >= 8) return 13; if (zoom >= 6) return 11; return 9; }

/* ============================================================
   MAP (MapLibre) — geo-anchored real layers
   ============================================================ */
const CARTO_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json";
const DEMO_STYLE = "https://demotiles.maplibre.org/style.json";
const SATELLITE_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

function initMap() {
  if (window.maplibreFailed || typeof maplibregl === "undefined") { $("#mapFallback").classList.remove("hidden"); $("#mapLoading").classList.add("hidden"); return; }
  const map = new maplibregl.Map({ container:"map", style:CARTO_STYLE,
    center:[state.loc?state.loc.longitude:77.209, state.loc?state.loc.latitude:28.6139], zoom:6.5,
    minZoom:1, maxZoom:18, fadeDuration:120, attributionControl:false });
  map.addControl(new maplibregl.NavigationControl({ showCompass:false }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact:true }), "bottom-left");
  map.on("error", (e) => {
    const msg = String((e && e.error && e.error.message) || (e && e.message) || "");
    if (msg.toLowerCase().includes("style")) { map.setStyle(DEMO_STYLE); return; }
    if (window.console && console.debug) console.debug("map-tile", msg);
  });
  map.on("load", () => {
    $("#mapLoading")?.classList.add("hidden");
    const defCoords = [[77,28],[78,28],[78,29],[77,29]];
    // Satellite: REAL NASA/Esri world imagery (geo-anchored raster tiles)
    map.addSource("satellite", { type:"raster", tiles:[SATELLITE_TILES], tileSize:256, attribution:"© Esri · Maxar · NASA" });
    map.addLayer({ id:"satellite", type:"raster", source:"satellite", layout:{ visibility:"none" }, minzoom:0, maxzoom:18 });
    // Rain radar: RainViewer real frames (raster tiles in the provider's tile coordinate system)
    map.addSource("radar", { type:"raster", tiles:[], tileSize:256, attribution:"© RainViewer" });
    map.addLayer({ id:"radar", type:"raster", source:"radar", paint:{ "raster-opacity":0.8 }, maxzoom:13 });
    // Canvas sources for forecast overlays (geographic corners set explicitly each draw)
    state.tempCanvas = document.createElement("canvas"); state.tempCanvas.width=640; state.tempCanvas.height=640;
    state.rainCanvas = document.createElement("canvas"); state.rainCanvas.width=640; state.rainCanvas.height=640;
    state.windCanvas = document.createElement("canvas"); state.windCanvas.width=640; state.windCanvas.height=640;
    map.addSource("tempfield", { type:"canvas", canvas:state.tempCanvas, animate:true, coordinates:defCoords });
    map.addSource("rainfield", { type:"canvas", canvas:state.rainCanvas, animate:true, coordinates:defCoords });
    map.addSource("windfield", { type:"canvas", canvas:state.windCanvas, animate:true, coordinates:defCoords });
    map.addLayer({ id:"temp-layer", type:"raster", source:"tempfield", paint:{ "raster-opacity":0.78 }, layout:{ visibility:"none" } });
    map.addLayer({ id:"rain-layer", type:"raster", source:"rainfield", paint:{ "raster-opacity":0.8 }, layout:{ visibility:"none" } });
    map.addLayer({ id:"wind-layer", type:"raster", source:"windfield", paint:{ "raster-opacity":0.95 }, layout:{ visibility:"none" } });
    // Cyclone watch markers (geojson)
    map.addSource("cyclone", { type:"geojson", data:emptyFC() });
    map.addLayer({ id:"cyclone-dot", type:"circle", source:"cyclone",
      paint:{ "circle-radius":["interpolate",["linear"],["zoom"],4,7,10,14], "circle-color":["get","color"], "circle-opacity":0.9, "circle-stroke-width":1.5, "circle-stroke-color":"#fff" },
      layout:{ visibility:"none" } });
    drawLayerTabs(); showLayerCore("precipitation"); loadRadar();
    // move-end: always keep overlays aligned to the exact viewport (geo-correct pan/zoom)
    let mt=null;
    map.on("moveend", () => { clearTimeout(mt); mt=setTimeout(()=>{ if(!state.map) return; if(state.play && state.play.mode==="grid") jumpGrid(state.play.idx); else loadGridOverlay("temperature"); }, 650); });
  });
  state.map = map;
}
function emptyFC() { return { type:"FeatureCollection", features:[] }; }

function showLayerCore(id) {
  if (!state.map) return;
  const rainForecast = id === "precipitation" && state.span !== "live";
  const radarLive = id === "precipitation" && state.span === "live";
  state.map.setLayoutProperty("satellite", "visibility", id === "satellite" ? "visible" : "none");
  state.map.setLayoutProperty("radar", "visibility", radarLive ? "visible" : "none");
  state.map.setLayoutProperty("rain-layer", "visibility", rainForecast ? "visible" : "none");
  state.map.setLayoutProperty("temp-layer", "visibility", id === "temperature" ? "visible" : "none");
  state.map.setLayoutProperty("wind-layer", "visibility", id === "wind" ? "visible" : "none");
  state.map.setLayoutProperty("cyclone-dot", "visibility", id === "cyclone" ? "visible" : "none");
  const showPlayer = ["precipitation","wind","temperature"].includes(id);
  $("#radarPlayer").style.display = showPlayer ? "" : "none";
  state.context.active_layer = id;
  const meta = LAYERS.find(l => l.id === id);
  const mt = $("#mapLayerTag"); if (mt) mt.textContent = meta ? meta.label : id;
  setLegend(id);
}
function setLayer(id) {
  if (!state.map) return;
  const meta = LAYERS.find(l => l.id === id); if (!meta) return;
  document.querySelectorAll(".ltab").forEach(b => b.classList.toggle("on", b.dataset.layer === id));
  showLayerCore(id);
  if (id === "precipitation") loadRadar();
  if (id === "cyclone") loadCycloneLayer();
  if (id === "wind" && state.windSim) state.windSim.active = true;
  if (id !== "wind" && state.windSim) state.windSim.active = false;
  buildPlaylist();
}
function setSpan(span) {
  if (!SPANS[span]) return;
  state.span = span;
  document.querySelectorAll("#spanTabs .span-tab").forEach(b => b.classList.toggle("on", b.dataset.span === span));
  if (state.map) { showLayerCore(state.context.active_layer); buildPlaylist(); }
}
function drawLayerTabs() {
  const w = $("#layerTabs"); if (!w) return;
  w.innerHTML = "";
  // Only Rain Radar + Thermal are shown (per product decision); the other layers
  // remain wired in code (wind/cyclone/satellite) and can be re-enabled anytime.
  const visible = LAYERS.filter(l => ["precipitation", "temperature"].includes(l.id));
  visible.forEach(l => {
    const b = document.createElement("button");
    b.className = "ltab" + (l.id === "precipitation" ? " on" : "");
    b.dataset.layer = l.id;
    b.innerHTML = `<span class="dot"></span>${l.label}`;
    b.onclick = () => setLayer(l.id);
    w.appendChild(b);
  });
}
function drawSpanTabs() { const w=$("#spanTabs"); if(!w) return; w.innerHTML=""; Object.entries(SPANS).forEach(([k,s])=>{ const b=document.createElement("button"); b.className="span-tab"+(k===state.span?" on":""); b.dataset.span=k; b.textContent=s.label; b.onclick=()=>setSpan(k); w.appendChild(b); }); }

function setLegend(id) {
  const lg = $("#mapLegend"); if (!lg) return;
  const spanLabel = (SPANS[state.span] || {}).label || "";
  const text = {
    precipitation: state.span === "live" ? `mm/h: light → heavy · RainViewer live radar (real frames; station coverage)` : `mm/h (NWP): light → heavy · real precipitation forecast · span ${spanLabel}`,
    wind: `wind stream = real 10m wind vectors (animated) · span ${spanLabel} · colors: low → high km/h`,
    temperature: `°C heatmap interpolated from real Open-Meteo grid · span ${spanLabel}`,
    cyclone: `cyclone / severe-wind watch — real NWP wind centers`,
    satellite: `NASA/Esri satellite imagery (true-colour, real Earth observation)`,
  };
  lg.innerHTML = text[id] || "";
}

/* --- RADAR (real RainViewer frames, geographic tile system) --- */
async function loadRadar() {
  try {
    const cat = await api("/api/radar"); state.radar = cat;
    if (cat.note) $("#mapLegend").innerHTML = `<span>⚠ ${escapeHtml(cat.note)}</span>`;
    buildPlaylist();
  } catch (e) { $("#mapLegend").textContent = "Radar is temporarily unavailable."; }
}
function radarTile(path, z, x, y) { return `${state.radar.host}${path}/256/${z}/${x}/${y}/2/1_1.png`; }
function jumpRadar(i) {
  const frames = (state.radar && state.radar.frames) || [];
  if (!frames.length || !state.map) return;
  state.radarIndex = Math.max(0, Math.min(i, frames.length - 1));
  const f = frames[state.radarIndex];
  
  if (state.map.getSource("radar")) {
      const visibility = state.map.getLayoutProperty("radar", "visibility") || "visible";
      state.map.removeLayer("radar");
      state.map.removeSource("radar");
      state.map.addSource("radar", { type:"raster", tiles:[radarTile(f.path, "{z}", "{x}", "{y}")], tileSize:256, attribution:"© RainViewer" });
      state.map.addLayer({ id:"radar", type:"raster", source:"radar", paint:{ "raster-opacity":0.8 }, layout:{ visibility } }, "temp-layer");
  }
  
  setStamp((f.time || "").replace("T", " "), "RainViewer");
  $("#radarSlider").value = state.radarIndex;
}
function setStamp(text, suffix) { const el=$("#radarStamp"); if(el) el.textContent = text + (suffix ? " · " + suffix : ""); }
function setPlayerLabel(t) { const el=$("#radarPlayer .rlabel"); if(el) el.textContent = t; }

function buildPlaylist() {
  stopPlayerLoop();
  const layer = state.context.active_layer;
  if (layer === "precipitation" && state.span === "live") {
    state.play = { mode:"radar", idx:0 };
    setPlayerLabel("RainViewer live radar (real frames)");
    const n = (state.radar?.frames||[]).length;
    if (n) { $("#radarSlider").max = n-1; $("#radarSlider").value = n-1; jumpRadar(n-1); }
    else { $("#radarSlider").max=0; $("#radarSlider").value=0; setStamp("loading radar…"); }
    return;
  }
  const kind = layer === "temperature" ? "thermal" : layer === "wind" ? "wind" : "rain";
  const offsets = spanOffsets(kind === "thermal" ? "thermal" : "rain");
  state.play = { mode:"grid", kind, offsets, idx:0 };
  $("#radarSlider").max = Math.max(offsets.length-1, 0); $("#radarSlider").value = 0;
  const spanLabel = (SPANS[state.span]||{}).label || "";
  setPlayerLabel(kind === "thermal" ? `Temperature forecast · real NWP · ${spanLabel}` : kind === "wind" ? `Wind forecast · real NWP particles · ${spanLabel}` : `Rain forecast · real NWP precipitation · ${spanLabel}`);
  jumpGrid(0);
}
function makeStampGrid(off, kind) { return `${offsetLabel(off)} · ${kind === "thermal" ? "NWP temperature" : kind === "wind" ? "NWP wind" : "NWP rain forecast"}`; }
function sourceNote(d) { const s = (d && d.source) || ""; if (s.includes("mock")) return " · MOCK"; if (s.includes("fallback") || s.includes("coarse")) return " · coarse (real single-point)"; return ""; }

function exactGrid(offset) { return state.gridCache[`${offset==null?0:offset}|${bboxKey(currentBBox())}`]; }
/* Safety fallback: any cached grid (same offset) that covers the current viewport. */
function coveringAny(offset) {
  const b = currentBBox(); if (!b || !state.gridCache) return null;
  const prefix = `${offset==null?0:offset}|`;
  let best = null;
  for (const key of Object.keys(state.gridCache)) {
    if (!key.startsWith(prefix)) continue;
    const d = state.gridCache[key]; if (!d || !d.points || !d.points.length) continue;
    const lats = d.points.map(p=>p.latitude), lons = d.points.map(p=>p.longitude);
    if (Math.min(...lats) <= b.south && Math.max(...lats) >= b.north &&
        Math.min(...lons) <= b.west && Math.max(...lons) >= b.east) { best = d; break; }
  }
  return best;
}
async function fetchGridFrame(offset) {
  const b = currentBBox();
  const zoom = state.map ? state.map.getZoom() : 0;
  const size = gridSizeFor(zoom);
  let url = "/api/temperature-map?";
  if (b) url += `south=${b.south}&west=${b.west}&north=${b.north}&east=${b.east}&size=${size}`;
  else url += `location=${encodeURIComponent(state.context.current_location || "Delhi")}`;
  if (offset != null) url += `&hour_offset=${offset}`;
  try {
    const d = await api(url);
    state.gridCache[`${offset==null?0:offset}|${bboxKey(b)}`] = d;
    return d;
  } catch (e) { return null; }
}
async function jumpGrid(i) {
  const p = state.play; if (!p || p.mode !== "grid") return;
  const off = p.offsets[i]; if (off == null) return;
  p.idx = i; $("#radarSlider").value = i;
  let d = exactGrid(off);
  if (!d) d = await fetchGridFrame(off);
  if (!d) d = coveringAny(off); // show a covering (slightly stale) field rather than a gap
  if (!d) { setStamp(offsetLabel(off), "fetching…"); return; }
  if (p.kind === "thermal") pushImageOverlay("temperature", gridCanvas(d.points, "temp"));
  else if (p.kind === "wind") renderWind(d);
  else pushImageOverlay("rain", gridCanvas(d.points, "rain"));
  setStamp(makeStampGrid(off, p.kind) + sourceNote(d) + (d !== exactGrid(off) && coveringAny(off) === d && !exactGrid(off) ? " · cached" : ""));
}
function _togglePlay() {
  const p = state.play;
  if (state.radarTimer) { stopPlayerLoop(); return; }
  if (!p) { buildPlaylist(); return; }
  $("#radarPlay").textContent = "⏸";
  const stepMs = p.mode === "radar" ? 750 : 1900;
  state.radarTimer = setInterval(() => {
    const pp = state.play; if (!pp) { stopPlayerLoop(); return; }
    if (pp.mode === "radar") { const n=(state.radar?.frames||[]).length; if(!n) return; state.radarIndex=(state.radarIndex+1)%n; jumpRadar(state.radarIndex); }
    else jumpGrid((pp.idx+1)%pp.offsets.length);
  }, stepMs);
}
function stopPlayerLoop() { if (state.radarTimer) { clearInterval(state.radarTimer); state.radarTimer=null; } const b=$("#radarPlay"); if(b) b.textContent="▶"; }
function bindRadarControls() {
  $("#radarPlay").onclick = _togglePlay;
  $("#radarSlider").oninput = (e) => { const i=parseInt(e.target.value); if(state.play?.mode==="radar") jumpRadar(i); else jumpGrid(i); };
  $("#radarLatest").onclick = () => { if(state.play?.mode==="radar"){const n=(state.radar?.frames||[]).length; if(n) jumpRadar(n-1);} else if(state.play) jumpGrid(state.play.offsets.length-1); };
}

/* --- draw a grid canvas (canvas top-left = NW, since coords are NW-first) --- */
function gridCanvas(points, kind) {
  if (!points || !points.length) return { canvas:null, bounds:null };
  const size = Math.round(Math.sqrt(points.length));
  const W = 640, H = 640;
  const cv = document.createElement("canvas"); cv.width=W; cv.height=H;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(W, H);
  const rainUsesMm = kind === "rain" && points.some(p => p.precipitation_mm != null);
  const keyMap = { temp:"temperature_2m", intensity:"intensity", cloud:"cloud_cover", lightning:"lightning_potential", rain: rainUsesMm ? "precipitation_mm" : "precipitation_probability" };
  const key = keyMap[kind] || "temperature_2m";
  const read = (a, b, k) => { const idx = a*size + b; const v = points[idx] && points[idx][k]; return (v == null || isNaN(v)) ? null : v; };
  const bilinear = (fi, fj, k) => {
    const i0=Math.floor(fi), j0=Math.floor(fj); const di=fi-i0, dj=fj-j0;
    const v=(a,b)=>{ const x=read(Math.min(Math.max(a,0),size-1),Math.min(Math.max(b,0),size-1),k); return x; };
    const a=v(i0,j0),b=v(i0,j0+1),c=v(i0+1,j0),d=v(i0+1,j0+1);
    if (a==null && b==null && c==null && d==null) return null;
    const a2=a??(b??c??d), b2=b??a2, c2=c??a2, d2=d??a2;
    return a2*(1-di)*(1-dj)+b2*(1-di)*dj+c2*di*(1-dj)+d2*di*dj;
  };
  const color = (v) => {
    if (kind === "temp") return tempColor(v);
    if (kind === "rain") return rainColor(v, rainUsesMm);
    if (kind === "intensity") return intensityColor(v);
    if (kind === "cloud") { const a=Math.min(v/100,1); return [238,240,245,Math.round(a*200)]; }
    if (kind === "lightning") { const a=Math.min(v/1200,1); return [255,200,40,Math.round(a*255)]; }
    return [255,255,255,0];
  };
  for (let py=0; py<H; py++) {
    for (let px=0; px<W; px++) {
      const fi = (1 - py/(H-1)) * (size-1); // north at top (grid rows are south->north)
      const fj = (px/(W-1)) * (size-1);
      const v = bilinear(fi, fj, key);
      if (v == null) continue;
      const c = color(v); const o=(py*W+px)*4;
      img.data[o]=c[0]; img.data[o+1]=c[1]; img.data[o+2]=c[2]; img.data[o+3]=c[3];
    }
  }
  ctx.putImageData(img,0,0);
  return { canvas:cv, bounds:gridBounds(points) };
}
function tempColor(v) { const t=Math.max(0,Math.min(1,(v+10)/55)); const c=[[30,30,80],[40,90,180],[20,150,200],[80,190,160],[170,200,120],[240,210,100],[240,140,60],[220,60,40]]; const i=Math.min(c.length-2,Math.floor(t*(c.length-1))); const f=t*(c.length-1)-i; const m=c[i].map((x,k)=>Math.round(x*(1-f)+c[i+1][k]*f)); return [m[0],m[1],m[2],190]; }
function rainColor(v, mmMode) { if (mmMode) { const t=Math.max(0,Math.min(1,v/20)); const stops=[[110,210,255],[70,150,255],[40,100,255],[160,70,250],[95,30,190]]; const i=Math.min(stops.length-2,Math.floor(t*(stops.length-1))); const f=t*(stops.length-1)-i; const m=stops[i].map((x,k)=>Math.round(x*(1-f)+stops[i+1][k]*f)); return [m[0],m[1],m[2],Math.round(50+t*190)]; } const t=Math.max(0,Math.min(1,v/100)); return [90,180,255,Math.round(30+t*200)]; }
function intensityColor(v) { const t=Math.max(0,Math.min(1,v/100)); const stops=[[45,190,90],[140,200,60],[250,210,60],[250,160,40],[235,80,35],[180,20,40]]; const i=Math.min(stops.length-2,Math.floor(t*(stops.length-1))); const f=t*(stops.length-1)-i; const m=stops[i].map((x,k)=>Math.round(x*(1-f)+stops[i+1][k]*f)); return [m[0],m[1],m[2],210]; }

function pushImageOverlay(key, ov) {
  if (!state.map || !ov.canvas) return;
  const srcName = key === "rain" ? "rainfield" : "tempfield";
  const canvas = key === "rain" ? state.rainCanvas : state.tempCanvas;
  const src = state.map.getSource(srcName);
  if (!src || !canvas || !ov.bounds) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0,0,canvas.width,canvas.height);
  try { ctx.drawImage(ov.canvas, 0, 0, canvas.width, canvas.height); } catch (e) {}
  try { src.setCoordinates(boundsCoords(ov.bounds)); } catch (e2) { console.warn(srcName, e2); }
  if (state.map.triggerRepaint) state.map.triggerRepaint();
}

function loadGridOverlay(kind) {
  fetchGridFrame(null).then(data => {
    if (!data) return;
    state.gridData = data;
    if (kind === "temperature") pushImageOverlay("temperature", gridCanvas(data.points, "temp"));
    else if (kind === "rain") pushImageOverlay("rain", gridCanvas(data.points, "rain"));
    else if (kind === "wind") renderWind(data);
  }).catch(() => {});
}

/* --- WIND particle stream (canvas source; particles from real vectors, north-up) --- */
function renderWind(data) {
  const pts = (data && data.points) || []; if (pts.length < 16 || !state.map) return;
  const size = Math.round(Math.sqrt(pts.length)); if (size < 4) return;
  const vec = []; let maxSpd = 1;
  pts.forEach(p => { const spd=p.wind_speed_10m||0; const dir=(p.wind_direction_10m||0)*Math.PI/180; vec.push({ u:-spd*Math.sin(dir), v:-spd*Math.cos(dir), spd }); if(spd>maxSpd) maxSpd=spd; });
  const b = gridBounds(pts);
  const sim = state.windSim = state.windSim || { particles:[], raf:null, active:false };
  sim.canvas = state.windCanvas; sim.ctx = sim.canvas.getContext("2d");
  sim.size=size; sim.vec=vec; sim.bounds=b; sim.maxSpd=maxSpd;
  sim.cvW=sim.canvas.width; sim.cvH=sim.canvas.height;
  if (!sim.particles.length) for (let i=0;i<900;i++) sim.particles.push({ x:Math.random()*sim.cvW, y:Math.random()*sim.cvH, age:Math.random()*60 });
  try { state.map.getSource("windfield").setCoordinates(boundsCoords(b)); } catch (e) {}
  startWindSim();
}
function sampleWind(sim, px, py) {
  const gi = (1 - py/sim.cvH) * (sim.size-1); // north-up
  const gj = (px/sim.cvW) * (sim.size-1);
  const i0=Math.max(0,Math.min(sim.size-2,Math.floor(gi))), j0=Math.max(0,Math.min(sim.size-2,Math.floor(gj)));
  const di=gi-i0, dj=gj-j0;
  const get=(a,b)=> sim.vec[a*sim.size+b] || null;
  const a=get(i0,j0), bb=get(i0,j0+1), c=get(i0+1,j0), d=get(i0+1,j0+1);
  if (!a) return null;
  const lerp=(p1,p2)=> p1 ? { u:p1.u+(p2?(p2.u-p1.u)*dj:0), v:p1.v+(p2?(p2.v-p1.v)*dj:0), spd:p1.spd+(p2?(p2.spd-p1.spd)*dj:0) } : null;
  const r1=lerp(a,bb), r2=lerp(c,d); if(!r1) return null;
  return { u:r1.u+(r2?(r2.u-r1.u)*di:0), v:r1.v+(r2?(r2.v-r1.v)*di:0), spd:r1.spd+(r2?(r2.spd-r1.spd)*di:0) };
}
function windSpeedColor(spd, maxSpd) { const t=Math.min(1,spd/Math.max(maxSpd,1)); const stops=[[120,170,255],[90,220,250],[140,240,190],[250,230,130],[255,150,90],[235,70,90]]; const i=Math.min(stops.length-2,Math.floor(t*(stops.length-1))); const f=t*(stops.length-1)-i; const c=stops[i].map((x,k)=>Math.round(x*(1-f)+stops[i+1][k]*f)); return `rgba(${c[0]},${c[1]},${c[2]},0.85)`; }
function startWindSim() {
  const sim = state.windSim; if (!sim || !sim.canvas || sim.raf) return;
  sim.active = true;
  const tick = () => {
    if (!sim.active || !state.windCanvas) { sim.raf=null; return; }
    const ctx = sim.ctx; ctx.fillStyle="rgba(6,10,24,0.045)"; ctx.fillRect(0,0,sim.cvW,sim.cvH); ctx.lineWidth=1.4;
    for (const p of sim.particles) {
      const v = sampleWind(sim, p.x, p.y);
      if (!v) { p.x=Math.random()*sim.cvW; p.y=Math.random()*sim.cvH; continue; }
      const k = 0.9 + Math.min(v.spd/40, 2.2);
      const nx=p.x+v.u*k, ny=p.y+v.v*k;
      if (nx<0||nx>sim.cvW||ny<0||ny>sim.cvH||p.age>90) { p.x=Math.random()*sim.cvW; p.y=Math.random()*sim.cvH; p.age=0; continue; }
      ctx.strokeStyle = windSpeedColor(v.spd, sim.maxSpd);
      ctx.beginPath(); ctx.moveTo(p.x,p.y); ctx.lineTo(nx,ny); ctx.stroke();
      p.x=nx; p.y=ny; p.age+=1;
    }
    sim.raf = requestAnimationFrame(tick);
  };
  sim.raf = requestAnimationFrame(tick);
}
/* --- Cyclone watch (real NWP wind centers + optional IMD track) --- */
function loadCycloneLayer() {
  const b = currentBBox();
  let url = "/api/cyclones";
  if (b) url += `?south=${b.south}&west=${b.west}&north=${b.north}&east=${b.east}`;
  fetch(url).then(r => r.json()).then(d => {
    if (!state.map) return;
    const feats = (d.storms || []).map(s => ({ type:"Feature",
      properties:{ color: s.wind_kmh>=62?"#ff9d6b":"#ffcf6e", event:`Severe wind ${s.wind_kmh} km/h`, desc:`Real NWP wind center ${s.wind_kmh} km/h — monitor for cyclonic development.` },
      geometry:{ type:"Point", coordinates:[s.lon, s.lat] } }));
    (d.tracks || []).forEach(t => feats.push({ type:"Feature",
      properties:{ color:"#ffcf6e", event:`${t.name} (IMD track)`, desc:`Official IMD cyclone track point (${t.intensity||"active"}).` },
      geometry:{ type:"Point", coordinates:[t.lon, t.lat] } }));
    state.map.getSource("cyclone").setData({ type:"FeatureCollection", features:feats });
    const watch = d.watch || (d.max_wind_kmh!=null ? `peak wind ${d.max_wind_kmh} km/h in view (real NWP).` : "");
    $("#mapLegend").innerHTML = feats.length ? `Cyclone watch · ${feats.length} point(s) · ${escapeHtml(watch)}` : `Cyclone watch · ${escapeHtml(watch)} · IMD tracks appear once IMD_API_KEY is set.`;
  }).catch(() => { $("#mapLegend").textContent = "Cyclone watch unavailable."; });
}

/* ============================================================
   DATA LOADERS (real, deterministic, no AI needed)
   ============================================================ */
function dailyToInner(d) { const t=d.time||[]; return t.map((dt,i)=>({ date:dt, weather_code:d.weather_code?.[i], temperature_max_c:d.temperature_2m_max?.[i], temperature_min_c:d.temperature_2m_min?.[i], precipitation_mm:d.precipitation_sum?.[i], rain_probability_pct:d.precipitation_probability_max?.[i], wind_gusts_kmh:d.wind_gusts_10m_max?.[i], uv_index:d.uv_index_max?.[i] })); }
function hourlyToInner(h) { const t=h.time||[]; return t.map((tm,i)=>({ time:tm, temperature_c:h.temperature_2m?.[i], apparent_temperature:h.apparent_temperature?.[i], rain_probability_pct:h.precipitation_probability?.[i], precipitation_mm:h.precipitation?.[i], weather_code:h.weather_code?.[i], wind_kmh:h.wind_speed_10m?.[i], relative_humidity_2m:h.relative_humidity_2m?.[i] })); }

async function highLow(name, lat, lon) {
  try {
    const cs = (lat != null && lon != null) ? `&lat=${lat}&lon=${lon}` : "";
    const fc = await api(`/api/weather/forecast?q=${encodeURIComponent(name||"")}${cs}&days=7`);
    const d = fc.daily || {};
    const hi=(d.temperature_2m_max||[]).filter(v=>v!=null), lo=(d.temperature_2m_min||[]).filter(v=>v!=null);
    const uv=d.uv_index_max?.[0], vis=fc.hourly?.visibility?.[0]; const arr=[];
    if(hi.length) arr.push(["7-day high", Math.max(...hi)+"°"]);
    if(lo.length) arr.push(["7-day low", Math.min(...lo)+"°"]);
    arr.push(["UV index", uv!=null?uv:"--"]);
    arr.push(["Visibility", vis!=null?(vis/1000).toFixed(1)+" km":"--"]);
    if(d.sunrise?.[0]) arr.push(["Sunrise",(d.sunrise[0]||"").slice(11,16)]);
    if(d.sunset?.[0]) arr.push(["Sunset",(d.sunset[0]||"").slice(11,16)]);
    return arr;
  } catch (e) { return []; }
}

async function loadLocation(name, lat, lon) {
  const q = name ? encodeURIComponent(name) : "";
  
  // UX Enhancement: Immediately show loading states and clear old data
  if ($("#heroLocName")) $("#heroLocName").textContent = name || "Loading location...";
  if ($("#curName")) $("#curName").textContent = name || "Loading...";
  if ($("#intelligence")) $("#intelligence").innerHTML = `<div class="brief-summary" style="color:var(--text-dim)">Loading forecast...</div>`;
  if ($("#curTemp")) $("#curTemp").textContent = "--°";
  if ($("#dailyStrip")) $("#dailyStrip").innerHTML = `<div class="skel-block mid" style="margin:0">Loading forecast...</div>`;
  if ($("#hourlyRow")) $("#hourlyRow").innerHTML = "";

  try {
    const now = await api(`/api/weather/now?q=${q}${lat!=null&&lon!=null?`&lat=${lat}&lon=${lon}`:""}`);
    state.loc = now.location || { latitude: lat||28.61, longitude: lon||77.2 };
    const cur = now.current || {};
    renderCurrent({ ...cur,
      temperature_c: cur.temperature_2m, feels_like_c: cur.apparent_temperature, humidity_pct: cur.relative_humidity_2m,
      wind_speed_kmh: cur.wind_speed_10m, wind_gusts_kmh: cur.wind_gusts_10m, cloud_cover_pct: cur.cloud_cover,
      wind_direction: windDir(cur.wind_direction_10m), pressure_hpa: cur.surface_pressure, precipitation_mm: cur.precipitation,
      place: now.location?.name || name || "My location", source: state.mock ? "mock" : "open-meteo (live)" },
      { desc: WMO[cur.weather_code] || "Current conditions", metrics: await highLow(name, lat, lon) });
    const fc = await api(`/api/weather/forecast?q=${encodeURIComponent(name||"")}&lat=${state.loc.latitude}&lon=${state.loc.longitude}&days=3`);
    state.tz = { timezone: fc.timezone, utc_offset_seconds: fc.utc_offset_seconds };
    renderDaily({ place:name, forecast: dailyToInner(fc.daily || {}) });
    renderHourly({ place:name, hours: hourlyToInner(fc.hourly || {}) }, state.tz);;
    state.context.current_location = now.location?.name || name || "My location";
    if (state.map) state.map.flyTo({ center:[state.loc.longitude, state.loc.latitude], zoom:8.5 });
    loadGridOverlay("temperature");
    refreshPanelData();
    loadBrief(name); loadClimate(); loadAdvisory("agriculture");
  } catch (e) {
    if ($("#hourlyRow")) $("#hourlyRow").innerHTML = '<div class="empty">Hourly forecast is temporarily unavailable.</div>';
    addPanelMsg("Location load failed. Weather data is temporarily unavailable.");
  }
}
async function loadBrief(name) {
  try { const b = await api(`/api/brief?q=${encodeURIComponent(name||state.context.current_location||"Delhi")}`); renderBrief(b); }
  catch (e) { $("#intelligence").innerHTML = `<div class="brief-summary">Weather data is temporarily unavailable for this location.</div><div class="brief-rec">➜ Try again in a moment.</div>`; }
}
async function loadClimate() {
  try { const d = await api(`/api/climate/trends?q=${encodeURIComponent(state.context.current_location||"Delhi")}`); renderClimate({ yearly_averages:d.years||[], trend_c_per_year:d.trend_c_per_year, source:"Open-Meteo archive (ERA5)" }); }
  catch (e) { const n=$("#climateNote"); if(n) n.textContent="Climate trends will appear here (archive data)."; }
}
async function loadAdvisory(sector) {
  sector = sector || state.sector || "agriculture"; state.sector=sector; drawSectorTabs();
  try { const a = await api(`/api/advisory?sector=${sector}&location=${encodeURIComponent(state.context.current_location||"Delhi")}`); renderAdvisory(a); }
  catch (e) { const b=$("#advisoryBody"); if(b) b.innerHTML=`<div class="advisory"><b>${escapeHtml(sector)}</b><ul><li>Advisory temporarily unavailable.</li></ul></div>`; }
}
async function refreshPanelData() {
  const q = encodeURIComponent(state.context.current_location||"Delhi");
  try { const al = await api(`/api/alerts?q=${q}`); renderAlerts(al.active?.length?{alerts:al.active.slice(0,8)}:{alerts:[]});
    if (al.active && al.active.length) { const a=al.active.find(x=>!x.mock)||al.active[0]; $("#alertBanner").classList.remove("hidden"); $("#alertBanner").innerHTML=`<b>⚠ ${escapeHtml(a.event||"Weather alert")}</b> — ${escapeHtml((a.description||"").slice(0,200))} <span style="opacity:.75">${a.mock?"(MOCK)":escapeHtml(a.source||"")}</span>`; }
  } catch (e) {}
  try { const risk = await api(`/api/risk?q=${q}`); if (risk.overall_risk) renderRisk(risk); } catch (e) {}
}
function addPanelMsg(text) { addMsg("ai", text); }

/* ============================================================
   CHATBOT
   ============================================================ */
function addMsg(role, text, meta) {
  const el=document.createElement("div"); el.className=`msg ${role}`; el.textContent=text;
  if (meta){ const m=document.createElement("div"); m.className="meta"; m.textContent=meta; el.appendChild(m); }
  if (role==="ai" && state.ttsEnabled && text.length>12){ const sp=document.createElement("span"); sp.className="tsspeak"; sp.textContent="🔊"; sp.onclick=()=>speak(text); el.appendChild(sp); }
  $("#messages").appendChild(el); $("#messages").scrollTop=$("#messages").scrollHeight;
}
let typingEl=null;
function typing(show){ if(show&&!typingEl){ typingEl=document.createElement("div"); typingEl.className="typing"; typingEl.textContent="Checking the latest forecast…"; $("#messages").appendChild(typingEl); $("#messages").scrollTop=$("#messages").scrollHeight; } else if(!show&&typingEl){ typingEl.remove(); typingEl=null; } }
async function send(text, voice=false) {
  text=(text||"").trim(); if(!text||state.busy) return; state.busy=true; addMsg("user", text); $("#input").value=""; typing(true);
  try {
    const res = await api("/api/chat", { method:"POST", headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ message:text, language:state.lang, units:state.units, voice, latitude:state.loc?.latitude||null, longitude:state.loc?.longitude||null, context:state.context, history:state.history }) });
    typing(false);
    state.history.push({role:"user",text}); state.history.push({role:"model",text:res.reply}); if(state.history.length>20) state.history=state.history.slice(-20);
    if (res.action) { if (res.action.type==="UPDATE_LOCATION"){ const loc=res.action.payload||{}; if(loc.latitude) loadLocation(loc.name||loc.latitude.toFixed(2), loc.latitude, loc.longitude); } else applyAction(res.action); }
    if (res.location && res.location.latitude) { state.loc=res.location; if(state.map) state.map.flyTo({center:[res.location.longitude,res.location.latitude],zoom:8.5}); }
    if (res.payload?.current) renderCurrent(res.payload.current);
    if (res.payload?.forecast) renderDaily(res.payload.forecast);
    if (res.payload?.hourly) renderHourly(res.payload.hourly);
    if (res.payload?.alerts) renderAlerts(res.payload.alerts);
    if (res.payload?.risk) renderRisk(res.payload.risk);
    if (res.payload?.advisory) renderAdvisory(res.payload.advisory);
    if (res.payload?.climate) renderClimate(res.payload.climate);
    if (res.context) state.context=res.context;
    // The AI answer must appear in the conversation (floating assistant + history)
    addMsg("ai", res.reply || "I couldn't parse that — try rephrasing.", `${res.engine||""} · ${res.intent||"query"} · ${res.latency_ms??""} ms`);
    if (voice) speak(res.reply);

    // Figma style ai briefing updates
    const intelligenceEl = $("#intelligence");
    if (intelligenceEl) {
      intelligenceEl.innerHTML = `<div class="brief-summary">${escapeHtml(res.reply).replace(/\n/g, "<br>")}</div>`;
    }
  } catch (e) {
    typing(false);
    addMsg("ai", "Could not reach the server. ("+e.message+")");
    const intelligenceEl = $("#intelligence");
    if (intelligenceEl) {
      intelligenceEl.innerHTML = `<div class="brief-summary" style="color:#ff8f8f">Error: Could not reach the server. Please try again.</div>`;
    }
  }
  finally { state.busy=false; }
}
function applyAction(action) {
  if (!action) return;
  if (action.type==="UPDATE_LAYER"){ if (action.span) setSpan(action.span); setLayer(action.layer); }
  const p=action.payload||{};
  if (p.current) renderCurrent(p.current);
  if (p.forecast) renderDaily(p.forecast);
  if (p.hourly) renderHourly(p.hourly);
  if (p.alerts) renderAlerts(p.alerts);
  if (p.risk) renderRisk(p.risk);
  if (p.advisory) renderAdvisory(p.advisory);
  if (p.climate) renderClimate(p.climate);
}

/* ============================================================
   VOICE
   ============================================================ */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
function localeFor(lang){ return ({hi:"hi-IN",ta:"ta-IN",te:"te-IN",bn:"bn-IN",mr:"mr-IN",gu:"gu-IN",kn:"kn-IN",ml:"ml-IN",pa:"pa-IN",ur:"ur-IN"}[lang]||"en-IN"); }
function setVoice(t){ const el=$("#voiceState"); if(el) el.textContent=t; }
function startVoice(){
  if(!SR){ setVoice("Voice needs Chrome/Edge"); return; }
  const rec=new SR(); rec.lang=localeFor(state.lang); rec.interimResults=false; rec.maxAlternatives=1;
  rec.start(); $("#micBtn").classList.add("rec"); setVoice("🎤 Listening…");
  rec.onresult=(e)=>{ const txt=e.results[0][0].transcript; $("#input").value=txt; setVoice("⏳ Processing…"); send(txt,true); };
  rec.onerror=(e)=>{ setVoice("Voice: "+e.error); $("#micBtn").classList.remove("rec"); };
  rec.onend=()=>$("#micBtn").classList.remove("rec");
}
function speak(text){
  if(!("speechSynthesis" in window)){ setVoice("TTS unavailable"); return; }
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text.replace(/[•⚠✅☂➜]/g,""));
  u.lang=localeFor(state.lang); u.rate=0.98; setVoice("🔊 Speaking…");
  u.onend=()=>setVoice("Voice ready — press 🎤"); u.onerror=()=>setVoice("Voice ready — press 🎤");
  speechSynthesis.speak(u);
}
/* 60s guided demo */
function runDemo(){
  if(state.demoRunning||state.busy) return; state.demoRunning=true;
  const seq=["What is the weather in Delhi right now?","Will it rain in Noida tomorrow?","Should I travel to Delhi tomorrow morning?","Explain today's weather warning in simple Hindi"];
  let i=0; const step=()=>{ if(i>=seq.length){ state.demoRunning=false; setVoice("Demo complete"); setTimeout(()=>setVoice("Voice ready — press 🎤"),2500); return; } send(seq[i]); i++; setTimeout(()=>{ if(!state.busy) step(); else setTimeout(step,1500); }, 4200); };
  step();
}

/* ============================================================
   SECTOR TABS
   ============================================================ */
function drawSectorTabs(){ const w=$("#sectorTabs"); if(!w) return; w.innerHTML="";
  ["agriculture","aviation","marine","urban"].forEach(s=>{ const b=document.createElement("button"); b.className="stab"+(s===state.sector?" on":""); b.textContent={agriculture:"🌾 Agriculture",aviation:"✈️ Aviation",marine:"⛵ Marine",urban:"🏙 Urban"}[s]; b.onclick=()=>loadAdvisory(s); w.appendChild(b); }); }

/* WS alerts */
function connectWS(){ const proto=location.protocol==="https:"?"wss":"ws"; const start=()=>{ state.ws=new WebSocket(`${proto}://${location.host}/ws/alerts`);
  state.ws.onmessage=(ev)=>{ try{ const m=JSON.parse(ev.data); if(m.type==="new_alert"){ renderAlerts({alerts:[m.alert]}); $("#alertBanner").classList.remove("hidden"); $("#alertBanner").innerHTML=`<b>⚠ ${escapeHtml(m.alert.event)}</b> — ${escapeHtml((m.alert.description||"").slice(0,200))}`; } }catch(e){} };
  state.ws.onclose=()=>setTimeout(start,8000); }; start(); }

/* ============================================================
   BOOT
   ============================================================ */
function welcome(){
  addMsg("ai","Hi! I can help you understand the weather and plan around it. What are you thinking of doing?");
  populateSuggests();
}
function populateSuggests(){
  const wrap=$("#chatSuggests"); if(!wrap||wrap.children.length) return;
  const qs=["Should I go outside?","Will it rain?","Best time for a walk?","Should I carry an umbrella?"];
  qs.forEach(q=>{ const b=document.createElement("button"); b.className="suggest"; b.textContent=q; b.onclick=()=>send(q); wrap.appendChild(b); });
}
function toggleChat(){ const panel=$("#chatPanel"); const was=panel.classList.contains("closed"); panel.classList.remove("closed"); if(!$("#messages").children.length) welcome(); }
function closeChat(){ $("#chatPanel").classList.add("closed"); }

async function boot() {
  $("#botFab").onclick=toggleChat; if($("#chatClose")) $("#chatClose").onclick=closeChat;
  if($("#heroMic")) $("#heroMic").onclick=startVoice;
  $("#heroSend").onclick=()=>{ 
    const v=$("#heroInput").value.trim(); 
    if(v) { 
      $("#heroInput").value=""; 
      const intelligenceEl = $("#intelligence");
      if (intelligenceEl) intelligenceEl.innerHTML = `<div class="brief-summary" style="color:var(--text-dim)">Checking the latest forecast...</div>`;
      send(v); 
    } 
  };
  $("#heroInput").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); $("#heroSend").click(); } });
  
  const changeLocBtn = $("#changeLocBtn");
  if (changeLocBtn) {
    changeLocBtn.onclick = (e) => {
      e.preventDefault();
      const wrap = $("#searchWrap");
      if (wrap) wrap.classList.toggle("hidden");
      if ($("#locSearch")) $("#locSearch").focus();
    };
  }
  $("#micBtn").onclick=startVoice;
  $("#sendBtn").onclick=()=>send($("#input").value);
  $("#input").addEventListener("keydown",(e)=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); send($("#input").value); } });
  $("#ttsBtn").onclick=()=>{ state.ttsEnabled=!state.ttsEnabled; $("#ttsBtn").textContent=state.ttsEnabled?"🔊 speak answers":"🔇 muted"; };
  $("#locGo").onclick=()=>{ const v=$("#locSearch").value.trim(); if(v) loadLocation(v); };
  $("#locSearch").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); $("#locGo").click(); } });
  $("#mapLocate").onclick=()=>navigator.geolocation.getCurrentPosition(p=>loadLocation("My location",p.coords.latitude,p.coords.longitude),()=>{ $("#mapLegend").textContent="Location permission denied — showing default location."; });
  $("#mapFull").onclick=()=>{ const c=document.querySelector(".map-card"); if(c&&c.requestFullscreen) c.requestFullscreen(); };
  $("#unitSelect").onchange=(e)=>{ state.units=e.target.value; if(state.loc) loadLocation(state.loc.name,state.loc.latitude,state.loc.longitude); };
  $("#langSelect").onchange=(e)=>{ state.lang=e.target.value; };

  try {
    const st=await api("/api/status"); state.mock=st.mock_mode;
    const g=st.gemini||{}; const authOk=g.gemini_configured && g.auth && g.auth.valid;
    const badge=$("#engineBadge");
    if(authOk){ badge.textContent=`${g.model}`; } else if(g.gemini_configured){ badge.textContent="AI key rejected → rule engine"; badge.classList.add("warn"); }
    else { badge.textContent="AI key not configured → rule engine"; badge.classList.add("off"); }
    if(state.mock) $("#mockBadge").classList.remove("hidden");
    const live=$("#mapLiveTag"); if(live) live.textContent=state.mock?"● MOCK":"● LIVE";
    $("#aiStatus").textContent=authOk?"Gemini + tools":"rule engine"; $("#aiStatus").classList.toggle("warn",!authOk);
    if(!authOk){ addMsg("ai","AI engine not configured (Gemini key / quota). Running on the built-in rule engine; all weather data is real and free."); renderIntel("AI engine not configured — dashboard running on the built-in rule engine + free weather APIs.","backend env"); }
  } catch (e) {}

  try { const langs=await api("/api/languages"); const sel=$("#langSelect"); Object.entries(langs.languages).forEach(([code,label])=>{ const o=document.createElement("option"); o.value=code; o.textContent=label; sel.appendChild(o); }); sel.value="en"; } catch(e){}

  drawSectorTabs(); drawLayerTabs(); drawSpanTabs();
  initMap(); bindRadarControls(); connectWS(); setInterval(updateAgo,30000);
  await loadLocation("Delhi");
}
function openChatPanel(){ $("#chatPanel").classList.remove("closed"); if(!$("#messages").children.length) welcome(); }
document.addEventListener("DOMContentLoaded", boot);
