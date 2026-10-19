// Palette commands: turn a line typed into the ⌘K palette ("ACME above 105", "mute ACME 2h",
// "watch NVDA AMD", "focus 1h", "push ACME high") into a desk edit. Pure: parse() only reads the
// text; app.js runs the result through deskEdit, which validates it on the server.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Cmd = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const SYM = "\\$?([A-Za-z][A-Za-z0-9.\\-]{0,9})", NUM = "\\$?(\\d+(?:\\.\\d+)?)";
  const RESERVED = new Set(["MUTE", "UNMUTE", "WATCH", "UNWATCH", "FOCUS", "PUSH", "ALERT", "SET", "ADD", "REMOVE", "DROP", "STOP", "END"]);
  const T = (s) => String(s).toUpperCase().replace(/^\$/, "");
  const isSym = (s) => /^\$?[A-Za-z][A-Za-z0-9.\-]{0,9}$/.test(s) && !RESERVED.has(T(s));
  const money = (v) => "$" + (+v).toLocaleString("en-US", { maximumFractionDigits: 2 });
  const hours = (n, unit) => (/^m/i.test(unit) ? n / 60 : /^d/i.test(unit) ? n * 24 : n);
  const LEVEL = { all: "all", everything: "all", high: "high", "high only": "high", off: "off", never: "off", default: "default", normal: "default" };
  const LEVEL_TEXT = { all: "everything", high: "HIGH only", off: "nothing", default: "its normal settings" };

  function parse(input) {
    let q = String(input || "").trim().replace(/\s+/g, " ");
    if (!q) return [];
    q = q.replace(/^(?:set |add |create |new )?(?:an? )?(?:price )?alert (?:on |for )?/i, "");
    const out = [], add = (label, action, body, done) => out.push({ label, action, body, done: done || label });
    let m;
    if ((m = q.match(new RegExp(`^${SYM} (?:goes |crosses |trades |closes )?(?:above|over|>=?|breaks|tops) ${NUM}$`, "i"))) && isSym(m[1]))
      add(`🔔 Alert when ${T(m[1])} goes above ${money(m[2])}`, "alert_add", { symbol: T(m[1]), kind: "above", value: +m[2] }, `🔔 Set: ${T(m[1])} above ${money(m[2])}`);
    else if ((m = q.match(new RegExp(`^${SYM} (?:goes |falls |drops |trades |closes )?(?:below|under|<=?) ${NUM}$`, "i"))) && isSym(m[1]))
      add(`🔔 Alert when ${T(m[1])} goes below ${money(m[2])}`, "alert_add", { symbol: T(m[1]), kind: "below", value: +m[2] }, `🔔 Set: ${T(m[1])} below ${money(m[2])}`);
    else if ((m = q.match(new RegExp(`^${SYM} (?:falls?|drops?|trails?|trailing)(?: by)? ${NUM} ?%(?: from (?:its |the )?high)?$`, "i"))) && isSym(m[1]))
      add(`🔔 Alert when ${T(m[1])} falls ${m[2]}% from its high`, "alert_add", { symbol: T(m[1]), kind: "trail", value: +m[2] }, `🔔 Set: ${T(m[1])} falls ${m[2]}% from its high`);
    else if ((m = q.match(new RegExp(`^${SYM} (?:(?:moves?|swings?|±|\\+/-) ?)?${NUM} ?%$`, "i"))) && isSym(m[1]))
      add(`🔔 Alert when ${T(m[1])} moves ±${m[2]}% on the day`, "alert_add", { symbol: T(m[1]), kind: "move", value: +m[2] }, `🔔 Set: ${T(m[1])} moves ±${m[2]}%`);
    else if ((m = q.match(/^watch (.+)$/i))) {
      const syms = m[1].split(/[\s,;]+/).filter(isSym).map(T);
      if (syms.length) add(`★ Watch ${syms.join(", ")}`, "watch_add", { symbols: syms }, `★ Watching ${syms.join(", ")}`);
    } else if ((m = q.match(new RegExp(`^(?:unwatch|remove|drop) ${SYM}$`, "i"))) && isSym(m[1]))
      add(`Stop watching ${T(m[1])}`, "watch_remove", { symbol: T(m[1]) }, `${T(m[1])} removed`);
    else if ((m = q.match(new RegExp(`^mute ${SYM}(?: (?:for )?(\\d+(?:\\.\\d+)?) ?(h|hrs?|hours?|m|mins?|minutes?|d|days?)| until (?:the )?(open|close))?$`, "i"))) && isSym(m[1])) {
      const sym = T(m[1]);
      if (m[4]) add(`🔕 Mute ${sym} until the ${m[4].toLowerCase()}`, "mute", { symbol: sym, until: m[4].toLowerCase() }, `🔕 ${sym} muted until the ${m[4].toLowerCase()}`);
      else {
        const h = m[2] ? hours(+m[2], m[3]) : 4;
        add(`🔕 Mute ${sym} for ${+h.toFixed(2)}h`, "mute", { symbol: sym, hours: h }, `🔕 ${sym} muted for ${+h.toFixed(2)}h`);
      }
    } else if ((m = q.match(new RegExp(`^unmute ${SYM}$`, "i"))) && isSym(m[1]))
      add(`Unmute ${T(m[1])}`, "unmute", { symbol: T(m[1]) }, `${T(m[1])} unmuted`);
    else if (/^(?:focus (?:off|end|stop)|stop focus|end focus)$/i.test(q))
      add("End focus mode", "focus", { minutes: 0 }, "Focus mode ended");
    else if ((m = q.match(/^focus(?: (?:for )?(\d+(?:\.\d+)?) ?(h|hrs?|hours?|m|mins?|minutes?))?( until (?:the )?open)?$/i))) {
      if (m[3]) add("🎯 Focus until the open: only CRITICAL comes through", "focus", { until: "open" }, "🎯 Focus on until the open");
      else {
        const mins = m[1] ? Math.round(hours(+m[1], m[2]) * 60) : 60;
        add(`🎯 Focus for ${mins >= 60 && mins % 60 === 0 ? mins / 60 + "h" : mins + " min"}: only CRITICAL comes through`, "focus", { minutes: mins }, `🎯 Focus on for ${mins} min`);
      }
    } else if ((m = q.match(new RegExp(`^(?:push|dial) ${SYM} (all|everything|high only|high|off|never|default|normal)$`, "i"))) && isSym(m[1])) {
      const lv = LEVEL[m[2].toLowerCase()], sym = T(m[1]);
      add(`📲 Push ${LEVEL_TEXT[lv]} on ${sym}`, "stock_push", { symbol: sym, level: lv }, `📲 ${sym}: pushes set to ${LEVEL_TEXT[lv]}`);
    }
    return out;
  }
  return { parse };
});
