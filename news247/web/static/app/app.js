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
let alerts = [], app = {}, tab = "tape", unread = 0;
const seen = new Set();

function setManifest() { $("manifest").href = "/app/manifest.webmanifest" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""); }
setManifest();
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 3200); }
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
function footRow(a) {
  const it = a.item || {}, e = a.edge || {}, parts = [];
  const lag = it.published && it.detected ? Math.max(0, it.detected - it.published) : null;
  if (lag != null) parts.push(`<span>⚡ ${dur(lag)} after posted</span>`);
  if (e.lead) parts.push(`<span class="lead">Beat ${esc(outlet(e.lead.source))} by ${dur(e.lead.lead_s)}</span>`);
  if (a.url) parts.push(`<a href="${esc(a.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Source ↗</a>`);
  return parts.length ? `<div class="foot">${parts.join("")}</div>` : "";
}
function alertHTML(a, opts = {}) {
  const an = a.analysis || {}, it = a.item || {};
  const src = a.kind === "price" ? "PRICE ACTION" : a.kind === "system" ? "FORETAPE" : (it.source || a.kind).toUpperCase();
  const why = an.summary || (a.kind !== "news" && a.body ? a.body.split("\n")[0] : "");
  const known = seen.has(a.id);
  const state = a._new ? "new in" : known ? "in" : "";
  return `<article class="alert ${opts.feature ? "feature " : ""}${known || a._new ? "" : "rv "}${state}" data-id="${esc(a.id)}">
    <div class="glass tilt" style="--sev:${SEV[a.severity] || SEV.LOW}"><i class="sevbar"></i>
      ${opts.feature ? `<div class="eyebrow" style="color:var(--gold);margin-bottom:12px">Top of the tape</div>` : ""}
      <div class="meta"><span class="sev">${esc(a.severity)}</span><span class="src">${esc(src)}</span><span class="ago" data-t="${a.created}">${ago(a.created)}</span></div>
      <h3>${esc(a.title)}</h3>
      ${why ? `<p class="why">${esc(why)}</p>` : ""}
      ${playRow(a)}${tickerChips(a)}${precLine(a)}${footRow(a)}
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
function renderTape() {
  const f = featured();
  $("featureWrap").innerHTML = f ? alertHTML(f, { feature: true }) : "";
  const rest = alerts.filter((a) => a !== f);
  $("alerts").innerHTML = rest.length ? rest.map((a) => alertHTML(a)).join("")
    : f ? "" : emptyHTML("Quiet tape. Foretape is watching every first-to-publish source; the next market-moving headline lands here and on your lock screen.");
  $("tapeCount").textContent = alerts.length ? `${alerts.length} recent` : "";
  alerts.forEach((a) => { seen.add(a.id); delete a._new; });
  Fx.observe($("featureWrap")); Fx.observe($("alerts"));
}
function renderTop() {
  const hour = new Date().getHours();
  const g = hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  $("greet").textContent = `${g} · ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}`;
  const m = app.market;
  $("mkt").lastElementChild.textContent = marketText(m);
  $("mkt").classList.toggle("open", !!m?.open);
  if (app.edge?.median_lead_s != null) countTo($("sLead"), app.edge.median_lead_s, (v) => dur(v).replace(/ 0?0s$/, ""));
  else $("sLead").textContent = "—";
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
  ].map(([k, v]) => `<div class="kv"><span>${k}</span><b class="num">${esc(v)}</b></div>`).join("");
  $("durable").hidden = app.durable;
  $("durable").textContent = app.durable ? "" : "This server forgets subscribed phones when it restarts. Foretape reconnects this phone every time you open it; for zero gaps, set STATE_DB (free Postgres, see docs).";
  const lvl = /CRITICAL/.test(app.phone_mode) ? "critical" : /MEDIUM/.test(app.phone_mode) ? "more" : /normal/.test(app.phone_mode) ? "normal" : "";
  document.querySelectorAll("#levels button").forEach((b) => b.classList.toggle("sel", b.dataset.cmd === lvl));
  if (tab === "brief") loadBrief().catch(() => {});
}

async function loadBrief() {
  const b = await api("/api/brief");
  $("bDate").textContent = `${b.greeting} · ${b.date}`;
  $("bSub").textContent = b.count ? `${b.count} catalyst${b.count > 1 ? "s" : ""} since the last close.` : "A quiet night. Nothing crossed the alert line since the last close.";
  $("bOvernight").innerHTML = b.overnight.length ? b.overnight.map((a) => `<div class="li" data-id="${esc(a.id)}" style="cursor:pointer">
      <span class="dot-sev" style="background:${SEV[a.severity]};color:${SEV[a.severity]}"></span>
      <div class="grow"><div style="font-weight:600;line-height:1.35">${esc(a.title)}</div>
      <div class="sub">${esc(a.source)} · ${ago(a.created)} ago${a.tickers.length ? " · " + esc(a.tickers.join(" ")) : ""}</div></div>
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
async function loadWatch() {
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
  let a;
  try { a = await api("/api/alert/" + encodeURIComponent(id)); } catch (_) { a = alerts.find((x) => x.id === id); }
  if (!a) return;
  const an = a.analysis || {}, it = a.item || {}, e = a.edge || {}, p = e.play || {}, since = a.since || {};
  const lag = it.published && it.detected ? Math.max(0, it.detected - it.published) : null;
  const tl = (a.story_sources || []).map((s, i) => `<div class="li"><div class="grow"><b>${esc(s.source)}</b>
      <div class="sub">${i === 0 ? "first" : "+" + dur(s.detected - a.story_sources[0].detected)} · ${esc(s.tier || "")}</div></div></div>`).join("");
  const chart = sparkline(a.series, a.created);
  const blocks = [
    `<div class="meta" style="--sev:${SEV[a.severity]}"><span class="sev">${esc(a.severity)}</span><span>${esc((it.source || a.kind).toUpperCase())}</span><span class="ago">${ago(a.created)} ago</span></div>`,
    `<h2>${esc(a.title)}</h2>`,
    an.summary ? `<p class="muted" style="font-size:15.5px">${esc(an.summary)}</p>` : a.body ? `<p class="muted">${esc(a.body)}</p>` : "",
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
    `<h4>The edge</h4><div class="panel glass">
      ${lag != null ? `<div class="kv"><span>Caught after it was posted</span><b class="num">${dur(lag)}</b></div>` : ""}
      <div class="kv"><span>First seen on</span><b>${esc(it.source || "—")}</b></div>
      <div class="kv"><span>Mainstream caught up</span><b class="num">${e.lead ? `${esc(outlet(e.lead.source))}, ${dur(e.lead.lead_s)} later` : "not yet"}</b></div></div>`,
    tl ? `<h4>Who carried it</h4><div class="tl">${tl}</div>` : "",
    an.reasons?.length ? `<h4>Why Foretape flagged it</h4><details><summary>Score ${Math.round(an.score)} / 100 — show the breakdown</summary><ul class="reasons" style="margin-top:10px">${an.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>` : "",
    a.url ? `<div style="margin-top:26px"><a class="btn primary" href="${esc(a.url)}" target="_blank" rel="noopener">Open the source ↗</a></div>` : "",
  ].filter(Boolean);
  $("sheetBody").innerHTML = blocks.join("");
  [...$("sheetBody").children].forEach((c, i) => c.style.setProperty("--k", i));
  document.body.style.overflow = "hidden";
  $("scrim").classList.add("on"); $("sheet").classList.add("on"); $("sheet").scrollTop = 0;
}
function closeSheet() {
  $("scrim").classList.remove("on"); $("sheet").classList.remove("on"); document.body.style.overflow = "";
  if (/#a=/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
}
$("scrim").onclick = closeSheet; $("closeSheet").onclick = closeSheet;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
document.addEventListener("click", (e) => {
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
    $("motionBtn").hidden = !(isIOS && !this.on && store.get("ft_motion") !== "0" && !reduced) || !!$("enable").offsetParent;
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
        ents.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top).forEach((e) => {
          e.target.style.setProperty("--d", (i++ * 0.07).toFixed(2) + "s"); e.target.classList.add("in"); this.io.unobserve(e.target);
        });
      }, { threshold: 0.08, rootMargin: "0px 0px -6% 0px" });
    }
    (root || document).querySelectorAll(".rv:not(.in)").forEach((el) => this.io.observe(el));
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
    doc.style.setProperty("--veil", veil.toFixed(3));
    doc.style.setProperty("--sy", clamp(y / vh, 0, 3).toFixed(3));
    // light follows the phone (or the scroll, when there's no gyro)
    const gx = 50 + (Motion.on ? Motion.gx : Motion.px) * 38, gy = Motion.on ? 14 + Motion.gy * 26 : 10 + (Motion.py + 1) * 12 + ((y / vh) % 1) * 24;
    doc.style.setProperty("--gx", gx.toFixed(1) + "%"); doc.style.setProperty("--gy", gy.toFixed(1) + "%");
    // hero parallax: the headline rides slower than the page and dissolves into the sky
    const hi = tab === "tape" ? $("heroIn") : document.querySelector(`[data-view="${tab}"] .thero-in`);
    if (hi) {
      const k = clamp(y / (vh * 0.62));
      hi.style.transform = `translate3d(0, ${(y * 0.42).toFixed(1)}px, 0) scale(${(1 - k * 0.07).toFixed(4)})`;
      hi.style.opacity = (1 - k * 1.1).toFixed(3);
    }
    document.querySelectorAll(".hero-dock").forEach((d) => { d.style.opacity = (1 - clamp(y / (vh * 0.4))).toFixed(3); d.style.transform = `translate3d(0, ${(y * 0.2).toFixed(1)}px, 0)`; });
    // cards sit on a cylinder: rising from the horizon, they straighten at eye level
    if (tab === "tape" || tab === "brief" || tab === "desk" || tab === "watch" || tab === "calendar") {
      const view = document.querySelector(`.view.on`);
      view?.querySelectorAll(".alert .tilt, .panel.tilt, .stat.tilt").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.bottom < -80 || r.top > vh + 80) return;
        const c = clamp(((r.top + r.height / 2) / vh - 0.52) * 2, -1.2, 1.2);
        el.style.setProperty("--srx", (c * 6.5).toFixed(2) + "deg");
        el.style.setProperty("--stz", (-Math.abs(c) * 46).toFixed(1) + "px");
      });
    }
    // altitude readout
    const alt = Scene.climb * 38;
    $("railAlt").textContent = "ALT " + (alt < 10 ? alt.toFixed(1) : Math.round(alt)) + " KM";
    $("railDot").style.top = clamp(Scene.climb / 2.4) * 100 + "%";
  },
  tick() { if (this.dirty || Math.abs(Scene.climb - Scene.climbTarget) > 0.0004 || Motion.on) { this.dirty = false; this.frame(); } requestAnimationFrame(() => this.tick()); },
};
addEventListener("scroll", () => (Fx.dirty = true), { passive: true });
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
  Promise.resolve(LOADERS[name]?.()).catch(() => {}).then(() => requestAnimationFrame(() => Fx.observe(document.querySelector(".view.on"))));
  Fx.observe(document.querySelector(".view.on"));
}
document.querySelectorAll(".dock button").forEach((b) => (b.onclick = () => go(b.dataset.tab)));
$("nextCat").onclick = () => go("calendar");
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
Scene.onReady = () => requestAnimationFrame(() => { $("curtain").classList.add("off"); Scene.ignite(); });
Scene.start($("scene"));
Fx.tick();
start().catch(() => {});
