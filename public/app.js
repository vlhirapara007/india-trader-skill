/* India Trader: the phone page. Talks to /api/* (app.py). Plans only, never orders. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const store = {
    get(k, d) { try { const v = localStorage.getItem("it." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("it." + k, JSON.stringify(v)); } catch {} },
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Indian digit grouping
  const inr = (x, d = 2) => x == null || isNaN(x) ? "—" :
    Number(x).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const rs = (x, d = 2) => x == null ? "—" : "₹" + inr(x, d);
  const pct = (x) => x == null ? "—" : (x > 0 ? "+" : "") + x.toFixed(2) + "%";
  const cls = (x) => x > 0 ? "up" : x < 0 ? "down" : "";
  const SETUP_NAMES = {
    breakout_20d: "20-day breakout", breakout_52w: "52-week breakout", bull_flag: "Bull flag",
    pullback_20sma: "Pullback to 20-day avg", reclaim_20sma: "Back above 20-day avg", stage2_trend: "Strong uptrend",
    oversold: "Oversold", oversold_bounce: "Oversold bounce", nr7: "Quiet day (NR7)", inside_day: "Inside day", orb: "Opening range",
  };

  // ---------------------------------------------------------------- API
  const DEFAULT_RULES = { capital: 20000, drawdown: 2000, risk: 200, minrr: 1.5, loss: 0, gain: 0, today: 0 };
  let rules = Object.assign({}, DEFAULT_RULES, store.get("rules", {}));

  async function api(path, params) {
    const url = "/api/" + path + (params ? "?" + new URLSearchParams(params).toString() : "");
    const headers = {};
    const key = store.get("key", "");
    if (key) headers["X-Key"] = key;
    let r;
    try { r = await fetch(url, { headers }); } catch { throw new Error("No connection. Check your internet and try again."); }
    let body = null;
    try { body = await r.json(); } catch {}
    if (r.status === 401) { go("rules"); $("#pw-hint").textContent = "This app has a password. Enter it below."; $("#pw").focus(); }
    if (!r.ok) throw new Error((body && body.error) || "The server answered " + r.status + ".");
    return body;
  }

  // ---------------------------------------------------------------- views
  const VIEWS = ["stock", "scan", "market", "rules"];
  function go(v, push = true) {
    if (!VIEWS.includes(v)) v = "stock";
    VIEWS.forEach((x) => { $("#view-" + x).hidden = x !== v; });
    $$(".tabs a").forEach((a) => a.setAttribute("aria-current", a.dataset.v === v ? "page" : "false"));
    if (push && location.hash.slice(1).split("/")[0] !== v) history.replaceState(null, "", "#" + v);
    if (v === "market") loadMarket();
    if (v === "rules") renderRules();
    window.scrollTo(0, 0);
  }
  $$(".tabs a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); go(a.dataset.v); }));

  // ---------------------------------------------------------------- status
  let STATUS = null;
  async function loadStatus() {
    try {
      STATUS = await api("status");
      const b = $("#status");
      b.dataset.state = STATUS.state;
      const when = STATUS.state === "open" ? "Open until 15:30" :
        STATUS.state.startsWith("pre-open") ? "Pre-open, opens 09:15" :
        "Closed, opens " + new Date(STATUS.next_open).toLocaleString("en-IN", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
      $("#status-text").textContent = when;
      b.title = STATUS.reason;
      $("#demo").hidden = !STATUS.demo;
    } catch (e) { $("#status-text").textContent = "Offline"; }
  }

  // ---------------------------------------------------------------- search
  let INDEX = null;
  async function loadIndex() {
    const cached = store.get("index", null);
    const today = new Date().toISOString().slice(0, 10);
    if (cached && cached.day === today && cached.items.length) { INDEX = cached.items; return; }
    try {
      const r = await api("search-index");
      INDEX = r.items;
      store.set("index", { day: today, items: INDEX });
    } catch (e) { INDEX = cached ? cached.items : []; }
  }
  function matches(q) {
    q = q.trim().toUpperCase();
    if (!q || !INDEX) return [];
    const starts = [], has = [];
    for (const i of INDEX) {
      const s = i.s, n = (i.n || "").toUpperCase();
      if (s === q || s.startsWith(q)) starts.push(i);
      else if (n.includes(q) || s.includes(q)) has.push(i);
      if (starts.length > 30) break;
    }
    starts.sort((a, b) => (a.s === q ? -1 : b.s === q ? 1 : a.s.length - b.s.length || (a.e === "NSE" ? -1 : 1)));
    const out = starts.concat(has).slice(0, 8);
    if (!out.some((i) => i.s === q) && /^[A-Z0-9&\-]{2,20}$/.test(q))
      out.push({ s: q, n: /^\d{6}$/.test(q) ? "BSE code" : "Try this symbol on NSE", e: /^\d{6}$/.test(q) ? "BSE" : "NSE", i: 1 });
    return out;
  }
  let sel = -1, shown = [];
  const q = $("#q"), sug = $("#suggest");
  function renderSuggest() {
    shown = matches(q.value);
    sel = shown.length ? 0 : -1;
    sug.hidden = !shown.length;
    sug.innerHTML = shown.map((i, k) => `<li role="option" data-k="${k}" aria-selected="${k === sel}">
      <span class="name"><b>${esc(i.s)}</b> ${esc(i.n || "")}</span><small>${esc(i.e)}</small></li>`).join("");
  }
  q.addEventListener("input", renderSuggest);
  q.addEventListener("focus", () => { if (q.value) renderSuggest(); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(shown.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      $$("li", sug).forEach((li, k) => li.setAttribute("aria-selected", k === sel));
    } else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); pick(shown[sel]); }
    else if (e.key === "Escape") sug.hidden = true;
  });
  sug.addEventListener("mousedown", (e) => { const li = e.target.closest("li"); if (li) { e.preventDefault(); pick(shown[+li.dataset.k]); } });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) sug.hidden = true; });
  function pick(i) { sug.hidden = true; q.value = ""; q.blur(); openStock(i.s, i.e); }

  function renderRecent() {
    const r = store.get("recent", []);
    $("#recent").innerHTML = r.map(([s, e]) => `<button type="button" data-s="${esc(s)}" data-e="${esc(e)}">${esc(s)}</button>`).join("");
  }
  $("#recent").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) openStock(b.dataset.s, b.dataset.e); });

  // ---------------------------------------------------------------- stock
  let STOCK = null, chart = null, preset = null;
  async function openStock(sym, ex, plan) {
    preset = plan || null;
    go("stock", false);
    history.replaceState(null, "", "#stock/" + encodeURIComponent(sym) + "." + ex);
    $("#stock-empty").hidden = true;
    $("#stock-error").hidden = true;
    $("#stock").hidden = false;
    $("#s-name").textContent = sym;
    $("#s-sub").textContent = "Loading…";
    $("#s-price").textContent = ""; $("#s-change").textContent = "";
    try {
      STOCK = await api("stock", { symbol: sym, exchange: ex });
    } catch (e) {
      $("#stock").hidden = true;
      const box = $("#stock-error"); box.hidden = false;
      box.textContent = `Couldn't load ${sym} on ${ex}: ${e.message}`;
      return;
    }
    const recent = store.get("recent", []).filter(([s, e2]) => !(s === STOCK.symbol && e2 === STOCK.exchange));
    recent.unshift([STOCK.symbol, STOCK.exchange]); store.set("recent", recent.slice(0, 6)); renderRecent();
    renderStock();
  }

  function renderStock() {
    const s = STOCK, st = s.stats;
    $("#s-name").textContent = s.name;
    const t = s.asof.length > 10 ? s.asof.slice(11, 16) + " IST" : "close of " + s.asof;
    $("#s-sub").textContent = `${s.symbol}, ${s.exchange}. Price as of ${t}${s.intraday_allowed ? "" : ". Delivery only (trade-to-trade)"}`;
    $("#s-price").textContent = rs(s.price);
    const ch = $("#s-change");
    ch.textContent = pct(s.change_pct) + " today"; ch.className = "chg " + cls(s.change_pct);
    drawChart($("#chart-range .on").dataset.r);

    const rsiNote = st.rsi == null ? "" : st.rsi < 30 ? ", oversold" : st.rsi > 70 ? ", overbought" : "";
    const cells = [
      ["Trend", st.trend + (st.above.length ? ", above " + st.above.join("/") + "-day" : ", below all averages")],
      ["From 52-week high", pct(st.from_hi_pct)],
      ["52-week range", inr(st.lo52, 0) + " – " + inr(st.hi52, 0)],
      ["1 week", pct(st.r1w)], ["1 month", pct(st.r1m)], ["3 months", pct(st.r3m)], ["1 year", pct(st.r1y)],
      ["RSI (14)", st.rsi == null ? "—" : st.rsi.toFixed(0) + rsiNote],
      ["Moves a day (ATR)", st.atr_pct == null ? "—" : st.atr_pct.toFixed(2) + "%"],
      ["Traded a day", st.value_cr == null ? "—" : "₹" + inr(st.value_cr, 0) + " cr"],
      ["Volume vs usual", st.vol_ratio == null ? "—" : st.vol_ratio.toFixed(1) + "×"],
      ["20-day average", rs(st.sma20)], ["50-day average", rs(st.sma50)], ["200-day average", rs(st.sma200)],
    ];
    $("#s-stats").innerHTML = cells.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd class="${k.match(/week|month|year/) && !k.includes("range") ? cls(parseFloat(v)) : ""}">${esc(v)}</dd></div>`).join("");

    const items = [];
    if (s.orb && s.orb.trigger) {
      const o = s.orb;
      const state = o.status === "triggered" ? `closed above it at ${o.entry_time} (${inr(o.entry_price)})` :
        o.status === "waiting" ? `${o.distance_pct.toFixed(2)}% below the top, waiting` : (o.note || o.status);
      items.push({ name: "Opening range today", why: `${inr(o.or_low)} to ${inr(o.or_high)}: ${state}`, trigger: o.trigger, stop: o.stop_ref, mode: "intraday", setup: "orb" });
    }
    for (const h of s.setups) items.push({ name: SETUP_NAMES[h.setup] || h.setup, why: h.why, trigger: h.trigger, stop: h.stop_ref, mode: h.mode.split("/")[0], setup: h.setup });
    $("#s-setups").innerHTML = items.length ? items.map((h, k) => `<div class="setup">
        <div><strong>${esc(h.name)}</strong><span class="tag">${esc(h.mode)}</span></div>
        <p>${esc(h.why)}</p>
        <div class="lv">Buy above <em>${inr(h.trigger)}</em>, wrong below ${inr(h.stop)}</div>
        <button type="button" class="ghost" data-k="${k}">Plan it</button></div>`).join("")
      : `<p class="hint">Nothing fired on the ${esc(s.last_close_date)} close. You can still plan your own levels below.</p>`;
    $$("#s-setups .ghost").forEach((b) => b.addEventListener("click", () => { fillPlan(items[+b.dataset.k]); $("#plan-h").scrollIntoView({ behavior: "smooth" }); }));

    const atr = st.atr || s.price * 0.02;
    const last = s.daily[s.daily.length - 1];
    fillPlan(preset || items[0] || { trigger: +(last[2] + 0.05).toFixed(2), stop: +(last[4] - 1.5 * atr).toFixed(2), mode: "swing", setup: "manual" });
    renderRanges();
  }

  // chart
  function sma(vals, n) { const out = []; let sum = 0; vals.forEach((v, i) => { sum += v; if (i >= n) sum -= vals[i - n]; out.push(i >= n - 1 ? sum / n : null); }); return out; }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function drawChart(range) {
    const el = $("#chart");
    if (chart) { chart.remove(); chart = null; }
    el.innerHTML = "";
    if (!window.LightweightCharts) { el.innerHTML = "<p class='hint'>Chart library didn't load.</p>"; return; }
    if (range === "today") {
      const IST = 5.5 * 3600;
      const rows = STOCK.today.map((r) => ({ time: Math.floor(Date.parse(r[0]) / 1000) + IST, open: r[1], high: r[2], low: r[3], close: r[4] }));
      if (!rows.length) { el.innerHTML = "<p class='hint'>No candles yet today.</p>"; return; }
      return drawToday(rows);
    }
    const ink = cssVar("--ink-3"), line = cssVar("--line");
    chart = LightweightCharts.createChart(el, {
      height: 300, layout: { background: { type: "solid", color: "transparent" }, textColor: ink, fontFamily: cssVar("--font") },
      grid: { vertLines: { visible: false }, horzLines: { color: line } },
      rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false },
      crosshair: { mode: 0 }, handleScroll: { vertTouchDrag: false }, localization: { priceFormatter: (p) => inr(p, p > 999 ? 0 : 2) },
    });
    const up = cssVar("--up"), down = cssVar("--down");
    const candles = chart.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    const d = STOCK.daily;
    candles.setData(d.map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] })));
    const closes = d.map((r) => r[4]);
    [[20, cssVar("--marigold")], [50, "#3B82F6"], [200, "#8B5CF6"]].forEach(([n, c]) => {
      const s = chart.addLineSeries({ color: c, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      s.setData(sma(closes, n).map((v, i) => v == null ? null : { time: d[i][0], value: v }).filter(Boolean));
    });
    const n = Math.min(+range, d.length);
    chart.timeScale().setVisibleLogicalRange({ from: d.length - n, to: d.length - 1 });
  }
  function drawToday(rows) {
    const el = $("#chart");
    const up = cssVar("--up"), down = cssVar("--down");
    chart = LightweightCharts.createChart(el, {
      height: 300, layout: { background: { type: "solid", color: "transparent" }, textColor: cssVar("--ink-3"), fontFamily: cssVar("--font") },
      grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--line") } },
      rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      handleScroll: { vertTouchDrag: false }, localization: { priceFormatter: (p) => inr(p, 2) },
    });
    const c = chart.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    c.setData(rows);
    if (STOCK.orb && STOCK.orb.or_high) {
      c.createPriceLine({ price: STOCK.orb.or_high, color: cssVar("--marigold"), lineWidth: 1, lineStyle: 2, title: "range high" });
      c.createPriceLine({ price: STOCK.orb.or_low, color: cssVar("--ink-3"), lineWidth: 1, lineStyle: 2, title: "range low" });
    }
    chart.timeScale().fitContent();
  }
  $("#chart-range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b || !STOCK) return;
    $$("#chart-range button").forEach((x) => x.classList.toggle("on", x === b));
    drawChart(b.dataset.r);
  });
  window.addEventListener("resize", () => { if (chart) chart.applyOptions({ width: $("#chart").clientWidth }); });

  // ---------------------------------------------------------------- plan
  let mode = "swing", planSetup = "manual", planTimer = null, planSeq = 0;
  function setMode(m) { mode = m; $$("#mode button").forEach((b) => b.classList.toggle("on", b.dataset.m === m)); }
  function fillPlan(h) {
    setMode(STOCK && !STOCK.intraday_allowed && h.mode === "intraday" ? "swing" : (["intraday", "swing", "positional"].includes(h.mode) ? h.mode : "swing"));
    $("#f-entry").value = (+h.trigger).toFixed(2);
    $("#f-stop").value = (+h.stop).toFixed(2);
    $("#f-target").value = "";
    planSetup = h.setup || "manual";
    makePlan();
  }
  $("#mode").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { setMode(b.dataset.m); makePlan(); } });
  $$("#plan-form input").forEach((i) => i.addEventListener("input", () => { clearTimeout(planTimer); planTimer = setTimeout(makePlan, 450); }));
  $("#plan-form").addEventListener("submit", (e) => { e.preventDefault(); makePlan(); });

  async function makePlan() {
    if (!STOCK) return;
    const entry = parseFloat($("#f-entry").value), stop = parseFloat($("#f-stop").value), target = parseFloat($("#f-target").value);
    const hint = $("#f-hint");
    if (!(entry > 0) || !(stop >= 0)) { hint.textContent = "Enter a buy-above price and a stop loss."; return; }
    hint.textContent = mode === "intraday" ? "Intraday: square off by 15:10, never overnight." :
      mode === "swing" ? "Swing: delivery, up to 10 trading days. Target blank = 2.5× the risk." : "Positional: delivery, up to 60 trading days. Target blank = 3× the risk.";
    if (mode === "intraday") hint.textContent += " Target blank = 2× the risk.";
    const seq = ++planSeq;
    const params = Object.assign({ symbol: STOCK.symbol, exchange: STOCK.exchange, mode, entry, stop, setup: planSetup }, target > 0 ? { target } : {});
    let p;
    try { p = await api("plan", Object.assign({}, rules, params)); } catch (e) { $("#plan").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
    if (seq !== planSeq) return;
    $("#plan").innerHTML = slip(p);
    $$("#plan [data-use-target]").forEach((b) => b.addEventListener("click", () => { $("#f-target").value = b.dataset.useTarget; makePlan(); }));
  }

  function slip(p) {
    const ok = p.status === "ok";
    const date = p.date ? new Date(p.date + "T00:00:00+05:30").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }) : "";
    let ladder = "";
    if (p.target && p.stop < p.entry) {
      const up = p.target - p.entry, dn = p.entry - p.stop, tot = up + dn;
      const upF = Math.max(0.12, up / tot), dnF = Math.max(0.12, dn / tot);
      ladder = `<div class="ladder" style="grid-template-rows:minmax(64px,${upF}fr) auto minmax(64px,${dnF}fr)"
          role="img" aria-label="Reward ${inr(up)} a share above the entry, risk ${inr(dn)} a share below">
        <div class="rw"></div>
        <div class="rung t"><div><div class="lab">Target</div><div class="val">${inr(p.target)}</div></div>
          <div><div class="lab">if hit</div><div class="net up">${p.qty ? "+" + rs(p.gain_at_target) : "—"}</div></div></div>
        <div class="en"></div>
        <div class="rung e"><div><div class="lab">Buy above</div><div class="val">${inr(p.entry)}</div></div>
          <div><div class="lab">${p.qty || 0} shares</div><div class="net">${rs(p.position_value || 0, 0)}</div></div></div>
        <div class="rk"></div>
        <div class="rung s"><div><div class="lab">Stop loss</div><div class="val">${inr(p.stop)}</div></div>
          <div><div class="lab">if stopped</div><div class="net down">${p.qty ? "−" + rs(p.loss_at_stop) : "—"}</div></div></div>
      </div>`;
    }
    const rows = p.qty ? [
      ["Reward : risk", p.reward_risk != null ? p.reward_risk.toFixed(2) + " after charges" : "—"],
      ["Charges", rs(p.charges_at_target) + " for the round trip"],
      ["Breakeven", rs(p.breakeven)],
      ["Exit by", (p.exit_by || "").replace("T", " at ")],
      ["Risk of your capital", (p.risk_pct_of_equity || 0).toFixed(2) + "%"],
      ["Quantity set by", (p.qty_limited_by || "—").replace(/Rs /g, "₹")],
    ] : [];
    const msgs = (p.blocks || []).map((b) => {
      const m = b.match(/would need to be ([\d.]+)/);
      return `<div class="msg block">${esc(b.replace(/Rs /g, "₹"))}${m ? ` <button type="button" class="ghost" style="margin-top:6px" data-use-target="${m[1]}">Use target ${inr(+m[1])}</button>` : ""}</div>`;
    }).concat((p.warnings || []).map((w) => `<div class="msg warn">${esc(w.replace(/Rs /g, "₹"))}</div>`)).join("");
    let odds = "";
    if (p.odds) {
      const o = p.odds;
      const verdict = o.expected_r > 0.15 ? "leans your way" : o.expected_r > -0.15 ? "close to a coin flip" : "leans against you";
      odds = `<div class="odds"><strong>Odds from this stock's own past moves</strong>
        <div class="oddsbar" role="img" aria-label="Target first ${o.target_first}%, neither ${o.neither}%, stop first ${o.stop_first}%">
          <span class="t" style="width:${o.target_first}%"></span><span class="n" style="width:${o.neither}%"></span><span class="s" style="width:${o.stop_first}%"></span></div>
        <div class="oddsrow"><div class="up"><b>${o.target_first.toFixed(0)}%</b>target first</div><div><b>${o.neither.toFixed(0)}%</b>neither by exit</div><div class="down" style="text-align:right"><b>${o.stop_first.toFixed(0)}%</b>stop first</div></div>
        <p>Average result <b>${o.expected_r > 0 ? "+" : ""}${o.expected_r.toFixed(2)}R</b> a trade before charges: ${verdict}.</p>
        <small>Replayed ${inr(o.sims, 0)} times from ${esc(o.basis)}. Uses closes only. Past moves, not a promise.</small></div>`;
    }
    const how = p.entry_rule ? `<div class="howto"><p><strong>Placing it in Groww.</strong> ${esc(p.entry_rule)}</p><p>${esc(p.exit_rule)}</p></div>` : "";
    return `<div class="slip">
      <div class="slip-head"><strong>${esc(p.symbol)} ${esc(p.mode)}${date ? ", " + esc(date) : ""}</strong>
        <span class="verdict ${ok ? "ok" : "no"}">${ok ? "Fits your rules" : "Doesn't fit"}</span></div>
      ${ladder}
      ${rows.length ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : ""}
      ${msgs ? `<div class="msgs">${msgs}</div>` : ""}
      ${how}${odds}</div>`;
  }

  // ---------------------------------------------------------------- ranges
  function renderRanges() {
    const r = STOCK.ranges, el = $("#ranges");
    if (!r) { el.innerHTML = "<p class='hint'>Not enough history to work out ranges.</p>"; return; }
    const R = r.ranges, hs = [5, 21, 63];
    const hist = STOCK.daily.slice(-120).map((d) => d[4]);
    const all = hist.concat(hs.flatMap((h) => [R[h].p10, R[h].p90]));
    const lo = Math.min(...all), hi = Math.max(...all);
    const W = 640, H = 230, padR = 64, histW = 360;
    const y = (v) => 12 + (H - 34) * (1 - (v - lo) / (hi - lo || 1));
    const xh = (i) => (i / (hist.length - 1)) * histW;
    const xf = (h) => histW + ((W - padR - histW) * h) / 63;
    const pathHist = hist.map((v, i) => (i ? "L" : "M") + xh(i).toFixed(1) + " " + y(v).toFixed(1)).join("");
    const pts = (k) => [[0, r.last]].concat(hs.map((h) => [h, R[h][k]]));
    const band = (a, b) => pts(a).map(([h, v], i) => (i ? "L" : "M") + xf(h).toFixed(1) + " " + y(v).toFixed(1)).join("") +
      pts(b).reverse().map(([h, v]) => "L" + xf(h).toFixed(1) + " " + y(v).toFixed(1)).join("") + "Z";
    const mid = pts("p50").map(([h, v], i) => (i ? "L" : "M") + xf(h).toFixed(1) + " " + y(v).toFixed(1)).join("");
    const labels = hs.map((h) => `<text x="${xf(h) - 4}" y="${H - 6}" text-anchor="end">${h === 5 ? "1 wk" : h === 21 ? "1 mo" : "3 mo"}</text>`).join("");
    const ind = cssVar("--indigo");
    el.innerHTML = `<p class="hint" style="margin-top:0">Where the price has room to go, from replaying this stock's last ${r.days_used} trading days of moves ${inr(r.sims, 0)} times. A range, not a forecast.</p>
      <svg class="fan" viewBox="0 0 ${W} ${H}" role="img" aria-label="Price ranges for 1 week, 1 month and 3 months">
        <path d="${band("p90", "p10")}" fill="${ind}" opacity=".13"/>
        <path d="${band("p75", "p25")}" fill="${ind}" opacity=".22"/>
        <path d="${pathHist}" fill="none" stroke="${cssVar("--ink-2")}" stroke-width="1.6"/>
        <path d="${mid}" fill="none" stroke="${ind}" stroke-width="2" stroke-dasharray="5 4"/>
        <line x1="${histW}" x2="${histW}" y1="6" y2="${H - 20}" stroke="${cssVar("--line")}"/>
        <text x="${histW - 4}" y="${H - 6}" text-anchor="end">today</text>${labels}
        ${hs.map((h) => `<text x="${W - padR + 6}" y="${y(R[h].p90) + 4}">${inr(R[h].p90, 0)}</text>`).slice(2).join("")}
        <text x="${W - padR + 6}" y="${y(R[63].p50) + 4}" fill="${ind}">${inr(R[63].p50, 0)}</text>
        <text x="${W - padR + 6}" y="${y(R[63].p10) + 4}">${inr(R[63].p10, 0)}</text>
      </svg>
      <table class="rt"><thead><tr><th>In</th><th>Low</th><th>Middle</th><th>High</th><th>Ends higher</th></tr></thead><tbody>
      ${[[5, "1 week"], [21, "1 month"], [63, "3 months"]].map(([h, n]) => `<tr><td>${n}</td><td>${inr(R[h].p10, 0)}</td><td class="mid">${inr(R[h].p50, 0)}</td><td>${inr(R[h].p90, 0)}</td><td>${R[h].prob_up.toFixed(0)}%</td></tr>`).join("")}
      </tbody></table>
      <p class="hint">8 in 10 replays ended between Low and High. Results, news or a market fall can push the price outside them.</p>
      ${STOCK.hit_rate ? `<p class="note">How far to trust this: at ${STOCK.hit_rate.checks} past dates, the 1-month range held the real price ${STOCK.hit_rate.inside} times (${STOCK.hit_rate.hit_rate.toFixed(0)}%). 80% is the mark; ${STOCK.hit_rate.hit_rate < 70 ? "this stock broke out of its ranges more often than it should, so treat them as too narrow" : STOCK.hit_rate.hit_rate > 90 ? "the ranges ran a little wide for this stock" : "for this stock they've been about right"}.</p>` : ""}`;
  }

  // ---------------------------------------------------------------- scan
  const KIND_HINTS = {
    swing: "Best after 3:30 PM, on the day's close. Buy only above the trigger next session.",
    positional: "Good on weekends. Trend setups to hold for weeks.",
    premarket: "Run before 9:15 AM. Quiet stocks that often move; watch yesterday's high.",
    orb: "From 9:31 AM. A breakout counts only when a 5-minute candle closes above the 9:15–9:30 high.",
  };
  const kindSel = $("#sc-kind"), uniSel = $("#sc-uni");
  const hintKind = () => { $("#sc-hint").textContent = KIND_HINTS[kindSel.value] + (["nse_all", "bse_all", "all"].includes(uniSel.value) ? " Big list: the first scan takes a few minutes." : ""); };
  kindSel.addEventListener("change", hintKind); uniSel.addEventListener("change", hintKind);
  let scanning = false;
  $("#sc-run").addEventListener("click", async () => {
    if (scanning) { scanning = false; return; }
    scanning = true;
    const btn = $("#sc-run"), bar = $("#sc-progress"), meta = $("#sc-meta"), list = $("#sc-results");
    btn.textContent = "Stop"; bar.hidden = false; bar.firstElementChild.style.width = "2%";
    list.innerHTML = ""; meta.textContent = "Starting…";
    let offset = 0, hits = [], total = 0, failed = 0, source = "";
    const kind = kindSel.value, uni = uniSel.value;
    try {
      while (offset != null && scanning) {
        const r = await api("scan", { universe: uni, kind, offset, limit: 60 });
        total = r.total; failed += r.error_count; source = r.source;
        hits = hits.concat(r.hits);
        offset = r.next;
        bar.firstElementChild.style.width = Math.round(((offset ?? total) / total) * 100) + "%";
        meta.textContent = `${offset ?? total} of ${total} stocks checked, ${countHits(hits, kind)} found`;
        renderHits(hits, kind);
      }
      meta.textContent = `${total} stocks checked, ${countHits(hits, kind)} found${failed ? `, ${failed} couldn't load` : ""}${source !== "live" ? ` (${source})` : ""}.`;
    } catch (e) { meta.textContent = e.message; }
    scanning = false; btn.textContent = "Run scan"; setTimeout(() => { bar.hidden = true; }, 600);
  });
  const ORDER = { triggered: 0, waiting: 1, not_ready: 2, expired: 3, failed: 4 };
  function countHits(h, kind) { return kind === "orb" ? h.filter((x) => x.status === "triggered" || x.status === "waiting").length : h.length; }
  function renderHits(hits, kind) {
    let rows = hits.slice();
    if (kind === "orb") rows = rows.filter((x) => x.status !== "not_ready").sort((a, b) => ORDER[a.status] - ORDER[b.status] || (a.distance_pct || 0) - (b.distance_pct || 0));
    else rows.sort((a, b) => b.score - a.score);
    $("#sc-results").innerHTML = rows.slice(0, 150).map((h, k) => {
      const why = kind === "orb"
        ? `<span class="st-${h.status}">${h.status === "triggered" ? "Broke out at " + h.entry_time : h.status === "waiting" ? h.distance_pct.toFixed(2) + "% below the range top" : esc(h.note || h.status)}</span>`
        : esc((SETUP_NAMES[h.setup] || h.setup) + (h.risk_pct != null ? `, ${h.risk_pct.toFixed(1)}% to the stop` : ""));
      const live = kind !== "orb" || h.status === "triggered" || h.status === "waiting";
      const lv = h.trigger != null && live ? `<b>${inr(h.trigger)}</b>buy above` : "";
      return `<li><button type="button" data-k="${k}"><span class="sym">${esc(h.symbol)}</span><span class="why">${why}</span><span class="lv">${lv}</span></button></li>`;
    }).join("");
    $$("#sc-results button").forEach((b) => b.addEventListener("click", () => {
      const h = rows[+b.dataset.k];
      openStock(h.symbol, h.exchange, h.trigger != null && (kind !== "orb" || h.status === "triggered" || h.status === "waiting") ? { trigger: h.trigger, stop: h.stop_ref, mode: (h.mode || "swing").split("/")[0], setup: h.setup } : null);
    }));
  }

  // ---------------------------------------------------------------- market
  let marketAt = 0;
  async function loadMarket() {
    if (Date.now() - marketAt < 50000) return;
    marketAt = Date.now();
    const holi = (STATUS && STATUS.holidays) || [];
    $("#mk-holidays").innerHTML = holi.map((h) => `<li><span>${esc(h.name)}</span><span>${new Date(h.date + "T00:00:00+05:30").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" })}</span></li>`).join("")
      + ((STATUS && STATUS.muhurat) || []).map((m) => `<li><span>Muhurat trading${m.time ? ", " + esc(m.time) : ""}</span><span>${new Date(m.date + "T00:00:00+05:30").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" })}</span></li>`).join("");
    $("#mk-indices").innerHTML = "<p class='hint'>Loading…</p>";
    let m;
    try { m = await api("market"); } catch (e) { $("#mk-indices").innerHTML = `<div class="error">${esc(e.message)}</div>`; marketAt = 0; return; }
    $("#mk-indices").innerHTML = m.indices.map((i) => {
      if (i.error) return `<div class="idx"><div class="n">${esc(i.name)}</div><div class="hint">${esc(i.error)}</div></div>`;
      const ch = i.price && i.prev_close ? (i.price / i.prev_close - 1) * 100 : null;
      return `<div class="idx"><div class="n">${esc(i.name)}</div><div class="v">${inr(i.price)}</div><div class="${cls(ch)}">${pct(ch)}</div>${spark(i.line.map((p) => p[1]), i.prev_close, ch)}</div>`;
    }).join("");
    const up = m.movers.filter((x) => x.change_pct > 0).slice(0, 5);
    const dn = m.movers.filter((x) => x.change_pct < 0).slice(-5).reverse();
    const list = (rows) => rows.length ? `<ol>${rows.map((r) => `<li><button type="button" data-s="${esc(r.symbol)}" data-e="${esc(r.exchange)}">${esc(r.symbol)}</button><span class="${cls(r.change_pct)}">${pct(r.change_pct)}</span></li>`).join("")}</ol>` : "<p class='hint'>None</p>";
    $("#mk-movers").innerHTML = m.movers_error ? `<p class="hint">${esc(m.movers_error)}</p>` :
      `<div><h4>Up most</h4>${list(up)}</div><div><h4>Down most</h4>${list(dn)}</div>`;
    $$("#mk-movers button").forEach((b) => b.addEventListener("click", () => openStock(b.dataset.s, b.dataset.e)));
  }
  function spark(vals, prev, ch) {
    if (!vals.length) return "";
    const all = vals.concat(prev ? [prev] : []), lo = Math.min(...all), hi = Math.max(...all), W = 160, H = 56;
    const y = (v) => 3 + (H - 6) * (1 - (v - lo) / (hi - lo || 1));
    const d = vals.map((v, i) => (i ? "L" : "M") + ((i / Math.max(1, vals.length - 1)) * W).toFixed(1) + " " + y(v).toFixed(1)).join("");
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${prev ? `<line x1="0" x2="${W}" y1="${y(prev)}" y2="${y(prev)}" stroke="${cssVar("--ink-3")}" stroke-dasharray="3 3" stroke-width="1" vector-effect="non-scaling-stroke"/>` : ""}<path d="${d}" fill="none" stroke="${ch >= 0 ? cssVar("--up") : cssVar("--down")}" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>`;
  }

  // ---------------------------------------------------------------- rules
  async function renderRules() {
    const f = $("#rules");
    for (const [k, v] of Object.entries(rules)) if (f.elements[k]) f.elements[k].value = v;
    $("#pw").value = store.get("key", "");
    const R = (STATUS && STATUS.rules) || {};
    $("#fixed").innerHTML = [
      ["Trades a day", R.max_trades_per_day], ["Open positions at once", R.max_open_positions],
      ["Largest overnight position", (R.max_position_pct ?? 50) + "% of equity"],
      ["Intraday", "cash only, no leverage"],
      ["Skip a trade when charges are over", (R.max_charges_pct_of_risk ?? 25) + "% of its risk"],
      ["Intraday entries", (R.intraday_entry_window || "09:30-11:00").replace("-", " to ") + ", out by " + (R.intraday_flat_by || "15:10")],
      ["Charges", "Groww, checked " + ((STATUS && STATUS.charges_as_of) || "")],
    ].map(([k, v]) => `<li><span>${esc(k)}</span><span>${esc(v ?? "—")}</span></li>`).join("");
    renderAcct();
  }
  function renderAcct() {
    const eq = rules.capital - rules.loss + +rules.gain;
    const room = Math.max(0, rules.drawdown - rules.loss);
    const next = Math.min(rules.risk, room);
    const halted = rules.loss >= rules.drawdown;
    $("#acct").innerHTML = `<div><span>Equity</span><b>${rs(eq, 0)}</b></div><div><span>Loss limit left</span><b>${rs(room, 0)}</b></div><div><span>Next trade may risk</span><b>${rs(next, 0)}</b></div>` +
      (halted ? `<div class="halt">Loss limit reached: no new trades. Every plan will say Doesn't fit until you reset this.</div>` : "");
  }
  $("#rules").addEventListener("input", (e) => {
    const k = e.target.name, v = parseFloat(e.target.value);
    rules[k] = isNaN(v) ? DEFAULT_RULES[k] : v;
    store.set("rules", rules); renderAcct();
  });
  $("#pw").addEventListener("change", (e) => { store.set("key", e.target.value); store.set("index", null); boot(); });

  // ---------------------------------------------------------------- boot
  async function boot() {
    hintKind(); renderRecent();
    await loadStatus();
    if (STATUS && STATUS.state === "open") kindSel.value = "orb";
    else if (STATUS && STATUS.state !== "post-close" && new Date().getHours() < 10) kindSel.value = "premarket";
    hintKind();
    loadIndex();
    const [v, arg] = location.hash.slice(1).split("/");
    if (v === "stock" && arg) { const m = decodeURIComponent(arg).match(/^(.+)\.(NSE|BSE)$/); if (m) return openStock(m[1], m[2]); }
    go(v || "stock", false);
  }
  window.addEventListener("hashchange", () => {
    const [v, arg] = location.hash.slice(1).split("/");
    if (v === "stock" && arg) {
      const m = decodeURIComponent(arg).match(/^(.+)\.(NSE|BSE)$/);
      if (m && !(STOCK && STOCK.symbol === m[1] && STOCK.exchange === m[2])) return openStock(m[1], m[2]);
      return;
    }
    go(v || "stock", false);
  });
  setInterval(loadStatus, 60000);
  boot();
})();
