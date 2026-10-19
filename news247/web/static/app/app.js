/* Foretape app: data, push, and the choreography that ties the page to the scene. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const SEV = { CRITICAL: "#FF8A55", HIGH: "#FFD08A", MEDIUM: "#A9C8FF", LOW: "rgba(255,255,255,.4)" };
const OUTLETS = [["cnbc", "CNBC"], ["yahoo", "Yahoo Finance"], ["gnews", "Google News"], ["mw-", "MarketWatch"], ["wsj", "WSJ"],
  ["bloomberg", "Bloomberg"], ["nyt", "NYT"], ["fortune", "Fortune"], ["axios", "Axios"], ["hackernews", "Hacker News"], ["sec-insider", "SEC Form 4"], ["sec-", "SEC"], ["globenewswire", "GlobeNewswire"], ["businesswire", "Business Wire"], ["prnewswire", "PR Newswire"]];
const params = new URLSearchParams(location.search);
const store = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} } };
let TOKEN = params.get("token") || store.get("ft_token") || "";
// this phone's id: its desk (watchlist, price alerts, tracked calls, preferences) lives on the server under it
const DEVICE = (() => {
  let d = store.get("ft_device");
  if (!d || !/^[A-Za-z0-9_-]{8,64}$/.test(d)) {
    d = "d" + Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
    store.set("ft_device", d);
  }
  return d;
})();
if (params.get("token")) store.set("ft_token", TOKEN);
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const pushCapable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
let alerts = [], app = {}, tab = "tape", unread = 0, showAll = false, sheetOpen = false;
let sheetAlert = null;  // the alert open in the sheet (copy as text, why-push)
let desk = null, mine = new Set();  // this phone's desk (watchlist, price alerts, tracked calls) and its watched symbols
const seen = new Set();

// display: "clear" (no 3D scene, no glass blur: legible and light on the battery) and larger text
function applyDisplay() {
  const light = store.get("ft_light") === "1", clear = light || store.get("ft_clear") === "1", big = store.get("ft_text") === "big";
  document.body.classList.toggle("clear", clear);
  document.body.classList.toggle("light", light);
  document.body.classList.toggle("compact", store.get("ft_compact") === "1");
  document.body.classList.toggle("big", big);
  try { Scene.setClear?.(clear); } catch (_) { /* the scene loads after this file on some paths */ }
}
// offline: the service worker answered with the last tape it saw
function showOffline(ts) {
  const el = $("offline");
  if (!el) return;
  el.hidden = !ts;
  if (ts) el.textContent = `Offline · showing the tape as of ${new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
function setManifest() { $("manifest").href = "/app/manifest.webmanifest" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""); }
setManifest();
function toast(msg, id, ms) {
  // an alert toast is tappable: it opens that alert's dossier (the global [data-id] handler)
  const t = $("toast"); t.textContent = msg;
  if (id) t.dataset.id = id; else delete t.dataset.id;
  t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), ms || (id ? 5200 : 3200));
}
// a toast with Undo: removals can be taken back for a few seconds
let pendingUndo = null;
function toastUndo(msg, fn) {
  pendingUndo = fn;
  const t = $("toast");
  t.innerHTML = `${esc(msg)} <button class="undo" data-act="undo">Undo</button>`;
  delete t.dataset.id;
  t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => { t.classList.remove("show"); pendingUndo = null; }, 6000);
}
// ⓘ: what a term means, for anyone new to it
const GLOSSARY = {
  dial: "The push dial: how loud this one stock is on your phone. Default follows your settings; HIGH only skips its MEDIUM alerts; Everything pushes even MEDIUM alerts and ignores your floor and the fades filter (your categories, mutes and quiet hours still apply); Off never pushes it.",
  implied: "Implied move: what the options market is paying for. The at-the-money call + put (the straddle) for an expiry, as a % of the stock: roughly the move priced in by that date, up or down.",
  callwall: "Call wall: the strike above the stock with the most call open interest. Dealers hedging those calls can make it act like a ceiling into expiry; a clean break through it can speed up.",
  putwall: "Put wall: the strike below the stock with the most put open interest. It often acts like a floor into expiry.",
  maxpain: "Max pain: the strike where option holders, in total, would collect the least at expiry. Some traders watch for the stock to drift toward it into expiry; it's a tendency, not a rule.",
  pcratio: "Put/call volume: puts traded today ÷ calls traded. Above ~1.2 is heavy put buying (bearish or hedging); under ~0.6 is call-heavy.",
  evidence: "The evidence tally: every signal on the stock, weighted (one-sided options ×2, an opportunistic insider buy ×3, alerts by severity, headlines ×0.5, tape ignition ×1-1.5). It leans one way only when that side is at least twice the other.",
  base: "Base rate: how alerts of the same kind did, graded an hour later and at the close on their own stock. 50% is a coin flip; under 40% on 20+ graded means they tend to fade.",
  confluence: "Confluence: 3+ independent kinds of evidence (headlines, options flow, insider buying, the tape) on the same side of one stock. Pushed when the combined weight reaches 7.",
  sympathy: "Sympathy move: when one stock jumps on news, its peers often follow. Peers that haven't moved yet are where that would show up, if it comes.",
  rel: "Relative strength: the stock's move minus the S&P 500's and its sector's. A big gap means the move is its own (news), not the market's.",
  ignition: "Ignition: a stock starting to move on a burst of volume (several times its normal pace) within minutes. Caught a median 20 minutes before it shows up as a big mover; on its own it's a coin flip, so pushes need confirmation.",
  gap: "Gap: a move from yesterday's close, often made before the open on overnight news. The board shows companies worth $300M+ moving 3%+.",
  halt: "Halt: the exchange stops trading in a stock. T1 = news pending, LUDP = a 5-minute volatility pause (limit up/limit down), H10 = SEC suspension. It reopens at the resumption time.",
  smart: "Smart money: options bought hard on one side, insiders buying with their own cash (Form 4), and stocks igniting on volume, in one feed.",
  vwap: "VWAP: the volume-weighted average price of the session, where the day's average share changed hands. Above it, buyers have been in control; traders watch reclaims and losses of it. The 9:35 price is the end of the first five minutes, the opening range.",
  regime: "Market regime: risk-on when cyclical sectors (tech, discretionary, industrials, financials, energy, materials) lead defensive ones (utilities, staples, health care, real estate), most stocks are up and small caps lead large; risk-off is the reverse. A read of today's tape, not a forecast.",
  inplay: "In play: one ranked list from every board: the day's move, volume against normal, news buzz, options flow, a confluence alert, the radar, and whether there's a known reason.",
  digest: "Digest: instead of one push per alert, one push every 15 minutes or hour listing them. CRITICAL alerts and your own price alerts still come at once.",
};
// a short two-note chime for alerts while the app is open (Web Audio: nothing to download)
let audioCtx = null;
function chime(sev) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const notes = sev === "CRITICAL" ? [880, 660, 880] : [660, 880];
    notes.forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = audioCtx.currentTime + i * 0.13;
      o.frequency.value = f; o.type = "sine";
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      o.connect(g).connect(audioCtx.destination); o.start(t); o.stop(t + 0.13);
    });
  } catch (_) { /* no audio: fine */ }
}
// the deep links: for iOS Shortcuts, widgets, bookmarks and other apps
function openLinks() {
  const base = location.origin + "/app/";
  const rows = [["A stock's page", base + "#t=NVDA"], ["An alert", base + "#a=ALERT_ID"], ["Search the tape", base + "#q=tariff"], ["The Brief", base + "#a=brief"], ["Your desk", base + "#a=desk"],
    ["Calendar feed (webcal)", icsUrl("webcal")]];
  showSheet(`<div class="meta"><span class="src">For Shortcuts, widgets and bookmarks</span></div><h2>Shortcuts &amp; links</h2>
    <p class="muted">Every page has a link. In the iOS Shortcuts app, an <b>Open URL</b> action with one of these opens Foretape right there (add it to the home screen or say it to Siri).</p>
    <div class="panel glass">${rows.map(([n, u]) => `<div class="li"><div class="grow"><div style="font-weight:600">${esc(n)}</div><div class="sub" style="word-break:break-all">${esc(u)}</div></div><button class="btn ghost small" data-act="copy" data-s="${esc(u)}">Copy</button></div>`).join("")}</div>
    <p class="sub">The calendar link carries your access key: keep it private.</p>`);
}
function openGlossary() {
  const names = { implied: "Implied move", callwall: "Call wall", putwall: "Put wall", maxpain: "Max pain", pcratio: "Put/call volume", evidence: "The evidence tally", base: "Base rate", confluence: "Confluence", sympathy: "Sympathy move", rel: "Relative strength", ignition: "Ignition", gap: "Gap", halt: "Halt", smart: "Smart money", digest: "Digest", vwap: "VWAP and the 9:35 price", regime: "Market regime", inplay: "In play" };
  showSheet(`<div class="meta"><span class="src">Foretape, explained</span></div><h2>What the terms mean</h2>` + Object.entries(GLOSSARY).map(([k, v]) => `<h4>${esc(names[k] || k)}</h4><p class="muted" style="margin:0">${esc(v.replace(/^[^:]+: /, "").replace(/^./, (c) => c.toUpperCase()))}</p>`).join(""));
}
const tip = (k) => `<button class="tip" data-act="term" data-s="${k}" aria-label="What is this?">ⓘ</button>`;
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN, "X-Device": DEVICE, ...(opts.headers || {}) } });
  if (r.status === 401) { showLock(); throw new Error("access key needed"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  const off = r.headers.get("X-Foretape-Offline");
  if (path === "/api/app") showOffline(off ? +off : 0);
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
    return `<span class="tk${mine.has(t) ? " mine" : ""}" data-tk="${esc(t)}" role="button">${esc(t)}${s != null ? ` <small class="${cls(s)}">${pct(s)}</small>` : ""}</span>`;
  });
  (play?.read_through || []).slice(0, Math.max(0, limit - chips.length + 2)).forEach((t) => chips.push(`<span class="tk rt" data-tk="${esc(t)}" role="button">${esc(t)}</span>`));
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
  const ig = a.edge?.ignition;
  if (ig) {  // a stock starting to move (market/ignition.py): what backs it, or that nothing does yet
    const by = (ig.confirmed_by || [])[0];
    const tag = ig.burst ? `${Math.round(ig.burst)}× volume` : ig.rvol ? `${ig.rvol}× day volume` : "";
    return `<div class="cap ${d}"><span class="capb">⚡ ${ig.kind === "buildup" ? "Building" : "Ignition"}</span><span class="capl">${by ? esc("Backed by " + by.split(":")[0]) : "Nothing backs it yet"}</span>${tag ? `<span class="capx">${esc(tag)}</span>` : ""}</div>`;
  }
  if (s.radar) {  // the title already names the stock and its size: say what the radar saw
    const r = a.edge.radar || {}, news = (a.related || []).some((x) => x.kind === "news");
    const tag = r.rvol && r.rvol >= 1.5 ? `${Math.round(r.rvol)}× normal volume` : r.session === "pre" ? "pre-market" : r.session === "post" ? "after hours" : "";
    return `<div class="cap ${d}"><span class="capb">◉ Radar</span><span class="capl">${news ? "On the news" : "Before any headline"}</span>${tag ? `<span class="capx">${esc(tag)}</span>` : ""}</div>`;
  }
  return `<div class="cap ${d}"><span class="capb">${esc(s.band)}</span><b class="sym">${esc(s.symbol)}</b><span class="capm">${esc(s.cap)}</span>
    <span class="capl">${esc(s.label)}</span>${s.move_text ? `<span class="capx">${esc(s.move_text)}</span>` : ""}</div>`;
}
// a move with no headline: what the instant EDGAR + news lookup found (market/why.py)
function whyRow(a) {
  const w = a.edge?.why;
  if (!w) return "";
  if (!w.found) return `<div class="sub" style="margin-top:4px">Why: nothing in today's filings or news yet (checked in ${esc(w.took_s)}s).</div>`;
  return `<div class="sub" style="margin-top:4px">Why: <b>${esc(w.title)}</b> · ${esc(w.source === "why-edgar" ? "EDGAR" : "news search")}${w.score != null ? ` · score ${Math.round(w.score)}` : ""}</div>`;
}
const isMine = (a) => mine.size > 0 && [...(a.tickers || []), ...(a.edge?.play?.direct || [])].some((t) => mine.has(t));
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
function baseRow(a) {
  const b = a.edge?.base;
  if (!b) return "";
  const bits = [b.h1 && `<b class="${b.h1.hit >= 0.5 ? "pos" : "neg"}">${Math.round(b.h1.hit * 100)}%</b> in 1h`, b.close && `<b class="${b.close.hit >= 0.5 ? "pos" : "neg"}">${Math.round(b.close.hit * 100)}%</b> by the close`].filter(Boolean);
  const n = Math.max(b.h1?.n || 0, b.close?.n || 0);
  return `<div class="base">${b.fades ? `<b class="neg">⚠ Historically fades:</b> alerts like this went their way only ` : "Alerts like this went their way "}${bits.join(", ")} <span class="faint">· ${n} graded</span></div>`;
}
const CONF_KIND = { news: "Headlines", options: "Options", insider: "Insider", tape: "Tape", macro: "Macro" };
function confRow(a) {
  const c = a.edge?.confluence;
  if (!c) return "";
  return `<div class="conf"><span class="dir ${c.side}">${c.side === "up" ? "▲ Bullish" : "▼ Bearish"}</span>${c.kinds.map((k) => `<span class="ck">${esc(CONF_KIND[k] || k)}</span>`).join("")}<span class="faint num">weight ${c.weight}${c.against ? ` vs ${c.against}` : ""}</span></div>`;
}
// how the alert played out: its stock an hour later and at the close, from its own reference price
function gradeRow(a) {
  const g = Object.entries(a.edge?.outcome || {}).find(([, r]) => r.sign);
  if (!g) return "";
  const [sym, r] = g, mark = (v) => (v == null ? "" : r.sign * v > 0 ? `<b class="pos">✓</b>` : `<b class="neg">✗</b>`);
  const bit = (k, label) => r[k] != null ? `${label} <span class="num ${cls(r[k])}">${pct(r[k])}</span> ${mark(r[k])}` : "";
  return `<div class="grade">${esc(sym)}: ${[bit("h1", "1h"), bit("close", "close"), bit("d1", "next day"), bit("d5", "5 days")].filter(Boolean).join(" · ")}</div>`;
}
function preRow(a) {
  const pre = a.edge?.pre;
  if (!pre) return "";
  const [sym, x] = Object.entries(pre)[0];
  if (Math.abs(x) < 1) return "";
  const d = a.edge?.play?.direction || a.analysis?.direction || a.edge?.confluence?.side;
  const way = d === "up" ? (x > 0 ? "its way" : "against it") : d === "down" ? (x < 0 ? "its way" : "against it") : "";
  return `<div class="pre">${esc(sym)} had already moved <b class="${cls(x)}">${pct(x)}</b> in the 15 minutes before${way ? ` (${way})` : ""}</div>`;
}
// the edge score: one 0-100 composite of the alert's own measured stats, with its parts shown
function edgeScore(a) {
  const e = a.edge || {}, parts = [];
  const hits = [e.base?.h1?.hit, e.base?.close?.hit].filter((x) => x != null);
  const hit = hits.length ? hits.reduce((x, y) => x + y, 0) / hits.length : null;
  parts.push(["Base rate", hit != null ? hit : 0.5, 0.45, hit != null ? `${Math.round(hit * 100)}% went their way` : "not enough graded yet (neutral)"]);
  const pre = e.pre ? Object.values(e.pre)[0] : null, d = e.play?.direction || a.analysis?.direction || e.confluence?.side;
  const y = pre != null && (d === "up" || d === "down") ? pre * (d === "up" ? 1 : -1) : null;
  parts.push(["Freshness", y == null ? 0.6 : Math.abs(y) < 1 ? 1 : y > 0 ? 0.35 : 0.6, 0.2, y == null ? "no pre-alert tape" : Math.abs(y) < 1 ? "the stock hadn't moved yet" : y > 0 ? `already moved ${pct(pre)} its way` : `moved ${pct(pre)} against it first`]);
  parts.push(["Confluence", e.confluence ? 1 : 0.4, 0.15, e.confluence ? `${e.confluence.kinds.length} kinds of evidence line up` : "one kind of evidence"]);
  const sev = { CRITICAL: 1, HIGH: 0.75, MEDIUM: 0.5, LOW: 0.25 }[a.severity] ?? 0.5;
  parts.push(["Severity", sev, 0.2, a.severity]);
  return { score: Math.round(parts.reduce((t, [, v, w]) => t + v * w, 0) * 100), parts };
}
// how much of your portfolio an alert is about: holdings among its direct and read-through names
let portf = null;  // {bySym: {SYM: value}, total}
async function loadPortf() {
  try {
    const r = await api("/api/portfolio");
    portf = { total: r.value || 0, bySym: Object.fromEntries((r.rows || []).filter((x) => x.value).map((x) => [x.symbol, x.value])) };
  } catch (_) { portf = null; }
}
function exposure(a) {
  if (!portf?.total) return null;
  const p = a.edge?.play || {}, direct = new Set([...(a.tickers || []), ...(p.direct || [])]);
  const d = [...direct].reduce((t, x) => t + (portf.bySym[x] || 0), 0);
  const r = (p.read_through || []).filter((x) => !direct.has(x)).reduce((t, x) => t + (portf.bySym[x] || 0), 0);
  return d || r ? { direct: d, related: r, pct: (d / portf.total) * 100, rpct: (r / portf.total) * 100 } : null;
}
function expoRow(a) {
  const e = exposure(a);
  if (!e) return "";
  const usd = (v) => "$" + Math.round(v).toLocaleString();
  return `<div class="expo">💼 ${e.direct ? `Touches <b>${e.pct.toFixed(e.pct < 10 ? 1 : 0)}%</b> of your portfolio (${usd(e.direct)})` : ""}${e.direct && e.related ? " · " : ""}${e.related ? `${e.rpct.toFixed(1)}% more via read-through names` : ""}</div>`;
}
// beginner mode: one plain-language line on each card
function plainLine(a) {
  if (store.get("ft_beginner") !== "1" || a.kind === "system") return "";
  const t = (a.edge?.play?.direct || a.tickers || [])[0], d = a.edge?.play?.direction || a.analysis?.direction || a.edge?.confluence?.side;
  const size = { CRITICAL: "Very big news", HIGH: "Big news", MEDIUM: "Notable news", LOW: "Minor news" }[a.severity] || "News";
  const what = a.kind === "options" ? "Traders are betting heavily with options" : a.kind === "confluence" ? "Several kinds of evidence agree" : a.edge?.radar || a.edge?.ignition ? "The stock is moving sharply on heavy trading" : size;
  const way = d === "up" ? "likely to push it up" : d === "down" ? "likely to push it down" : "could move it either way";
  return t ? `<div class="plain">In plain English: ${esc(what)} on ${esc(t)}, ${way}. Not advice; check the details.</div>` : "";
}
function relatedChip(a) {
  const t = (a.tickers || [])[0];
  if (!t || a.kind === "system") return "";
  const n = alerts.filter((x) => x !== a && x.id !== a.id && (x.tickers || []).includes(t) && Math.abs(x.created - a.created) < 86400).length;
  return n ? `<span class="relc" data-tk="${esc(t)}" role="button">+${n} more on ${esc(t)}</span>` : "";
}
let tapeSort = store.get("ft.sort") || "new";
let collapsedOf = {};
const expanded = new Set();
function collapsedChip(a) {
  const n = collapsedOf[a.id], t = (a.tickers || [])[0];
  return n ? `<span class="relc" data-act="expand" data-s="${esc(t)}">+${n} earlier on ${esc(t)} this hour</span>` : "";
}
function bestScore(a) {
  const rank = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 }[a.severity] || 0;
  const hit = a.edge?.base?.h1?.hit ?? a.edge?.base?.close?.hit;
  const age = Math.max(0, Date.now() / 1000 - a.created) / 3600;
  const taste = (desk?.taste || []).find((t) => t.key === (a.edge?.rk || a.edge?.base?.key));
  return rank * 2 + (hit != null ? (hit - 0.5) * 6 : 0) + (isMine(a) ? 2 : 0) + (exposure(a) ? 2 : 0) + (a.edge?.confluence ? 1.5 : 0) + (taste ? taste.score * 3 : 0) - age * 0.6;
}
function alertHTML(a, opts = {}) {
  const an = a.analysis || {}, it = a.item || {};
  const src = a.edge?.radar ? "RADAR" : a.kind === "price" ? "PRICE ACTION" : a.kind === "system" ? "FORETAPE" : (it.source || a.kind).toUpperCase();
  const why = an.summary || (a.kind !== "news" && a.body ? a.body.split("\n")[0] : "");
  const known = seen.has(a.id);
  const state = a._new ? "new in" : known ? "in" : "";
  return `<article class="alert ${a.edge?.radar ? "radar " : ""}${opts.feature ? "feature " : ""}${known || a._new ? "" : "rv "}${state}" data-id="${esc(a.id)}">
    <div class="glass tilt" style="--sev:${SEV[a.severity] || SEV.LOW}"><i class="sevbar"></i>
      ${opts.feature ? `<div class="eyebrow" style="color:var(--gold);margin-bottom:12px">Top of the tape</div>` : ""}
      <div class="meta"><span class="sev">${esc(a.severity)}</span><span class="src">${esc(src)}</span>${a.kind !== "system" && a.edge?.base ? `<span class="edge">edge ${edgeScore(a).score}</span>` : ""}${isMine(a) ? `<span class="minec">★ yours</span>` : ""}<span class="ago" data-t="${a.created}">${ago(a.created)}</span></div>
      <h3>${esc(a.title)}</h3>
      ${why ? `<p class="why">${esc(why)}</p>` : ""}
      ${confRow(a)}${capRow(a)}${whyRow(a)}${playRow(a)}${a.edge?.radar ? "" : tickerChips(a)}${plainLine(a)}${expoRow(a)}${precLine(a)}${preRow(a)}${baseRow(a)}${gradeRow(a)}${footRow(a)}${relatedChip(a)}${collapsedChip(a)}
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
  const pool = tapeFilter === "small" ? alerts.filter(isSmall) : tapeFilter === "mine" ? alerts.filter(isMine) : alerts;
  const f = tapeFilter === "all" ? featured() : null;
  $("featureWrap").innerHTML = f ? alertHTML(f, { feature: true }) : "";
  let rest = pool.filter((a) => a !== f);
  // several alerts on one stock within the hour: the newest card stands for them ("+2 earlier this hour")
  collapsedOf = {};
  if (tapeFilter === "all" && tapeSort !== "best") {
    const newest = {};
    rest = rest.filter((a) => {
      const t = (a.tickers || [])[0];
      if (!t || a.kind === "system" || expanded.has(t)) return true;
      const top = newest[t];
      if (top && top.created - a.created < 3600) { collapsedOf[top.id] = (collapsedOf[top.id] || 0) + 1; return false; }
      newest[t] = a;
      return true;
    });
  }
  if (tapeSort === "best") rest = [...rest].sort((x, y) => bestScore(y) - bestScore(x));
  $("sortBtn").textContent = tapeSort === "best" ? "Best first" : "Newest";
  const shown = rest.slice(0, showAll ? 150 : 30);   // dozens of blurred glass layers are expensive on a phone: older ones load on demand
  $("alerts").innerHTML = rest.length ? shown.map((a) => alertHTML(a)).join("") + (rest.length > shown.length ? `<button class="more" id="moreBtn">Show ${rest.length - shown.length} older</button>` : "")
    : f ? "" : emptyHTML(tapeFilter === "mine"
      ? (mine.size ? `Nothing yet on your ${mine.size} watched stock${mine.size > 1 ? "s" : ""}. Alerts on them land here, and on your lock screen even at MEDIUM.`
        : "Your watchlist is empty. Open any ticker (tap a chip, or search above) and tap ☆ Watch: its alerts collect here.")
      : tapeFilter === "small"
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
  if (n) $("nextText").innerHTML = `<b>${esc(n.title)}</b><small>${esc(prettyDate(n.date))}${n.time ? " · " + esc(localTimes() ? showTime(n.date, n.time) : n.time + " ET") : ""} · <span id="nextIn">${inDays(n.in_days)}</span></small>`;
  nextEvent = n || null; tickNext();
}
// a release today: a live countdown to it ("in 1h 12m"), then "just out" for half an hour
let nextEvent = null;
function etNowMinutes() { const t = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" })); return t.getHours() * 60 + t.getMinutes() + t.getSeconds() / 60; }
function tickNext() {
  const n = nextEvent, el = $("nextIn");
  if (!n || !el || n.in_days !== 0 || !/^\d{1,2}:\d{2}$/.test(n.time || "")) return;
  const [h, m] = n.time.split(":").map(Number), left = h * 60 + m - etNowMinutes();
  el.textContent = left > 0 ? `in ${left >= 60 ? Math.floor(left / 60) + "h " : ""}${Math.ceil(left % 60)}m` : left > -30 ? "just out" : "today";
  $("nextCat").classList.toggle("soon", left > 0 && left <= 30);
}
setInterval(tickNext, 15000);
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
    ["Movers radar", !app.smallcap?.radar ? "off" : `${app.smallcap.scans} scans · ${app.smallcap.hits} flagged`],
    ["Options tape", !app.options?.enabled ? "off" : `${app.options.feed || "?"} · ${app.options.read_today.toLocaleString()}/${app.options.companies.toLocaleString()} chains today · ${app.options.hits} flagged${app.options.fallbacks ? ` · ${app.options.fallbacks} via backup` : ""}${["failing", "degraded"].includes(app.options.status) ? " · " + app.options.status.toUpperCase() : ""}`],
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
  const mineRows = b.mine || [];
  $("bMineWrap").hidden = !mineRows.length;
  $("bMine").innerHTML = mineRows.map((r) => `<div class="li" data-tk="${esc(r.symbol)}" role="button"><span class="sym" style="width:64px">${esc(r.symbol)}</span>
      <div class="grow">${r.news ? `<div style="font-weight:600;line-height:1.35">${esc(r.news.title)}</div><div class="sub">${esc(outlet(r.news.source))}</div>` : `<div class="sub">No overnight headline</div>`}
      ${r.earnings ? `<div class="sub gold">Reports ${r.earnings.in_days === 0 ? "today" : r.earnings.in_days === 1 ? "tomorrow" : "in 2 days"}, ${esc(r.earnings.when || "")}</div>` : ""}
      ${(r.events || []).map((e) => `<div class="sub gold">${esc(e.title)}${e.time ? " · " + esc(e.time) + " ET" : ""}</div>`).join("")}
      ${r.alerts ? `<div class="sub">${r.alerts} alert${r.alerts > 1 ? "s" : ""} since the close</div>` : ""}</div>
      <span class="chg num ${cls(r.chg)}">${pct(r.chg)}</span></div>`).join("");
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
  ].map(([k, v]) => `<div class="kv"><span>${k}</span><b class="num">${esc(v)}</b></div>`).join("") + scoreRows(b.scorecard);
  loadDay(); loadClose(); loadTomorrow(); loadMemory();
  Fx.observe($("main"));
}
// the scorecard: last session's big moves, and whether we had each one before the price
function scoreRows(c) {
  if (!c) return "";
  const n = c.counts || {}, rate = c.ahead_rate != null ? ` (${Math.round(c.ahead_rate * 100)}%)` : "";
  const rows = [["Last session's big moves", c.moves], ["Had it before the price", `${(n.pushed || 0) + (n.app || 0)}${rate}`],
    ["Radar first", n.radar || 0], ["Gaps to fix", (n.scored_low || 0) + (n.app || 0) + (n.unexplained || 0)]]
    .map(([k, v]) => `<div class="kv"><span>${k}</span><b class="num">${esc(v)}</b></div>`).join("");
  return rows + (c.gaps || []).map((g) => `<div class="li"><span class="sym" style="width:64px">${esc(g.symbol)}</span><span class="grow sub">${esc(g.hint)}</span></div>`).join("");
}
function barHTML(v) { const w = Math.min(42, Math.abs(v) * 6); return `<span class="bar2"><i style="${v >= 0 ? `left:42px;width:${w}px;background:var(--up)` : `right:42px;width:${w}px;background:var(--down)`}"></i></span>`; }
function moverRow(m) {
  const v = m.chg_day ?? 0;
  return `<div class="li"><span class="sym" style="width:64px">${esc(m.symbol)}</span><span class="grow num faint">${m.price != null ? m.price.toFixed(2) : ""}</span>${barHTML(v)}<span class="chg num ${cls(v)}">${pct(m.chg_day)}</span></div>`;
}
const KIND = { fed: "Federal Reserve", data: "Economic data", politics: "Politics", trade: "Trade policy", options: "Options expiry", market: "Market hours", earnings: "Earnings", binary: "Binary event" };
// "Sun 4:00 PM ET": when a calendar entry's source was released and when Foretape read it
// times: ET (the market's clock, default) or this phone's own time zone (the Desk's Display setting)
const localTimes = () => store.get("ft_tz") === "local";
const etTime = (t) => localTimes() ? new Date(t * 1000).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short" })
  : new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit" }) + " ET";
// an "HH:MM" ET time on a date, shown in this phone's zone when asked (else as is)
function showTime(dateISO, hhmm) {
  if (!localTimes() || !/^\d{1,2}:\d{2}$/.test(hhmm || "") || !dateISO) return hhmm;
  const noon = new Date(dateISO + "T12:00:00Z");
  const off = (new Date(noon.toLocaleString("en-US", { timeZone: "America/New_York" })) - new Date(noon.toLocaleString("en-US", { timeZone: "UTC" }))) / 60000;
  const [y, mo, d] = dateISO.split("-").map(Number), [h, mi] = hhmm.split(":").map(Number);
  return new Date(Date.UTC(y, mo - 1, d, h, mi) - off * 60000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}
function calRow(e, grouped) {
  const sub = grouped ? [KIND[e.kind] || e.kind, e.note].filter(Boolean).map(esc).join(" · ")
    : `${esc(prettyDate(e.date))} · ${inDays(e.in_days)}${e.note ? " · " + esc(e.note) : ""}`;
  const title = e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener" class="calink">${esc(e.title)}</a>` : esc(e.title);
  const from = e.kind === "binary" && (e.published || e.seen) ? `<div class="sub">${esc([e.source, e.published && "released " + etTime(e.published), e.seen && "seen " + etTime(e.seen)].filter(Boolean).join(" · "))}</div>` : "";
  return `<div class="li${e.kind === "binary" ? " binary" : ""}"><span class="time">${esc(showTime(e.date, e.time) || "—")}</span><div class="grow"><div style="font-weight:600">${e.kind === "binary" ? `<span class="capb bin">Binary</span>` : ""}${title}</div><div class="sub">${sub}</div>${from}</div>
    <span class="imp">${[1, 2, 3].map((i) => `<i class="${i <= (e.impact || 1) ? "on" : ""}"></i>`).join("")}</span></div>`;
}
async function loadEarningsWeek() {
  try {
    const r = await api("/api/earnings-week");
    if (!r.enabled || !r.days.length) return "";
    return `<div class="sec-title rv"><h2>Earnings this week</h2><span>$2B+ companies · tap for the implied move</span></div><div class="panel glass tilt rv">`
      + r.days.map((d) => `<div class="li"><div class="grow"><div style="font-weight:600">${esc(prettyDate(d.date))} <span class="sub">${inDays(d.in_days)}</span></div><div class="tks">${d.rows.slice(0, 18).map((x) => `<span class="tk${mine.has(x.symbol) ? " mine" : ""}" data-tk="${esc(x.symbol)}" role="button" title="${esc(x.name)}">${esc(x.symbol)} <small>${/before/i.test(x.when) ? "☀ pre" : /after/i.test(x.when) ? "☾ post" : ""}${x.implied != null ? ` · ±${x.implied.toFixed(1)}% priced` : ""}${x.usual != null ? ` · usually ±${x.usual.toFixed(1)}%` : ""}</small></span>`).join("")}${d.rows.length > 18 ? `<span class="tk rt">+${d.rows.length - 18}</span>` : ""}</div></div></div>`).join("") + "</div>";
  } catch (_) { return ""; }
}
async function loadCalendar() {
  const [c, ew] = await Promise.all([api("/api/calendar?days=60"), loadEarningsWeek()]);
  const byDay = {};
  c.events.forEach((e) => (byDay[e.date] = byDay[e.date] || []).push(e));
  $("cal").innerHTML = ew + (Object.keys(byDay).length ? Object.entries(byDay).map(([d, evs]) => `<div class="day-head rv"><b>${esc(prettyDate(d))}</b><span>${inDays(evs[0].in_days)}</span></div>
      <div class="panel glass tilt rv">${evs.map((e) => calRow(e, true)).join("")}</div>`).join("") : emptyHTML("Nothing scheduled in the next 60 days."));
  Fx.observe($("cal"));
}
// ---------------------------------------------------------------- the Camillo desk
const SRC = (s) => (s || []).map((x) => x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label)}</a>` : esc(x.label)).join(" · ");
function ideaCard(r) {
  const arrow = { up: "▲", down: "▼" }[r.direction] || "◆";
  const dcls = { up: "pos", down: "neg" }[r.direction] || "faint";
  const move = r.move_pct != null ? `stock ${r.move_pct > 0 ? "+" : ""}${r.move_pct.toFixed(1)}% since the evidence began` : "";
  const ev = (r.evidence || []).map((e) => `<div>• ${esc(e.detail)}${e.url ? ` <a class="src" href="${esc(e.url)}" target="_blank" rel="noopener">source</a>` : ""}</div>`).join("");
  const checks = (r.checklist || []).map((c) => `<div class="chk ${c.pass ? "ok" : "no"}"><i>${c.pass ? "✓" : "✗"}</i><div>${esc(c.rule)}<span class="sub">${esc(c.detail)}</span></div></div>`).join("");
  return `<article class="idea glass tilt rv">
    <div class="sub"><span style="color:${SEV[r.severity]}">${esc(r.severity)}</span> · ${r.passed}/${(r.checklist || []).length} tests · ${ago(r.created)} ago</div>
    <h3><span class="${dcls}">${arrow}</span> ${esc((r.brands || []).join(", "))} <span class="faint">${esc(r.ticker)}</span></h3>
    <div class="sub">${esc(move)}</div>
    <div class="ev">${ev}</div>
    ${checks}
    ${r.conviction ? `<div class="sub" style="margin-top:8px"><b style="font-weight:600">${r.conviction.level === "high" ? "High conviction" : "Low conviction"}.</b> ${esc(r.conviction.rule)}</div>` : ""}
    ${(r.other_side || []).length ? `<div class="sub" style="margin-top:8px"><b style="font-weight:600">The other side</b>${r.other_side.map((o) => `<div>• ${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.title)}</a>` : esc(o.title)} <span class="faint">${esc(o.source || "")}</span></div>`).join("")}</div>` : ""}
    <div class="plan">${esc(r.exit || "")}</div>
  </article>`;
}
const LEVEL = { accelerating: "Accelerating", elevated: "Elevated", quiet: "Quiet", learning: "Learning" };
function narrRows(rows) {
  if (!(rows || []).length) return `<div class="li"><span class="muted">No narratives tracked.</span></div>`;
  return rows.map((r) => {
    const n = r.news || {}, so = r.social;
    const hot = r.level === "accelerating", cls2 = r.side === "fear" ? "neg" : "pos";
    const bits = [!n.count ? "no matching headlines in 3 days" : n.ratio != null ? `news ${n.ratio}× normal · ${n.count} headlines, ${r.sources_3d} sources in 3 days` : `news: ${n.count} headlines in 3 days (baseline still building)`];
    if (so && so.ratio != null) bits.push(`Bluesky ${so.ratio}× · ${so.count} posts`);
    const ex = (r.examples || [])[0];
    return `<div class="li"><div class="grow"><div style="font-weight:600"><span class="${cls2}">${r.side === "fear" ? "▼" : "▲"}</span> ${esc(r.name)}${(r.hits || []).length ? ` <span class="faint">${esc(r.hits.join(" "))}</span>` : ""}</div>
      <div class="sub">${esc(bits.join(" · "))}</div>${ex ? `<div class="sub">“${ex.url ? `<a href="${esc(ex.url)}" target="_blank" rel="noopener">${esc(ex.title)}</a>` : esc(ex.title)}”</div>` : ""}</div>
      <span class="num ${hot ? cls2 : "faint"}" style="font-size:13px">${esc(LEVEL[r.level] || r.level)}</span></div>`;
  }).join("");
}
function viewsHTML(v) {
  if (!v || !(v.items || []).length) return "";
  return v.items.map((x) => `<div class="step"><h4>${esc(x.thesis)}${x.ticker ? ` <span class="faint">${esc(x.ticker)}</span>` : ""}</h4>${x.quote ? `<div class="quote">“${esc(x.quote)}”</div>` : ""}<p>${esc(x.detail || "")}</p>${x.since ? `<p class="sub">Since the interview (Aug 7 → Oct 9 close): ${esc(x.since)}</p>` : ""}</div>`).join("")
    + (v.source ? `<div class="src" style="margin-top:6px">${SRC([v.source])}</div>` : "");
}
function heatRows(c) {
  const rows = [
    ...(c.attention || []).map((r) => ({ brand: r.brand, ticker: r.ticker, what: `Wikipedia ${r.ratio.toFixed(2)}× normal`, sub: `${Number(r.avg7).toLocaleString()}/day vs ${Number(r.baseline).toLocaleString()}` })),
    ...(c.chatter || []).map((r) => ({ brand: r.brand, ticker: r.ticker, what: r.ratio != null ? `Bluesky chatter ${r.ratio.toFixed(1)}× normal` : "Bluesky chatter (new)", sub: `${r.mentions} mentions in 3 days${r.example ? " · “" + r.example.slice(0, 90) + "”" : ""}` })),
  ];
  return rows.length ? rows.map((r) => `<div class="li"><div class="grow"><div style="font-weight:600">${esc(r.brand)} <span class="faint">${esc(r.ticker)}</span></div><div class="sub">${esc(r.sub)}</div></div><span class="num" style="font-size:13px">${esc(r.what)}</span></div>`).join("")
    : `<div class="li"><span class="muted">Nothing unusual yet. Wikipedia is read daily, the App Store every 6 hours, Bluesky every hour.</span></div>`;
}
function methodHTML(p) {
  const princ = (p.principles || []).map((x) => `<div class="step"><h4>${esc(x.title)}</h4><p>${esc(x.text)}</p>${x.quote ? `<div class="quote">“${esc(x.quote)}”</div>` : ""}${x.sources ? `<div class="src" style="margin-top:6px">${SRC(x.sources)}</div>` : ""}</div>`).join("");
  const steps = (p.process || []).map((x, i) => `<div class="step"><h4>${i + 1}. ${esc(x.step)}</h4><p>${esc(x.text)}</p>${x.how ? `<p class="sub">On this desk: ${esc(x.how)}</p>` : ""}</div>`).join("");
  const avoid = (p.avoid || []).map((x) => `<div class="step"><p>✗ ${esc(x.text || x)}</p>${x.sources ? `<div class="src">${SRC(x.sources)}</div>` : ""}</div>`).join("");
  const limits = (p.limits || []).map((x) => `<div class="step"><p>${esc(x)}</p></div>`).join("");
  const record = (p.record || []).map((x) => `<div class="step"><p>${esc(x.claim)}</p><div class="src">${SRC(x.sources)}</div></div>`).join("");
  const unver = (p.unverified || []).map((x) => `<div class="step"><p>? ${esc(x)}</p></div>`).join("");
  return [
    princ && `<div class="panel glass tilt rv padded method">${princ}</div>`,
    steps && `<div class="sec-title rv"><h2 style="font-size:20px">The process</h2></div><div class="panel glass tilt rv padded method">${steps}</div>`,
    avoid && `<div class="sec-title rv"><h2 style="font-size:20px">What he avoids</h2></div><div class="panel glass tilt rv padded method">${avoid}</div>`,
    record && `<div class="sec-title rv"><h2 style="font-size:20px">His record, as reported</h2></div><div class="panel glass tilt rv padded method">${record}</div>`,
    limits && `<div class="sec-title rv"><h2 style="font-size:20px">What this desk can't do</h2></div><div class="panel glass tilt rv padded method">${limits}</div>`,
    unver && `<div class="sec-title rv"><h2 style="font-size:20px">Said about him, not sourced</h2></div><div class="panel glass tilt rv padded method">${unver}</div>`,
  ].filter(Boolean).join("");
}
function caseCard(c) {
  const rows = [["What he noticed", c.noticed], ["The trade", c.trade], ["What happened", c.outcome], ["Price check", c.price_check]].filter(([, v]) => v);
  return `<article class="idea glass tilt rv"><div class="sub">${esc(c.period || "")}</div><h3>${esc(c.name)}${c.ticker ? ` <span class="faint">${esc(c.ticker)}</span>` : ""}</h3>
    ${rows.map(([k, v]) => `<div class="chk"><div><b style="font-weight:600">${esc(k)}</b><span class="sub">${esc(v)}</span></div></div>`).join("")}
    <div class="src" style="margin-top:8px">${SRC(c.sources)}</div></article>`;
}
async function loadCamillo() {
  const c = await api("/api/camillo");
  const p = c.playbook || {};
  $("cSub").textContent = p.intro || "";
  $("cIdeas").innerHTML = (c.ideas || []).length ? c.ideas.map(ideaCard).join("")
    : emptyHTML("No live ideas. One appears when a brand's evidence passes the trend lane's tests.");
  $("cHeat").innerHTML = heatRows(c);
  $("cNarr").innerHTML = narrRows(c.narratives);
  $("cViews").innerHTML = viewsHTML(p.views) || `<div class="step"><p class="muted">No dated views loaded.</p></div>`;
  $("cMethod").innerHTML = methodHTML(p);
  $("cCases").innerHTML = (p.cases || []).map(caseCard).join("") || emptyHTML("No case studies loaded.");
  const by = (c.scorecard || {}).by || {};
  $("cScore").innerHTML = Object.keys(by).length ? Object.entries(by).map(([k, s]) => `<div class="kv"><span>${esc(k)} · ${s.n} idea${s.n > 1 ? "s" : ""}</span><b class="num ${s.avg_excess >= 0 ? "pos" : "neg"}">${Math.round(s.hit_rate * 100)}% · ${s.avg_excess > 0 ? "+" : ""}${s.avg_excess}% vs SPY</b></div>`).join("")
    : `<div class="kv"><span>${(c.scorecard || {}).signals || 0} ideas recorded</span><b>marked 30, 60 and 90 days after</b></div>`;
  const st = c.status || {}, ch = st.chatter || {}, ea = st.earnings || {};
  $("cFeeds").innerHTML = [
    ["Brands mapped to companies", `${st.brands ?? 0} → ${st.companies ?? 0}`],
    ["Wikipedia (daily)", st.last_wiki_day ? `views through ${st.last_wiki_day}` : "not read yet"],
    ["App Store chart (6-hourly)", st.last_app_snapshot ? `${st.apps_tracked} apps tracked, last ${ago(st.last_app_snapshot)} ago` : "not read yet"],
    ["Bluesky chatter (hourly sample)", ch.enabled ? (ch.last_sample ? `${ch.days} days, ${Number(ch.posts_today || 0).toLocaleString()} posts today` : "first sample pending") : "off"],
    ["Next report dates", ea.enabled ? (ea.last_day ? `${ea.companies} companies, read ${ea.last_day}` : "first read pending") : "off"],
    ["Narratives (every headline read)", (st.narratives || {}).enabled ? `${st.narratives.tracked} tracked${st.narratives.last_scan ? `, measured ${ago(st.narratives.last_scan)} ago` : ""}` : "off"],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
  Fx.observe(document.querySelector('.view[data-view="camillo"]'));
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
  const ig = r.ignition;
  if (ig) bits.unshift(`<b class="${ig.direction === "down" ? "neg" : "pos"}">⚡ ${ig.kind === "buildup" ? "building" : `${pct(ig.move_pct)} in ${Math.round(ig.window_s / 60)}m`}${ig.burst ? ` on ${Math.round(ig.burst)}× vol` : ""}</b>`);
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
  loadRadar(); loadSmart(); loadSectors(); loadGaps(); loadHalts(); loadBreadth(); loadVolume(); loadBuzz(); loadInPlay();
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
// an options alert's contract after the alert (market/follow.py): its state now and its exit lines
const FSTATE = { RUNNING: ["pos", "Still running"], HOLDING: ["", "Holding"], FADING: ["neg", "Fading"], STOPPED: ["neg", "Stop hit"] };
function followBlock(f) {
  if (!f) return "";
  const [c, label] = FSTATE[f.state] || ["", f.state];
  const m = (v) => (v == null ? "—" : "$" + Number(v).toFixed(2));
  const t = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET" : "");
  const sp = f.stock != null && f.stock_prev ? (f.stock / f.stock_prev - 1) * 100 : null;
  return `<h4>After the alert</h4><div class="panel glass">
      <div class="kv"><span>${esc(f.label)}</span><b class="${f.ended ? "" : c}">${f.ended ? "Done · " + esc(f.ended) : esc(label)}</b></div>
      <div class="kv"><span>Entry (the ask at the alert, ${esc(t(f.alert_ts))})</span><b class="num">${m(f.entry)}</b></div>
      <div class="kv"><span>Bid now</span><b class="num ${cls(f.since_alert_pct)}">${m(f.bid)}${f.since_alert_pct != null ? " · " + pct(f.since_alert_pct) : ""}</b></div>
      <div class="kv"><span>Best bid since</span><b class="num">${m(f.high)} ${esc(t(f.high_ts))}</b></div>
      <div class="kv"><span>Fading under · stop under</span><b class="num">${m(f.fade)} · ${m(f.stop)}</b></div>
      ${f.state !== "STOPPED" ? `<div class="kv"><span>Next "still running"</span><b class="num">${m(f.next_run)}</b></div>` : ""}
      ${f.stock != null ? `<div class="kv"><span>${esc(f.underlying)}</span><b class="num">$${Number(f.stock).toFixed(2)}${sp != null ? ` <span class="${cls(sp)}">${pct(sp)}</span>` : ""}</b></div>` : ""}
      <div class="sub" style="padding:10px 0 4px">Judged on the bid, on live quotes; a drop must show on two quotes. Followed until the close. The system never trades.</div></div>`;
}
async function openSheet(id) {
  $("toast").classList.remove("show");
  let a;
  try { a = await api("/api/alert/" + encodeURIComponent(id)); } catch (_) { a = alerts.find((x) => x.id === id); }
  if (!a) return;
  sheetAlert = a;
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
      ${e.ignition ? `<div class="kv"><span>${e.ignition.kind === "buildup" ? "From yesterday's close" : "Launch"}</span><b class="num">$${(+e.ignition.launch).toFixed(2)} → $${(+e.ignition.price).toFixed(2)} <span class="${cls(e.ignition.move_pct)}">${pct(e.ignition.move_pct)}</span></b></div>
        <div class="kv"><span>The move fails past</span><b class="num">$${(+e.ignition.stop).toFixed(2)} · ${(+e.ignition.risk_pct).toFixed(1)}% away</b></div>
        <div class="kv"><span>Volume</span><b class="num">${e.ignition.burst ? `${e.ignition.burst}× normal in the push · ` : ""}${e.ignition.rvol != null ? `${e.ignition.rvol}× normal today` : "—"}</b></div>
        <div class="kv"><span>Backed by</span><b>${(e.ignition.confirmed_by || []).length ? e.ignition.confirmed_by.map(esc).join("<br>") : "nothing yet: its option chain is being read"}</b></div>` : ""}
      ${sc.blocked ? `<div class="sub" style="padding:10px 0 4px">Not boosted: ${esc(sc.blocked)}</div>` : ""}
      ${sc.move_text ? `<div class="sub" style="padding:10px 0 4px">Typical moves are rules of thumb from 2024–26 small-cap history, not a forecast.</div>` : ""}</div>` : "";
  const blocks = [
    `<div class="meta" style="--sev:${SEV[a.severity]}"><span class="sev">${esc(a.severity)}</span><span>${esc(e.radar ? "RADAR" : (it.source || a.kind).toUpperCase())}</span><span class="ago">${ago(a.created)} ago</span></div>`,
    `<h2>${esc(a.title)}</h2>`,
    an.summary ? `<p class="muted" style="font-size:15.5px">${esc(an.summary)}</p>` : a.body && !e.confluence ? `<p class="muted" style="white-space:pre-line">${esc(a.body)}</p>` : "",
    capBlock,
    rel.length ? `<h4>The headline behind it</h4><div class="panel glass">${rel.map((x) => `<div class="li"><div class="grow">
        <a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a>
        <div class="sub">${esc(x.source)} · ${esc(x.age)} before the move</div></div></div>`).join("")}</div>` : "",
    chart ? `<h4>The tape since the alert</h4>${chart}` : "",
    followBlock(a.follow),
    p.direction ? `<h4>The play</h4><div class="panel glass padded">
        <div class="play" style="margin:0">${p.direction in dirLabel ? `<span class="dir ${p.direction}">${dirLabel[p.direction]}</span>` : ""}${meter(p.meter)}<span class="conv">${esc(p.conviction)} conviction · score ${Math.round(p.score)}</span></div>
        ${p.direction_basis === "precedents" ? `<div class="sub" style="margin-top:8px">Direction inferred from how similar events traded.</div>` : ""}
        ${p.direct?.length ? `<div class="sub" style="margin-top:14px">Direct</div><div class="tks" style="margin-top:7px">${p.direct.map((t) => `<span class="tk">${esc(t)}${since[t] != null ? ` <small class="${cls(since[t])}">${pct(since[t])} since alert</small>` : ""}</span>`).join("")}</div>` : ""}
        ${p.read_through?.length ? `<div class="sub" style="margin-top:14px">Read-through</div><div class="tks" style="margin-top:7px">${p.read_through.map((t) => `<span class="tk rt">${esc(t)}</span>`).join("")}</div>` : ""}
        ${p.themes?.length ? `<div class="sub" style="margin-top:14px">${esc(p.themes.join(" · "))}</div>` : ""}</div>` : "",
    e.confluence ? `<h4>What lines up ${tip("confluence")}</h4><div class="panel glass padded">${confRow(a)}${e.confluence.signals.map((x) => `<div class="sig"${x.id ? ` data-id="${esc(x.id)}" role="button"` : ""}><span class="${x.side === "up" ? "pos" : "neg"}">${x.side === "up" ? "▲" : "▼"}</span><div class="grow"><div>${esc(x.label)}</div><div class="sub">${esc(KIND_LABEL[x.kind] || x.kind)} · weighs ${x.weight}${x.ts ? ` · ${ago(x.ts)} ago` : ""}</div></div></div>`).join("")}
      <div class="sub" style="margin-top:10px">Fires when 3+ independent kinds of evidence line up on one side (weight 4+, at least one from the last 2 hours); pushed at weight 7+. Each stock and side once a day.</div></div>` : "",
    a.kind !== "system" ? (() => { const es = edgeScore(a); return `<h4>Edge score</h4><div class="panel glass"><div class="kv"><span>Composite of the stats on this page</span><b class="num ${es.score >= 60 ? "pos" : es.score < 45 ? "neg" : ""}">${es.score} / 100</b></div>${es.parts.map(([n, v, w, why]) => `<div class="kv"><span>${n} <span class="sub">× ${Math.round(w * 100)}% · ${esc(why)}</span></span><b class="num">${Math.round(v * 100)}</b></div>`).join("")}<div class="sub" style="padding:4px 0 12px">A weighted summary of measured facts (how alerts like it did, whether the stock had already moved, how much evidence agrees, severity), not a forecast.</div></div>`; })() : "",
    priceInBlock(a),
    e.base ? `<h4>How alerts like this did ${tip("base")}</h4><div class="panel glass">
      ${e.base.h1 ? `<div class="kv"><span>An hour later</span><b class="num"><span class="${e.base.h1.hit >= 0.5 ? "pos" : "neg"}">${Math.round(e.base.h1.hit * 100)}%</span> went its way · median ${pct(e.base.h1.median)} · ${e.base.h1.n} graded</b></div>` : ""}
      ${e.base.close ? `<div class="kv"><span>At the close</span><b class="num"><span class="${e.base.close.hit >= 0.5 ? "pos" : "neg"}">${Math.round(e.base.close.hit * 100)}%</span> went its way · median ${pct(e.base.close.median)} · ${e.base.close.n} graded</b></div>` : ""}
      ${pathChart(e.base.path)}
      <div class="sub" style="padding:4px 0 12px">${esc(e.base.label)} alerts over the last 60 days, each graded on its own stock from its own reference price. A base rate, not a forecast.</div></div>` : "",
    e.precedents?.length ? `<h4>Precedents</h4><div class="panel glass">${e.precedents.map((x) => `<div class="li"><div class="grow">
        <div class="sub">${esc(prettyDate(x.date))}, ${esc(x.date.slice(0, 4))} · ${esc(x.category)}${x.first_source ? " · first: " + esc(x.first_source) : ""}</div>
        <div style="font-weight:600;margin:3px 0">${esc(x.headline)}</div><div class="${/(^|\s)[-−]\d/.test(x.move) ? "neg" : "pos"}" style="font-size:13.5px">${esc(x.move)}</div></div></div>`).join("")}</div>` : "",
    a.kind === "news" && `<h4>The edge</h4><div class="panel glass">
      ${lag != null ? `<div class="kv"><span>Caught after it was posted</span><b class="num">${dur(lag)}</b></div>` : ""}
      <div class="kv"><span>First seen on</span><b>${esc(it.source || "—")}</b></div>
      <div class="kv"><span>Mainstream caught up</span><b class="num">${e.lead ? `${esc(outlet(e.lead.source))}, ${dur(e.lead.lead_s)} later` : "not yet"}</b></div></div>`,
    (a.story_items || []).length >= 2 ? `<h4>The story</h4><div class="tl">${a.story_items.map((x, i) => `<div class="li"><div class="grow"><div class="sub">${i === 0 ? "first" : "+" + dur(x.detected - a.story_items[0].detected)} · ${esc(outlet(x.source))}${x.score != null ? ` · score ${Math.round(x.score)}` : ""}</div>${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a>` : `<b>${esc(x.title)}</b>`}</div></div>`).join("")}</div>`
      : tl ? `<h4>Who carried it</h4><div class="tl">${tl}</div>` : "",
    `<div id="similar"></div>`,
    an.reasons?.length ? `<h4>Why Foretape flagged it</h4><details><summary>Score ${Math.round(an.score)} / 100 — show the breakdown</summary><ul class="reasons" style="margin-top:10px">${an.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>` : "",
    a.url ? `<div style="margin-top:26px"><a class="btn primary" href="${esc(a.url)}" target="_blank" rel="noopener">${rd && !rel.length ? "Open the quote" : "Open the source"} ↗</a></div>` : "",
    alertActions(a),
    voteRow(a),
  ].filter(Boolean);
  showSheet(blocks.join(""));
  loadSimilar(a);
  if (a.kind !== "system") api("/api/desk", { method: "POST", body: JSON.stringify({ action: "opened", id: a.id }) }).catch(() => {});
}
// earlier alerts of the same kind on the same stock, and how they did by the close
async function loadSimilar(a) {
  if (!(a.tickers || []).length || a.kind === "system") return;
  let r;
  try { r = await api("/api/similar/" + encodeURIComponent(a.id)); } catch (_) { return; }
  const el = $("similar");
  if (!el || !r.rows.length) return;
  el.innerHTML = `<h4>Earlier alerts like this on ${esc(r.symbol)}</h4><div class="panel glass">
    ${r.stats ? `<div class="kv"><span>By the close</span><b class="num"><span class="${r.stats.hit >= 0.5 ? "pos" : "neg"}">${Math.round(r.stats.hit * 100)}%</span> went their way · median ${pct(r.stats.median)} · ${r.stats.n} graded</b></div>` : ""}
    ${r.rows.slice(0, 6).map((x) => `<div class="li" data-id="${esc(x.id)}" role="button"><div class="grow"><div class="sub">${esc(prettyDate(new Date(x.created * 1000).toISOString().slice(0, 10)))}</div><div style="font-weight:600">${esc(x.title)}</div>${x.grade ? gradeRow({ edge: { outcome: { [r.symbol]: x.grade } } }) : ""}</div></div>`).join("")}</div>`;
}
// was it useful? one vote per phone; the counts are every phone's
// priced in? how far the stock had moved before the alert, and how alerts after such a move did
let recordCache = null;
function priceInBlock(a) {
  const pre = a.edge?.pre;
  if (!pre) return "";
  const [sym, x] = Object.entries(pre)[0];
  const d = a.edge?.play?.direction || a.analysis?.direction || a.edge?.confluence?.side;
  const sign = d === "up" ? 1 : d === "down" ? -1 : 0, y = sign * x;
  const bucket = !sign ? null : Math.abs(y) < 1 ? "fresh" : y > 0 ? "already moved its way" : "moved against it first";
  const row = bucket && (recordCache?.priced_in || []).find((r) => r.bucket === bucket);
  if (!recordCache) api("/api/record?days=60").then((r) => { recordCache = r; }).catch(() => {});
  const st = row?.h1 || row?.close;
  return `<h4>Priced in?</h4><div class="panel glass"><div class="kv"><span>${esc(sym)} in the 15 minutes before the alert</span><b class="num ${cls(x)}">${pct(x)}</b></div>
    ${st && st.n >= 8 ? `<div class="kv"><span>Alerts that came ${esc(bucket === "fresh" ? "before much of a move" : bucket)} <span class="sub">${st.n} graded</span></span><b class="num ${st.hit >= 0.5 ? "pos" : "neg"}">${Math.round(st.hit * 100)}% went their way</b></div>` : ""}</div>`;
}
// the typical path after alerts like this: median move in the alert's direction at each horizon
function pathChart(path) {
  if (!path || path.length < 2) return "";
  const pts = [{ at: "alert", median: 0 }, ...path], W = 520, H = 90, pad = 26;
  const vs = pts.map((p) => p.median), lo = Math.min(0, ...vs), hi = Math.max(0, ...vs), span = hi - lo || 1;
  const X = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad), Y = (v) => 14 + (1 - (v - lo) / span) * (H - 36);
  const d = pts.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.median).toFixed(1)).join(" ");
  return `<div class="sub" style="padding-top:12px">Typical path after alerts like this (median, in the alert's direction)</div>
    <svg viewBox="0 0 ${W} ${H}" class="pathc"><line x1="${pad}" x2="${W - pad}" y1="${Y(0)}" y2="${Y(0)}" stroke="rgba(255,255,255,.18)" stroke-dasharray="3 5"/>
    <path d="${d}" fill="none" stroke="var(--gold)" stroke-width="2.2"/>${pts.map((p, i) => `<circle cx="${X(i)}" cy="${Y(p.median)}" r="3.5" fill="var(--gold)"/>
    <text x="${X(i)}" y="${H - 4}" text-anchor="middle" fill="rgba(255,255,255,.55)" font-size="10" font-family="Inter">${esc(p.at)}</text>
    ${i ? `<text x="${X(i)}" y="${Y(p.median) - 7}" text-anchor="middle" fill="${p.median >= 0 ? "#8CEBC4" : "#FF9E86"}" font-size="10.5" font-family="Inter">${p.median > 0 ? "+" : ""}${p.median.toFixed(1)}%</text>` : ""}`).join("")}</svg>`;
}
function voteRow(a) {
  if (a.kind === "system") return "";
  const mineV = (desk?.votes || {})[a.id] || 0, fb = a.edge?.feedback || {};
  return `<div class="vote"><span class="sub">Was this alert useful?</span>
    <button class="btn ghost small${mineV === 1 ? " sel" : ""}" data-act="vote" data-s="${esc(a.id)}" data-v="${mineV === 1 ? 0 : 1}">👍 ${fb.up || ""}</button>
    <button class="btn ghost small${mineV === -1 ? " sel" : ""}" data-act="vote" data-s="${esc(a.id)}" data-v="${mineV === -1 ? 0 : -1}">👎 ${fb.down || ""}</button></div>`;
}
// the stocks an alert is about, each a door to its page; share it; paper-track it from here
function alertActions(a) {
  const p = a.edge?.play || {}, syms = [...new Set([...(p.direct || []), ...(a.tickers || [])])].filter(Boolean).slice(0, 3);
  const dirs = p.direction === "up" ? ["long"] : p.direction === "down" ? ["short"] : ["long", "short"];
  const track = syms[0] && a.kind !== "system" ? dirs.map((d) => `<button class="btn ghost small" data-act="track" data-s="${esc(syms[0])}" data-dir="${d}" data-aid="${esc(a.id)}">${d === "long" ? "▲" : "▼"} Track ${esc(syms[0])} ${d}</button>`).join("") : "";
  return `<h4>Act on it</h4><div class="acts">${syms.map((s) => `<button class="btn ghost small" data-tk="${esc(s)}">${esc(s)} page ›</button>`).join("")}${track}
    <button class="btn ghost small" data-act="share-a" data-s="${esc(a.id)}" data-title="${esc(a.title)}">Share ↗</button>
    ${(desk?.saved || []).includes(a.id) ? `<button class="btn ghost small sel" data-act="unsave" data-s="${esc(a.id)}">★ Saved</button>` : `<button class="btn ghost small" data-act="save" data-s="${esc(a.id)}">☆ Save</button>`}
    <button class="btn ghost small" data-act="copy-a">⧉ Copy as text</button>
    <button class="btn ghost small" data-act="why-push" data-s="${esc(a.id)}">📲 Did this reach my phone?</button></div>
    ${a.kind === "system" ? "" : remindRow(a)}
    <div id="whyPush"></div>
    ${track ? `<div class="sub" style="margin-top:8px">Tracking takes the live price as your entry and keeps the P&amp;L on your desk. Paper only: the system never trades.</div>` : ""}`;
}
function showSheet(html) {
  delete $("sheet").dataset.sym;
  $("sheetBody").innerHTML = html;
  [...$("sheetBody").children].forEach((c, i) => c.style.setProperty("--k", i));
  document.body.style.overflow = "hidden"; sheetOpen = true; Fx.dirty = true;
  $("scrim").classList.add("on"); $("sheet").classList.add("on"); $("sheet").scrollTop = 0;
}
function closeSheet() {
  sheetOpen = false; Fx.dirty = true; delete $("sheet").dataset.sym;
  $("scrim").classList.remove("on"); $("sheet").classList.remove("on"); document.body.style.overflow = "";
  if (/#[at]=/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
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
// keyboard and screen readers: every tappable row is focusable and opens with Enter or Space
new MutationObserver(() => document.querySelectorAll('[role="button"]:not([tabindex])').forEach((el) => el.setAttribute("tabindex", "0"))).observe(document.body, { childList: true, subtree: true });
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.('[role="button"]')) { e.preventDefault(); e.target.click(); }
});
// keyboard (desktop): Esc closes, / searches, 1-6 switch tabs, w watches the open stock, ? lists them
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); Palette.toggle(); return; }
  if (e.key === "Escape") { if (Palette.open) { Palette.hide(); return; } closeSheet(); return; }
  if (e.target.closest?.("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); closeSheet(); go("tape"); setTimeout(() => $("q").focus(), 50); }
  else if (/^[1-6]$/.test(e.key)) { closeSheet(); go(TABS[+e.key - 1]); }
  else if (e.key === "w" && sheetOpen) document.querySelector("#sheetBody [data-act=watch]")?.click();
  else if ((e.key === "j" || e.key === "k") && !sheetOpen && tab === "tape") {
    const cards = [...document.querySelectorAll(".stage .alert")];
    if (!cards.length) return;
    let i = cards.findIndex((c) => c.classList.contains("kfocus"));
    cards.forEach((c) => c.classList.remove("kfocus"));
    i = Math.max(0, Math.min(cards.length - 1, i + (e.key === "j" ? 1 : -1)));
    cards[i].classList.add("kfocus"); cards[i].scrollIntoView({ block: "center", behavior: "smooth" });
  }
  else if (e.key === "o" && !sheetOpen) { const c = document.querySelector(".stage .alert.kfocus"); if (c) openSheet(c.dataset.id); }
  else if (e.key === "?") toast("Keys: ⌘K / Ctrl-K jump anywhere · / search · 1-6 tabs · j/k move through the tape · o open · w watch the open stock · Esc close", null, 7000);
});
document.addEventListener("click", (e) => {
  const seg = e.target.closest("#tapeSeg button");
  if (seg) {
    tapeFilter = seg.dataset.f; showAll = false;
    try { localStorage.setItem("ft.tape", tapeFilter); } catch (_) { /* private mode */ }
    renderTape(); return;
  }
  // the nearest target wins: a ticker chip inside a tappable card opens the ticker, not the card
  const el = e.target.closest("[data-act],[data-tk],[data-id]");
  if (!el) return;
  if (el.dataset.act) { onAct(el); return; }
  if (e.target.closest("a")) return;  // links open their page
  if (el.dataset.tk) openTicker(el.dataset.tk);
  else openSheet(el.dataset.id);
});

