/* Foretape app: data, push, and the choreography that ties the page to the scene. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const SEV = { CRITICAL: "#FF8A55", HIGH: "#FFD08A", MEDIUM: "#A9C8FF", LOW: "rgba(255,255,255,.4)" };
const OUTLETS = [["cnbc", "CNBC"], ["yahoo", "Yahoo Finance"], ["gnews", "Google News"], ["mw-", "MarketWatch"], ["wsj", "WSJ"],
  ["bloomberg", "Bloomberg"], ["nyt", "NYT"], ["fortune", "Fortune"], ["axios", "Axios"], ["hackernews", "Hacker News"]];
const params = new URLSearchParams(location.search);
const store = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} } };
let TOKEN = params.get("token") || store.get("ft_token") || "";
if (params.get("token")) store.set("ft_token", TOKEN);
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const pushCapable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
let alerts = [], app = {}, tab = "tape", unread = 0, showAll = false, sheetOpen = false;
const seen = new Set();

function setManifest() { $("manifest").href = "/app/manifest.webmanifest" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""); }
setManifest();
function toast(msg, id) {
  // an alert toast is tappable: it opens that alert's dossier (the global [data-id] handler)
  const t = $("toast"); t.textContent = msg;
  if (id) t.dataset.id = id; else delete t.dataset.id;
  t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), id ? 5200 : 3200);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN, ...(opts.headers || {}) } });
  if (r.status === 401) { showLock(); throw new Error("access key needed"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

// ------------------------------------------------------------------ formatting
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
}
function dur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
  return Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h";
}
const pct = (v) => (v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%");
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const outlet = (src) => (OUTLETS.find(([p]) => (src || "").startsWith(p)) || [0, src])[1];
const dirLabel = { up: "▲ Bullish", down: "▼ Bearish", mixed: "◆ Two-way" };
function marketText(m) {
  if (!m) return "—";
  const until = m.until_s != null ? dur(m.until_s).replace(/ \d+s$/, "") : "";
  if (m.phase === "open") return "Open · closes in " + until;
  if (m.phase === "pre-market") return "Pre-market · opens in " + until;
  if (m.phase === "after-hours") return "After hours";
  return "Closed" + (until ? " · opens in " + until : "");
}
function prettyDate(iso) { return new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }); }
function inDays(n) { return n === 0 ? "today" : n === 1 ? "tomorrow" : "in " + n + " days"; }

// ------------------------------------------------------------------ living numbers
function countTo(el, to, fmt) {
  if (el._to === to) return;
  const from = el._to == null || reduced ? to : (el._now ?? 0);
  el._to = to;
  cancelAnimationFrame(el._raf);
  const t0 = performance.now(), D = reduced ? 1 : 1500;
  const step = (now) => {
    const k = clamp((now - t0) / D), e = 1 - Math.pow(1 - k, 4);
    el._now = from + (to - from) * e;
    el.textContent = fmt(el._now);
    if (k < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

// ------------------------------------------------------------------ rendering
function meter(n) { return `<span class="meter">${[1, 2, 3, 4, 5].map((i) => `<i class="${i <= n ? "on" : ""}"></i>`).join("")}</span>`; }
function tickerChips(a, limit = 6) {
  const play = a.edge?.play, since = a.since || {};
  const direct = play?.direct?.length ? play.direct : (a.tickers || []);
  const chips = direct.slice(0, limit).map((t) => {
    const s = since[t];
    return `<span class="tk">${esc(t)}${s != null ? ` <small class="${cls(s)}">${pct(s)}</small>` : ""}</span>`;
  });
  (play?.read_through || []).slice(0, Math.max(0, limit - chips.length + 2)).forEach((t) => chips.push(`<span class="tk rt">${esc(t)}</span>`));
  return chips.length ? `<div class="tks">${chips.join("")}</div>` : "";
}
function precLine(a) {
  const p = a.edge?.precedents?.[0];
  return p ? `<div class="prec"><b>Last time</b> · ${esc(prettyDate(p.date))}, ${esc(p.date.slice(0, 4))} — ${esc(p.move)}</div>` : "";
}
function playRow(a) {
  const p = a.edge?.play;
  if (!p) return "";
  const d = p.direction in dirLabel ? `<span class="dir ${p.direction}">${dirLabel[p.direction]}</span>` : "";
  return `<div class="play">${d}${meter(p.meter)}<span class="conv">${esc(p.conviction)} conviction</span></div>`;
}
function capRow(a) {
  const s = a.edge?.smallcap;
  if (!s || !(s.material || s.radar)) return "";
  const d = ["up", "down"].includes(s.direction) ? s.direction : "mixed";
  if (s.radar) {  // the title already names the stock and its size: say what the radar saw
    const r = a.edge.radar || {}, news = (a.related || []).some((x) => x.kind === "news");
    const tag = r.rvol && r.rvol >= 1.5 ? `${Math.round(r.rvol)}× normal volume` : r.session === "pre" ? "pre-market" : r.session === "post" ? "after hours" : "";
    return `<div class="cap ${d}"><span class="capb">◉ Radar</span><span class="capl">${news ? "On the news" : "Before any headline"}</span>${tag ? `<span class="capx">${esc(tag)}</span>` : ""}</div>`;
  }
  return `<div class="cap ${d}"><span class="capb">${esc(s.band)}</span><b class="sym">${esc(s.symbol)}</b><span class="capm">${esc(s.cap)}</span>
    <span class="capl">${esc(s.label)}</span>${s.move_text ? `<span class="capx">${esc(s.move_text)}</span>` : ""}</div>`;
}
const isSmall = (a) => !!(a.edge?.smallcap && (a.edge.smallcap.material || a.edge.smallcap.radar));
function footRow(a) {
  const it = a.item || {}, e = a.edge || {}, parts = [];
  const lag = it.published && it.detected ? Math.max(0, it.detected - it.published) : null;
  if (lag != null) parts.push(`<span>⚡ ${dur(lag)} after posted</span>`);
  if (e.lead) parts.push(`<span class="lead">Beat ${esc(outlet(e.lead.source))} by ${dur(e.lead.lead_s)}</span>`);
  const quote = a.edge?.radar && !(a.related || []).some((x) => x.kind === "news");
  if (a.url) parts.push(`<a href="${esc(a.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${quote ? "Quote" : "Source"} ↗</a>`);
  return parts.length ? `<div class="foot">${parts.join("")}</div>` : "";
}
function alertHTML(a, opts = {}) {
  const an = a.analysis || {}, it = a.item || {};
  const src = a.edge?.radar ? "SMALL-CAP RADAR" : a.kind === "price" ? "PRICE ACTION" : a.kind === "system" ? "FORETAPE" : (it.source || a.kind).toUpperCase();
  const why = an.summary || (a.kind !== "news" && a.body ? a.body.split("\n")[0] : "");
  const known = seen.has(a.id);
  const state = a._new ? "new in" : known ? "in" : "";
  return `<article class="alert ${a.edge?.radar ? "radar " : ""}${opts.feature ? "feature " : ""}${known || a._new ? "" : "rv "}${state}" data-id="${esc(a.id)}">
    <div class="glass tilt" style="--sev:${SEV[a.severity] || SEV.LOW}"><i class="sevbar"></i>
      ${opts.feature ? `<div class="eyebrow" style="color:var(--gold);margin-bottom:12px">Top of the tape</div>` : ""}
      <div class="meta"><span class="sev">${esc(a.severity)}</span><span class="src">${esc(src)}</span><span class="ago" data-t="${a.created}">${ago(a.created)}</span></div>
      <h3>${esc(a.title)}</h3>
      ${why ? `<p class="why">${esc(why)}</p>` : ""}
      ${capRow(a)}${playRow(a)}${a.edge?.radar ? "" : tickerChips(a)}${precLine(a)}${footRow(a)}
    </div>
  </article>`;
}
const emptyHTML = (msg) => `<div class="empty rv"><img src="/app/logo.svg" alt=""><div>${msg}</div></div>`;

function featured() {
  const cutoff = Date.now() / 1000 - 6 * 3600;
  const rank = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 };
  return alerts.filter((a) => a.created >= cutoff && a.kind !== "system")
    .sort((x, y) => (rank[y.severity] - rank[x.severity]) || (y.created - x.created))[0];
}
let tapeFilter = (() => { try { return localStorage.getItem("ft.tape") || "all"; } catch (_) { return "all"; } })();
function placeThumb() {
  const seg = $("tapeSeg"), on = seg?.querySelector("button.on"), th = seg?.querySelector(".thumb");
  if (!on || !th || !on.offsetWidth) return;  // hidden tab: placed again when shown
  th.style.width = on.offsetWidth + "px";
  th.style.transform = `translateX(${on.offsetLeft - 3}px)`;
  seg.classList.add("ready");
}
addEventListener("resize", placeThumb);
function renderTape() {
  document.querySelectorAll("#tapeSeg button").forEach((b) => { const on = b.dataset.f === tapeFilter; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
  placeThumb();
  const pool = tapeFilter === "small" ? alerts.filter(isSmall) : alerts;
  const f = tapeFilter === "small" ? null : featured();
  $("featureWrap").innerHTML = f ? alertHTML(f, { feature: true }) : "";
  const rest = pool.filter((a) => a !== f);
  const shown = rest.slice(0, showAll ? 150 : 30);   // dozens of blurred glass layers are expensive on a phone: older ones load on demand
  $("alerts").innerHTML = rest.length ? shown.map((a) => alertHTML(a)).join("") + (rest.length > shown.length ? `<button class="more" id="moreBtn">Show ${rest.length - shown.length} older</button>` : "")
    : f ? "" : emptyHTML(tapeFilter === "small"
      ? "No small-cap catalysts yet. Every listed company is sized against its news; the first one that moves the needle for its size lands here."
      : "Quiet tape. Foretape is watching every first-to-publish source; the next market-moving headline lands here and on your lock screen.");
  alerts.forEach((a) => { seen.add(a.id); delete a._new; });
  $("moreBtn")?.addEventListener("click", () => { showAll = true; renderTape(); });
  Fx.observe($("featureWrap")); Fx.observe($("alerts"));
}
function renderTop() {
  const hour = new Date().getHours();
  const g = hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  $("greet").textContent = `${g} · ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}`;
  const m = app.market;
  // ambient state: lighting follows the market; a CRITICAL alert in the last 15 minutes warms the frame
  const hot = alerts.some((x) => x.severity === "CRITICAL" && x.kind !== "system" && Date.now() / 1000 - x.created < 900);
  Scene.setMood(!!m?.open, hot ? 1 : 0);
  $("mkt").lastElementChild.textContent = marketText(m);
  $("mkt").classList.toggle("open", !!m?.open);
  if (app.edge?.median_lead_s != null) countTo($("sLead"), app.edge.median_lead_s, (v) => dur(v).replace(/ 0?0s$/, ""));
  else $("sLead").textContent = "—";
  Array.from($("hud").querySelectorAll(".co")).forEach((c) => (c._w = 0));  // labels changed: re-measure
  $("hudA").textContent = app.sources ? `${app.sources_ok} of ${app.sources} live` : "—";
  const last = alerts.find((x) => x.kind === "news");
  $("hudB").textContent = last ? `${ago(last.created)} ago · ${(last.edge?.play?.direct?.[0] || last.tickers?.[0] || (last.item?.source || "")).toString().slice(0, 12)}` : "none yet";
  $("hudC").textContent = app.edge?.median_lead_s != null ? "+" + dur(app.edge.median_lead_s).replace(/ 0?0s$/, "") + " median" : "measuring…";
  if (app.alerts_24h != null) countTo($("sAlerts"), app.alerts_24h, (v) => String(Math.round(v)));
  if (app.sources) countTo($("sSources"), app.sources_ok, (v) => `${Math.round(v)}/${app.sources}`);
  const n = (app.next || []).find((e) => (e.impact || 1) >= 2) || (app.next || [])[0];
  $("nextCat").hidden = !n;
  if (n) $("nextText").innerHTML = `<b>${esc(n.title)}</b><small>${esc(prettyDate(n.date))}${n.time ? " · " + esc(n.time) + " ET" : ""} · ${inDays(n.in_days)}</small>`;
}
async function load() {
  app = await api("/api/app");
  alerts = app.alerts;
  renderTop(); renderTape();
  $("mode").textContent = app.phone_mode;
  $("briefTime").textContent = app.brief_time ? app.brief_time + " ET, weekdays" : "off";
  $("engine").innerHTML = [
    ["Sources healthy", `${app.sources_ok}/${app.sources}`], ["Running for", dur(app.uptime_s).replace(/ \d+s$/, "")],
    ["Alerts (24h)", app.alerts_24h], ["Phones subscribed", app.devices], ["Stories beaten (7d)", app.edge?.stories ?? 0],
    ["Companies sized", app.smallcap?.listings ? `${app.smallcap.listings.toLocaleString()}${app.smallcap.age_s != null ? " · " + dur(app.smallcap.age_s).replace(/ \d+s$/, "") + " old" : ""}` : "loading…"],
    ["Small-cap radar", !app.smallcap?.radar ? "off" : `${app.smallcap.scans} scans · ${app.smallcap.hits} flagged`],
  ].map(([k, v]) => `<div class="kv"><span>${k}</span><b class="num">${esc(v)}</b></div>`).join("");
  $("durable").hidden = app.durable;
  $("durable").textContent = app.durable ? "" : "This server forgets subscribed phones when it restarts. Foretape reconnects this phone every time you open it; for zero gaps, set STATE_DB (free Postgres, see docs).";
  const lvl = /CRITICAL/.test(app.phone_mode) ? "critical" : /MEDIUM/.test(app.phone_mode) ? "more" : /normal/.test(app.phone_mode) ? "normal" : "";
  document.querySelectorAll("#levels button").forEach((b) => b.classList.toggle("sel", b.dataset.cmd === lvl));
  if (tab === "brief") loadBrief().catch(() => {});
  loadTicker();
}

async function loadBrief() {
  const b = await api("/api/brief");
  $("bDate").textContent = `${b.greeting} · ${b.date}`;
  $("bSub").textContent = b.count ? `${b.count} catalyst${b.count > 1 ? "s" : ""} since the last close.` : "A quiet night. Nothing crossed the alert line since the last close.";
  $("bOvernight").innerHTML = b.overnight.length ? b.overnight.map((a) => `<div class="li" data-id="${esc(a.id)}" style="cursor:pointer">
      <span class="dot-sev" style="background:${SEV[a.severity]};color:${SEV[a.severity]}"></span>
      <div class="grow"><div style="font-weight:600;line-height:1.35">${esc(a.title)}</div>
      <div class="sub">${esc(a.source)} · ${ago(a.created)} ago${a.tickers.length ? " · " + esc(a.tickers.join(" ")) : ""}</div>
      ${a.smallcap ? `<div class="bcap"><span class="capb">${esc(a.smallcap.band)}</span><span>${esc(a.smallcap.cap)}</span><span class="${a.smallcap.direction === "down" ? "neg" : a.smallcap.direction === "up" ? "pos" : ""}">${esc(a.smallcap.move_text || "")}</span></div>` : ""}</div>
      <span class="${a.direction === "down" ? "neg" : a.direction === "up" ? "pos" : "faint"}" style="font-size:13px">${a.direction === "down" ? "▼" : a.direction === "up" ? "▲" : "◆"}</span></div>`).join("")
    : `<div class="li"><span class="muted">Nothing overnight.</span></div>`;
  $("bThemesWrap").hidden = !b.themes.length;
  $("bThemes").innerHTML = b.themes.map((t) => `<span class="chip" style="color:var(--cream)">${esc(t)}</span>`).join("");
  $("bMkt").textContent = marketText(b.market);
  $("bIdx").innerHTML = ["SPY", "QQQ", "IWM", "DIA"].map((s) => `<div class="tile glass tilt"><small>${s}</small><b class="num ${cls(b.indexes[s])}">${pct(b.indexes[s])}</b></div>`).join("");
  $("bMovers").innerHTML = b.movers.length ? b.movers.map(moverRow).join("") : `<div class="li"><span class="muted">No quotes yet.</span></div>`;
  $("bCal").innerHTML = b.calendar.length ? b.calendar.map((e) => calRow(e)).join("") : `<div class="li"><span class="muted">Nothing scheduled this week.</span></div>`;
  const e = b.edge;
  $("bEdge").innerHTML = [
    ["Stories you had before the mainstream", e.stories], ["Median head start", e.median_lead_s != null ? dur(e.median_lead_s) : "—"],
    ["Biggest head start", e.best_lead_s != null ? dur(e.best_lead_s) : "—"],
  ].map(([k, v]) => `<div class="kv"><span>${k}</span><b class="num">${esc(v)}</b></div>`).join("");
  Fx.observe($("main"));
}
function barHTML(v) { const w = Math.min(42, Math.abs(v) * 6); return `<span class="bar2"><i style="${v >= 0 ? `left:42px;width:${w}px;background:var(--up)` : `right:42px;width:${w}px;background:var(--down)`}"></i></span>`; }
function moverRow(m) {
  const v = m.chg_day ?? 0;
  return `<div class="li"><span class="sym" style="width:64px">${esc(m.symbol)}</span><span class="grow num faint">${m.price != null ? m.price.toFixed(2) : ""}</span>${barHTML(v)}<span class="chg num ${cls(v)}">${pct(m.chg_day)}</span></div>`;
}
const KIND = { fed: "Federal Reserve", data: "Economic data", politics: "Politics", trade: "Trade policy", options: "Options expiry", market: "Market hours", earnings: "Earnings" };
function calRow(e, grouped) {
  const sub = grouped ? [KIND[e.kind] || e.kind, e.note].filter(Boolean).map(esc).join(" · ")
    : `${esc(prettyDate(e.date))} · ${inDays(e.in_days)}${e.note ? " · " + esc(e.note) : ""}`;
  return `<div class="li"><span class="time">${esc(e.time || "—")}</span><div class="grow"><div style="font-weight:600">${esc(e.title)}</div><div class="sub">${sub}</div></div>
    <span class="imp">${[1, 2, 3].map((i) => `<i class="${i <= (e.impact || 1) ? "on" : ""}"></i>`).join("")}</span></div>`;
}
async function loadCalendar() {
  const c = await api("/api/calendar?days=60");
  const byDay = {};
  c.events.forEach((e) => (byDay[e.date] = byDay[e.date] || []).push(e));
  $("cal").innerHTML = Object.keys(byDay).length ? Object.entries(byDay).map(([d, evs]) => `<div class="day-head rv"><b>${esc(prettyDate(d))}</b><span>${inDays(evs[0].in_days)}</span></div>
      <div class="panel glass tilt rv">${evs.map((e) => calRow(e, true)).join("")}</div>`).join("") : emptyHTML("Nothing scheduled in the next 60 days.");
  Fx.observe($("cal"));
}
async function loadTicker() {
  try {
    const w = await api("/api/watch");
    const top = w.symbols.filter((x) => x.chg_day != null).slice(0, 14);
    $("ticker").hidden = !top.length;
    if (!top.length) return;
    const row = top.map((x) => `<span>${esc(x.symbol)} <small class="${cls(x.chg_day)}">${x.chg_day > 0 ? "▲" : "▼"} ${Math.abs(x.chg_day).toFixed(2)}%</small></span>`).join("");
    $("belt").innerHTML = row + row;     // doubled, so the loop is seamless
    $("belt").style.setProperty("--dur", Math.max(24, top.length * 3.6) + "s");
  } catch (_) { /* the ticker is decoration: never break the page for it */ }
}
function radarRow(r, i = 0) {
  const v = r.change_pct ?? 0, news = (r.news || [])[0];
  const bits = [`${esc(r.cap)} ${esc(r.band)}`];
  if (r.dollar_volume) bits.push(`$${r.dollar_volume >= 1e9 ? (r.dollar_volume / 1e9).toFixed(1) + "B" : r.dollar_volume >= 1e6 ? (r.dollar_volume / 1e6).toFixed(r.dollar_volume < 1e7 ? 1 : 0) + "M" : Math.round(r.dollar_volume / 1e3) + "K"} traded`);
  if (r.rvol && r.rvol >= 1.5) bits.push(`${Math.round(r.rvol)}× volume`);
  const why = news ? `<a href="${esc(news.url)}" target="_blank" rel="noopener" class="rnews">${esc(news.title)}</a>`
    : `<span class="rnone">No headline yet</span>`;
  return `<div class="li radar-li${r.flagged ? " hot" : ""}" style="--k:${i}"${r.alert_id ? ` data-id="${esc(r.alert_id)}" role="button"` : ""}><span class="sym" style="width:64px">${r.flagged ? `<i class="ping"></i>` : ""}${esc(r.symbol)}</span>
    <div class="grow"><div class="rname">${esc(r.name || "")}</div><div class="sub">${bits.join(" · ")}${(r.flags || []).length ? ` · <span class="neg">⚠ ${esc(r.flags[0])}</span>` : ""}</div><div class="sub">${why}</div></div>
    <span class="chg num ${cls(v)}" style="width:auto;min-width:64px"><span data-pct="${v}">${pct(v)}</span>${r.session && r.session !== "regular" ? `<small class="rsess">${r.session === "pre" ? "pre-mkt" : "after hrs"}</small>` : ""}</span></div>`;
}
async function loadRadar() {
  try {
    const r = await api("/api/radar");
    const rows = r.rows || [];
    $("radarSub").textContent = r.enabled ? (r.market?.phase === "closed" ? "sleeps until 4:00 ET" : "moving before the news") : "off";
    const prev = {}, settled = $("radar").dataset.ready === "1";
    $("radar").classList.toggle("settled", settled);  // only the first paint staggers in
    $("radar").dataset.ready = "1";
    $("radar").querySelectorAll("[data-sym]").forEach((el) => (prev[el.dataset.sym] = +el.dataset.pct));
    $("radar").innerHTML = rows.length ? rows.slice(0, 25).map((x, i) => radarRow(x, i)).join("")
      : `<div class="li"><span class="muted">${!r.enabled ? "The radar runs with the market feed (smallcap.radar in config)."
        : r.market?.phase === "closed" ? "Market closed. The radar wakes for pre-market at 4:00 ET and scans every small cap each minute."
        : "Nothing ripping on real volume right now. Breakouts of 20%+ on $2M+ traded land here, and big ones on your lock screen."}</span></div>`;
    // living numbers: each move counts up from where it was (or from zero on first sight)
    $("radar").querySelectorAll(".radar-li").forEach((row, i) => {
      const r = rows[i], el = row.querySelector("[data-pct]");
      if (!r || !el) return;
      row.dataset.sym = r.symbol; row.dataset.pct = r.change_pct ?? 0;
      if (settled && !(r.symbol in prev)) row.classList.add("arrive");  // a new contact on the scope
      el._to = NaN; el._now = prev[r.symbol] ?? 0;  // NaN: "animate from _now", unlike null
      countTo(el, r.change_pct ?? 0, pct);
    });
  } catch (_) { $("radar").innerHTML = `<div class="li"><span class="muted">Radar unavailable.</span></div>`; }
}
// the radar board is live: while Watch is on screen it refreshes every 30 s
setInterval(() => { if (tab === "watch" && !document.hidden && !sheetOpen) loadRadar(); }, 30000);
async function loadWatch() {
  loadRadar();
  const w = await api("/api/watch");
  $("wMkt").textContent = marketText(w.market);
  $("watch").innerHTML = w.symbols.length ? w.symbols.slice(0, 60).map((s) => {
    const v = s.chg_day ?? 0;
    return `<div class="li"><span class="sym" style="width:64px">${esc(s.symbol)}</span>
      <span class="grow num" style="white-space:nowrap"><div>${s.price != null ? s.price.toFixed(2) : "—"}</div><div class="sub">${s.chg_5m != null ? `<span class="${cls(s.chg_5m)}">${pct(s.chg_5m)}</span> in 5 min` : "&nbsp;"}</div></span>
      ${barHTML(v)}<span class="chg num ${cls(v)}">${pct(s.chg_day)}</span></div>`;
  }).join("") : `<div class="li"><span class="muted">No quotes yet; prices appear once the market feed connects.</span></div>`;
}