// ------------------------------------------------------------------ your desk (this phone's, kept on the server)
async function loadDesk() {
  try { desk = await api("/api/desk"); } catch (_) { return; }
  deskChanged();
}
function deskChanged() {
  setTimeout(prefetchWatch, 1500);
  // holdings drive the exposure line on cards: refresh their values, then the tape
  if (Object.keys(desk?.holdings || {}).length) loadPortf().then(() => { if (tab === "tape") renderTape(); });
  else portf = null;
  mine = new Set([...(desk?.watchlist || []).map((w) => w.symbol), ...Object.keys(desk?.holdings || {})]);
  renderStrip();
  if (tab === "desk") renderDesk();
  if (tapeFilter === "mine" && tab === "tape") renderTape();
}
async function deskEdit(action, body = {}) {
  try {
    desk = await api("/api/desk", { method: "POST", body: JSON.stringify({ action, ...body }) });
    deskChanged();
    return desk;
  } catch (e) { toast(String(e.message || e).slice(0, 160)); return null; }
}
// the watchlist strip on the tape: your stocks, live, one tap from their page
function renderStrip() {
  const wl = desk?.watchlist || [];
  $("wl").hidden = !desk;
  $("wl").innerHTML = (desk?.focus ? `<button class="wlc focus" data-act="focus" data-m="0" title="Focus mode: only CRITICAL alerts push. Tap to end it.">🎯 Focus until ${new Date(desk.focus * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · end</button>` : "") + wl.map((w) => `<button class="wlc" data-tk="${esc(w.symbol)}"><b>${esc(w.symbol)}</b><small class="num ${cls(w.chg_day)}">${w.chg_day != null ? pct(w.chg_day) : "—"}</small></button>`).join("")
    + `<button class="wlc add" data-act="wl-add">${wl.length ? "＋" : "☆ Build your watchlist"}</button>`;
}
const fmtPx = (v) => (v == null ? "—" : "$" + (v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v >= 1 ? v.toFixed(2) : v.toFixed(4)));
const money = (v) => v == null ? "—" : v >= 1e9 ? "$" + (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? "$" + (v / 1e6).toFixed(1) + "M" : "$" + Math.round(v / 1e3) + "K";
const CATS = [["news", "Headlines"], ["radar", "Radar & ignition"], ["options", "Options flow"], ["insider", "Insider buys"], ["macro", "Macro & indexes"], ["confluence", "Confluence"]];
const FLOORS = [["", "All the server sends"], ["HIGH", "HIGH and up"], ["CRITICAL", "CRITICAL only"]];
const KIND_LABEL = { news: "Headline", radar: "Radar", options: "Options", insider: "Insider buy", macro: "Macro", tape: "Tape", system: "System", ignition: "Ignition", confluence: "Confluence" };

// the watchlist in its groups (ungrouped first), each group in the order its stocks were added
function sortWatch(wl) {
  const how = store.get("ft.wsort") || "added";
  if (how === "move") return [...wl].sort((x, y) => (y.chg_day ?? -999) - (x.chg_day ?? -999));
  if (how === "name") return [...wl].sort((x, y) => x.symbol.localeCompare(y.symbol));
  return wl;
}
function sparkSVG(pts) {
  if (!pts || pts.length < 3) return "";
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1, W = 54, H = 18;
  const d = pts.map((p, i) => (i ? "L" : "M") + (i / (pts.length - 1) * W).toFixed(1) + " " + (H - 2 - (p - lo) / span * (H - 4)).toFixed(1)).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path d="${d}" fill="none" stroke="${pts[pts.length - 1] >= pts[0] ? "var(--up)" : "var(--down)"}" stroke-width="1.6"/></svg>`;
}
function groupedWatch(wl) {
  const groups = desk.groups || {}, out = new Map([["", []]]);
  wl.forEach((w) => { const g = groups[w.symbol] || ""; if (!out.has(g)) out.set(g, []); out.get(g).push(w); });
  return [...out.entries()].filter(([, rows]) => rows.length);
}
function renderDesk() {
  if (!desk) return;
  const wl = desk.watchlist;
  $("dWatch").innerHTML = `<div class="addrow"><input id="wlIn" class="key" placeholder="Add tickers: NVDA, AMD, PLTR" autocapitalize="characters" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="wl-in">Add</button></div>`
    + (wl.length ? `<div class="row" style="padding:0 0 6px">${[["added", "As added"], ["move", "By move"], ["name", "A-Z"]].map(([k, l]) => `<button class="btn ghost small${(store.get("ft.wsort") || "added") === k ? " sel" : ""}" data-act="wsort" data-s="${k}">${l}</button>`).join("")}</div>` : "")
    + (wl.length ? groupedWatch(sortWatch(wl)).map(([g, rows]) => (g ? `<div class="sub sh">${esc(g)}</div>` : "") + rows.map((w) => `<div class="li"><span class="sym" style="width:64px" data-tk="${esc(w.symbol)}" role="button">${esc(w.symbol)}${(desk.notes || {})[w.symbol] ? ` <small title="note">📝</small>` : ""}</span>
        <span class="grow num">${fmtPx(w.price)}</span>${sparkSVG(w.spark) || (w.chg_day != null ? barHTML(w.chg_day) : "")}<span class="chg num ${cls(w.chg_day)}">${pct(w.chg_day)}</span>
        <button class="x" data-act="wl-group" data-s="${esc(w.symbol)}" aria-label="Group ${esc(w.symbol)}">⋯</button><button class="x" data-act="wl-rm" data-s="${esc(w.symbol)}" aria-label="Remove ${esc(w.symbol)}">✕</button></div>`).join("")).join("")
      : `<div class="li"><span class="muted">Empty. Add tickers above, or tap ☆ Watch on any stock's page. Alerts on your stocks get the <b>Mine</b> filter on the tape, and reach your lock screen even at MEDIUM.</span></div>`);
  const pa = desk.price_alerts;
  $("dAlerts").innerHTML = pa.length ? pa.map((a) => {
    const line = a.kind === "above" ? `above ${fmtPx(a.value)}` : a.kind === "below" ? `below ${fmtPx(a.value)}` : a.kind === "trail" ? `falls ${a.value}% from its high${a.peak ? ` (high ${fmtPx(a.peak)}, fires under ${fmtPx(a.peak * (1 - a.value / 100))})` : ""}` : `moves ±${a.value}% on the day`;
    const now = a.price != null ? `now ${fmtPx(a.price)}${a.chg_day != null ? ` · ${pct(a.chg_day)}` : ""}` : "no live price yet";
    return `<div class="li${a.fired ? " done" : ""}"><span class="sym" style="width:64px" data-tk="${esc(a.symbol)}" role="button">${esc(a.symbol)}</span>
      <div class="grow"><div>${esc(line)}</div><div class="sub">${a.fired ? `✓ fired ${ago(a.fired)} ago at ${fmtPx(a.fired_price)}` : esc(now)}</div></div>
      <button class="x" data-act="pa-rm" data-s="${esc(a.id)}" aria-label="Remove">✕</button></div>`;
  }).join("") : `<div class="li"><span class="muted">No price alerts. Open a stock's page and tap 🔔 to set one: above a price, below a price, or a % move on the day. It pushes to this phone once.</span></div>`;
  const tr = desk.tracked, rec = desk.record;
  $("dTracked").innerHTML = (tr.length ? tr.map((t) => `<div class="li"><span class="sym" style="width:64px" data-tk="${esc(t.symbol)}" role="button">${esc(t.symbol)}</span>
      <div class="grow"><div><span class="${t.direction === "long" ? "pos" : "neg"}">${t.direction === "long" ? "▲ Long" : "▼ Short"}</span> from ${fmtPx(t.entry)} · now ${fmtPx(t.price)}</div>
      <div class="sub">${ago(t.ts)} ago${t.alert_id ? ` · <span data-id="${esc(t.alert_id)}" role="button" class="lnk">from an alert</span>` : ""}${t.note ? " · " + esc(t.note) : ""}${levelsText(t)}</div></div>
      <span class="chg num ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</span><div class="col"><button class="btn ghost small" data-act="tr-levels" data-s="${esc(t.id)}">Levels</button><button class="btn ghost small" data-act="tr-journal" data-s="${esc(t.id)}">Journal</button><button class="btn ghost small" data-act="tr-close" data-s="${esc(t.id)}">Close</button></div></div>`).join("")
    : `<div class="li"><span class="muted">No open calls. Tap ▲ Track on an alert or a stock's page: it takes the live price as entry and follows the P&amp;L, so you learn which of your reads actually work. Paper only.</span></div>`)
    + (rec.closed ? `<div class="kv"><span>Closed calls</span><b class="num">${rec.closed} · ${rec.wins} won (${Math.round(rec.wins / rec.closed * 100)}%) · avg <span class="${cls(rec.avg_pnl_pct)}">${pct(rec.avg_pnl_pct)}</span></b></div>`
      + desk.closed.slice(0, 8).map((c) => `<div class="li done"><span class="sym" style="width:64px" data-tk="${esc(c.symbol)}" role="button">${esc(c.symbol)}</span>
        <div class="grow"><div>${c.direction === "long" ? "▲" : "▼"} ${fmtPx(c.entry)} → ${fmtPx(c.exit)}</div><div class="sub">closed ${ago(c.closed)} ago · held ${dur(c.closed - c.ts).replace(/ \d+s$/, "")}${(c.tags || []).length ? " · " + c.tags.map((x) => "#" + esc(x)).join(" ") : ""}</div>${c.lesson ? `<div class="sub gold">${esc(c.lesson)}</div>` : ""}</div>
        <span class="chg num ${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</span><button class="x" data-act="tr-journal" data-s="${esc(c.id)}" aria-label="Journal">✎</button></div>`).join("")
      + (rec.by_tag?.length > 1 || rec.by_tag?.[0]?.tag !== "untagged" ? `<div class="sub" style="padding:12px 0 4px">By tag</div>` + rec.by_tag.map((g) => `<div class="kv"><span>#${esc(g.tag)}</span><b class="num">${g.wins} of ${g.n} won · avg <span class="${cls(g.avg)}">${pct(g.avg)}</span></b></div>`).join("") : "") : "");
  renderKeywords();
  loadSaved();
  renderRules();
  loadPortfolio();
  const ts = desk.taste || [];
  $("dTasteWrap").hidden = !ts.length;
  $("dTaste").innerHTML = ts.map((t) => `<div class="kv"><span>${esc(t.label)} <span class="sub">👍 ${t.up} · 👎 ${t.down} · saved ${t.saved} · tracked ${t.tracked} · opened ${t.opened}</span>
      ${t.suggest_off ? `<br><button class="btn ghost small" data-act="cat-off" data-s="${esc(t.category === "news" ? "news" : t.category)}" style="margin-top:6px">You keep voting these down: turn them off here?</button>` : ""}</span>
      <b class="num ${t.score > 0.15 ? "pos" : t.score < -0.15 ? "neg" : ""}">${t.score > 0 ? "+" : ""}${t.score.toFixed(2)}</b></div>`).join("")
    + `<div class="sub" style="padding:4px 0 12px">From what you do on this phone: votes, saves, tracked calls, opens. "Best first" on the tape ranks your favorite kinds higher.</div>`;
  renderFollow();
  const sh = desk.share;
  $("dShare").innerHTML = `<div class="sub" style="margin-bottom:12px">Share your watchlist: a code anyone can use for 7 days to add your stocks to theirs (a copy; nothing else of yours is shared).</div>
    ${sh ? `<div class="code">${esc(sh.code.slice(0, 4))}-${esc(sh.code.slice(4))}</div><div class="sub">${sh.n} stocks · valid ${esc(dur(sh.until - Date.now() / 1000).replace(/ \d+[ms]$/, ""))}</div>` : `<button class="btn ghost small" data-act="share-list">Make a share code</button>`}
    ${sh ? `<div class="row" style="margin-top:8px"><button class="btn ghost small" data-act="list-link">Share as a link ↗</button></div>` : ""}
    <div class="addrow" style="margin-top:8px"><input id="listIn" class="key" placeholder="Got a list code? XXXX-XXXX" autocapitalize="characters" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="import-list">Add those stocks</button></div>`;
  // the heatmap: your stocks at a glance, colored by today's move
  const hm = desk.watchlist.filter((w) => w.chg_day != null);
  $("dHeat").hidden = !hm.length;
  $("dHeat").innerHTML = hm.map((w) => { const k = Math.min(1, Math.abs(w.chg_day) / 5); return `<div class="hm" data-tk="${esc(w.symbol)}" role="button" style="--h:${w.chg_day >= 0 ? 150 : 8};--a:${(0.12 + 0.5 * k).toFixed(2)}"><b>${esc(w.symbol)}</b><small class="num">${pct(w.chg_day)}</small></div>`; }).join("");
  const tr2 = desk.transfer;
  $("dMove").innerHTML = `<div class="sub" style="margin-bottom:12px">Your desk lives with this phone. To use it on another phone or browser, make a code here and enter it there within 15 minutes: watchlist, alerts, calls, journal, keywords and settings are copied.</div>
    ${tr2 ? `<div class="code">${esc(tr2.code.slice(0, 4))}-${esc(tr2.code.slice(4))}</div><div class="sub">valid for ${esc(dur(tr2.until - Date.now() / 1000).replace(/ \d+s$/, ""))}, once</div>` : `<button class="btn ghost small" data-act="xfer-out">Make a code</button>`}
    <div class="addrow" style="margin-top:8px"><input id="xferIn" class="key" placeholder="Have a code? XXXX-XXXX" autocapitalize="characters" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="xfer-in">Copy here</button></div>`;
  const pr = desk.prefs, q = pr.quiet || {};
  $("dPrefs").innerHTML = `<div class="sub" style="padding-top:12px">What may push to this phone</div>
    <div class="row" style="padding:10px 0 4px">${CATS.map(([k, l]) => `<button class="btn ghost small${pr.cats[k] !== false ? " sel" : ""}" data-act="cat" data-s="${k}" aria-pressed="${pr.cats[k] !== false}">${l}</button>`).join("")}</div>
    <div class="sub" style="padding-top:8px">Snooze for an hour (tap again to end it)</div>
    <div class="row" style="padding:10px 0 4px">${CATS.map(([k, l]) => { const until = (desk.snooze || {})[k]; return `<button class="btn ghost small${until ? " sel" : ""}" data-act="snooze" data-s="${k}">${until ? `💤 ${l} until ${new Date(until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : l}</button>`; }).join("")}</div>
    <div class="kv"><span>Skip what fades ${tip("base")}<div class="sub">don't push alert kinds that went their way 40% of the time or less over 20+ graded (CRITICAL still comes)</div></span><b><button class="btn ghost small${pr.skip_fades ? " sel" : ""}" data-act="flag" data-s="skip_fades">${pr.skip_fades ? "On" : "Off"}</button></b></div>
    <div class="sub" style="padding-top:8px">Display (this phone)</div>
    <div class="row" style="padding:10px 0 4px"><button class="btn ghost small${store.get("ft_clear") === "1" ? "" : " sel"}" data-act="display" data-s="cinema">Cinematic</button><button class="btn ghost small${store.get("ft_clear") === "1" ? " sel" : ""}" data-act="display" data-s="clear">Clear (no 3D, no blur)</button><button class="btn ghost small${store.get("ft_text") === "big" ? " sel" : ""}" data-act="display" data-s="text">Larger text</button>
      <button class="btn ghost small${store.get("ft_light") === "1" ? " sel" : ""}" data-act="display" data-s="light">Light</button><button class="btn ghost small${store.get("ft_compact") === "1" ? " sel" : ""}" data-act="display" data-s="compact">Compact</button>
      <button class="btn ghost small${store.get("ft_haptic") === "1" ? " sel" : ""}" data-act="display" data-s="haptic">Vibrate on alerts</button><button class="btn ghost small${store.get("ft_chime") === "1" ? " sel" : ""}" data-act="display" data-s="chime">Chime on alerts</button><button class="btn ghost small${store.get("ft_beginner") === "1" ? " sel" : ""}" data-act="display" data-s="beginner">Beginner mode</button><button class="btn ghost small${store.get("ft_onestock") === "1" ? " sel" : ""}" data-act="display" data-s="onestock">One notification per stock</button></div>
    <div class="row" style="padding:6px 0 4px"><button class="btn ghost small${localTimes() ? " sel" : ""}" data-act="tz">${localTimes() ? "Times: your time zone" : "Times: ET"}</button><button class="btn ghost small" data-act="glossary">What the terms mean</button><button class="btn ghost small" data-act="links">Shortcuts &amp; links</button></div>
    <div class="kv"><span>Follow-ups<div class="sub">an hour after an alert on a stock you watch or hold, if it moved 2%+: how it played out</div></span><b><button class="btn ghost small${pr.followups ? " sel" : ""}" data-act="flag" data-s="followups">${pr.followups ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>Tomorrow, 20:00 ET (Sun-Thu)<div class="sub">after-hours movers and why, the next session's reporters (yours first) and events</div></span><b><button class="btn ghost small${pr.tomorrow ? " sel" : ""}" data-act="flag" data-s="tomorrow">${pr.tomorrow ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>The close, 16:15 ET<div class="sub">a market recap: indexes, breadth, sectors, the biggest movers and why, how the alerts did</div></span><b><button class="btn ghost small${pr.close_recap ? " sel" : ""}" data-act="flag" data-s="close_recap">${pr.close_recap ? "On" : "Off"}</button></b></div>
    <div class="sub" style="padding-top:8px">A move alert for every stock you add</div>
    <div class="row" style="padding:10px 0 4px">${[0, 3, 5, 8, 10].map((v) => `<button class="btn ghost small${(pr.auto_move || 0) === v ? " sel" : ""}" data-act="automove" data-s="${v}">${v ? `±${v}%` : "Off"}</button>`).join("")}</div>
    <div class="sub" style="padding-top:8px">Also send this phone's alerts to</div>
    ${desk.forward ? `<div class="kv"><span>${esc(desk.forward.where)}</span><b><button class="btn ghost small" data-act="fw-off">Stop</button></b></div>`
      : `<div class="addrow"><select id="fwKind"><option value="discord">Discord webhook</option><option value="telegram">Telegram bot</option><option value="ntfy">ntfy topic</option></select>
        <input id="fwA" class="key" placeholder="webhook URL · bot token · topic" autocomplete="off" spellcheck="false"></div>
        <div class="addrow" style="padding-top:0"><input id="fwB" class="key" placeholder="Telegram chat id (Telegram only)" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="fw-save">Save</button></div>`}
    <div class="sub" style="padding-top:8px">This phone's floor</div>
    <div class="row" style="padding:10px 0 4px">${FLOORS.map(([k, l]) => `<button class="btn ghost small${(pr.min_sev || "") === k ? " sel" : ""}" data-act="floor" data-s="${k}">${l}</button>`).join("")}</div>
    <div class="sub" style="padding-top:8px">How pushes arrive ${tip("digest")}${desk.held ? ` · <span class="gold">${desk.held} held for the next digest</span>` : ""}</div>
    <div class="row" style="padding:10px 0 4px">${[[0, "Each alert"], [15, "Digest · 15 min"], [60, "Digest · hourly"]].map(([k, l]) => `<button class="btn ghost small${(pr.digest || 0) === k ? " sel" : ""}" data-act="digest" data-s="${k}">${l}</button>`).join("")}</div>
    <div class="kv"><span>Earnings-day reminders<div class="sub">before a stock you watch reports: what options price, its usual move</div></span><b><button class="btn ghost small${pr.earn_remind !== false ? " sel" : ""}" data-act="flag" data-s="earn_remind">${pr.earn_remind !== false ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>The open plan, 9:20 ET<div class="sub">your stocks pre-market, the top gappers and why, today's timed events</div></span><b><button class="btn ghost small${pr.open_plan !== false ? " sel" : ""}" data-act="flag" data-s="open_plan">${pr.open_plan !== false ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>Your week, Friday 16:30 ET<div class="sub">your calls, how the alerts on your stocks did, next week's reports and big dates</div></span><b><button class="btn ghost small${pr.week !== false ? " sel" : ""}" data-act="flag" data-s="week">${pr.week !== false ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>End-of-day report card<div class="sub">16:05 ET: your watchlist's day, your calls, what fired, how the alerts did</div></span><b><button class="btn ghost small${pr.eod !== false ? " sel" : ""}" data-act="eod">${pr.eod !== false ? "On" : "Off"}</button></b></div>
    <div class="sub" style="padding-top:8px">Focus: only CRITICAL comes through${desk.focus ? ` · <span class="gold">on until ${new Date(desk.focus * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>` : ""}</div>
    <div class="row" style="padding:10px 0 4px"><button class="btn ghost small" data-act="focus" data-m="60">1 hour</button><button class="btn ghost small" data-act="focus" data-m="180">3 hours</button><button class="btn ghost small" data-act="focus" data-u="open">Until the open</button>${desk.focus ? `<button class="btn ghost small sel" data-act="focus" data-m="0">End focus</button>` : ""}</div>
    <div class="sub" style="padding-top:8px">Burst protection: after this many pushes in 10 minutes, the rest fold into one summary (CRITICAL still comes)</div>
    <div class="row" style="padding:10px 0 4px">${[[0, "Off"], [4, "4"], [6, "6"], [10, "10"]].map(([k, l]) => `<button class="btn ghost small${(pr.burst || 0) === k ? " sel" : ""}" data-act="burst" data-s="${k}">${l}</button>`).join("")}</div>
    <div class="kv"><span>Watchlist pushes<div class="sub">a MEDIUM alert on a stock you watch still reaches your lock screen</div></span><b><button class="btn ghost small${pr.watch_push ? " sel" : ""}" data-act="wpush">${pr.watch_push ? "On" : "Off"}</button></b></div>
    <div class="kv"><span>Quiet hours (ET)<div class="sub">${pr.quiet ? `only CRITICAL between ${esc(q.start)} and ${esc(q.end)}` : "off: everything you allow, any hour"}</div></span>
      <b class="qh"><input type="time" id="qStart" value="${esc(q.start || "22:00")}"><input type="time" id="qEnd" value="${esc(q.end || "07:00")}">
      <button class="btn ghost small" data-act="quiet">${pr.quiet ? "Update" : "Set"}</button>${pr.quiet ? `<button class="btn ghost small" data-act="quiet-off">Off</button>` : ""}</b></div>`;
}
async function loadRecord() {
  try {
    const r = await api("/api/record?days=30");
    const label = (k) => { const [a, b] = k.split(":"); return (KIND_LABEL[a] || a) + (b ? ` · ${b}` : ""); };
    const cell = (x) => x ? `<b class="num ${x.hit >= 0.5 ? "pos" : "neg"}">${Math.round(x.hit * 100)}%</b> <span class="sub">of ${x.n} · med ${pct(x.median)}</span>` : `<span class="sub">—</span>`;
    const cells = (x) => [["1h", x.h1], ["close", x.close], ["next day", x.d1], ["5 days", x.d5]].filter(([, v]) => v).map(([k, v]) => `<span class="faint">${k}</span> ${cell(v)}`).join("<br>");
    $("dRecord").innerHTML = r.rows.length ? `<div class="kv head"><span>Alert kind</span><b>1 hour · close · next day · 5 days</b></div>` + r.rows.map((x) => `<div class="kv"><span>${esc(label(x.kind))}</span><b class="rec">${cells(x)}</b></div>`).join("")
      + `<div class="sub" style="padding:10px 0 14px">${esc(r.note)} Hit = the stock went the alert's way.</div>`
      : `<div class="li"><span class="muted">Every alert is graded an hour later and at its session's close, against its own reference price. The first grades land after the next session; this fills in on its own.</span></div>`;
    recordCache = r;
    const bucketRows = (title, rows) => rows.length ? `<div class="kv head"><span>${title}</span><b>1 hour · close</b></div>` + rows.map((x) => `<div class="kv"><span>${esc(x.bucket)}</span><b class="rec">${cell(x.h1)}<br>${cell(x.close)}</b></div>`).join("") : "";
    $("dRecord").innerHTML += bucketRows("Already moved before the alert?", r.priced_in || []) + bucketRows("By time of day (ET)", r.by_time || []);
    const src = await api("/api/sources").catch(() => ({ rows: [] }));
    if (src.rows.length) $("dRecord").innerHTML += `<div class="kv head"><span>By first source</span><b>1 hour · close</b></div>` + src.rows.slice(0, 10).map((x) => { const m = (desk?.prefs?.mute_sources || []).includes(x.source); return `<div class="kv"><span>${esc(outlet(x.source))}<br><button class="btn ghost small${m ? " sel" : ""}" data-act="src-mute" data-s="${esc(x.source)}" style="margin-top:6px">${m ? "Muted here · unmute" : "Mute on this phone"}</button></span><b class="rec">${cell(x.h1)}<br>${cell(x.close)}</b></div>`; }).join("");
  } catch (_) { $("dRecord").innerHTML = `<div class="li"><span class="muted">Track record unavailable.</span></div>`; }
}

// your data: the desk as a JSON backup, or your calls and journal as CSV
async function exportDesk(fmt) {
  try {
    const r = await fetch("/api/export" + (fmt === "csv" ? "?format=csv" : ""), { headers: { Authorization: "Bearer " + TOKEN, "X-Device": DEVICE } });
    if (!r.ok) throw new Error(await r.text());
    const url = URL.createObjectURL(await r.blob()), link = document.createElement("a");
    link.href = url; link.download = fmt === "csv" ? "foretape-calls.csv" : "foretape-desk.json";
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { toast("Export failed: " + e.message); }
}
async function loadSaved() {
  const ids = (desk?.saved || []).slice(0, 20);
  $("dSavedWrap").hidden = !ids.length;
  if (!ids.length) return;
  const got = await Promise.all(ids.map((id) => alerts.find((x) => x.id === id) || api("/api/alert/" + encodeURIComponent(id)).catch(() => null)));
  $("dSaved").innerHTML = got.filter(Boolean).map((a) => `<div class="li" data-id="${esc(a.id)}" role="button"><span class="dot-sev" style="background:${SEV[a.severity]};color:${SEV[a.severity]}"></span><div class="grow"><div style="font-weight:600;line-height:1.35">${esc(a.title)}</div><div class="sub">${esc(a.severity)} · ${ago(a.created)} ago</div>${gradeRow(a)}</div><button class="x" data-act="unsave" data-s="${esc(a.id)}" aria-label="Remove">✕</button></div>`).join("");
}
const icsUrl = (scheme) => `${scheme === "webcal" ? "webcal:" : location.protocol}//${location.host}/api/calendar.ics?token=${encodeURIComponent(TOKEN)}&device=${encodeURIComponent(DEVICE)}`;
// the portfolio: holdings with shares (and cost), live
async function loadPortfolio() {
  let r;
  try { r = await api("/api/portfolio"); } catch (_) { return; }
  const usd = (v) => v == null ? "—" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2 });
  const sgn = (v) => v == null ? "—" : (v > 0 ? "+" : "") + usd(v);
  $("dPort").innerHTML = (r.rows.length ? `<div class="kv"><span>Value</span><b class="num">${usd(r.value)}</b></div>
      <div class="kv"><span>Today</span><b class="num ${cls(r.day_pnl)}">${sgn(r.day_pnl)}${r.day_pct != null ? ` · ${pct(r.day_pct)}` : ""}</b></div>
      ${r.unreal != null ? `<div class="kv"><span>Unrealized (where you gave a cost)</span><b class="num ${cls(r.unreal)}">${sgn(r.unreal)} · ${pct(r.unreal_pct)}</b></div>` : ""}
      ${r.contributors.length ? `<div class="kv"><span>Moving it today</span><b class="num">${r.contributors.map((c) => `${esc(c.symbol)} <span class="${cls(c.day_pnl)}">${sgn(c.day_pnl)}</span>`).join(" · ")}</b></div>` : ""}
      ${r.sectors.length ? `<div class="sbar">${r.sectors.map((x, i) => `<i style="flex:${x.weight};--i:${i}" title="${esc(x.sector)} ${x.weight}%"></i>`).join("")}</div><div class="sub" style="padding:4px 0 8px">${r.sectors.slice(0, 5).map((x) => `${esc(x.sector)} ${x.weight}%`).join(" · ")}</div>` : ""}
      ${r.rows.map((x) => `<div class="li"><span class="sym" style="width:56px" data-tk="${esc(x.symbol)}" role="button">${esc(x.symbol)}</span><div class="grow"><div class="num">${x.shares.toLocaleString()} × ${fmtPx(x.price)} = ${usd(x.value)}</div><div class="sub">${x.weight != null ? x.weight + "% · " : ""}today <span class="${cls(x.day_pnl)}">${sgn(x.day_pnl)}</span>${x.unreal != null ? ` · since cost <span class="${cls(x.unreal)}">${pct(x.unreal_pct)}</span>` : ""}</div>${x.news ? `<div class="sub"><a href="${esc(x.news.url)}" target="_blank" rel="noopener" class="rnews">${esc(x.news.title)}</a></div>` : ""}</div><span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span><button class="x" data-act="hold-rm" data-s="${esc(x.symbol)}" aria-label="Remove ${esc(x.symbol)}">✕</button></div>`).join("")}`
    : `<div class="li"><span class="muted">Add what you hold: live value, today's P&amp;L in dollars, exposure by sector. Held stocks count as yours for alerts (the Mine filter, and pushes even at MEDIUM). Stored for this phone only.</span></div>`)
    + `<div class="sub" style="padding-top:10px">Push me when the portfolio moves today</div><div class="row" style="padding:8px 0 4px">${[0, 1, 2, 3, 5].map((v) => `<button class="btn ghost small${(desk.prefs.port_alert || 0) === v ? " sel" : ""}" data-act="portalert" data-s="${v}">${v ? `±${v}%` : "Off"}</button>`).join("")}</div>`
    + `<div class="addrow"><input id="hSym" class="key" placeholder="Ticker" autocapitalize="characters" autocomplete="off" spellcheck="false" style="max-width:110px"><input id="hShares" class="key" inputmode="decimal" placeholder="Shares"><input id="hCost" class="key" inputmode="decimal" placeholder="Cost/share (optional)"><button class="btn ghost small" data-act="hold-add">Add</button></div>
      <details class="paste"><summary class="sub">Paste a list (one per line: ticker, shares, cost)</summary><textarea id="hPaste" rows="4" placeholder="AAPL 10 150&#10;MSFT, 5&#10;NVDA 2.5 120.50"></textarea><button class="btn ghost small" data-act="hold-import">Import</button></details>`;
}
// follow a theme or a sector: its MEDIUM alerts reach this phone like a watched stock's
let followOpts = null;
const prettyTheme = (t) => t.replace(/_/g, " ").replace(/\bai\b/g, "AI").replace(/\bfda\b/g, "FDA").replace(/\bgse\b/g, "GSE").replace(/^./, (c) => c.toUpperCase());
async function renderFollow() {
  if (!followOpts) { try { followOpts = await api("/api/follow-options"); } catch (_) { return; } }
  const f = desk.follow || { themes: [], sectors: [] };
  const chips = (kind, list, label) => list.map((n) => `<button class="btn ghost small${(f[kind] || []).includes(n) ? " sel" : ""}" data-act="follow" data-k="${kind}" data-s="${esc(n)}">${esc(label(n))}</button>`).join("");
  $("dFollow").innerHTML = `<div class="sub" style="padding-top:12px">Sectors</div><div class="row" style="padding:8px 0">${chips("sectors", followOpts.sectors, (x) => x)}</div>
    <div class="sub">Themes (as the scorer tags headlines)</div><div class="row" style="padding:8px 0 14px">${chips("themes", followOpts.themes, prettyTheme)}</div>`;
}
// smart alerts: this phone's own triggers
const RULE_TEXT = {
  headline: (r) => `a headline on ${r.symbol === "*" ? "any stock you watch" : r.symbol} scoring ${r.value}+`,
  news_move: (r) => `a headline on ${r.symbol === "*" ? "any stock you watch" : r.symbol}, then a ${r.value}% move within the hour`,
  volume: (r) => `${r.symbol === "*" ? "any stock you watch" : r.symbol} trades ${r.value}× its normal volume`,
  vwap: (r) => `${r.symbol === "*" ? "any stock you watch" : r.symbol} crosses ${r.dir} VWAP`,
  gap: (r) => `${r.symbol === "*" ? "any stock you watch" : r.symbol} gaps ${r.value}%+ pre-market`,
};
// how often a rule would have fired over the last 30 days
async function previewRule() {
  let kind = $("ruleKind").value;
  if (kind.startsWith("vwap_")) kind = "vwap";
  const defaults = { headline: 20, news_move: 3, volume: 5, gap: 4, vwap: 0 };
  const body = { kind, symbol: $("ruleSym").value.trim() || "*", value: $("ruleVal").value.trim() || defaults[kind] };
  let r;
  try { r = await api("/api/rule-preview", { method: "POST", body: JSON.stringify(body) }); } catch (e) { $("rulePrev").textContent = e.message; return; }
  $("rulePrev").innerHTML = !r.replayable ? esc(r.why)
    : kind === "headline" ? `Over the last ${r.days} days: <b>${r.headlines}</b> headline${r.headlines === 1 ? "" : "s"} would have fired it (about ${r.per_week} a week).`
    : `Over the last ${r.days} days: <b>${r.headlines}</b> headlines could have started it; of ${r.graded} that became graded alerts, <b>${r.moved}</b> moved ${esc(String(body.value))}%+ within the hour.`;
}
function renderRules() {
  const rules = desk.rules || [];
  $("dRules").innerHTML = (rules.length ? rules.map((r) => `<div class="li"><div class="grow">⚡ ${esc(RULE_TEXT[r.kind]?.(r) || r.kind)}</div><button class="x" data-act="rule-rm" data-s="${esc(r.id)}" aria-label="Remove">✕</button></div>`).join("")
    : `<div class="li"><span class="muted">Your own triggers, pushed to this phone only, each once per headline or once a day. Add one below, or from any stock's page (⚡ Smart alert).</span></div>`)
    + `<div class="addrow"><select id="ruleKind"><option value="news_move">Headline, then a move of %</option><option value="headline">Headline scoring at least</option><option value="volume">Volume, × normal</option><option value="gap">Pre-market gap of %</option><option value="vwap_above">Crosses above VWAP</option><option value="vwap_below">Loses VWAP</option></select></div>
      <div class="addrow" style="padding-top:0"><input id="ruleSym" class="key" placeholder="Ticker, or * for all you watch" autocapitalize="characters" autocomplete="off" spellcheck="false" value="*"><input id="ruleVal" class="key" inputmode="decimal" placeholder="value" style="max-width:90px"><button class="btn ghost small" data-act="rule-prev">Preview</button><button class="btn ghost small" data-act="rule-add">Add</button></div>
      <div class="sub" id="rulePrev" style="padding-bottom:12px"></div>`;
}
// keywords: your own words, pushed to this phone when a scored headline names one
function renderKeywords() {
  if (!desk) return;
  const kws = desk.keywords || [], hits = desk.kw_hits || [];
  $("dKeys").innerHTML = `<div class="addrow"><input id="kwIn" class="key" placeholder="A word or phrase: tariff, FDA, buyback, Jensen Huang" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="kw-in">Add</button></div>
    ${kws.length ? `<div class="row" style="padding:0 0 12px">${kws.map((k) => `<span class="kwc">${esc(k.k)}<button class="x" data-act="kw-rm" data-s="${esc(k.k)}" aria-label="Remove">✕</button></span>`).join("")}</div>` : `<div class="sub" style="padding:0 0 12px">Any scored headline that names one is pushed to this phone, even under the alert line (once per story; not in quiet hours; not when it's only about stocks you muted).</div>`}
    <div class="sub" style="padding-top:6px">Never push headlines with…</div>
    <div class="addrow"><input id="negIn" class="key" placeholder="crypto, airdrop, rumor" autocomplete="off" spellcheck="false"><button class="btn ghost small" data-act="neg-in">Add</button></div>
    ${(desk.kw_neg || []).length ? `<div class="row" style="padding:0 0 12px">${desk.kw_neg.map((k) => `<span class="kwc neg">${esc(k)}<button class="x" data-act="neg-rm" data-s="${esc(k)}" aria-label="Remove">✕</button></span>`).join("")}</div>` : ""}
    ${hits.length ? `<div class="sub" style="padding:4px 0">Recent hits</div>` + hits.slice(0, 8).map((h) => `<div class="li"><div class="grow"><div class="sub">🔑 ${esc(h.k)} · ${esc(outlet(h.source))} · ${ago(h.ts)} ago</div><a href="${esc(h.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(h.title)}</a></div></div>`).join("") : ""}`;
  const muted = Object.entries(desk.muted || {});
  const dials = Object.entries(desk.stock_push || {});
  $("dDial").hidden = !dials.length;
  $("dDial").innerHTML = dials.map(([sym, lv]) => `<div class="kv"><span><span data-tk="${esc(sym)}" role="button">${esc(sym)}</span> <span class="sub">${DIAL_TEXT[lv]}</span></span><b class="dialb">${dialBtns(sym, lv)}</b></div>`).join("")
    + `<div class="sub" style="padding:4px 0 12px">Set per stock on its page: HIGH only, everything (even MEDIUM, even under your floor) or nothing.</div>`;
  const rem = desk.reminders || [];
  $("dRemind").hidden = !rem.length;
  $("dRemind").innerHTML = rem.map((r) => `<div class="kv"><span data-id="${esc(r.alert_id)}" role="button">⏰ ${esc(r.title)} <span class="sub">${esc(new Date(r.due * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</span></span><b><button class="x" data-act="remind-clear" data-s="${esc(r.id)}" aria-label="Cancel the reminder">✕</button></b></div>`).join("");
  $("dMuted").hidden = !muted.length;
  $("dMuted").innerHTML = muted.map(([sym, until]) => `<div class="kv"><span>🔕 <span data-tk="${esc(sym)}" role="button">${esc(sym)}</span> <span class="sub">for ${esc(dur(until - Date.now() / 1000).replace(/ \d+s$/, ""))} more</span></span><b><button class="btn ghost small" data-act="unmute" data-s="${esc(sym)}">Unmute</button></b></div>`).join("");
}
async function loadWorth() {
  try {
    const r = await api("/api/worth");
    $("dWorthWrap").hidden = !r.rows.length;
    $("dWorth").innerHTML = r.rows.map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span>
      <div class="grow"><div>${esc(x.name)}</div><div class="sub">${x.why.map(esc).join(" · ")} · peer of ${esc(x.peer_of)}</div></div><span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("");
  } catch (_) { /* optional panel */ }
}

// the Desk's "This week" (pushed Friday 16:30 ET as "your week")
async function loadWeek() {
  let r;
  try { r = await api("/api/week"); } catch (_) { return; }
  $("dWeekWrap").hidden = !r.week_of;
  if (!r.week_of) return;
  const c = r.closed, o = r.open, al = r.alerts, nx = r.next;
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short" });
  $("dWeek").innerHTML = [
    `<div class="kv"><span>Calls closed</span><b class="num">${c.n ? `${c.wins} of ${c.n} won · avg <span class="${cls(c.avg)}">${pct(c.avg)}</span>${c.best ? ` · best ${esc(c.best.symbol)} <span class="${cls(c.best.pnl_pct)}">${pct(c.best.pnl_pct)}</span>` : ""}` : "none yet"}</b></div>`,
    o.n ? `<div class="kv"><span>Open calls</span><b class="num">${o.n} · avg <span class="${cls(o.avg)}">${pct(o.avg)}</span></b></div>` : "",
    `<div class="kv"><span>Alerts on your stocks</span><b class="num">${al.graded ? `${al.right} of ${al.graded} went their way by the close` : "none graded yet"}</b></div>`,
    nx.earnings.length ? `<div class="kv"><span>Reporting next</span><b>${nx.earnings.map((e) => `<span data-tk="${esc(e.symbol)}" role="button">${esc(e.symbol)}</span> ${esc(day(e.date))}`).join(" · ")}</b></div>` : "",
    nx.events.length ? `<div class="kv"><span>Big dates</span><b>${nx.events.slice(0, 3).map((e) => `${esc(e.title)} (${esc(day(e.date))})`).join("<br>")}</b></div>` : "",
  ].join("");
}
// the Brief's "day in one screen": today's alerts on a time axis, over SPY
async function loadDay() {
  let r;
  try { r = await api("/api/day"); } catch (_) { return; }
  $("bDayWrap").hidden = !r.rows.length && !r.spy.length;
  if ($("bDayWrap").hidden) return;
  const W = 560, H = 150, pad = 8, t0 = r.start + 4 * 3600, t1 = r.start + 20 * 3600;
  const X = (t) => pad + Math.max(0, Math.min(1, (t - t0) / (t1 - t0))) * (W - 2 * pad);
  const ps = r.spy.map(([, p]) => p), lo = Math.min(...ps, Infinity), hi = Math.max(...ps, -Infinity), span = hi - lo || 1;
  const Y = (p) => 26 + (1 - (p - lo) / span) * (H - 50);
  const line = r.spy.length >= 2 ? `<path d="${r.spy.map(([t, p], i) => (i ? "L" : "M") + X(t).toFixed(1) + " " + Y(p).toFixed(1)).join(" ")}" fill="none" stroke="rgba(159,198,255,.8)" stroke-width="2"/>` : "";
  const lane = { CRITICAL: 10, HIGH: 16, MEDIUM: H - 14, LOW: H - 8 };
  const dots = r.rows.map((a) => `<circle data-id="${esc(a.id)}" cx="${X(a.t).toFixed(1)}" cy="${lane[a.severity] || H - 10}" r="${a.severity === "CRITICAL" ? 6 : a.severity === "HIGH" ? 5 : 3.5}" fill="${SEV[a.severity] || SEV.LOW}" stroke="rgba(0,0,0,.35)"><title>${esc(a.title)}</title></circle>`).join("");
  const tick = (h, m, label) => { const x = X(r.start + h * 3600 + m * 60); return `<line x1="${x}" x2="${x}" y1="22" y2="${H - 20}" stroke="rgba(255,255,255,.12)"/><text x="${x}" y="${H - 22}" fill="rgba(255,255,255,.5)" font-size="10" text-anchor="middle" font-family="Inter">${label}</text>`; };
  const spyChg = r.spy.length >= 2 ? (r.spy[r.spy.length - 1][1] / r.spy[0][1] - 1) * 100 : null;
  $("bDay").innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="dayline">${tick(9, 30, "9:30")}${tick(12, 0, "12:00")}${tick(16, 0, "16:00")}${line}${dots}</svg>
    <div class="sub" style="padding:8px 0 12px">${["CRITICAL", "HIGH", "MEDIUM"].filter((k) => r.counts[k]).map((k) => `<span style="color:${SEV[k]}">●</span> ${r.counts[k]} ${k}`).join(" · ") || "No alerts yet today"}${spyChg != null ? ` · <span style="color:#9FC6FF">SPY</span> <span class="${cls(spyChg)}">${pct(spyChg)}</span> since the first mark` : ""}. Tap a dot for its alert.</div>`;
}
// the Brief's market memory: the last sessions side by side, built one close at a time
async function loadMemory() {
  let m;
  try { m = await api("/api/memory"); } catch (_) { return; }
  $("bMemWrap").hidden = m.days.length < 2;
  if (m.days.length < 2) return;
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "numeric", day: "numeric" });
  $("bMem").innerHTML = m.days.map((d) => { const best = Object.entries(d.sectors || {}).sort((x, y) => y[1] - x[1])[0];
    return `<div class="kv"><span>${esc(day(d.date))} <span class="sub">${esc(d.regime)}${best ? ` · led by ${esc(best[0])}` : ""}</span></span><b class="num">SPY <span class="${cls(d.indexes?.SPY)}">${pct(d.indexes?.SPY)}</span> · ${d.pct_up != null ? d.pct_up.toFixed(0) + "% up" : "—"}</b></div>`; }).join("")
    + (m.sectors.length ? `<div class="sub" style="padding:12px 0 4px">Sectors over these sessions</div>` + m.sectors.slice(0, 6).map((x) => `<div class="kv"><span>${esc(x.sector)} <span class="sub">up ${x.up} of ${x.sessions}</span></span><b class="num ${cls(x.sum)}">${pct(x.sum)}</b></div>`).join("") : "");
}
// the Brief's "Tomorrow": after the close, the next session ahead
async function loadTomorrow() {
  const etH = etNowMinutes() / 60;
  if (etH < 16) { $("bTomWrap").hidden = true; return; }
  let t;
  try { t = await api("/api/tomorrow"); } catch (_) { return; }
  $("bTomWrap").hidden = !(t.movers.length || t.reports.length || t.events.length);
  $("bTomSub").textContent = prettyDate(t.date);
  $("bTom").innerHTML = t.movers.map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:56px">${esc(x.symbol)}</span><div class="grow"><div class="sub">${x.why ? esc(x.why.title) : "No headline found"}</div></div><span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("")
    + (t.reports.length ? `<div class="kv"><span>Reporting</span><b>${t.reports.map((r) => `<span data-tk="${esc(r.symbol)}" role="button" class="${r.mine ? "gold" : ""}">${r.mine ? "★" : ""}${esc(r.symbol)}</span>${/before/i.test(r.when) ? " ☀" : /after/i.test(r.when) ? " ☾" : ""}`).join(" · ")}</b></div>` : "")
    + t.events.map((e) => `<div class="kv"><span>${esc(e.title)}</span><b class="num">${e.time ? esc(showTime(t.date, e.time)) + (localTimes() ? "" : " ET") : ""}</b></div>`).join("");
}
// the Brief's "The close": shown once the regular session is over
async function loadClose() {
  const etH = etNowMinutes() / 60;
  if (etH < 16 || etH >= 24) { $("bCloseWrap").hidden = true; return; }
  let c;
  try { c = await api("/api/close"); } catch (_) { return; }
  const b = c.breadth || {};
  $("bCloseWrap").hidden = !Object.keys(c.indexes).length && !b.n;
  $("bClose").innerHTML = [
    Object.keys(c.indexes).length ? `<div class="kv"><span>Indexes</span><b class="num">${Object.entries(c.indexes).map(([k, v]) => `${k} <span class="${cls(v)}">${pct(v)}</span>`).join(" · ")}</b></div>` : "",
    b.n ? `<div class="kv"><span>Breadth · ${esc(c.regime.label)}</span><b class="num">${b.up} up · ${b.down} down (${b.pct_up}%)</b></div>` : "",
    c.best.length ? `<div class="kv"><span>Led · lagged</span><b>${c.best.map((x) => `${esc(x.sector)} <span class="pos">${pct(x.chg)}</span>`).join(", ")}${c.worst.length ? "<br>" + c.worst.map((x) => `${esc(x.sector)} <span class="${cls(x.chg)}">${pct(x.chg)}</span>`).join(", ") : ""}</b></div>` : "",
    ...[...c.up, ...c.down].slice(0, 6).map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:56px">${esc(x.symbol)}</span><div class="grow"><div class="sub">${x.why ? esc(x.why.title) : "No headline found"}</div></div><span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`),
    c.alerts.graded ? `<div class="kv"><span>Today's alerts by the close</span><b class="num">${c.alerts.right} of ${c.alerts.graded} went their way</b></div>` : "",
  ].join("");
}
// first run: make it yours in one sheet
const POPULAR = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "NFLX", "PLTR", "COIN", "JPM", "BAC", "XOM", "LLY", "UNH", "COST", "WMT", "DIS", "BA", "INTC", "SMCI", "MSTR"];
const KW_IDEAS = ["tariff", "FDA", "buyback", "guidance", "acquisition", "bankruptcy", "Fed", "CPI", "short report", "export controls", "antitrust", "layoffs"];
function openOnboard() {
  showSheet(`<div class="meta"><span class="src">Takes 20 seconds</span></div><h2>Make Foretape yours</h2>
    <p class="muted">Everything here lives on this phone and can be changed on the Desk any time.</p>
    <h4>Stocks you follow</h4><div class="row" id="obSyms">${POPULAR.map((t) => `<button class="btn ghost small" data-act="ob-tog" data-s="${t}">${t}</button>`).join("")}</div>
    <div class="addrow"><input id="obMore" class="key" placeholder="Others: tickers, comma separated" autocapitalize="characters" autocomplete="off" spellcheck="false"></div>
    <h4>Words that matter to you</h4><div class="row" id="obKws">${KW_IDEAS.map((k) => `<button class="btn ghost small" data-act="ob-tog" data-s="${esc(k)}">${esc(k)}</button>`).join("")}</div>
    <div class="sub" style="margin-top:8px">A scored headline naming one is pushed to you, even under the alert line.</div>
    <h4>How much to push</h4><div class="row" id="obStyle">
      <button class="btn ghost small" data-act="ob-style" data-s="few">Only the big ones</button>
      <button class="btn ghost small sel" data-act="ob-style" data-s="normal">Normal</button>
      <button class="btn ghost small" data-act="ob-style" data-s="digest">Everything, in a 15-min digest</button></div>
    <div class="row" style="margin-top:24px"><button class="btn primary" data-act="ob-save">Save</button></div>
    <div style="text-align:center;margin-top:10px"><button class="btn ghost small" data-act="ob-skip">Skip for now</button></div>`);
}
async function saveOnboard() {
  const picked = [...document.querySelectorAll("#obSyms .sel")].map((b) => b.dataset.s).concat(syms($("obMore").value));
  const kws = [...document.querySelectorAll("#obKws .sel")].map((b) => b.dataset.s);
  const style = document.querySelector("#obStyle .sel")?.dataset.s || "normal";
  if (picked.length) await deskEdit("watch_add", { symbols: picked });
  for (const k of kws) await deskEdit("kw_add", { keyword: k });
  await deskEdit("prefs", style === "few" ? { min_sev: "CRITICAL", digest: 0 } : style === "digest" ? { min_sev: null, digest: 15 } : { min_sev: null, digest: 0 });
  store.set("ft_onboarded", "1");
  closeSheet();
  toast(picked.length ? `Following ${picked.length} stock${picked.length > 1 ? "s" : ""}. Their alerts are under Mine.` : "Saved");
}
function maybeOnboard() {
  if (store.get("ft_onboarded") || !desk || desk.watchlist.length || (desk.keywords || []).length || sheetOpen || location.hash) return;
  openOnboard();
}

// the Desk's "Today": this phone's day so far (also pushed at 16:05 ET as the report card)
async function loadReport() {
  let r;
  try { r = await api("/api/report"); } catch (_) { return; }
  $("dTodayWrap").hidden = !r.date;
  if (!r.date) return;
  const w = r.watch, t = r.tracked, al = r.alerts;
  $("dToday").innerHTML = (r.summary ? `<p class="dsum">${esc(r.summary)}</p>` : "") + [
    w.n ? `<div class="kv"><span>Watchlist</span><b class="num">${w.up} of ${w.n} up${w.best ? ` · best <span data-tk="${esc(w.best.symbol)}" role="button">${esc(w.best.symbol)}</span> <span class="${cls(w.best.chg)}">${pct(w.best.chg)}</span>` : ""}${w.worst ? ` · worst <span data-tk="${esc(w.worst.symbol)}" role="button">${esc(w.worst.symbol)}</span> <span class="${cls(w.worst.chg)}">${pct(w.worst.chg)}</span>` : ""}</b></div>` : "",
    t.n ? `<div class="kv"><span>Tracked calls</span><b class="num">${t.n} open · avg <span class="${cls(t.avg)}">${pct(t.avg)}</span></b></div>` : "",
    r.fired.length || r.levels.length ? `<div class="kv"><span>Fired today</span><b class="num">${r.fired.length} price alert${r.fired.length === 1 ? "" : "s"} · ${r.levels.length} stop/target</b></div>` : "",
    `<div class="kv"><span>Foretape on your stocks</span><b class="num">${al.graded ? `${al.right} of ${al.graded} went the alert's way by the close` : "none graded at the close yet"}</b></div>`,
  ].join("");
}

// ------------------------------------------------------------------ while you were away
const SEEN_KEY = "ft_seen_at";
const markSeen = () => store.set(SEEN_KEY, String(Date.now() / 1000));
function renderAway() {
  const since = +(store.get(SEEN_KEY) || 0), now = Date.now() / 1000;
  if (!since || now - since < 1200) { markSeen(); return; }   // gone under 20 minutes: nothing to catch up on
  const missed = alerts.filter((a) => a.created > since && a.kind !== "system");
  const movers = (desk?.watchlist || []).filter((w) => w.chg_day != null && Math.abs(w.chg_day) >= 2).sort((x, y) => Math.abs(y.chg_day) - Math.abs(x.chg_day)).slice(0, 6);
  markSeen();
  if (!missed.length && !movers.length) return;
  const rank = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 };
  const top = [...missed].sort((x, y) => (rank[y.severity] - rank[x.severity]) || (y.created - x.created)).slice(0, 3);
  const counts = ["CRITICAL", "HIGH"].map((v) => [v, missed.filter((a) => a.severity === v).length]).filter(([, n]) => n);
  const atLeast = missed.length && missed.length === alerts.filter((a) => a.kind !== "system").length ? "at least " : "";
  $("away").innerHTML = `<div class="glass away-in"><button class="x" data-act="away-x" aria-label="Dismiss">✕</button>
    <div class="eyebrow" style="color:var(--gold)">While you were away · ${esc(dur(now - since).replace(/ \d+s$/, ""))}</div>
    <div class="away-n">${missed.length ? `${atLeast}${missed.length} alert${missed.length > 1 ? "s" : ""}${counts.length ? " · " + counts.map(([v, n]) => `${n} ${v}`).join(" · ") : ""}` : "No new alerts"}</div>
    ${top.map((a) => `<div class="li" data-id="${esc(a.id)}" role="button"><span class="dot-sev" style="background:${SEV[a.severity]};color:${SEV[a.severity]}"></span><div class="grow"><div style="font-weight:600;line-height:1.35">${esc(a.title)}</div><div class="sub">${esc(a.severity)} · ${ago(a.created)} ago</div></div></div>`).join("")}
    ${movers.length ? `<div class="sub" style="margin-top:10px">Your stocks</div><div class="tks" style="margin-top:6px">${movers.map((w) => `<span class="tk mine" data-tk="${esc(w.symbol)}" role="button">${esc(w.symbol)} <small class="${cls(w.chg_day)}">${pct(w.chg_day)}</small></span>`).join("")}</div>` : ""}</div>`;
  $("away").hidden = false;
}
setInterval(() => { if (!document.hidden && TOKEN && !$("main").hidden) markSeen(); }, 60000);
addEventListener("pagehide", markSeen);

// ------------------------------------------------------------------ the ticker page: everything about one stock
const LEAN = { bullish: ["pos", "▲ Evidence leans bullish"], bearish: ["neg", "▼ Evidence leans bearish"], mixed: ["", "◆ Evidence is mixed"], none: ["", "No directional evidence"] };
function tallyBlock(t) {
  if (!t || !(t.bull || t.bear)) return `<h4>The evidence</h4><div class="panel glass padded"><div class="sub">Nothing directional on it lately: no scored headline in 3 days, no one-sided options today, no insider buy in 90 days, no ignition on the tape.</div></div>`;
  const [c, l] = LEAN[t.lean] || LEAN.none, share = t.share_bull ?? 0.5;
  return `<h4>The evidence ${tip("evidence")}</h4><div class="panel glass padded">
    <div class="lean ${c}">${l}</div>
    <div class="tally"><span class="pos num">${t.bull}</span><div class="tbar"><i style="width:${Math.round(share * 100)}%"></i></div><span class="neg num">${t.bear}</span></div>
    ${t.signals.map((x) => `<div class="sig"${x.id ? ` data-id="${esc(x.id)}" role="button"` : ""}><span class="${x.side === "up" ? "pos" : "neg"}">${x.side === "up" ? "▲" : "▼"}</span>
      <div class="grow"><div>${esc(x.label)}</div><div class="sub">${esc(KIND_LABEL[x.kind] || x.kind)} · weighs ${x.weight} · ${ago(x.ts)} ago</div></div></div>`).join("")}
    <div class="sub" style="margin-top:12px">Weighted: one-sided options ×2, an opportunistic insider buy ×3 (a routine or planned one ×0.5), alerts by severity, other scored headlines ×0.5, a tape ignition ×1–1.5. A tally of the evidence, not a forecast.</div></div>`;
}
function tickerHTML(d) {
  const q = d.quote, sym = esc(d.symbol), watching = mine.has(d.symbol);
  const mine_pa = (desk?.price_alerts || []).filter((a) => a.symbol === d.symbol && !a.fired);
  const mine_tr = (desk?.tracked || []).filter((t) => t.symbol === d.symbol);
  const head = `<div class="meta"><span class="src">${esc([d.cap, d.band, d.sector].filter(Boolean).join(" · ") || "Stock")}</span>${d.otc ? `<span class="neg">OTC</span>` : ""}${q ? `<span class="ago">${ago(q.ts)} ago</span>` : ""}</div>
    <h2>${sym}${d.name ? ` <span class="tname">${esc(d.name)}</span>` : ""}</h2>
    ${q ? `<div class="tquote"><b class="num">${fmtPx(q.price)}</b><span class="num ${cls(q.chg_day)}">${pct(q.chg_day)}</span>${d.range?.low ? `<small class="faint num">day ${fmtPx(d.range.low)} – ${fmtPx(d.range.high)}</small>` : ""}</div>`
      : `<div class="sub" style="margin:6px 0 4px">No live price yet. ${watching ? "It's on your watchlist, so it's quoted from now on." : "Watching it adds it to the live quotes."}</div>`}
`;
  const acts = `<div class="acts">
    <button class="btn ghost small${watching ? " sel" : ""}" data-act="watch" data-s="${sym}">${watching ? "★ Watching" : "☆ Watch"}</button>
    <button class="btn ghost small" data-act="pa-form">🔔 Price alert</button>
    <button class="btn ghost small" data-act="track" data-s="${sym}" data-dir="long">▲ Track</button>
    <button class="btn ghost small" data-act="track" data-s="${sym}" data-dir="short">▼ Short</button>
    <button class="btn ghost small" data-act="share-t" data-s="${sym}" data-title="${esc(d.symbol + (d.name ? " · " + d.name : ""))}">Share ↗</button>
    <button class="btn ghost small" data-act="rule-menu">⚡ Smart alert</button>
    ${(desk?.muted || {})[d.symbol] > Date.now() / 1000 ? `<button class="btn ghost small sel" data-act="unmute" data-s="${sym}">🔕 Muted · unmute</button>` : `<button class="btn ghost small" data-act="mute" data-s="${sym}" data-h="4">🔕 Mute 4h</button><button class="btn ghost small" data-act="mute" data-s="${sym}" data-u="open">🔕 until the open</button>`}</div>
    ${dialRow(d.symbol)}
    <div class="panel glass padded paform" id="ruleForm" hidden><div class="sub" style="margin-bottom:8px">Push to this phone when…</div><div class="row">
      <button class="btn ghost small" data-act="rule-quick" data-k="headline" data-v="10" data-s="${sym}">any headline on ${sym}</button>
      <button class="btn ghost small" data-act="rule-quick" data-k="news_move" data-v="3" data-s="${sym}">a headline, then a 3% move</button>
      <button class="btn ghost small" data-act="rule-quick" data-k="volume" data-v="5" data-s="${sym}">5× normal volume</button>
      <button class="btn ghost small" data-act="rule-quick" data-k="vwap" data-d="above" data-s="${sym}">it crosses above VWAP</button>
      <button class="btn ghost small" data-act="rule-quick" data-k="vwap" data-d="below" data-s="${sym}">it loses VWAP</button>
      <button class="btn ghost small" data-act="rule-quick" data-k="gap" data-v="4" data-s="${sym}">it gaps 4%+ pre-market</button></div></div>
    <div class="panel glass padded paform" id="paForm" hidden>
      <div class="addrow"><select id="paKind"><option value="above">Above $</option><option value="below">Below $</option><option value="move">Moves ±% today</option><option value="trail">Falls % from high</option></select>
      <input id="paVal" class="key" inputmode="decimal" placeholder="${q ? q.price.toFixed(2) : "value"}"><button class="btn ghost small sel" data-act="pa-save" data-s="${sym}">Set</button></div>
      <label class="rearm"><input type="checkbox" id="paRearm"> every day (re-arms the next session after it fires)</label><div class="row sug" id="paSug"></div><div class="sub">Checked every 15 seconds against the live price; pushed to this phone only, once.</div></div>
    ${mine_pa.length || mine_tr.length ? `<div class="panel glass">${mine_pa.map((a) => `<div class="kv"><span>🔔 ${a.kind === "move" ? `±${a.value}% on the day` : a.kind === "trail" ? `falls ${a.value}% from its high` : `${a.kind} ${fmtPx(a.value)}`}</span><b><button class="x" data-act="pa-rm" data-s="${esc(a.id)}">✕</button></b></div>`).join("")}
      ${mine_tr.map((t) => `<div class="kv"><span>${t.direction === "long" ? "▲ Long" : "▼ Short"} from ${fmtPx(t.entry)} · ${ago(t.ts)} ago${levelsText(t)}</span><b class="num ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</b></div>`).join("")}</div>` : ""}`;
  const chart = (d.series || []).length >= 3 ? sparkline({ [d.symbol]: d.series }, -1) : "";
  const rel = d.rel, er = d.earnings;
  const ss = d.session;
  const sessLine = ss && (ss.vs_vwap != null || ss.vs_first5 != null) ? `<div class="rel">${ss.vs_vwap != null ? `<span>${ss.vs_vwap >= 0 ? "Above" : "Below"} VWAP <b class="num ${cls(ss.vs_vwap)}">${pct(ss.vs_vwap)}</b> <span class="faint">(${fmtPx(ss.vwap)})</span></span>` : ""}${ss.vs_first5 != null ? `<span>vs 9:35 <b class="num ${cls(ss.vs_first5)}">${pct(ss.vs_first5)}</b></span>` : ""}${tip("vwap")}</div>` : "";
  const relLine = rel && (rel.spy != null || rel.sector != null) ? `<div class="rel"><span>vs S&amp;P <b class="num ${cls(rel.vs_spy)}">${rel.vs_spy != null ? (rel.vs_spy > 0 ? "+" : "") + rel.vs_spy.toFixed(1) : "—"}</b></span>${rel.sector != null ? `<span>vs ${esc(rel.sector_name)} <b class="num ${cls(rel.vs_sector)}">${(rel.vs_sector > 0 ? "+" : "") + rel.vs_sector.toFixed(1)}</b></span>` : ""}${rel.read ? `<span class="rread">${esc(rel.read)}</span>` : ""}${tip("rel")}</div>` : "";
  const earnLine = er ? `<div class="sub gold" style="margin-top:6px">Reports ${esc(prettyDate(er.date))}${er.when ? ", " + esc(er.when) : ""} · ${inDays(er.in_days)}</div>` : "";
  const pr = d.peers || {};
  const peerBlock = (pr.rows || []).length ? `<h4>Peers · ${esc(pr.group || "")}</h4><div class="panel glass">
    ${pr.laggards?.length ? `<div class="sub" style="padding:12px 0 4px">${esc(d.symbol)} is ${pct(pr.me)}; these haven't moved yet: <b class="gold">${pr.laggards.map((x) => esc(x.symbol)).join(", ")}</b>. Where a sympathy move would come from, if it comes. ${tip("sympathy")}</div>` : ""}
    ${pr.rows.map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span><div class="grow"><div>${esc(x.name)}</div><div class="sub">${esc(x.cap)}${(pr.laggards || []).some((l) => l.symbol === x.symbol) ? ` · <span class="gold">not moved yet</span>` : ""}</div></div>${x.chg != null ? barHTML(x.chg) : ""}<span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("")}
    ${pr.avg != null ? `<div class="kv"><span>Peers on average</span><b class="num ${cls(pr.avg)}">${pct(pr.avg)}</b></div>` : ""}</div>` : "";
  const why = `<h4>Why it's moving</h4><div class="panel glass" id="whyBox">${(d.why || []).length ? d.why.map((x) => `<div class="li"><div class="grow"><a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a><div class="sub">${esc(outlet(x.source))} · ${esc(x.age)} ago</div></div></div>`).join("")
    : `<div class="li"><span class="muted grow">No headline on it since the last session.</span><button class="btn ghost small" data-act="why" data-s="${sym}">Find why now</button></div>`}</div>`;
  const note = (desk?.notes || {})[d.symbol];
  const noteBlock = `<div class="note glass" data-act="note" data-s="${sym}" role="button">${note ? `<span>📝 ${esc(note.text)}</span><small class="faint">${ago(note.ts)} ago · edit</small>` : `<span class="faint">📝 Add a note on ${sym}</span>`}</div>`;
  const ign = d.radar?.ignition, o = d.options;
  const tape = ign || d.radar?.change_pct != null || o ? `<h4>On the tape</h4><div class="panel glass">
    ${ign ? `<div class="kv"><span>⚡ ${ign.kind === "buildup" ? "Building" : "Ignition"} ${ago(ign.detected)} ago</span><b class="num ${cls(ign.move_pct)}">${pct(ign.move_pct)}${ign.burst ? ` · ${Math.round(ign.burst)}× vol` : ""}</b></div>` : ""}
    ${d.radar?.change_pct != null ? `<div class="kv"><span>Radar · ${money(d.radar.dollar_volume)} traded${d.radar.rvol ? ` · ${Math.round(d.radar.rvol)}× volume` : ""}</span><b class="num ${cls(d.radar.change_pct)}">${pct(d.radar.change_pct)}</b></div>` : ""}
    ${o ? `<div class="kv"><span>Options · ${esc(o.kind)} ${ago(o.detected)} ago</span><b class="num">calls ${money(o.call_premium)} · puts ${money(o.put_premium)}</b></div>
      ${(o.flow || []).slice(0, 3).map((f) => `<div class="kv"><span class="sub">${esc(f.label || f.contract || "")}</span><b class="num sub">${f.premium ? money(f.premium) : ""}</b></div>`).join("")}` : ""}</div>` : "";
  const al = (d.alerts || []).length ? `<h4>Foretape on ${sym}</h4><div class="panel glass">${d.alerts.slice(0, 12).map((a) => {
    const oc = a.outcome || {}, dir = a.direction === "up" ? `<span class="pos">▲</span>` : a.direction === "down" ? `<span class="neg">▼</span>` : "";
    const grade = oc.close != null ? `close ${pct(oc.close)}` : oc.h1 != null ? `1h ${pct(oc.h1)}` : "";
    return `<div class="li" data-id="${esc(a.id)}" role="button"><div class="grow"><div class="sub">${esc(a.severity)} · ${esc(KIND_LABEL[a.kind] || a.kind)} · ${ago(a.created)} ago</div><div style="font-weight:600">${/^[▲▼]/.test(a.title) ? "" : dir + " "}${esc(a.title)}</div></div>
      <span class="chg num ${cls(a.since)}" style="width:auto;min-width:64px">${a.since != null ? pct(a.since) : ""}${grade ? `<small class="rsess">${grade}</small>` : ""}</span></div>`;
  }).join("")}<div class="sub" style="padding:4px 0 12px">The % is the move since each alert, from its own reference price.</div></div>` : "";
  const ins = (d.insider || []).length ? `<h4>Insider buys</h4><div class="panel glass">${d.insider.map((x) => `<div class="li"><div class="grow">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a>` : esc(x.title)}<div class="sub">Form 4 · ${ago(x.detected)} ago</div></div></div>`).join("")}</div>` : "";
  const cal = (d.calendar || []).length ? `<h4>Coming up</h4><div class="panel glass">${d.calendar.map((e) => calRow(e, false)).join("")}</div>` : "";
  const news = (d.news || []).length ? `<h4>Headlines, 7 days</h4><div class="panel glass">${d.news.map((x) => `<div class="li"><div class="grow"><a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a><div class="sub">${esc(outlet(x.source))} · ${ago(x.detected)} ago${x.score != null ? ` · score ${Math.round(x.score)}` : ""}</div></div></div>`).join("")}</div>` : "";
  const facts = `<h4>Company</h4><div class="panel glass">
    ${d.market_cap ? `<div class="kv"><span>Market cap</span><b class="num">${esc(d.cap)} · ${esc(d.band)}</b></div>` : ""}
    ${d.industry || d.sector ? `<div class="kv"><span>Industry</span><b>${esc(d.industry || d.sector)}</b></div>` : ""}
    <div class="kv"><span>On watchlists</span><b class="num">${d.watchers} phone${d.watchers === 1 ? "" : "s"}</b></div></div>`;
  const implied = `<h4>What options price in ${tip("implied")}</h4><div class="panel glass" id="implied"><div class="li"><span class="muted">Reading the option chain…</span></div></div>
    <h4>Past earnings</h4><div class="panel glass" id="pastEarn"><div class="li"><span class="muted">Reading its last reports…</span></div></div>`;
  return [head + relLine + sessLine + earnLine, sinceBlock(lookCache[d.symbol]), acts, noteBlock, (d.series || []).length >= 3 ? `<h4>Today, with the levels</h4><div id="lvlChart">${levelChart(d)}</div><div class="sub lvlhint">Tap the chart at a price to set an alert there.</div>` : "", tallyBlock(d.tally), why, tape, implied, peerBlock, al, ins, cal, news, facts].filter(Boolean).join("");
}
let tickerReq = 0, tickerNow = null;
const impliedNow = {};
// the stock's session in dollars with its levels: the day's range, VWAP, the 9:35 price, the option
// walls, and this phone's own entry, stop, target and price-alert lines
function levelChart(d) {
  const pts = d.series || [];
  if (pts.length < 3) return "";
  const px = d.quote?.price || pts[pts.length - 1][1], ss = d.session || {}, pz = impliedNow[d.symbol]?.positioning || {};
  const lines = [];
  const add = (v, label, color, dash) => { if (v && Math.abs(v / px - 1) < 0.15) lines.push({ v: +v, label, color, dash }); };
  add(ss.s_high || d.range?.high, "day high", "rgba(255,255,255,.35)", "2 4"); add(ss.s_low || d.range?.low, "day low", "rgba(255,255,255,.35)", "2 4");
  add(ss.vwap, "VWAP", "#9FC6FF", ""); add(ss.first5, "9:35", "rgba(255,217,160,.6)", "4 4");
  add(pz.call_wall?.strike, "call wall", "#8CEBC4", "6 4"); add(pz.put_wall?.strike, "put wall", "#FF9E86", "6 4");
  (desk?.tracked || []).filter((t) => t.symbol === d.symbol).forEach((t) => { add(t.entry, "your entry", "#FFD9A0", ""); add(t.stop, "your stop", "#FF9E86", ""); add(t.target, "your target", "#8CEBC4", ""); });
  (desk?.price_alerts || []).filter((a) => a.symbol === d.symbol && !a.fired && (a.kind === "above" || a.kind === "below")).forEach((a) => add(a.value, `🔔 ${a.kind}`, "#FFD9A0", "1 3"));
  const vals = [...pts.map((p) => p[1]), ...lines.map((l) => l.v)], lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const W = 560, H = 190, padL = 6, padR = 112, t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const X = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (W - padL - padR), Y = (v) => 10 + (1 - (v - lo) / span) * (H - 20);
  const path = pts.map(([t, p], i) => (i ? "L" : "M") + X(t).toFixed(1) + " " + Y(p).toFixed(1)).join(" ");
  // labels: in line order, nudged apart so close levels stay readable
  lines.sort((a, b) => Y(a.v) - Y(b.v));
  let lastY = -99;
  lines.forEach((l) => { l.ly = Math.max(Y(l.v) + 3.5, lastY + 11); lastY = l.ly; });
  const up = pts[pts.length - 1][1] >= pts[0][1];
  return `<svg viewBox="0 0 ${W} ${H}" class="lvl" role="img" aria-label="${esc(d.symbol)} today with its levels. Tap a level to set a price alert there." data-lo="${lo}" data-span="${span}" data-h="${H}" data-w="${W - padR}" data-px="${px}" data-s="${esc(d.symbol)}">
    ${lines.map((l) => `<line x1="${padL}" x2="${W - padR}" y1="${Y(l.v).toFixed(1)}" y2="${Y(l.v).toFixed(1)}" stroke="${l.color}" stroke-dasharray="${l.dash}"/><text x="${W - padR + 4}" y="${l.ly.toFixed(1)}" fill="${l.color}" font-size="10" font-family="Inter">${esc(l.label)} ${l.v >= 1000 ? Math.round(l.v) : l.v.toFixed(2)}</text>`).join("")}
    <path d="${path}" fill="none" stroke="${up ? "var(--up)" : "var(--down)"}" stroke-width="2"/>
    <circle cx="${X(pts[pts.length - 1][0]).toFixed(1)}" cy="${Y(pts[pts.length - 1][1]).toFixed(1)}" r="3.5" fill="${up ? "var(--up)" : "var(--down)"}"/></svg>`;
}
// one-tap lines for a price alert: the day's high and low, the option walls, the implied-move bounds
function renderSuggest() {
  const d = tickerNow, im = impliedNow[d?.symbol], el = $("paSug");
  if (!d || !el) return;
  const px = d.quote?.price, out = [];
  const add = (label, v) => { if (v && px && Math.abs(v / px - 1) > 0.002) out.push(`<button class="btn ghost small" data-act="pa-sug" data-k="${v > px ? "above" : "below"}" data-v="${(+v).toFixed(2)}">${label} ${fmtPx(v)}</button>`); };
  add("Day high", d.range?.high); add("Day low", d.range?.low);
  const pz = im?.positioning;
  add("Call wall", pz?.call_wall?.strike); add("Put wall", pz?.put_wall?.strike);
  if (im?.rows?.[0]) { add(`+${im.rows[0].move_pct.toFixed(1)}% implied`, im.rows[0].high); add(`−${im.rows[0].move_pct.toFixed(1)}% implied`, im.rows[0].low); }
  el.innerHTML = out.join("");
}
async function openTicker(sym) {
  sym = String(sym || "").trim().toUpperCase().replace(/^\$/, "");
  if (!sym) return;
  $("toast").classList.remove("show");
  const n = ++tickerReq;
  const hit = tickerCache.get(sym);
  const visit = (d) => api("/api/desk", { method: "POST", body: JSON.stringify({ action: "viewed", symbol: d.symbol }) }).catch(() => {});  // after the page is built, so it reads the visit before this one
  if (hit && Date.now() - hit.at < 180e3) {  // prefetched: show it now, then bring it up to date in place
    lookCache[hit.d.symbol] = hit.d.since_look || null;
    tickerNow = hit.d; showSheet(tickerHTML(hit.d)); $("sheet").dataset.sym = hit.d.symbol; noteRecent(hit.d);
    visit(hit.d).then(() => tickerCache.delete(sym));  // its "since" is spent: the next visit fetches a fresh one
    loadImplied(hit.d.symbol).finally(() => loadEarnings(hit.d.symbol));
    api("/api/ticker/" + encodeURIComponent(sym)).then((d) => {
      if (n !== tickerReq || $("sheet").dataset.sym !== d.symbol) return;
      tickerNow = d;
      const y = $("sheet").scrollTop, imp = $("implied")?.innerHTML, pe = $("pastEarn")?.innerHTML;
      $("sheetBody").innerHTML = tickerHTML(d);
      if (imp && $("implied")) $("implied").innerHTML = imp;
      if (pe && $("pastEarn")) $("pastEarn").innerHTML = pe;
      $("sheet").scrollTop = y;
    }).catch(() => {});
    return;
  }
  let d;
  try { d = await api("/api/ticker/" + encodeURIComponent(sym)); } catch (_) { toast(`No page for ${sym}`); return; }
  if (n !== tickerReq) return;  // a newer tap won
  lookCache[d.symbol] = d.since_look || null;
  tickerNow = d;
  showSheet(tickerHTML(d));
  $("sheet").dataset.sym = d.symbol;
  noteRecent(d);
  visit(d);
  loadImplied(d.symbol).finally(() => loadEarnings(d.symbol));
}
// stock pages open instantly for the stocks you watch: fetched quietly in the background, kept 3 minutes
const tickerCache = new Map();
let prefetching = false;
async function prefetchWatch() {
  if (prefetching || document.hidden || navigator.connection?.saveData) return;
  prefetching = true;
  try {
    for (const w of (desk?.watchlist || []).slice(0, 8)) {
      const c = tickerCache.get(w.symbol);
      if (c && Date.now() - c.at < 120e3) continue;
      try { tickerCache.set(w.symbol, { d: await api("/api/ticker/" + encodeURIComponent(w.symbol)), at: Date.now() }); } catch (_) { break; }
      await new Promise((r) => setTimeout(r, 400));  // gently: one at a time
    }
  } finally { prefetching = false; }
}
// recently viewed stocks: under the search box, one tap back to them
function noteRecent(d) {
  const r = (store.get("ft_recent") || "").split(",").filter((x) => x && x !== d.symbol);
  store.set("ft_recent", [d.symbol, ...r].slice(0, 10).join(","));
  renderRecent();
}
function renderRecent() {
  const r = (store.get("ft_recent") || "").split(",").filter(Boolean), el = $("recentRow");
  if (!el) return;
  el.hidden = !r.length || $("q").value.trim().length >= 2;
  el.innerHTML = r.length ? `<span class="sub">Recent</span>${r.map((x) => `<button class="tk" data-tk="${esc(x)}">${esc(x)}</button>`).join("")}<button class="x" data-act="recent-clear" aria-label="Clear recent stocks">✕</button>` : "";
}
// the last reports: EPS vs the estimate and the two-day move around each, against what options price now
async function loadEarnings(sym) {
  let r;
  try { r = await api("/api/earnings/" + encodeURIComponent(sym)); } catch (_) { r = { available: false, why: "Couldn't read it." }; }
  const el = $("pastEarn");
  if (!el || $("sheet").dataset.sym !== sym) return;
  if (!r.available) { el.innerHTML = `<div class="li"><span class="muted">${esc(r.why || "Not available.")}</span></div>`; return; }
  const im = r.implied;
  el.innerHTML = (r.avg_abs_move != null ? `<div class="kv"><span>Average move on a report (2 days)</span><b class="num">±${r.avg_abs_move.toFixed(1)}% · beat ${r.beat} of ${r.reported}</b></div>` : "")
    + (im ? `<div class="kv"><span>Options price for the next one</span><b class="num">±${im.move_pct.toFixed(1)}% <span class="${im.ratio > 1.15 ? "neg" : im.ratio < 0.85 ? "pos" : ""}">(${im.ratio}× its usual move)</span></b></div>` : "")
    + r.rows.map((x) => `<div class="kv"><span>${esc(x.quarter || x.date)} <span class="sub">${esc(prettyDate(x.date))}</span></span><b class="num">${x.surprise_pct != null ? `<span class="${cls(x.surprise_pct)}">EPS ${x.surprise_pct > 0 ? "+" : ""}${x.surprise_pct.toFixed(1)}% vs est</span> · ` : ""}<span class="${cls(x.move_pct)}">${x.move_pct != null ? pct(x.move_pct) : "—"}</span></b></div>`).join("")
    + `<div class="sub" style="padding:4px 0 12px">The move is the close before the report day to the close after it. Nasdaq's earnings table, Yahoo daily closes.${im ? " Above 1× means options price a bigger move than usual." : ""}</div>`;
}
// the at-the-money straddle by expiry: the move the options market is paying for
async function loadImplied(sym) {  // returns once the panel is filled
  let r;
  try { r = await api("/api/implied/" + encodeURIComponent(sym)); } catch (_) { r = { available: false, why: "Couldn't read it." }; }
  const el = $("implied");
  if (!el || $("sheet").dataset.sym !== sym) return;
  if (!r.available) { el.innerHTML = `<div class="li"><span class="muted">${esc(r.why || "Not available.")}</span></div>`; return; }
  impliedNow[sym] = r;
  if (tickerNow?.symbol === sym && $("lvlChart")) $("lvlChart").innerHTML = levelChart(tickerNow);
  const dt = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  el.innerHTML = r.rows.map((x) => `<div class="kv"><span>By ${esc(dt(x.expiry))} <span class="sub">${x.dte}d${x.earnings ? ` · <span class="gold">earnings inside</span>` : ""}</span></span>
      <b class="num">±${x.move_pct.toFixed(1)}% <span class="sub">${fmtPx(x.low)} – ${fmtPx(x.high)}</span></b></div>`).join("")
    + positioningRows(r.positioning)
    + `<div class="sub" style="padding:4px 0 12px">The at-the-money straddle (the $${esc(String(r.rows[0].strike))} call + put)${r.iv30 ? `; 30-day implied vol ${r.iv30}%` : ""}. ${r.rows.some((x) => !x.quoted) ? "Some from last trades, not live quotes. " : ""}${esc(r.source || "")}.</div>`;
}
// where the options market is leaning: today's put/call volume, the open-interest walls, max pain
function positioningRows(p) {
  if (!p) return "";
  const dt = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return [
    p.pc_volume != null ? `<div class="kv"><span>Put/call volume ${tip("pcratio")} <span class="sub">${(p.put_volume || 0).toLocaleString()} puts · ${(p.call_volume || 0).toLocaleString()} calls</span></span><b class="num ${p.pc_volume > 1.2 ? "neg" : p.pc_volume < 0.6 ? "pos" : ""}">${p.pc_volume.toFixed(2)}</b></div>` : "",
    p.call_wall ? `<div class="kv"><span>Call wall ${tip("callwall")} <span class="sub">most call open interest above, ${esc(dt(p.expiry))}</span></span><b class="num">$${esc(String(p.call_wall.strike))} <span class="sub">${p.call_wall.oi.toLocaleString()} OI</span></b></div>` : "",
    p.put_wall ? `<div class="kv"><span>Put wall ${tip("putwall")} <span class="sub">most put open interest below</span></span><b class="num">$${esc(String(p.put_wall.strike))} <span class="sub">${p.put_wall.oi.toLocaleString()} OI</span></b></div>` : "",
    p.max_pain != null ? `<div class="kv"><span>Max pain ${tip("maxpain")}, ${esc(dt(p.expiry))} <span class="sub">where holders collect least</span></span><b class="num">$${esc(String(p.max_pain))}</b></div>` : "",
  ].join("");
}
function levelsText(t) {
  const bit = (w) => t[w] != null ? ` · ${w} ${fmtPx(t[w])}${t[w + "_hit"] ? " ✓" : ""}` : "";
  return bit("stop") + bit("target");
}
async function refreshTickerSheet() {
  const sym = $("sheet").dataset.sym;
  if (!sheetOpen || !sym) return;
  try { const d = await api("/api/ticker/" + encodeURIComponent(sym)); const y = $("sheet").scrollTop; $("sheetBody").innerHTML = tickerHTML(d); $("sheet").scrollTop = y; } catch (_) { /* keep what's shown */ }
}
async function share(title, url) {
  try { if (navigator.share) { await navigator.share({ title, text: title, url }); return; } } catch (e) { if (e?.name === "AbortError") return; }
  try { await navigator.clipboard.writeText(title + "\n" + url); toast("Link copied"); } catch (_) { toast(url); }
}
const syms = (txt) => String(txt || "").toUpperCase().split(/[\s,;]+/).map((s) => s.replace(/^\$/, "")).filter((s) => /^[A-Z][A-Z0-9.-]{0,9}$/.test(s));
const DIAL_TEXT = { high: "HIGH alerts only", all: "pushes everything", off: "never pushes" };
const dialBtns = (sym, lv) => [["default", "Default"], ["high", "HIGH only"], ["all", "Everything"], ["off", "Off"]].map(([k, l]) => `<button class="btn ghost small${(lv || "default") === k ? " sel" : ""}" data-act="dial" data-s="${esc(sym)}" data-lv="${k}">${l}</button>`).join("");
// a stock's own push level, on its page
const dialRow = (sym) => `<div class="dialrow"><div class="sub">Pushes for ${esc(sym)} ${tip("dial")}</div><div class="row">${dialBtns(sym, (desk?.stock_push || {})[sym])}</div></div>`;
let lookCache = {};  // symbol -> "since you last looked", kept while its page is open (the visit itself resets it on the server)
const sinceBlock = (sl) => !sl ? "" : `<div class="since glass"><div class="sub">Since you last looked · ${ago(sl.ts)} ago</div>
  ${sl.quiet ? `<div>Nothing new on it.</div>` : `<div class="srow">${sl.move_pct != null ? `<b class="num ${cls(sl.move_pct)}">${pct(sl.move_pct)}</b>` : ""}${sl.alerts ? `<span>${sl.alerts} alert${sl.alerts === 1 ? "" : "s"}</span>` : ""}${sl.headlines ? `<span>${sl.headlines} headline${sl.headlines === 1 ? "" : "s"}</span>` : ""}</div>
  ${sl.top.map((a) => `<div class="li" data-id="${esc(a.id)}" role="button"><div class="grow"><div class="sub">${esc(a.severity)} · ${ago(a.created)} ago</div><div style="font-weight:600">${esc(a.title)}</div></div></div>`).join("")}`}</div>`;