// ------------------------------------------------------------------ detail sheet
function sparkline(series, alertTs) {
  const syms = Object.keys(series || {}).filter((s) => (series[s] || []).length >= 3).slice(0, 2);
  if (!syms.length) return "";
  const W = 560, H = 130, pad = 6, colors = ["#FFD08A", "#9FC6FF"];
  const lines = syms.map((s) => {
    const pts = series[s];
    const ref = (pts.find((p) => p[0] >= alertTs) || pts[0])[1];
    return { s, pts: pts.map(([t, p]) => [t, (p / ref - 1) * 100]) };
  });
  const all = lines.flatMap((l) => l.pts), t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]), t0 + 1);
  let lo = Math.min(0, ...all.map((p) => p[1])), hi = Math.max(0, ...all.map((p) => p[1]));
  if (hi - lo < 0.6) { hi += 0.3; lo -= 0.3; }
  const X = (t) => pad + ((t - t0) / (t1 - t0)) * (W - 2 * pad), Y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
  const paths = lines.map((l, i) => {
    const d = l.pts.map((p, j) => (j ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(" ");
    const area = d + ` L${X(l.pts[l.pts.length - 1][0]).toFixed(1)} ${Y(0).toFixed(1)} L${X(l.pts[0][0]).toFixed(1)} ${Y(0).toFixed(1)} Z`;
    const last = l.pts[l.pts.length - 1];
    return `<defs><linearGradient id="ag${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${colors[i]}" stop-opacity=".35"/><stop offset="1" stop-color="${colors[i]}" stop-opacity="0"/></linearGradient></defs>
      <path d="${area}" fill="url(#ag${i})"/><path d="${d}" fill="none" stroke="${colors[i]}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" pathLength="1" style="stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.6s var(--ease) .35s forwards"/>
      <circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="4" fill="${colors[i]}"><animate attributeName="r" values="4;7;4" dur="2.2s" repeatCount="indefinite"/></circle>`;
  }).join("");
  const ax = alertTs >= t0 && alertTs <= t1 ? `<line x1="${X(alertTs)}" x2="${X(alertTs)}" y1="0" y2="${H}" stroke="rgba(255,255,255,.4)" stroke-dasharray="3 5"/><text x="${X(alertTs) + 6}" y="12" fill="rgba(255,255,255,.65)" font-size="10" font-family="Inter" letter-spacing=".12em">ALERT</text>` : "";
  const legend = lines.map((l, i) => { const v = l.pts[l.pts.length - 1][1]; return `<span style="color:${colors[i]}">${esc(l.s)} <span class="${cls(v)}">${pct(v)}</span></span>`; }).join("");
  return `<style>@keyframes draw{to{stroke-dashoffset:0}}</style><div class="chart"><div class="legend">${legend}</div><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="rgba(255,255,255,.18)"/>${ax}${paths}</svg></div>`;
}
async function openSheet(id) {
  $("toast").classList.remove("show");
  let a;
  try { a = await api("/api/alert/" + encodeURIComponent(id)); } catch (_) { a = alerts.find((x) => x.id === id); }
  if (!a) return;
  const an = a.analysis || {}, it = a.item || {}, e = a.edge || {}, p = e.play || {}, since = a.since || {};
  const lag = it.published && it.detected ? Math.max(0, it.detected - it.published) : null;
  const tl = (a.story_sources || []).map((s, i) => `<div class="li"><div class="grow"><b>${esc(s.source)}</b>
      <div class="sub">${i === 0 ? "first" : "+" + dur(s.detected - a.story_sources[0].detected)} · ${esc(s.tier || "")}</div></div></div>`).join("");
  const chart = sparkline(a.series, a.created);
  const sc = e.smallcap, rd = e.radar, rel = a.kind === "price" ? (a.related || []).filter((x) => x.kind === "news") : [];
  const money = (v) => v == null ? "—" : v >= 1e9 ? "$" + (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? "$" + (v / 1e6).toFixed(1) + "M" : "$" + Math.round(v / 1e3) + "K";
  const capBlock = sc ? `<h4>${sc.radar ? "On the radar" : "The little thing"}</h4><div class="panel glass">
      <div class="kv"><span>Company</span><b>${esc(sc.name || sc.symbol)} (${esc(sc.symbol)})</b></div>
      <div class="kv"><span>Size</span><b class="num">${esc(sc.cap)} · ${esc(sc.band)}</b></div>
      ${sc.label && !sc.radar ? `<div class="kv"><span>Catalyst</span><b>${esc(sc.label)}</b></div>` : ""}
      ${sc.radar ? `<div class="kv"><span>Headline</span><b>${(a.related || []).some((x) => x.kind === "news") ? "found — below" : "none yet"}</b></div>` : ""}
      ${sc.move_text ? `<div class="kv"><span>For a company this size</span><b class="${sc.direction === "down" ? "neg" : sc.direction === "up" ? "pos" : ""}">${esc(sc.move_text)}</b></div>` : ""}
      ${sc.relative != null ? `<div class="kv"><span>Deal size vs. the company</span><b class="num">${Math.round(sc.relative * 100)}% of market cap</b></div>` : ""}
      ${rd ? `<div class="kv"><span>Price</span><b class="num">$${(rd.price ?? 0).toFixed(2)} <span class="${cls(rd.change_pct)}">${pct(rd.change_pct)}</span></b></div>
        <div class="kv"><span>Traded</span><b class="num">${money(rd.dollar_volume)}${rd.rvol ? ` · ${rd.rvol}× normal volume` : ""}</b></div>` : ""}
      ${sc.blocked ? `<div class="sub" style="padding:10px 0 4px">Not boosted: ${esc(sc.blocked)}</div>` : ""}
      ${sc.move_text ? `<div class="sub" style="padding:10px 0 4px">Typical moves are rules of thumb from 2024–26 small-cap history, not a forecast.</div>` : ""}</div>` : "";
  const blocks = [
    `<div class="meta" style="--sev:${SEV[a.severity]}"><span class="sev">${esc(a.severity)}</span><span>${esc(e.radar ? "SMALL-CAP RADAR" : (it.source || a.kind).toUpperCase())}</span><span class="ago">${ago(a.created)} ago</span></div>`,
    `<h2>${esc(a.title)}</h2>`,
    an.summary ? `<p class="muted" style="font-size:15.5px">${esc(an.summary)}</p>` : a.body ? `<p class="muted" style="white-space:pre-line">${esc(a.body)}</p>` : "",
    capBlock,
    rel.length ? `<h4>The headline behind it</h4><div class="panel glass">${rel.map((x) => `<div class="li"><div class="grow">
        <a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a>
        <div class="sub">${esc(x.source)} · ${esc(x.age)} before the move</div></div></div>`).join("")}</div>` : "",
    chart ? `<h4>The tape since the alert</h4>${chart}` : "",
    p.direction ? `<h4>The play</h4><div class="panel glass padded">
        <div class="play" style="margin:0">${p.direction in dirLabel ? `<span class="dir ${p.direction}">${dirLabel[p.direction]}</span>` : ""}${meter(p.meter)}<span class="conv">${esc(p.conviction)} conviction · score ${Math.round(p.score)}</span></div>
        ${p.direction_basis === "precedents" ? `<div class="sub" style="margin-top:8px">Direction inferred from how similar events traded.</div>` : ""}
        ${p.direct?.length ? `<div class="sub" style="margin-top:14px">Direct</div><div class="tks" style="margin-top:7px">${p.direct.map((t) => `<span class="tk">${esc(t)}${since[t] != null ? ` <small class="${cls(since[t])}">${pct(since[t])} since alert</small>` : ""}</span>`).join("")}</div>` : ""}
        ${p.read_through?.length ? `<div class="sub" style="margin-top:14px">Read-through</div><div class="tks" style="margin-top:7px">${p.read_through.map((t) => `<span class="tk rt">${esc(t)}</span>`).join("")}</div>` : ""}
        ${p.themes?.length ? `<div class="sub" style="margin-top:14px">${esc(p.themes.join(" · "))}</div>` : ""}</div>` : "",
    e.precedents?.length ? `<h4>Precedents</h4><div class="panel glass">${e.precedents.map((x) => `<div class="li"><div class="grow">
        <div class="sub">${esc(prettyDate(x.date))}, ${esc(x.date.slice(0, 4))} · ${esc(x.category)}${x.first_source ? " · first: " + esc(x.first_source) : ""}</div>
        <div style="font-weight:600;margin:3px 0">${esc(x.headline)}</div><div class="${/(^|\s)[-−]\d/.test(x.move) ? "neg" : "pos"}" style="font-size:13.5px">${esc(x.move)}</div></div></div>`).join("")}</div>` : "",
    a.kind === "news" && `<h4>The edge</h4><div class="panel glass">
      ${lag != null ? `<div class="kv"><span>Caught after it was posted</span><b class="num">${dur(lag)}</b></div>` : ""}
      <div class="kv"><span>First seen on</span><b>${esc(it.source || "—")}</b></div>
      <div class="kv"><span>Mainstream caught up</span><b class="num">${e.lead ? `${esc(outlet(e.lead.source))}, ${dur(e.lead.lead_s)} later` : "not yet"}</b></div></div>`,
    tl ? `<h4>Who carried it</h4><div class="tl">${tl}</div>` : "",
    an.reasons?.length ? `<h4>Why Foretape flagged it</h4><details><summary>Score ${Math.round(an.score)} / 100 — show the breakdown</summary><ul class="reasons" style="margin-top:10px">${an.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>` : "",
    a.url ? `<div style="margin-top:26px"><a class="btn primary" href="${esc(a.url)}" target="_blank" rel="noopener">${rd && !rel.length ? "Open the quote" : "Open the source"} ↗</a></div>` : "",
  ].filter(Boolean);
  $("sheetBody").innerHTML = blocks.join("");
  [...$("sheetBody").children].forEach((c, i) => c.style.setProperty("--k", i));
  document.body.style.overflow = "hidden"; sheetOpen = true; Fx.dirty = true;
  $("scrim").classList.add("on"); $("sheet").classList.add("on"); $("sheet").scrollTop = 0;
}
function closeSheet() {
  sheetOpen = false; Fx.dirty = true;
  $("scrim").classList.remove("on"); $("sheet").classList.remove("on"); document.body.style.overflow = "";
  if (/#a=/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
}
$("scrim").onclick = closeSheet; $("closeSheet").onclick = closeSheet;
// swipe down to dismiss: drag from the top of the sheet, with rubber-band resistance and a flick threshold
(() => {
  const sh = $("sheet"); let y0 = 0, dy = 0, t0 = 0, active = false;
  sh.addEventListener("touchstart", (e) => { if (sh.scrollTop <= 0) { y0 = e.touches[0].clientY; t0 = performance.now(); active = true; dy = 0; sh.style.transition = "none"; } }, { passive: true });
  sh.addEventListener("touchmove", (e) => {
    if (!active) return;
    const d = e.touches[0].clientY - y0;
    if (d <= 0) { dy = 0; sh.style.transform = ""; return; }
    dy = d; e.preventDefault();
    sh.style.transform = `translateY(${(d * 0.92).toFixed(1)}px)`;
    $("scrim").style.opacity = String(Math.max(0, 1 - d / 420));
  }, { passive: false });
  const end = () => {
    if (!active) return; active = false; sh.style.transition = "";
    const v = dy / Math.max(1, performance.now() - t0);
    $("scrim").style.opacity = "";
    if (dy > 120 || (dy > 40 && v > 0.6)) closeSheet(); sh.style.transform = "";
  };
  sh.addEventListener("touchend", end); sh.addEventListener("touchcancel", end);
})();
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
document.addEventListener("click", (e) => {
  const seg = e.target.closest("#tapeSeg button");
  if (seg) {
    tapeFilter = seg.dataset.f; showAll = false;
    try { localStorage.setItem("ft.tape", tapeFilter); } catch (_) { /* private mode */ }
    renderTape(); return;
  }
  const el = e.target.closest("[data-id]");
  if (el && !e.target.closest("a")) openSheet(el.dataset.id);
});

// ------------------------------------------------------------------ live stream
function connect() {
  const es = new EventSource("/events?token=" + encodeURIComponent(TOKEN));
  const live = $("live");
  let dropped = false;
  es.onopen = () => { live.classList.add("live"); if (dropped) { dropped = false; load().catch(() => {}); } };
  es.onerror = () => { dropped = true; live.classList.remove("live"); };
  es.addEventListener("alert", (e) => {
    const a = JSON.parse(e.data); a._new = true; a.since = a.since || {};
    alerts = [a, ...alerts.filter((x) => x.id !== a.id)].slice(0, 150);
    renderTape();
    Scene.pulse?.();
    if (document.hidden || tab !== "tape") { unread++; $("unread").textContent = unread; $("unread").hidden = false; }
    if (a.edge?.radar && tab === "watch") loadRadar();  // the board updates the moment the radar fires
    if (!document.hidden && tab !== "tape" && (a.severity === "HIGH" || a.severity === "CRITICAL")) {
      toast((a.edge?.radar ? "◉ " : a.severity === "CRITICAL" ? "🔴 " : "🟠 ") + a.title.slice(0, 140), a.id);
    }
  });
  es.addEventListener("edge", (e) => {
    const d = JSON.parse(e.data), a = alerts.find((x) => x.id === d.id);
    if (!a) return;
    a.edge = { ...(a.edge || {}), ...d };
    renderTape();
  });
}

// ------------------------------------------------------------------ push
function b64ToBytes(b64) { const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - b64.length % 4) % 4)); return Uint8Array.from(s, (c) => c.charCodeAt(0)); }
function deviceLabel() {
  const ua = navigator.userAgent;
  const kind = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Browser";
  return kind + (standalone ? " app" : " browser");
}
async function subscribe(force) {
  const reg = await navigator.serviceWorker.ready;
  const key = (await (await fetch("/app/push-key")).text()).trim();
  let sub = await reg.pushManager.getSubscription();
  if (sub && (force || store.get("ft_vapid") !== key)) { await sub.unsubscribe(); sub = null; }
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  store.set("ft_vapid", key);
  await api("/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: sub.toJSON(), label: deviceLabel() }) });
  reg.active?.postMessage({ type: "token", token: TOKEN });
  load().catch(() => {});
  return sub;
}
async function refreshPushUI() {
  const perm = pushCapable ? Notification.permission : "unsupported";
  $("install").hidden = !(isIOS && !standalone);
  $("enable").hidden = !(pushCapable && perm === "default" && !(isIOS && !standalone));
  $("denied").hidden = perm !== "denied" || (isIOS && !standalone);
  Motion.ui();
  $("pushState").textContent = !pushCapable ? (isIOS && !standalone ? "add to home screen first" : "not supported here")
    : perm === "granted" ? "on ✓" : perm === "denied" ? "blocked" : "off";
  if (pushCapable && perm === "granted") { try { await subscribe(false); } catch (e) { $("pushState").textContent = "error: " + e.message; } }
}
$("enableBtn").onclick = async () => {
  try {
    const perm = await Notification.requestPermission();      // must come straight from the tap (iOS)
    if (perm !== "granted") { refreshPushUI(); return; }
    await subscribe(true);
    toast("Alerts are on. Sending a test…");
    await api("/api/push/test", { method: "POST", body: JSON.stringify({}) });
  } catch (e) { toast("Couldn't turn on alerts: " + e.message); }
  refreshPushUI();
};
$("testBtn").onclick = async () => {
  try { const r = await api("/api/push/test", { method: "POST", body: JSON.stringify({}) }); toast(r.sent ? `Test sent to ${r.sent} phone(s)` : "No phone subscribed yet"); }
  catch (e) { toast(e.message); }
};
$("resubBtn").onclick = async () => { try { await subscribe(true); toast("Reconnected"); } catch (e) { toast(e.message); } };
document.querySelectorAll("[data-cmd]").forEach((b) => b.onclick = async () => {
  try { const r = await api("/api/control", { method: "POST", body: JSON.stringify({ command: b.dataset.cmd }) }); toast(r.reply); $("mode").textContent = r.phone_mode; load().catch(() => {}); }
  catch (e) { toast(e.message); }
});

// ------------------------------------------------------------------ motion: pointer + gyro -> scene tilt and glass glare
const Motion = {
  on: false, gx: 0, gy: 0, px: 0, py: 0,
  needsPermission: typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function",
  handler(e) {
    if (e.gamma == null) return;
    const portrait = innerHeight >= innerWidth;
    const g = portrait ? e.gamma : e.beta, b = portrait ? e.beta : -e.gamma;
    Motion.gx = clamp(g / 22, -1, 1); Motion.gy = clamp((b - 55) / 22, -1, 1);
    Fx.dirty = true;
  },
  async enable() {
    if (reduced) return false;
    if (this.needsPermission) { try { if ((await DeviceOrientationEvent.requestPermission()) !== "granted") return false; } catch (_) { return false; } }
    if (typeof DeviceOrientationEvent === "undefined") return false;
    addEventListener("deviceorientation", this.handler);
    this.on = true; store.set("ft_motion", "1"); this.ui(); return true;
  },
  disable() { removeEventListener("deviceorientation", this.handler); this.on = false; this.gx = this.gy = 0; store.set("ft_motion", "0"); this.ui(); },
  ui() {
    $("motionState").textContent = this.on ? "on ✓ (tap to turn off)" : reduced ? "off (reduced motion)" : "off (tap to enable)";
    const card = !!document.querySelector(".hero-dock .dockcard:not([hidden])");
    $("motionBtn").hidden = !(isIOS && !this.on && store.get("ft_motion") !== "0" && !reduced) || card;
  },
};
$("motionBtn").onclick = async () => { if (!(await Motion.enable())) toast("Motion access was declined"); };
$("motionState").parentElement.style.cursor = "pointer";
$("motionState").parentElement.onclick = async () => { if (Motion.on) Motion.disable(); else if (!(await Motion.enable())) toast("Motion access was declined"); };
addEventListener("pointermove", (e) => {
  if (e.pointerType !== "mouse") return;
  Motion.px = (e.clientX / innerWidth - 0.5) * 2; Motion.py = (e.clientY / innerHeight - 0.5) * 2; Fx.dirty = true;
  const card = e.target.closest?.(".tilt:not(.alert .tilt)") || e.target.closest?.(".alert .tilt");
  document.querySelectorAll(".tilt.hot").forEach((c) => c !== card && Fx.rest(c));
  if (card) {
    const r = card.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    card.classList.add("hot");
    card.style.setProperty("--pry", ((x - 0.5) * 12).toFixed(2) + "deg");
    card.style.setProperty("--prx", ((0.5 - y) * 10).toFixed(2) + "deg");
    card.style.setProperty("--gx", (x * 100).toFixed(1) + "%"); card.style.setProperty("--gy", (y * 100).toFixed(1) + "%");
  }
}, { passive: true });
document.addEventListener("pointerleave", () => document.querySelectorAll(".tilt.hot").forEach(Fx.rest), true);

// ------------------------------------------------------------------ the choreography
const TAB_BASE = { tape: 0, brief: 0.5, calendar: 1.0, watch: 0.14, desk: 1.75 };
const Fx = {
  dirty: true, io: null, y: 0,
  observe(root) {
    if (!this.io) {
      this.io = new IntersectionObserver((ents) => {
        let i = 0;
        // 8% in view, or 90px of it for panels so tall that 8% is more than the screen shows
        ents.filter((e) => e.isIntersecting && (e.intersectionRatio >= 0.08 || e.intersectionRect.height >= 90)).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top).forEach((e) => {
          e.target.style.setProperty("--d", (i++ * 0.07).toFixed(2) + "s"); e.target.classList.add("in"); this.io.unobserve(e.target);
        });
      }, { threshold: [0, 0.02, 0.04, 0.06, 0.08], rootMargin: "0px 0px -6% 0px" });
    }
    (root || document).querySelectorAll(".rv:not(.in)").forEach((el) => {
      // a panel taller than most of the screen would swing out of view while tilted back for
      // its entrance (and so never trigger it): it rises flat instead
      el.classList.toggle("tall", el.offsetHeight > innerHeight * 0.6);
      this.io.observe(el);
    });
  },
  rest(c) { c.classList.remove("hot"); c.style.removeProperty("--pry"); c.style.removeProperty("--prx"); c.style.removeProperty("--gx"); c.style.removeProperty("--gy"); },
  split() {
    document.querySelectorAll(".display").forEach((h) => {
      if (h.dataset.done) return; h.dataset.done = "1";
      let i = 0; const out = document.createDocumentFragment();
      h.childNodes.forEach((n) => {
        if (n.nodeType === 3) n.textContent.split(/(\s+)/).forEach((w) => {
          if (!w) return;
          if (/^\s+$/.test(w)) { out.append(" "); return; }
          const s = document.createElement("span"); s.className = "w"; s.style.setProperty("--i", i++); s.textContent = w; out.append(s);
        });
        else { const s = document.createElement("span"); s.className = "w"; s.style.setProperty("--i", i++); s.append(n.cloneNode(true)); out.append(s); }
      });
      h.textContent = ""; h.append(out);
    });
  },
  frame() {
    const vh = innerHeight, y = scrollY;
    const doc = document.documentElement;
    doc.classList.toggle("scrolled", y > 24);
    doc.classList.toggle("on-scroll", y > 40);
    const base = TAB_BASE[tab] ?? 0;
    const climb = base + y / (vh * 2.7);
    Scene.setClimb(climb);
    Scene.setTilt(Motion.on ? Motion.gx : Motion.px * 0.6, Motion.on ? Motion.gy : Motion.py * 0.6);
    // veil: the hero is the raw photograph; content gets a darker sky behind it
    const veil = tab === "tape" ? clamp((y - vh * 0.30) / (vh * 0.55)) * 0.9 : 0.55 + clamp(y / vh) * 0.3;
    Scene.setPull(clamp(-y / 160));   // iOS rubber-band at the top: the camera dips toward the pad
    doc.style.setProperty("--veil", veil.toFixed(3));
    // rack focus: the scene softens behind content so cards and text separate from it, like a lens pulling focus
    Scene.setBlur(sheetOpen ? 3.2 : tab === "tape" ? clamp((y - vh * 0.35) / (vh * 0.9)) * 1.6 : 1.2 + clamp(y / vh) * 0.6);
    doc.style.setProperty("--sy", clamp(y / vh, 0, 3).toFixed(3));
    // light follows the phone (or the scroll, when there's no gyro)
    const gx = 50 + (Motion.on ? Motion.gx : Motion.px) * 38, gy = Motion.on ? 14 + Motion.gy * 26 : 10 + (Motion.py + 1) * 12 + ((y / vh) % 1) * 24;
    doc.style.setProperty("--gx", gx.toFixed(1) + "%"); doc.style.setProperty("--gy", gy.toFixed(1) + "%");
    // hero parallax: the headline rides slower than the page and dissolves into the sky
    const hi = tab === "tape" ? $("heroIn") : document.querySelector(`[data-view="${tab}"] .thero-in`);
    if (hi) {
      // a short tab header must be gone before the first section slides under it: dissolve over
      // its own height (the tall launch hero keeps its long, slow fade)
      const span = tab === "tape" ? vh * 0.62 : Math.max(120, (hi.parentElement?.offsetHeight || vh * 0.46) * 0.5);
      const k = clamp(y / span);
      hi.style.transform = `translate3d(0, ${(y * (tab === "tape" ? 0.42 : 0.3)).toFixed(1)}px, 0) scale(${(1 - k * 0.07).toFixed(4)})`;
      hi.style.opacity = (1 - k * 1.1).toFixed(3);
      hi.style.filter = tab === "tape" || k < 0.02 ? "" : `blur(${(k * 7).toFixed(1)}px)`;
    }
    document.querySelectorAll(".hero-dock").forEach((d) => { d.style.opacity = (1 - clamp(y / (vh * 0.4))).toFixed(3); d.style.transform = `translate3d(0, ${(y * 0.2).toFixed(1)}px, 0)`; });
    // cards sit on a cylinder: rising from the horizon, they straighten at eye level
    if (tab === "tape" || tab === "brief" || tab === "desk" || tab === "watch" || tab === "calendar") {
      const view = document.querySelector(`.view.on`);
      view?.querySelectorAll(".alert .tilt, .panel.tilt, .stat.tilt").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.bottom < -80 || r.top > vh + 80) return;
        // a panel taller than most of the screen (the board) would skew like a banner in the wind: leave it flat
        const tall = r.height > vh * 0.62;
        const c = tall ? 0 : clamp(((r.top + r.height / 2) / vh - 0.52) * 2, -1.2, 1.2);
        el.style.setProperty("--srx", (c * 6.5).toFixed(2) + "deg");
        el.style.setProperty("--stz", (-Math.abs(c) * 46).toFixed(1) + "px");
      });
    }
    // callouts ride on the rocket; they belong to the hero, so they fade as the page scrolls away
    const hud = $("hud");
    if (hud) {
      const cardUp = !!document.querySelector(".hero-dock .dockcard:not([hidden])");  // onboarding covers the rocket: callouts step aside
      const show = tab === "tape" && y < vh * 0.5 && !$("main").hidden && !cardUp;
      hud.classList.toggle("on", show);
      if (!show) hud.style.opacity = "0";   // an inline opacity from a previous frame would otherwise outlive the class
      if (show) {
        hud.style.opacity = (1 - clamp(y / (vh * 0.42))).toFixed(3);
        hud.querySelectorAll(".co").forEach((co) => {
          const [px, py] = Scene.project(+co.dataset.u, +co.dataset.v);
          const left = co.classList.contains("l") ? px - (co._w || (co._w = co.offsetWidth || 150)) : px;  // "l" callouts extend leftwards from the anchor
          co.style.transform = `translate3d(${left.toFixed(1)}px, ${py.toFixed(1)}px, 0)`;
          if (Scene.intro > 0.7 && !co.classList.contains("show")) { co.style.setProperty("--n", co.dataset.d); co.classList.add("show"); }
        });
      }
    }
    // altitude readout
    const alt = Scene.climb * 38;
    $("railAlt").textContent = "ALT " + (alt < 10 ? alt.toFixed(1) : Math.round(alt)) + " KM";
    $("railDot").style.top = clamp(Scene.climb / 2.4) * 100 + "%";
  },
  tick() {
    // the callouts must follow the camera every frame while the hero is on screen (intro zoom, tilt, breathing)
    const heroLive = tab === "tape" && scrollY < innerHeight * 0.5;
    const moving = Math.abs(Scene.climb - Scene.climbTarget) > 0.0004 || Scene.intro < 1 || Motion.on
      || Math.abs(Scene.tilt[0] - Scene.tiltTarget[0]) + Math.abs(Scene.tilt[1] - Scene.tiltTarget[1]) > 0.002;
    if (this.dirty || moving || heroLive) { this.dirty = false; this.frame(); }
    requestAnimationFrame(() => this.tick());
  },
};
let lastY = 0;
addEventListener("scroll", () => {
  Fx.dirty = true;
  const y = scrollY, d = y - lastY;
  if (Math.abs(d) > 6) { $("dock").classList.toggle("tucked", d > 0 && y > 120 && !sheetOpen); lastY = y; }   // tuck away while reading down, return on the way up
}, { passive: true });
addEventListener("resize", () => (Fx.dirty = true));
// ------------------------------------------------------------------ navigation
const LOADERS = { brief: loadBrief, calendar: loadCalendar, watch: loadWatch };
const TABS = ["tape", "brief", "calendar", "watch", "desk"];
function go(name) {
  if (name === tab && !document.querySelector(`.view[data-view="${name}"]`).hidden) { scrollTo({ top: 0, behavior: "smooth" }); return; }
  tab = name;
  document.querySelectorAll(".dock button").forEach((x) => x.classList.toggle("on", x.dataset.tab === name));
  $("pip").style.transform = `translateX(${TABS.indexOf(name) * 100}%)`;
  document.querySelectorAll(".view").forEach((v) => {
    const on = v.dataset.view === name;
    v.hidden = !on; v.classList.toggle("on", on);
    if (on) { v.classList.remove("enter"); void v.offsetWidth; v.classList.add("enter"); }
  });
  if (name === "tape") { unread = 0; $("unread").hidden = true; }
  scrollTo(0, 0);
  Fx.dirty = true;
  if (name === "desk") load().catch(() => {});
  if (name === "tape") requestAnimationFrame(placeThumb);
  Promise.resolve(LOADERS[name]?.()).catch(() => {}).then(() => requestAnimationFrame(() => Fx.observe(document.querySelector(".view.on"))));
  Fx.observe(document.querySelector(".view.on"));
}
document.querySelectorAll(".dock button").forEach((b) => (b.onclick = () => go(b.dataset.tab)));
$("nextCat").onclick = () => go("calendar");
$("ticker").onclick = () => go("watch");
setInterval(() => document.querySelectorAll(".ago[data-t]").forEach((el) => (el.textContent = ago(+el.dataset.t))), 15000);
setInterval(() => { if (!document.hidden && TOKEN && !$("main").hidden) { load().catch(() => {}); if (tab === "watch") loadWatch().catch(() => {}); } }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && TOKEN) { navigator.clearAppBadge?.(); load().catch(() => {}); } });
function showLock() { $("lock").hidden = false; $("main").hidden = true; $("dock").hidden = true; }
$("unlock").onclick = async () => {
  TOKEN = $("key").value.trim(); store.set("ft_token", TOKEN); setManifest();
  try { await start(); } catch (e) { toast("That key didn't work"); }
};
function route() {
  const m = location.hash.match(/a=([\w-]+)/);
  if (!m) return;
  if (m[1] === "brief") return go("brief");
  if (m[1] !== "test") openSheet(m[1]);
}
addEventListener("hashchange", route);

async function start() {
  if (!TOKEN) { showLock(); return; }
  await load();
  $("lock").hidden = true; $("main").hidden = false; $("dock").hidden = false;
  Fx.split(); Fx.observe(document);
  connect();
  if ("serviceWorker" in navigator) await navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" });
  navigator.clearAppBadge?.();
  refreshPushUI();
  if (store.get("ft_motion") === "1" || (!Motion.needsPermission && !reduced && matchMedia("(pointer: coarse)").matches)) Motion.enable().then(() => Motion.ui());
  Motion.ui();
  route();
}

// boot: the scene starts immediately (it's behind the lock screen too), the app follows
Fx.split();
let lifted = false;
const lift = () => { if (lifted) return; lifted = true; $("curtain").classList.add("off"); Scene.ignite(); };
Scene.onReady = () => requestAnimationFrame(lift);
setTimeout(lift, 9000);   // never leave someone on a black screen if the photo is slow
Scene.start($("scene"));
Fx.tick();
start().catch(() => {});