// "remind me" on an alert: it comes back as a push
function remindRow(a) {
  const r = (desk?.reminders || []).find((x) => x.alert_id === a.id);
  if (r) return `<div class="remind"><span class="sub">⏰ Reminder set for ${esc(new Date(r.due * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</span><button class="btn ghost small" data-act="remind-clear" data-s="${esc(r.id)}">Cancel</button></div>`;
  return `<div class="sub" style="margin-top:12px">Remind me about this</div><div class="acts">${[["1h", "in 1 hour"], ["3h", "in 3 hours"], ["close", "at the close"], ["open", "at the open"]].map(([w, l]) => `<button class="btn ghost small" data-act="remind" data-s="${esc(a.id)}" data-w="${w}">⏰ ${l}</button>`).join("")}</div>`;
}
// the gates between an alert and this phone, one line each
const gatesHTML = (steps) => `<div class="wsteps">${steps.map((st) => `<div class="wstep">${st.ok === true ? `<i class="ok">✓</i>` : st.ok === false ? `<i class="no">✕</i>` : `<i class="mid">•</i>`}<span>${esc(st.text)}</span></div>`).join("")}</div>`;
async function onAct(el) {
  const a = el.dataset.act, s = el.dataset.s;
  if (a === "watch") { await deskEdit(mine.has(s) ? "watch_remove" : "watch_add", { symbol: s }); toast(mine.has(s) ? `★ ${s} is on your watchlist` : `${s} removed`); refreshTickerSheet(); }
  else if (a === "wl-add") { const v = prompt("Add to your watchlist (tickers, comma separated):"); if (v && syms(v).length) { await deskEdit("watch_add", { symbols: syms(v) }); } }
  else if (a === "wl-in") { const v = syms($("wlIn").value); if (v.length) { if (await deskEdit("watch_add", { symbols: v })) $("wlIn").value = ""; } }
  else if (a === "wl-rm") { const g = (desk.groups || {})[s]; if (await deskEdit("watch_remove", { symbol: s })) toastUndo(`${s} removed`, async () => { await deskEdit("watch_add", { symbol: s }); if (g) await deskEdit("watch_group", { symbol: s, group: g }); }); }
  else if (a === "undo") { const fn = pendingUndo; pendingUndo = null; $("toast").classList.remove("show"); if (fn) { await fn(); toast("Restored"); } }
  else if (a === "pa-form") { const f = $("paForm"); f.hidden = !f.hidden; if (!f.hidden) { renderSuggest(); $("paVal").focus(); } }
  else if (a === "pa-sug") { $("paKind").value = el.dataset.k; $("paVal").value = el.dataset.v; }
  else if (a === "term") toast(GLOSSARY[s] || s, null, 9000);
  else if (a === "sort") { tapeSort = tapeSort === "best" ? "new" : "best"; store.set("ft.sort", tapeSort); renderTape(); }
  else if (a === "ob-tog") el.classList.toggle("sel");
  else if (a === "ob-style") { el.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("sel", b === el)); }
  else if (a === "ob-skip") { store.set("ft_onboarded", "1"); closeSheet(); }
  else if (a === "onboard") openOnboard();
  else if (a === "ob-save") saveOnboard();
  else if (a === "pa-save") {
    const kind = $("paKind").value, value = parseFloat($("paVal").value), rearm = $("paRearm")?.checked;
    if (!(value > 0)) { toast("Enter a price (or a % for a move)"); return; }
    if (await deskEdit("alert_add", { symbol: s, kind, value, rearm })) { toast(`🔔 Set: ${s} ${kind === "move" ? `moves ±${value}%` : kind === "trail" ? `falls ${value}% from its high` : `${kind} $${value}`}`); refreshTickerSheet(); }
  }
  else if (a === "pa-rm") {
    const old = (desk.price_alerts || []).find((x) => x.id === s);
    await deskEdit("alert_remove", { id: s }); refreshTickerSheet();
    if (old && !old.fired) toastUndo("Price alert removed", () => deskEdit("alert_add", { symbol: old.symbol, kind: old.kind, value: old.value, rearm: old.rearm }));
  }
  else if (a === "track") {
    const r = await deskEdit("track_add", { symbol: s, direction: el.dataset.dir, alert_id: el.dataset.aid || "" });
    if (r) { const t = r.tracked[r.tracked.length - 1]; toast(`${el.dataset.dir === "long" ? "▲" : "▼"} Tracking ${s} from ${fmtPx(t?.entry)} · on your desk`); refreshTickerSheet(); }
  }
  else if (a === "tr-close") {
    const lesson = prompt("Closing at the live price. A lesson for your journal? (optional)", "");
    if (lesson == null) return;
    const r = await deskEdit("track_close", { id: s, lesson }); const c = r?.closed?.[0]; if (c) toast(`Closed ${c.symbol} ${pct(c.pnl_pct)}`);
  }
  else if (a === "tr-journal") {
    const t = [...desk.tracked, ...desk.closed].find((x) => x.id === s);
    if (!t) return;
    const v = prompt(`${t.symbol}: your thesis or notes. Add #tags (#earnings #options #momentum…) to read your record by tag.`, t.note || "");
    if (v != null && await deskEdit("track_journal", { id: s, note: v })) toast("Journal saved");
  }
  else if (a === "kw-in") { const v = $("kwIn").value.trim(); if (v && await deskEdit("kw_add", { keyword: v })) $("kwIn").value = ""; }
  else if (a === "kw-rm") await deskEdit("kw_remove", { keyword: s });
  else if (a === "mute") {
    const u = el.dataset.u;
    if (await deskEdit("mute", u ? { symbol: s, until: u } : { symbol: s, hours: +el.dataset.h || 4 })) { toast(`🔕 ${s} muted on this phone ${u ? "until the " + u : "for " + (el.dataset.h || 4) + "h"}`); refreshTickerSheet(); }
  }
  else if (a === "unmute") { await deskEdit("unmute", { symbol: s }); refreshTickerSheet(); }
  else if (a === "ics") { location.href = icsUrl("webcal"); }
  else if (a === "ics-copy") { try { await navigator.clipboard.writeText(icsUrl("https")); toast("Calendar link copied. In Google Calendar: Other calendars → From URL."); } catch (_) { toast(icsUrl("https")); } }
  else if (a === "sector") openSector(s);
  else if (a === "save" || a === "unsave") { if (await deskEdit(a, { id: s })) { toast(a === "save" ? "★ Saved to your desk" : "Removed from saved"); openSheet(s); } }
  else if (a === "snooze") { const on = (desk.snooze || {})[s] > Date.now() / 1000; await deskEdit("snooze", { cat: s, hours: on ? 0 : 1 }); }
  else if (a === "export") exportDesk(el.dataset.f);
  else if (a === "rule-menu") { const f = $("ruleForm"); f.hidden = !f.hidden; }
  else if (a === "rule-quick") { if (await deskEdit("rule_add", { kind: el.dataset.k, symbol: s, value: el.dataset.v, dir: el.dataset.d })) { toast("⚡ Smart alert set: it pushes to this phone"); $("ruleForm").hidden = true; } }
  else if (a === "rule-add") {
    let kind = $("ruleKind").value, dir = "";
    if (kind.startsWith("vwap_")) { dir = kind.slice(5); kind = "vwap"; }
    if (await deskEdit("rule_add", { kind, dir, symbol: $("ruleSym").value.trim() || "*", value: $("ruleVal").value.trim() })) toast("⚡ Smart alert added");
  }
  else if (a === "rule-rm") { const r = (desk.rules || []).find((x) => x.id === s); if (await deskEdit("rule_remove", { id: s }) && r) toastUndo("Smart alert removed", () => deskEdit("rule_add", { kind: r.kind, symbol: r.symbol, value: r.value, dir: r.dir })); }
  else if (a === "wsort") { store.set("ft.wsort", s); renderDesk(); }
  else if (a === "cat-off") { if (await deskEdit("prefs", { cats: { [s]: false } })) toast("Turned off for this phone (Desk → What reaches you to turn back on)"); }
  else if (a === "links") openLinks();
  else if (a === "expand") { expanded.add(s); renderTape(); }
  else if (a === "neg-in") { const v = $("negIn").value.trim(); if (v && await deskEdit("neg_add", { keyword: v })) $("negIn").value = ""; }
  else if (a === "neg-rm") await deskEdit("neg_remove", { keyword: s });
  else if (a === "src-mute") { const on = !(desk.prefs.mute_sources || []).includes(s); if (await deskEdit("prefs", { mute_source: s, on })) { toast(on ? `${outlet(s)} won't push to this phone` : `${outlet(s)} unmuted`); loadRecord(); } }
  else if (a === "portalert") await deskEdit("prefs", { port_alert: +s });
  else if (a === "hold-import") { const r = await deskEdit("hold_import", { text: $("hPaste").value }); if (r) { toast(`Imported ${r.imported} holding${r.imported === 1 ? "" : "s"}`); } }
  else if (a === "sfilter") { searchF[el.dataset.k] = s; renderFilters(); runSearch(); }
  else if (a === "copy") { try { await navigator.clipboard.writeText(s); toast("Copied"); } catch (_) { toast(s); } }
  else if (a === "hold-add") {
    const sym = syms($("hSym").value)[0];
    if (!sym) { toast("Enter a ticker"); return; }
    if (await deskEdit("hold_set", { symbol: sym, shares: $("hShares").value.trim(), cost: $("hCost").value.trim() })) toast(`${sym} added to your portfolio`);
  }
  else if (a === "hold-rm") { const h = (desk.holdings || {})[s]; if (await deskEdit("hold_set", { symbol: s, shares: 0 }) && h) toastUndo(`${s} removed from your portfolio`, () => deskEdit("hold_set", { symbol: s, shares: h.shares, cost: h.cost ?? "" })); }
  else if (a === "follow") { const on = !(desk.follow?.[el.dataset.k] || []).includes(s); await deskEdit("follow", { kind: el.dataset.k, name: s, on }); }
  else if (a === "rule-prev") previewRule();
  else if (a === "share-list") await deskEdit("share_list", {});
  else if (a === "import-list") { const code = $("listIn").value.trim(); if (code && await deskEdit("import_list", { code })) toast("Stocks added to your watchlist"); }
  else if (a === "glossary") openGlossary();
  else if (a === "tz") { store.set("ft_tz", localTimes() ? "" : "local"); renderDesk(); renderTop(); toast(localTimes() ? "Times now in your time zone" : "Times now in ET"); }
  else if (a === "display") {
    const flip = (k) => store.set(k, store.get(k) === "1" ? "" : "1");
    if (s === "text") store.set("ft_text", store.get("ft_text") === "big" ? "" : "big");
    else if (s === "light") flip("ft_light");
    else if (s === "compact") flip("ft_compact");
    else if (s === "beginner") { flip("ft_beginner"); renderTape(); }
    else if (s === "chime") { flip("ft_chime"); if (store.get("ft_chime") === "1") chime("HIGH"); }
    else if (s === "haptic") { flip("ft_haptic"); if (store.get("ft_haptic") === "1") navigator.vibrate?.(30); }
    else if (s === "onestock") { flip("ft_onestock"); navigator.serviceWorker?.ready.then((r) => r.active?.postMessage({ type: "onestock", on: store.get("ft_onestock") === "1" })); }
    else { store.set("ft_clear", s === "clear" ? "1" : ""); if (s === "cinema") store.set("ft_light", ""); }
    applyDisplay(); renderDesk();
  }
  else if (a === "automove") { if (await deskEdit("prefs", { auto_move: +s })) toast(+s ? `New stocks you watch get a ±${s}% move alert` : "Off"); }
  else if (a === "fw-off") await deskEdit("prefs", { forward: null });
  else if (a === "fw-save") {
    const kind = $("fwKind").value, va = $("fwA").value.trim(), vb = $("fwB").value.trim();
    const fw = kind === "discord" ? { kind, url: va } : kind === "telegram" ? { kind, token: va, chat: vb } : { kind, topic: va.replace(/^https?:\/\/ntfy\.sh\//, "") };
    if (await deskEdit("prefs", { forward: fw })) toast("Forwarding on: every push to this phone is copied there");
  }
  else if (a === "why") {
    el.disabled = true; el.textContent = "Searching filings and news…";
    let r;
    try { r = await api("/api/why/" + encodeURIComponent(s), { method: "POST", body: "{}" }); } catch (e) { r = { available: false, why: e.message }; }
    const box = $("whyBox");
    if (!box) return;
    box.innerHTML = !r.available || r.busy ? `<div class="li"><span class="muted">${esc(r.why || "Not available.")}</span></div>`
      : r.found.length ? r.found.map((x) => `<div class="li"><div class="grow"><a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a><div class="sub">${esc(outlet(x.source))} · score ${x.score}${x.published ? ` · ${ago(x.published)} ago` : ""}</div></div></div>`).join("")
      : `<div class="li"><span class="muted">Searched ${esc((r.searched || []).join(" and "))}: nothing on it since the last session. A move with no news is its own signal.</span></div>`;
  }
  else if (a === "note") {
    const cur = (desk?.notes || {})[s]?.text || "";
    const v = prompt(`Your note on ${s} (this phone only; empty removes it):`, cur);
    if (v != null && await deskEdit("note", { symbol: s, text: v })) refreshTickerSheet();
  }
  else if (a === "wl-group") {
    const v = prompt(`Group for ${s} (e.g. Earnings plays, Semis; empty: no group):`, (desk.groups || {})[s] || "");
    if (v != null) await deskEdit("watch_group", { symbol: s, group: v });
  }
  else if (a === "vote") {
    if (await deskEdit("vote", { id: s, v: +el.dataset.v })) { toast(+el.dataset.v ? "Thanks: counted" : "Vote removed"); openSheet(s); }
  }
  else if (a === "digest") await deskEdit("prefs", { digest: +s });
  else if (a === "flag") await deskEdit("prefs", { [s]: ["skip_fades", "close_recap", "tomorrow", "followups"].includes(s) ? !desk.prefs[s] : desk.prefs[s] === false });
  else if (a === "xfer-out") await deskEdit("transfer_out", {});
  else if (a === "xfer-in") {
    const code = $("xferIn").value.trim();
    if (code && confirm("Replace this phone's desk with the one from the other phone?") && await deskEdit("transfer_in", { code })) toast("Desk copied to this phone");
  }
  else if (a === "cat") { const on = desk.prefs.cats[s] !== false; await deskEdit("prefs", { cats: { [s]: !on } }); }
  else if (a === "wpush") await deskEdit("prefs", { watch_push: !desk.prefs.watch_push });
  else if (a === "floor") await deskEdit("prefs", { min_sev: s || null });
  else if (a === "eod") await deskEdit("prefs", { eod: desk.prefs.eod === false });
  else if (a === "tr-levels") {
    const t = desk.tracked.find((x) => x.id === s);
    if (!t) return;
    const v = prompt(`${t.symbol} ${t.direction} from $${t.entry}: stop and target, comma separated (blank clears). A ${t.direction === "long" ? "long's stop goes under" : "short's stop goes over"} the entry.`, [t.stop ?? "", t.target ?? ""].join(", "));
    if (v == null) return;
    const [stop, target] = v.split(",").map((x) => x.trim().replace(/^\$/, ""));
    if (await deskEdit("track_levels", { id: s, stop: stop || null, target: target || null })) toast(`${t.symbol}: levels set. You'll get a push when one is reached.`);
  }
  else if (a === "away-x") { $("away").hidden = true; }
  else if (a === "quiet") await deskEdit("prefs", { quiet: { start: $("qStart").value, end: $("qEnd").value } });
  else if (a === "quiet-off") await deskEdit("prefs", { quiet: null });
  else if (a === "share-a") share(el.dataset.title || "Foretape alert", location.origin + "/app/#a=" + s);
  else if (a === "share-t") share(el.dataset.title || s, location.origin + "/app/#t=" + s);
  else if (a === "search-clear") { $("q").value = ""; $("searchRes").innerHTML = ""; renderRecent(); }
  else if (a === "close") closeSheet();
  else if (a === "dial") { if (await deskEdit("stock_push", { symbol: s, level: el.dataset.lv })) { toast(el.dataset.lv === "default" ? `${s}: its normal push settings` : `📲 ${s}: ${DIAL_TEXT[el.dataset.lv]}`); refreshTickerSheet(); } }
  else if (a === "focus") {
    const r = await deskEdit("focus", el.dataset.u ? { until: el.dataset.u } : { minutes: +el.dataset.m });
    if (r) toast(r.focus ? `🎯 Focus on until ${new Date(r.focus * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: only CRITICAL comes through` : "Focus mode ended");
  }
  else if (a === "burst") { if (await deskEdit("prefs", { burst: +s })) toast(+s ? `After ${s} pushes in 10 minutes, the rest fold into one summary` : "Burst protection is off"); }
  else if (a === "remind") { const r = await deskEdit("remind", { id: s, when: el.dataset.w }); if (r) { const m = r.reminders.find((x) => x.alert_id === s); toast(`⏰ I'll remind you ${new Date(m.due * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`); if (sheetAlert?.id === s) { const h = el.closest(".acts"); if (h) { h.previousElementSibling?.remove(); h.outerHTML = remindRow(sheetAlert); } } } }
  else if (a === "remind-clear") { if (await deskEdit("remind_clear", { id: s })) { toast("Reminder cancelled"); if (sheetAlert) { const b = el.closest(".remind"); if (b) b.outerHTML = remindRow(sheetAlert); } } }
  else if (a === "whatif") whatIf();
  else if (a === "restore") $("restoreFile").click();
  else if (a === "why-push") whyPush(s);
  else if (a === "push-fix") {
    const fx = JSON.parse(el.dataset.fix || "{}");
    const again = () => { if (el.dataset.aid) whyPush(el.dataset.aid); else if ($("wiOut")) whatIf(); };
    if (fx.app === "push-on") { if (await pushOn()) again(); return; }
    if (fx.action && await deskEdit(fx.action, fx.body || {})) { toast("Done: " + fx.label); again(); }
  }
  else if (a === "copy-a") copyAlert(sheetAlert);
  else if (a === "push-log") openPushLog();
  else if (a === "health") openHealth();
  else if (a === "recent-clear") { store.set("ft_recent", ""); renderRecent(); }
  else if (a === "palette") Palette.show();
  else if (a === "list-link") { const c = desk?.share?.code; if (c) share(`${desk.share.n} stocks on my Foretape watchlist`, location.origin + "/app/#list=" + c.slice(0, 4) + "-" + c.slice(4)); }
  else if (a === "list-add") {
    const r = await deskEdit("import_list", { code: s });
    if (r) { closeSheet(); toast(r.imported ? `Added ${r.imported} stock${r.imported === 1 ? "" : "s"} to your watchlist` : "You already watch all of them"); go("desk"); }
  }
  else if (a === "lvl-set") { const k = el.dataset.k, v = +el.dataset.v; if (await deskEdit("alert_add", { symbol: s, kind: k, value: v })) { toast(`🔔 Set: ${s} ${k} ${fmtPx(v)}`); refreshTickerSheet(); } }
}

// ------------------------------------------------------------------ search: tickers, companies, alerts, headlines
let qTimer = 0;
const searchF = { days: "30", sev: "", kind: "" };
function renderFilters() {
  const chip = (k, v, l) => `<button class="btn ghost small${searchF[k] === v ? " sel" : ""}" data-act="sfilter" data-k="${k}" data-s="${v}">${l}</button>`;
  $("sFilters").innerHTML = [chip("days", "1", "Today"), chip("days", "7", "7 days"), chip("days", "30", "30 days"), chip("sev", "", "Any"), chip("sev", "HIGH", "HIGH+"),
    chip("kind", "", "All kinds"), chip("kind", "news", "Headlines"), chip("kind", "options", "Options"), chip("kind", "radar", "Radar"), chip("kind", "insider", "Insider"), chip("kind", "confluence", "Confluence")].join("");
}
async function runSearch() {
  const q = $("q").value.trim();
  if (q.length < 2) { $("searchRes").innerHTML = ""; return; }
  let r;
  try { r = await api("/api/search?q=" + encodeURIComponent(q) + `&days=${searchF.days}&sev=${searchF.sev}&kind=${searchF.kind}`); } catch (_) { return; }
  if ($("q").value.trim() !== q) return;  // typed on
  const bare = syms(q).length === 1 && q.replace(/^\$/, "").length <= 6 ? syms(q)[0] : "";
  const co = r.companies.map((c) => `<div class="li" data-tk="${esc(c.symbol)}" role="button"><span class="sym" style="width:64px">${esc(c.symbol)}</span><div class="grow"><div style="font-weight:600">${esc(c.name)}</div><div class="sub">${esc(c.cap || "")}</div></div><span class="faint">›</span></div>`);
  if (bare && !r.companies.some((c) => c.symbol === bare)) co.push(`<div class="li" data-tk="${esc(bare)}" role="button"><span class="sym" style="width:64px">${esc(bare)}</span><div class="grow"><div class="sub">Open its page</div></div><span class="faint">›</span></div>`);
  const al = r.alerts.slice(0, 8).map((a) => `<div class="li" data-id="${esc(a.id)}" role="button"><div class="grow"><div class="sub">${esc(a.severity)} · ${ago(a.created)} ago${a.tickers.length ? " · " + esc(a.tickers.slice(0, 3).join(" ")) : ""}</div><div style="font-weight:600">${esc(a.title)}</div></div></div>`);
  const it = r.items.slice(0, 8).map((x) => `<div class="li"><div class="grow"><div class="sub">${esc(outlet(x.source))} · ${ago(x.detected)} ago</div><a href="${esc(x.url)}" target="_blank" rel="noopener" style="font-weight:600">${esc(x.title)}</a></div></div>`);
  const sec = (h, rows) => rows.length ? `<div class="sub sh">${h}</div>${rows.join("")}` : "";
  $("searchRes").innerHTML = co.length || al.length || it.length
    ? `<div class="panel glass sres">${sec("Companies", co)}${sec("Alerts, 30 days", al)}${sec("Headlines, 30 days", it)}</div>`
    : `<div class="panel glass sres"><div class="li"><span class="muted">Nothing for “${esc(q)}” in the last 30 days.</span></div></div>`;
}

// ------------------------------------------------------------------ smart money + market map (the Watch tab)
// the gap board: every $300M+ company moving 3%+ on the whole-market tape, with the reason when there is one
function gapRow(x) {
  return `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span>
    <div class="grow"><div class="rname">${esc(x.name)}</div><div class="sub">${esc(x.cap)} ${esc(x.band)}${x.sector ? " · " + esc(x.sector) : ""}</div>
    <div class="sub">${x.why ? `<a href="${esc(x.why.url)}" target="_blank" rel="noopener" class="rnews">${esc(x.why.title)}</a> <span class="faint">· ${esc(outlet(x.why.source))}, ${esc(x.why.age)} ago</span>` : `<span class="rnone">No headline found</span>`}</div></div>
    <span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`;
}
async function loadGaps() {
  try {
    const r = await api("/api/gaps");
    $("gapSub").textContent = r.phase === "pre-market" ? "pre-market gappers · $300M+" : r.phase === "closed" ? "last session · $300M+" : "moving 3%+ today · $300M+";
    const both = [...r.up.slice(0, 10), ...r.down.slice(0, 10)];
    $("gaps").innerHTML = both.length ? (r.up.length ? `<div class="sub sh">Up</div>${r.up.slice(0, 10).map(gapRow).join("")}` : "") + (r.down.length ? `<div class="sub sh">Down</div>${r.down.slice(0, 10).map(gapRow).join("")}` : "")
      : `<div class="li"><span class="muted">Nothing worth $300M+ is moving 3% or more on the live tape right now.</span></div>`;
  } catch (_) { $("gaps").innerHTML = ""; }
}
async function loadHalts() {
  try {
    const r = await api("/api/halts");
    $("haltsWrap").hidden = !r.rows.length;
    $("halts").innerHTML = r.rows.slice(0, 15).map((x) => `<div class="li" ${x.symbol ? `data-tk="${esc(x.symbol)}" role="button"` : ""}><span class="sym" style="width:64px">${esc(x.symbol || "MKT")}</span>
      <div class="grow"><div>${esc(x.reason || x.title)} <span class="faint">${esc(x.code)}</span></div><div class="sub">halted ${ago(x.halted)} ago${x.market ? " · " + esc(x.market) : ""}</div></div>
      <span class="chg" style="width:auto">${x.resume ? `<small class="pos">resumes ${esc(x.resume)}</small>` : `<small class="faint">halted</small>`}</span></div>`).join("");
  } catch (_) { /* optional */ }
}
// in play: the stocks that matter right now, from every board at once
async function loadInPlay() {
  try {
    const [r, g] = await Promise.all([api("/api/inplay"), api("/api/regime").catch(() => null)]);
    $("inplayWrap").hidden = !r.rows.length && !(g && g.label !== "unknown");
    $("regime").innerHTML = g && g.label !== "unknown" ? `<span class="rg ${g.label === "risk-on" ? "pos" : g.label === "risk-off" ? "neg" : ""}">${g.label === "risk-on" ? "▲ Risk-on" : g.label === "risk-off" ? "▼ Risk-off" : "◆ Mixed"}</span> <span class="sub">${g.why.map(esc).join(" · ")}</span> ${tip("regime")}` : "";
    $("inplay").innerHTML = r.rows.map((x, i) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="rank">${i + 1}</span><span class="sym" style="width:56px">${esc(x.symbol)}</span>
      <div class="grow"><div class="rname">${esc(x.name || "")}</div><div class="sub">${x.why.map(esc).join(" · ")}</div>${x.reason ? `<div class="sub"><a href="${esc(x.reason.url)}" target="_blank" rel="noopener" class="rnews">${esc(x.reason.title)}</a></div>` : ""}</div>
      <span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("") || `<div class="li"><span class="muted">Nothing is in play right now.</span></div>`;
  } catch (_) { /* optional */ }
}
// the market under the index: up vs down across the tape, and by size
async function loadBreadth() {
  try {
    const b = await api("/api/breadth");
    $("breadthWrap").hidden = !b.n;
    if (!b.n) return;
    const upW = (b.up / b.n) * 100, dnW = (b.down / b.n) * 100;
    $("breadth").innerHTML = `<div class="bbar"><i class="u" style="width:${upW.toFixed(1)}%"></i><i class="d" style="width:${dnW.toFixed(1)}%"></i></div>
      <div class="kv"><span><b class="pos">${b.up.toLocaleString()} up</b> · <b class="neg">${b.down.toLocaleString()} down</b> <span class="sub">of ${b.n.toLocaleString()} companies $300M+</span></span><b class="num">${b.pct_up}% up</b></div>
      <div class="kv"><span>All of them, cap-weighted <span class="sub">· equal-weighted ${pct(b.equal_weighted)}</span></span><b class="num ${cls(b.cap_weighted)}">${pct(b.cap_weighted)}</b></div>
      <div class="kv"><span>Up 5%+ · down 5%+</span><b class="num"><span class="pos">${b.up5}</span> · <span class="neg">${b.down5}</span></b></div>
      ${b.bands.map((x) => `<div class="kv"><span>${esc(x.band)} <span class="sub">${x.up}/${x.n} up</span></span><b class="num ${cls(x.chg)}">${pct(x.chg)}</b></div>`).join("")}`;
  } catch (_) { /* optional */ }
}
async function loadVolume() {
  try {
    const r = await api("/api/volume");
    $("volWrap").hidden = !r.rows.length;
    $("vol").innerHTML = r.rows.slice(0, 15).map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span>
      <div class="grow"><div class="rname">${esc(x.name || "")}</div><div class="sub"><b class="gold">${x.rvol}× normal volume</b> for this time of day${x.dollar_volume ? ` · ${money(x.dollar_volume)} traded` : ""}${x.cap ? ` · ${esc(x.cap)}` : ""}</div>
      <div class="sub">${x.why ? `<a href="${esc(x.why.url)}" target="_blank" rel="noopener" class="rnews">${esc(x.why.title)}</a>` : `<span class="rnone">No headline found</span>`}</div></div>
      <span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("");
  } catch (_) { /* optional */ }
}
async function loadBuzz() {
  try {
    const r = await api("/api/buzz");
    $("buzzWrap").hidden = !r.rows.length;
    $("buzz").innerHTML = r.rows.slice(0, 12).map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span>
      <div class="grow"><div class="sub"><b class="gold">${x.n} headlines in the last hour</b> from ${x.sources} sources · usually ${x.usual_per_hour < 0.1 ? "almost none" : x.usual_per_hour.toFixed(1) + "/h"}</div>
      <div style="font-weight:600;margin-top:2px">${x.top.url ? `<a href="${esc(x.top.url)}" target="_blank" rel="noopener">${esc(x.top.title)}</a>` : esc(x.top.title)}</div></div>
      <span class="chg num" style="width:auto">${x.ratio}×</span></div>`).join("");
  } catch (_) { /* optional */ }
}
// one sector of the market map: its industries and every stock in it
async function openSector(name) {
  let r;
  try { r = await api("/api/sector?name=" + encodeURIComponent(name)); } catch (_) { return; }
  showSheet(`<div class="meta"><span class="src">Market map · ${r.n} stocks · $300M+</span></div><h2>${esc(name)}</h2>
    <h4>Industries</h4><div class="panel glass">${r.industries.map((x) => `<div class="kv"><span>${esc(x.industry)} <span class="sub">${x.up}/${x.n} up</span></span><b class="num ${cls(x.chg)}">${pct(x.chg)}</b></div>`).join("")}</div>
    <h4>Every stock, best to worst</h4><div class="panel glass">${r.stocks.map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span><div class="grow"><div>${esc(x.name)}</div><div class="sub">${esc(x.cap)} · ${esc(x.industry)}</div></div>${barHTML(x.chg)}<span class="chg num ${cls(x.chg)}">${pct(x.chg)}</span></div>`).join("")}</div>`);
}
const SM_ICON = { options: "◈", insider: "✦", ignition: "⚡" };
async function loadSmart() {
  try {
    const r = await api("/api/smart");
    $("smart").innerHTML = r.rows.length ? r.rows.slice(0, 20).map((x) => `<div class="li"${x.alert_id ? ` data-id="${esc(x.alert_id)}" role="button"` : ""}>
        <span class="sym" style="width:64px" data-tk="${esc(x.symbol)}" role="button">${esc(x.symbol)}</span>
        <div class="grow"><div class="sub">${SM_ICON[x.kind] || "·"} ${esc(KIND_LABEL[x.kind] || x.kind)} · ${ago(x.ts)} ago</div><div class="smlab">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label)}</a>` : esc(x.label)}</div></div>
        <span class="chg ${x.side === "up" ? "pos" : x.side === "down" ? "neg" : ""}" style="width:28px">${x.side === "up" ? "▲" : x.side === "down" ? "▼" : "◆"}</span></div>`).join("")
      : `<div class="li"><span class="muted">No informed money on the board yet: options bought hard on one side, insiders buying with their own cash, and stocks igniting on volume all land here as they print.</span></div>`;
  } catch (_) { $("smart").innerHTML = `<div class="li"><span class="muted">Unavailable.</span></div>`; }
}
async function loadSectors() {
  try {
    const r = await api("/api/sectors");
    const max = Math.max(1, ...r.rows.map((x) => Math.abs(x.chg)));
    $("sectors").innerHTML = r.rows.length ? r.rows.map((x) => {
      const k = Math.min(1, Math.abs(x.chg) / max);
      return `<div class="smap glass" data-act="sector" data-s="${esc(x.sector)}" role="button" style="--h:${x.chg >= 0 ? "150" : "8"};--a:${(0.1 + 0.35 * k).toFixed(2)}">
        <div class="sname">${esc(x.sector)}</div><div class="num big ${cls(x.chg)}">${pct(x.chg)}</div>
        <div class="sub">${x.up}/${x.n} up</div>
        <div class="tks">${x.leaders.slice(0, 2).map((l) => `<span class="tk" data-tk="${esc(l.symbol)}" role="button">${esc(l.symbol)} <small class="${cls(l.chg)}">${pct(l.chg)}</small></span>`).join("")}
        ${x.laggards[0] && x.laggards[0].chg < 0 ? `<span class="tk" data-tk="${esc(x.laggards[0].symbol)}" role="button">${esc(x.laggards[0].symbol)} <small class="neg">${pct(x.laggards[0].chg)}</small></span>` : ""}</div></div>`;
    }).join("") : `<div class="panel glass"><div class="li"><span class="muted">${r.market?.open ? "Mapping: the tape needs a few minutes of quotes across each sector." : "The map fills in from live quotes while the market (and the pre-market) trades."}</span></div></div>`;
    $("secSub").textContent = r.covered ? `${r.covered.toLocaleString()} stocks · cap-weighted` : "cap-weighted, live";
  } catch (_) { $("sectors").innerHTML = ""; }
}

// ------------------------------------------------------------------ live stream
function connect() {
  const es = new EventSource("/events?token=" + encodeURIComponent(TOKEN));
  const live = $("live");
  let dropped = false;
  es.onopen = () => { live.classList.add("live"); if (dropped) { dropped = false; load().catch(() => {}); } };
  es.onerror = () => { dropped = true; live.classList.remove("live"); };
  es.addEventListener("alert", (e) => {
    const a = JSON.parse(e.data); a._new = true; a.since = a.since || {};
    if (store.get("ft_chime") === "1" && !document.hidden && (a.severity === "HIGH" || a.severity === "CRITICAL" || isMine(a))) chime(a.severity);
    if (store.get("ft_haptic") === "1" && !document.hidden && (a.severity === "HIGH" || a.severity === "CRITICAL" || isMine(a))) navigator.vibrate?.(a.severity === "CRITICAL" ? [40, 60, 40, 60, 80] : [30, 40, 30]);
    alerts = [a, ...alerts.filter((x) => x.id !== a.id)].slice(0, 150);
    renderTape();
    Scene.pulse?.();
    if (document.hidden || tab !== "tape") { unread++; $("unread").textContent = unread; $("unread").hidden = false; }
    if (a.edge?.radar && tab === "watch") loadRadar();  // the board updates the moment the radar fires
    if (!document.hidden && tab !== "tape" && (a.severity === "HIGH" || a.severity === "CRITICAL")) {
      toast((a.edge?.radar ? "◉ " : a.severity === "CRITICAL" ? "🔴 " : "🟠 ") + a.title.slice(0, 140), a.id);
    }
  });
  es.addEventListener("kw_hit", (e) => {
    const d = JSON.parse(e.data);
    if (d.device !== DEVICE) return;
    toast(`🔑 ${d.k}: ${d.title}`.slice(0, 160)); loadDesk();
  });
  es.addEventListener("price_alert", (e) => {
    const d = JSON.parse(e.data);
    if (d.device !== DEVICE) return;  // someone else's line
    toast(d.title + " · " + d.body); loadDesk();
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
  reg.active?.postMessage({ type: "device", device: DEVICE });
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
const TAB_BASE = { tape: 0, brief: 0.5, calendar: 1.0, watch: 0.14, camillo: 1.35, desk: 1.75 };
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
    // pull to refresh: the ring fills with the pull; past the line, releasing ignites a reload
    const pull = clamp(-y / PULL_AT);
    if (pull !== Fx.pull) {
      Fx.pull = pull;
      doc.style.setProperty("--pull", pull.toFixed(3));
      $("pull").classList.toggle("armed", pull >= 1);
      if (!refreshing) $("pullText").textContent = pull >= 1 ? "Release to refresh" : "Pull to refresh";
    }
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
const LOADERS = { brief: loadBrief, calendar: loadCalendar, watch: loadWatch, camillo: loadCamillo, desk: () => Promise.all([loadDesk(), loadRecord(), loadReport(), loadWorth(), loadWeek()]) };

// ------------------------------------------------------------------ pull to refresh (iOS rubber band)
const PULL_AT = 92;
let refreshing = false;
// ------------------------------------------------------------------ is my phone getting what it should?
// why one alert did or didn't reach this phone, gate by gate, with the one tap that changes it
async function whyPush(id) {
  const el = $("whyPush");
  if (!el || !id) return;
  el.innerHTML = `<div class="panel glass"><div class="li"><span class="muted">Checking this phone…</span></div></div>`;
  let r;
  try { r = await api("/api/why-push/" + encodeURIComponent(id)); } catch (e) { el.innerHTML = `<div class="panel glass"><div class="li"><span class="muted">${esc(e.message || "Couldn't check.")}</span></div></div>`; return; }
  el.innerHTML = `<div class="panel glass padded whyp ${r.got ? "got" : r.got === false ? "not" : ""}">
    <div class="wverdict">${r.got ? "Delivered" : r.got === false ? "Not delivered" : "Should have arrived"}</div><p>${esc(r.verdict)}</p>
    ${gatesHTML(r.steps)}
    ${r.fix ? `<button class="btn primary small" data-act="push-fix" data-aid="${esc(id)}" data-fix="${esc(JSON.stringify(r.fix))}">${esc(r.fix.label)}</button>` : ""}
    ${r.settings_now ? `<div class="sub" style="margin-top:10px">Worked out from your settings as they are now: this alert isn't in the push log.</div>` : ""}
    <div class="row" style="margin-top:10px"><button class="btn ghost small" data-act="push-log">This phone's push log</button><button class="btn ghost small" data-act="health">Is it working?</button></div></div>`;
}
// turn alerts on from anywhere (the fix buttons): asks for permission straight from the tap
async function pushOn() {
  if (!pushCapable) { toast(isIOS && !standalone ? "On iPhone: Share → Add to Home Screen, then open Foretape from there" : "This browser can't show notifications", null, 6000); return false; }
  try {
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") { toast("Notifications are blocked for this app: allow them in your phone's settings", null, 6000); refreshPushUI(); return false; }
    await subscribe(true); refreshPushUI(); toast("Alerts are on"); return true;
  } catch (e) { toast("Couldn't turn on alerts: " + e.message); return false; }
}
// the last 7 days of pushes to this phone: delivered, held for a summary, kept back by its settings
function weekChart(week) {
  if (!week?.length || !week.some((d) => d.sent + d.held + d.filtered + d.failed)) return "";
  const top = Math.max(1, ...week.map((d) => d.sent + d.held + d.filtered + d.failed));
  const seg = (n, c) => n ? `<i class="${c}" style="height:${(n / top * 54).toFixed(1)}px" title="${n}"></i>` : "";
  return `<div class="wk" role="img" aria-label="Pushes in the last 7 days">${week.map((d) => `<div class="wkd"><div class="wkb">${seg(d.failed, "f")}${seg(d.filtered, "k")}${seg(d.held, "h")}${seg(d.sent, "s")}</div><small>${new Date(d.date + "T12:00:00").toLocaleDateString([], { weekday: "narrow" })}</small><b class="num">${d.sent}</b></div>`).join("")}</div>
    <div class="sub wkl"><i class="s"></i>delivered <i class="h"></i>held for a summary <i class="k"></i>kept back <i class="f"></i>failed</div>`;
}
const LOG_LABEL = { sent: ["✓", "ok", "delivered"], held: ["⏸", "mid", "held for a summary"], filtered: ["⦸", "mid", "kept back"], failed: ["✕", "no", "failed"], gone: ["✕", "no", "subscription expired"] };
async function openPushLog() {
  let r;
  try { r = await api("/api/push-log"); } catch (e) { toast(e.message); return; }
  const today = Object.entries(r.today).map(([k, n]) => `${n} ${(LOG_LABEL[k] || [, , k])[2]}`).join(" · ");
  showSheet(`<h2>Push log</h2><p class="muted">The last ${r.size} pushes meant for this phone: what was delivered, what your settings kept back and why, and what the push service answered.</p>
    ${today ? `<div class="sub" style="margin:10px 0 2px">Today: ${esc(today)}</div>` : ""}
    ${weekChart(r.week)}${r.hint ? `<div class="sub gold" style="margin-bottom:8px">${esc(r.hint)}</div>` : ""}
    <div class="panel glass plog">${r.rows.length ? r.rows.map((x) => {
      const [ic, c, l] = LOG_LABEL[x.status] || ["•", "mid", x.status], alert = /^[0-9a-f]{16}$/.test(x.id);
      return `<div class="li"${alert ? ` data-id="${esc(x.id)}" role="button"` : ""}><i class="${c}">${ic}</i><div class="grow"><div style="font-weight:600">${esc(x.title)}</div>
        <div class="sub">${esc(etTime(x.ts))} · ${esc(l)}${x.detail ? ": " + esc(x.detail) : ""}</div></div>${alert ? `<span class="faint">›</span>` : ""}</div>`;
    }).join("") : `<div class="li"><span class="muted">Nothing pushed to this phone yet${pushCapable && Notification.permission === "granted" ? "" : " (alerts aren't on here)"}.</span></div>`}</div>
    <div class="row" style="margin-top:14px"><button class="btn ghost small" data-act="health">Is it working?</button></div>`);
}
// is it working? each link from the news feeds to this lock screen
async function openHealth() {
  let r;
  try { r = await api("/api/phone-health"); } catch (e) { toast(e.message); return; }
  const local = [];
  if (pushCapable) local.push({ ok: Notification.permission === "granted", title: "Notifications on this device", text: Notification.permission === "granted" ? "Allowed." : Notification.permission === "denied" ? "Blocked: allow them for this app in your phone's settings." : "Not asked yet.", fix: Notification.permission === "granted" ? null : { label: "Turn on alerts", app: "push-on" } });
  else local.push({ ok: false, title: "Notifications on this device", text: isIOS && !standalone ? "On iPhone they work once Foretape is on your Home Screen (Share → Add to Home Screen)." : "This browser can't show notifications." });
  if (!navigator.onLine) local.push({ ok: false, title: "Connection", text: "This device is offline." });
  const rows = [...r.checks, ...local];
  const bad = rows.filter((c) => c.ok === false);
  showSheet(`<h2>Is it working?</h2><p class="muted">${bad.length ? `${bad.length} thing${bad.length > 1 ? "s" : ""} to look at.` : "Everything from the news feeds to this phone checks out."}</p>
    <div class="panel glass health">${rows.map((c) => `<div class="li"><i class="${c.ok === true ? "ok" : c.ok === false ? "no" : "mid"}">${c.ok === true ? "✓" : c.ok === false ? "✕" : "•"}</i><div class="grow"><div style="font-weight:600">${esc(c.title)}</div><div class="sub">${esc(c.text)}</div>
      ${c.fix ? `<button class="btn ghost small" style="margin-top:8px" data-act="push-fix" data-fix="${esc(JSON.stringify(c.fix))}">${esc(c.fix.label)}</button>` : ""}</div></div>`).join("")}</div>
    <h4>Would I get it?</h4><div class="panel glass padded"><div class="sub" style="margin-bottom:10px">Pick a kind of alert and see whether it would reach this phone, with your settings as they are now.</div>
      <div class="addrow"><select id="wiCat">${CATS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select><select id="wiSev"><option>HIGH</option><option>MEDIUM</option><option>CRITICAL</option></select></div>
      <div class="addrow"><input id="wiSym" class="key" placeholder="on this stock (optional)" autocapitalize="characters" autocomplete="off" spellcheck="false"><button class="btn ghost small sel" data-act="whatif">Check</button></div><div id="wiOut"></div></div>
    <div class="row" style="margin-top:14px"><button class="btn ghost small" data-act="push-log">Push log</button><button class="btn ghost small" id="hTest">Send a test alert</button></div>`);
  $("hTest").onclick = () => $("testBtn").click();
}
async function whatIf() {
  let r;
  try { r = await api("/api/would-push", { method: "POST", body: JSON.stringify({ category: $("wiCat").value, severity: $("wiSev").value, symbol: $("wiSym").value.trim() }) }); }
  catch (e) { $("wiOut").innerHTML = `<div class="sub neg" style="padding-top:10px">${esc(String(e.message || e).slice(0, 160))}</div>`; return; }
  $("wiOut").innerHTML = `<div class="whyp ${r.got ? "got" : "not"}" style="padding-top:12px"><div class="wverdict">${r.got ? "Yes" : "No"}</div><p>${esc(r.verdict)}</p>${gatesHTML(r.steps)}
    ${r.fix ? `<button class="btn primary small" data-act="push-fix" data-fix="${esc(JSON.stringify(r.fix))}" data-aid="">${esc(r.fix.label)}</button>` : ""}</div>`;
}
// an alert as plain text, for a group chat or a note
async function copyAlert(a) {
  if (!a) return;
  const e = a.edge || {}, b = e.base, since = a.since || {};
  const lines = [a.title, `${a.severity} · ${KIND_LABEL[a.kind] || a.kind} · ${etTime(a.created)}`];
  if (a.analysis?.summary) lines.push(a.analysis.summary);
  const mv = Object.entries(since).filter(([, v]) => v != null).map(([t, v]) => `${t} ${pct(v)} since the alert`);
  if (mv.length) lines.push(mv.join(" · "));
  if (b?.h1?.n) lines.push(`Alerts like this: ${Math.round(b.h1.hit * 100)}% went their way within an hour (median ${pct(b.h1.median)}, ${b.h1.n} graded)`);
  if (a.url) lines.push(a.url);
  lines.push("via Foretape · " + location.origin + "/app/#a=" + a.id);
  const text = lines.join("\n");
  try { await navigator.clipboard.writeText(text); toast("Copied as text"); } catch (_) { prompt("Copy this:", text); }
}
// a shared watchlist link (#list=CODE): see the stocks first, then add them
async function openSharedList(code) {
  let r;
  try { r = await api("/api/list/" + encodeURIComponent(code)); } catch (_) { toast("That list link is wrong or has expired", null, 5000); return; }
  showSheet(`<h2>A shared watchlist${r.name ? `: ${esc(r.name)}` : ""}</h2><p class="muted">${r.rows.length} stock${r.rows.length === 1 ? "" : "s"}${r.new < r.rows.length ? `, ${r.rows.length - r.new} already on yours` : ""}. Adding them copies the symbols to your watchlist; nothing of yours is shared back.</p>
    <div class="panel glass">${r.rows.map((x) => `<div class="li" data-tk="${esc(x.symbol)}" role="button"><span class="sym" style="width:64px">${esc(x.symbol)}</span><div class="grow">${esc(x.name || "")}</div>${x.mine ? `<span class="sub">watching</span>` : ""}</div>`).join("")}</div>
    <div class="row" style="margin-top:16px">${r.new ? `<button class="btn primary" data-act="list-add" data-s="${esc(code)}">Add ${r.new} to my watchlist</button>` : `<span class="sub">You already watch all of them.</span>`}<button class="btn ghost" data-act="close">Not now</button></div>`);
}
// tap the level chart at a price: a price alert there, above or below where it trades now
document.addEventListener("click", (e) => {
  const svg = e.target.closest?.("svg.lvl");
  if (!svg) return;
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const m = svg.getScreenCTM();
  if (!m) return;
  const p = pt.matrixTransform(m.inverse()), H = +svg.dataset.h, lo = +svg.dataset.lo, span = +svg.dataset.span, px = +svg.dataset.px;
  if (p.x > +svg.dataset.w) return;  // the label column
  let v = lo + (1 - (p.y - 10) / (H - 20)) * span;
  v = v >= 100 ? Math.round(v * 4) / 4 : v >= 10 ? Math.round(v * 20) / 20 : Math.round(v * 100) / 100;  // a tidy level
  if (!(v > 0) || !px) return;
  const kind = v >= px ? "above" : "below", sym = svg.dataset.s, y = 10 + (1 - (v - lo) / span) * (H - 20);
  svg.querySelector(".tapline")?.remove();
  svg.insertAdjacentHTML("beforeend", `<g class="tapline"><line x1="6" x2="${svg.dataset.w}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#FFD9A0" stroke-dasharray="2 3"/><circle cx="${p.x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="#FFD9A0"/></g>`);
  const box = $("lvlAsk") || (() => { $("lvlChart").insertAdjacentHTML("afterend", `<div id="lvlAsk" class="lvlask glass"></div>`); return $("lvlAsk"); })();
  box.innerHTML = `<span>🔔 Alert when ${esc(sym)} goes <b>${kind}</b> <b class="num">${fmtPx(v)}</b> <span class="sub">(${pct((v / px - 1) * 100)} from here)</span></span>
    <button class="btn primary small" data-act="lvl-set" data-s="${esc(sym)}" data-k="${kind}" data-v="${v}">Set it</button>`;
});
// restore a backup: the JSON from "Backup": its stocks, alerts, words, rules and calls are added to this desk
$("restoreFile").addEventListener("change", async () => {
  const f = $("restoreFile").files[0];
  $("restoreFile").value = "";
  if (!f) return;
  let data;
  try { data = JSON.parse(await f.text()); } catch (_) { toast("That file isn't a Foretape backup"); return; }
  if (!confirm("Add what's in this backup (stocks, price alerts, words, rules, calls, notes) to your desk?\nNothing you have now is replaced or deleted." + ($("restorePrefs")?.checked ? "\nIt will also bring back its settings." : ""))) return;
  const r = await deskEdit("restore", { desk: data.desk || data, prefs: !!$("restorePrefs")?.checked });
  if (!r) return;
  const n = r.restored || {}, L = { stocks: "stocks", alerts: "price alerts", holdings: "holdings", keywords: "keywords", words: "never-push words", notes: "notes", rules: "smart alerts", calls: "calls", follows: "follows", dials: "push dials" };
  const bits = Object.entries(L).filter(([k]) => n[k]).map(([k, l]) => `${n[k]} ${l}`);
  toast(bits.length ? "Restored: " + bits.join(", ") : "Nothing new in that backup: you already have it all", null, 7000);
});
// the command palette (⌘K / Ctrl-K, or the ⌘ button): any page, stock, alert or setting by typing
const Palette = {
  open: false, sel: 0, items: [],
  show() {
    if (!TOKEN || $("main").hidden) return;
    this.open = true; $("pal").hidden = false; $("palIn").value = ""; this.render(); $("palIn").focus(); requestAnimationFrame(() => $("palIn").focus());
  },
  hide() { this.open = false; $("pal").hidden = true; },
  toggle() { this.open ? this.hide() : this.show(); },
  build(q) {
    const Q = q.trim().toLowerCase(), out = [], has = (t) => !Q || t.toLowerCase().includes(Q);
    // best match first: a title that starts with what you typed, then a word in it, then anywhere
    const rank = (t) => !Q ? 0 : t.toLowerCase().startsWith(Q) ? 3 : new RegExp("\\b" + Q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(t.toLowerCase()) ? 2 : 1;
    (window.Cmd ? Cmd.parse(q) : []).forEach((c) => out.push({ ic: "⚡", t: c.label, sub: "press Enter to do it", r: 9, run: async () => { if (await deskEdit(c.action, c.body)) toast(c.done); } }));
    const sym = syms(q).length === 1 && /^\$?[A-Za-z][A-Za-z0-9.-]{0,5}$/.test(q.trim()) ? syms(q)[0] : "";
    // typed in capitals or with a $, it's surely a ticker; in lowercase, a matching page or setting comes first
    if (sym) out.push({ ic: "◎", t: `Open ${sym}`, sub: "stock page", run: () => openTicker(sym), r: /^\$|^[A-Z0-9.-]+$/.test(q.trim()) ? 5 : 2.5 });
    const seen = new Set(sym ? [sym] : []);
    const stocks = [...(desk?.watchlist || []).map((w) => [w.symbol, "on your watchlist"]), ...(store.get("ft_recent") || "").split(",").filter(Boolean).map((x) => [x, "recently viewed"])];
    stocks.forEach(([x, why]) => { if (!seen.has(x) && has(x)) { seen.add(x); out.push({ ic: "★", t: x, sub: why, run: () => openTicker(x), r: x.toLowerCase() === Q ? 6 : rank(x) + 0.5 }); } });
    const pages = [["The tape", "tape"], ["The brief", "brief"], ["Calendar", "calendar"], ["Watch: market map, radar, options", "watch"], ["Camillo desk", "camillo"], ["Your desk", "desk"]];
    pages.forEach(([t, v]) => { if (has(t)) out.push({ ic: "▤", t, sub: "page", run: () => { closeSheet(); go(v); }, r: rank(t) }); });
    const acts = [
      ["Is it working?", "check feeds, prices and this phone", openHealth], ["Push log", "the last pushes to this phone", openPushLog],
      ["Send a test alert", "to this phone", () => $("testBtn").click()], ["Turn on alerts", "on this device", pushOn],
      ["Glossary", "what every term means", openGlossary], ["Links", "every page in one place", openLinks],
      ["Focus for 1 hour", "only CRITICAL comes through", () => deskEdit("focus", { minutes: 60 }).then((r) => r && toast("🎯 Focus on for an hour"))],
      ["End focus mode", "", () => deskEdit("focus", { minutes: 0 }).then((r) => r && toast("Focus mode ended"))],
      ["Would I get it? (test my push settings)", "", openHealth],
      ["Back up my desk (JSON)", "", () => exportDesk("json")], ["Restore from a backup", "add a saved desk to this one", () => $("restoreFile").click()],
      ["Export my alerts (CSV)", "", () => exportDesk("csv")], ["First-run setup", "pick what you care about", openOnboard],
    ];
    acts.forEach(([t, sub, fn]) => { if (has(t)) out.push({ ic: "⚙", t, sub, run: fn, r: rank(t) }); });
    if (Q.length >= 2) {
      alerts.filter((a) => a.title.toLowerCase().includes(Q) || (a.tickers || []).some((t) => t.toLowerCase() === Q)).slice(0, 6)
        .forEach((a) => out.push({ ic: "◆", t: a.title, sub: `${a.severity} · ${ago(a.created)} ago`, run: () => openSheet(a.id), r: 0.5 }));
      out.push({ r: 0, ic: "⌕", t: `Search everything for “${q.trim()}”`, sub: "alerts and headlines, 30 days", run: () => { closeSheet(); go("tape"); $("q").value = q.trim(); runSearch(); setTimeout(() => $("q").scrollIntoView({ block: "start", behavior: "smooth" }), 250); } });
    }
    return out.map((x, i) => [x, i]).sort((a, b) => (b[0].r || 0) - (a[0].r || 0) || a[1] - b[1]).map(([x]) => x).slice(0, 14);
  },
  render() {
    this.items = this.build($("palIn").value); this.sel = 0; this.paint();
  },
  paint() {
    $("palList").innerHTML = this.items.length ? this.items.map((x, i) => `<div class="pi${i === this.sel ? " on" : ""}" data-pi="${i}" role="option" aria-selected="${i === this.sel}"><span class="pic">${x.ic}</span><span class="grow">${esc(x.t)}${x.sub ? `<small>${esc(x.sub)}</small>` : ""}</span></div>`).join("")
      : `<div class="pi"><span class="muted">Nothing matches.</span></div>`;
    $("palList").querySelector(".pi.on")?.scrollIntoView({ block: "nearest" });
  },
  run(i) { const x = this.items[i]; if (!x) return; this.hide(); x.run(); },
};
$("palIn").addEventListener("input", () => Palette.render());
$("palIn").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); Palette.sel = (Palette.sel + (e.key === "ArrowDown" ? 1 : -1) + Palette.items.length) % Math.max(1, Palette.items.length); Palette.paint(); }
  else if (e.key === "Enter") { e.preventDefault(); Palette.run(Palette.sel); }
  else if (e.key === "Escape") Palette.hide();
});
$("palList").addEventListener("click", (e) => { const el = e.target.closest("[data-pi]"); if (el) Palette.run(+el.dataset.pi); });
$("pal").addEventListener("click", (e) => { if (e.target.id === "pal") Palette.hide(); });

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  $("pull").classList.add("firing"); $("pullText").textContent = "Refreshing…";
  Scene.pulse?.();   // the engine flares as the data reloads
  try {
    await Promise.all([load(), Promise.resolve(LOADERS[tab]?.())]);
    toast("Up to date · " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
  } catch (_) { toast("Couldn't refresh. Check your connection."); }
  setTimeout(() => { refreshing = false; $("pull").classList.remove("firing"); Fx.pull = -1; Fx.dirty = true; }, 650);
}
addEventListener("touchend", () => { if (!sheetOpen && scrollY <= -PULL_AT) refresh(); }, { passive: true });
const TABS = ["tape", "brief", "calendar", "watch", "camillo", "desk"];
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
  if (name === "tape") { renderTape(); requestAnimationFrame(placeThumb); }  // desk changes (holdings, taste) show on return
  Promise.resolve(LOADERS[name]?.()).catch(() => {}).then(() => requestAnimationFrame(() => Fx.observe(document.querySelector(".view.on"))));
  Fx.observe(document.querySelector(".view.on"));
}
document.querySelectorAll(".dock button").forEach((b) => (b.onclick = () => go(b.dataset.tab)));
$("nextCat").onclick = () => go("calendar");
$("ticker").onclick = () => go("watch");
setInterval(() => document.querySelectorAll(".ago[data-t]").forEach((el) => (el.textContent = ago(+el.dataset.t))), 15000);
setInterval(() => { if (!document.hidden && TOKEN && !$("main").hidden) { load().catch(() => {}); loadDesk(); if (tab === "watch") loadWatch().catch(() => {}); } }, 60000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { markSeen(); return; }
  if (TOKEN) { navigator.clearAppBadge?.(); Promise.all([load(), loadDesk()]).then(renderAway).catch(() => {}); }
});
$("q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(runSearch, 220); $("sFilters").hidden = $("q").value.trim().length < 2; renderRecent(); });
renderRecent();
renderFilters();
$("q").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const v = $("q").value.trim(), one = syms(v);
  if (one.length === 1 && v.replace(/^\$/, "").length <= 6) { $("q").blur(); openTicker(one[0]); }
});
function showLock() { $("lock").hidden = false; $("main").hidden = true; $("dock").hidden = true; }
$("unlock").onclick = async () => {
  TOKEN = $("key").value.trim(); store.set("ft_token", TOKEN); setManifest();
  try { await start(); } catch (e) { toast("That key didn't work"); }
};
function route() {
  const lst = location.hash.match(/list=([A-Za-z0-9-]{8,9})/);
  if (lst) { history.replaceState(null, "", location.pathname + location.search); return openSharedList(lst[1]); }
  const q = location.hash.match(/q=([^&]+)/);
  if (q) { go("tape"); $("q").value = decodeURIComponent(q[1]); runSearch(); setTimeout(() => $("q").scrollIntoView({ block: "start", behavior: "smooth" }), 300); return; }
  const t = location.hash.match(/t=\$?([A-Za-z][A-Za-z0-9.-]{0,9})/);
  if (t) return openTicker(t[1]);
  const m = location.hash.match(/a=([\w-]+)/);
  if (!m) return;
  if (m[1] === "brief") return go("brief");
  if (m[1] === "desk") return go("desk");
  if (m[1] !== "test") openSheet(m[1]);
}
addEventListener("hashchange", route);

async function start() {
  if (!TOKEN) { showLock(); return; }
  await load();
  $("lock").hidden = true; $("main").hidden = false; $("dock").hidden = false;
  Fx.split(); Fx.observe(document);
  connect();
  loadDesk().then(() => { renderAway(); maybeOnboard(); });
  if ("serviceWorker" in navigator) await navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" });
  navigator.clearAppBadge?.();
  refreshPushUI();
  if (store.get("ft_motion") === "1" || (!Motion.needsPermission && !reduced && matchMedia("(pointer: coarse)").matches)) Motion.enable().then(() => Motion.ui());
  Motion.ui();
  route();
}

// boot: the scene starts immediately (it's behind the lock screen too), the app follows
applyDisplay();
Fx.split();
let lifted = false;
const lift = () => { if (lifted) return; lifted = true; $("curtain").classList.add("off"); Scene.ignite(); };
Scene.onReady = () => requestAnimationFrame(lift);
setTimeout(lift, 9000);   // never leave someone on a black screen if the photo is slow
if (store.get("ft_clear") === "1" || store.get("ft_light") === "1") requestAnimationFrame(lift);  // clear mode renders no scene: nothing to wait for
Scene.start($("scene"));
Fx.tick();
start().catch(() => {});
