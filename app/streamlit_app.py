"""India Trader: the web app. Plans only, never places orders.

Run locally:   streamlit run app/streamlit_app.py
Deploy:        Streamlit Community Cloud, main file app/streamlit_app.py
"""

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))         # the core first: scan.py then reuses this module
sys.path.insert(1, os.path.join(ROOT, "india-scanner"))
sys.path.insert(2, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TRADER_DIR", os.path.join("/tmp" if os.name != "nt" else os.getcwd(), "indtrader"))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

import indtrader as it  # noqa: E402
import scan  # noqa: E402

DEMO = os.environ.get("INDTRADER_DEMO") == "1"
if DEMO:
    import demo_data
    demo_data.install(it)

st.set_page_config(page_title="India Trader", page_icon="📈", layout="centered",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 4rem; max-width: 760px;}
  div[data-testid="stMetricValue"] {font-size: 1.25rem;}
  div[data-testid="stMetricLabel"] p {font-size: .78rem; opacity: .75;}
  .it-note {font-size: .8rem; opacity: .7; margin-top: -.4rem;}
  .it-plan {border: 1px solid rgba(128,128,128,.35); border-radius: 12px; padding: .9rem 1rem; margin: .4rem 0 .8rem;}
  .it-plan table {width: 100%; border-collapse: collapse; font-size: .92rem;}
  .it-plan td {padding: .28rem 0; border-bottom: 1px solid rgba(128,128,128,.15); vertical-align: top;}
  .it-plan td:first-child {opacity: .7; width: 42%;}
  .it-ok {color: #1a7f4b; font-weight: 600;} .it-bad {color: #c0392b; font-weight: 600;}
  @media (prefers-color-scheme: dark) { .it-ok {color: #4cc38a;} .it-bad {color: #ff7b72;} }
  /* phones: two boxes per row instead of one long column */
  @media (max-width: 640px) {
    .block-container {padding-left: 1rem; padding-right: 1rem; padding-top: 1rem;}
    div[data-testid="stHorizontalBlock"] {flex-direction: row !important; flex-wrap: wrap !important; gap: .6rem !important;}
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
      flex: 1 1 calc(50% - .6rem) !important; min-width: calc(50% - .6rem) !important; width: calc(50% - .6rem) !important;}
    h1 {font-size: 1.6rem !important;}
  }
  header[data-testid="stHeader"] {height: 0; min-height: 0;}
  div[data-testid="stSegmentedControl"] button {padding-left: .55rem; padding-right: .55rem;}
</style>
""", unsafe_allow_html=True)

UNIVERSE_LABELS = {"nifty50": "Nifty 50", "nifty200": "Nifty 200", "banknifty": "Bank Nifty",
                   "nse_all": "All NSE", "bse_all": "All BSE", "all": "All India (NSE + BSE-only)"}
SNAPSHOT_DIR = os.path.join(ROOT, "data", "universe")
UP, DOWN = "#1a9e5c", "#d64545"


# --------------------------------------------------------------------------
# Data (cached)
# --------------------------------------------------------------------------

@st.cache_data(ttl=24 * 3600, show_spinner=False)
def universe(name):
    """Live list, else the snapshot committed in data/universe/."""
    try:
        return it.load_universe(name), "live"
    except SystemExit as e:
        snap = it.read_json(os.path.join(SNAPSHOT_DIR, name + ".json"))
        if snap:
            return snap["items"], "snapshot from %s" % snap.get("fetched", "?")[:10]
        if name == "all":
            try:
                nse, _ = universe("nse_all")
                bse, _ = universe("bse_all")
                return it.merge_all(nse, bse), "snapshot"
            except Exception:
                pass
        raise RuntimeError(str(e))


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def search_index():
    seen, out = set(), []
    for name in ("nifty200", "nse_all", "bse_all"):
        try:
            items, _ = universe(name)
        except RuntimeError:
            continue
        for i in items:
            key = (i["isin"] or i["symbol"], )
            if key in seen and i["exchange"] == "BSE":
                continue
            seen.add(key)
            out.append(i)
    return out


@st.cache_data(ttl=900, show_spinner=False)
def daily(yahoo, exchange):
    r = it.fetch_bars(yahoo, exchange, "1d", "2y")
    return it.completed_bars(r["bars"], "1d"), r["meta"]


@st.cache_data(ttl=60, show_spinner=False)
def intraday(yahoo, exchange, rng="5d"):
    r = it.fetch_bars(yahoo, exchange, "5m", rng)
    return r["bars"], r["meta"]


@st.cache_data(ttl=60, show_spinner=False)
def quote(yahoo, exchange):
    r = it.fetch_bars(yahoo, exchange, "5m", "1d")
    return r["meta"], r["bars"]


@st.cache_data(ttl=1800, show_spinner=False)
def run_daily_scan(uni, mode, stamp):
    items, src = universe(uni)
    wanted = set(scan.SWING if mode == "swing" else scan.POSITIONAL if mode == "positional"
                 else scan.SWING + scan.POSITIONAL)
    cfg = it.DEFAULT_CONFIG

    def one(inst):
        return scan.daily_setups(inst, daily(inst["yahoo"], inst["exchange"])[0], cfg, wanted)
    hits, errors = scan.run_parallel(one, items, 8)
    return sorted(hits, key=lambda h: -h["score"]), errors, len(items), src


@st.cache_data(ttl=1800, show_spinner=False)
def run_premarket_scan(uni, stamp):
    items, src = universe(uni)
    items = [i for i in items if i.get("intraday_allowed", True)]

    def one(inst):
        return scan.premarket_setups(inst, daily(inst["yahoo"], inst["exchange"])[0], it.DEFAULT_CONFIG)
    hits, errors = scan.run_parallel(one, items, 8)
    return sorted(hits, key=lambda h: -h["score"]), errors, len(items), src


@st.cache_data(ttl=60, show_spinner=False)
def run_orb_scan(uni, minutes, window, stamp):
    items, src = universe(uni)
    items = [i for i in items if i.get("intraday_allowed", True)]

    def one(inst):
        return scan.orb_status(inst, intraday(inst["yahoo"], inst["exchange"])[0], minutes, window=window)
    rows, errors = scan.run_parallel(one, items, 8)
    return rows, errors, len(items), src


# --------------------------------------------------------------------------
# Rules (per browser session; the defaults are the agreed plan)
# --------------------------------------------------------------------------

def rules():
    ss = st.session_state
    cfg = dict(it.DEFAULT_CONFIG)
    for k in ("capital", "max_drawdown", "risk_per_trade", "min_reward_risk"):
        if k in ss:
            cfg[k] = ss[k]
    journal = []
    loss = ss.get("loss_so_far", 0.0)
    if loss:
        journal.append({"status": "closed", "net_pnl": -abs(loss), "entry_date": "2000-01-01"})
    gain = ss.get("gain_so_far", 0.0)
    if gain:
        journal.append({"status": "closed", "net_pnl": abs(gain), "entry_date": "2000-01-01"})
    return cfg, it.account_state(cfg, journal)


def rs(x, d=2):
    return "—" if x is None else "₹" + it.money(x)[: None if d == 2 else -3]


def pct(x):
    return "—" if x is None else "%+.2f%%" % x


NAV = [("stock", "Stock"), ("scanner", "Scan"), ("market", "Market"), ("rules", "Rules")]


def nav(current):
    """A tappable menu row at the top of every page (Streamlit's own menu hides on phones)."""
    labels = [l for _, l in NAV]
    here = dict(NAV)[current]
    pick = st.segmented_control("Go to", labels, default=here, key="nav_" + current, label_visibility="collapsed")
    if pick and pick != here:
        st.switch_page(PAGES[[k for k, l in NAV if l == pick][0]])


def demo_banner():
    if DEMO:
        st.warning("Demo mode: these prices are generated, not real.", icon="🧪")


def market_line():
    s = it.market_status()
    icon = {"open": "🟢", "pre-open": "🟡", "pre-open-matching": "🟡"}.get(s["state"], "⚪")
    extra = (" · closes 15:30" if s["state"] == "open" else
             " · opens %s" % dt.datetime.fromisoformat(s["next_open"]).strftime("%a %d %b, %H:%M") if "next_open" in s else "")
    st.caption("%s Market **%s** — %s%s · %s IST" % (icon, s["state"].replace("-", " "), s["reason"], extra,
                                                     it.now_ist().strftime("%a %d %b %H:%M")))
    return s


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

def candle_chart(bars, show_sma=True, height=360, title=None):
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"])
    fig = go.Figure(go.Candlestick(x=df["t"], open=df["o"], high=df["h"], low=df["l"], close=df["c"],
                                   increasing_line_color=UP, decreasing_line_color=DOWN, name="price",
                                   increasing_fillcolor=UP, decreasing_fillcolor=DOWN))
    if show_sma:
        for n, color in ((20, "#e0a100"), (50, "#3b82f6"), (200, "#8b5cf6")):
            if len(df) >= n:
                fig.add_trace(go.Scatter(x=df["t"], y=df["c"].rolling(n).mean(), mode="lines",
                                         line=dict(width=1.3, color=color), name="%d-day avg" % n))
    fig.update_layout(height=height, margin=dict(l=4, r=4, t=28 if title else 8, b=4), title=title,
                      xaxis_rangeslider_visible=False, showlegend=show_sma,
                      legend=dict(orientation="h", y=1.02, x=0, font=dict(size=10)),
                      yaxis=dict(side="right", tickformat=",.0f"), hovermode="x unified")
    return fig


def add_levels(fig, levels):
    for y, label, color in levels:
        if y:
            fig.add_hline(y=y, line=dict(color=color, width=1, dash="dot"),
                          annotation_text="%s %s" % (label, it.money(y)), annotation_position="top left",
                          annotation_font_size=10)
    return fig


def fan_chart(bars, ranges):
    hist = bars[-120:]
    df = pd.DataFrame(hist)
    df["t"] = pd.to_datetime(df["t"])
    last_t = df["t"].iloc[-1]
    last = hist[-1]["c"]
    xs = [last_t] + [pd.Timestamp(it.add_trading_days(last_t.date(), h).isoformat()) for h in sorted(ranges)]
    get = lambda k: [last] + [ranges[h][k] for h in sorted(ranges)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["t"], y=df["c"], mode="lines", line=dict(color="#64748b", width=1.6), name="close"))
    fig.add_trace(go.Scatter(x=xs, y=get("p90"), mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=xs, y=get("p10"), mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(59,130,246,.15)", name="80% range"))
    fig.add_trace(go.Scatter(x=xs, y=get("p75"), mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=xs, y=get("p25"), mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(59,130,246,.3)", name="50% range"))
    fig.add_trace(go.Scatter(x=xs, y=get("p50"), mode="lines+markers", line=dict(color="#3b82f6", width=2, dash="dot"),
                             name="middle"))
    fig.update_layout(height=300, margin=dict(l=4, r=4, t=8, b=4), yaxis=dict(side="right", tickformat=",.0f"),
                      legend=dict(orientation="h", y=1.05, x=0, font=dict(size=10)), hovermode="x unified")
    return fig


CHART_CFG = {"displayModeBar": False, "scrollZoom": False}


# --------------------------------------------------------------------------
# Stock page
# --------------------------------------------------------------------------

def label(i):
    return "%s · %s (%s)" % (i["symbol"], i["name"][:40] or "—", i["exchange"])


def stock_stats(bars):
    ctx = scan.context(bars)
    c = ctx["c"]
    last = bars[-1]
    hi52 = max(b["h"] for b in bars[-252:])
    lo52 = min(b["l"] for b in bars[-252:])
    s20, s50, s200 = ctx["s20"][-1], ctx["s50"][-1], ctx["s200"][-1]
    above = [n for n, v in (("20", s20), ("50", s50), ("200", s200)) if v and last["c"] > v]
    if s50 and s200 and last["c"] > s50 > s200:
        trend = "Uptrend"
    elif s50 and s200 and last["c"] < s50 < s200:
        trend = "Downtrend"
    else:
        trend = "Sideways / mixed"
    ret = lambda n: (c[-1] / c[-1 - n] - 1) * 100 if len(c) > n else None
    return {"hi52": hi52, "lo52": lo52, "from_hi": (last["c"] / hi52 - 1) * 100,
            "pos52": (last["c"] - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None,
            "rsi": ctx["rsi"][-1], "atr_pct": ctx["atr_now"] / last["c"] * 100 if ctx["atr_now"] else None,
            "value_cr": ctx["value_cr"], "vol_ratio": ctx["vol_ratio"], "trend": trend, "above": above,
            "s20": s20, "s50": s50, "s200": s200, "r1w": ret(5), "r1m": ret(21), "r3m": ret(63), "r1y": ret(250)}


def plan_card(p):
    ok = p["status"] == "ok"
    head = '<span class="it-ok">✓ Fits your rules</span>' if ok else '<span class="it-bad">✗ Blocked</span>'
    rows = [("Stock", "%s (%s) · %s" % (p["symbol"], p["exchange"], p["mode"].title())),
            ("Date", dt.date.fromisoformat(p["date"]).strftime("%a %d %b %Y") if p.get("date") else "—"),
            ("Entry", "Above %s" % rs(p["entry"])),
            ("Stop loss", "%s (−%.2f%%)" % (rs(p["stop"]), p.get("stop_pct") or 0)),
            ("Target", rs(p.get("target"))),
            ("Quantity", "%s shares · %s" % (p.get("qty", 0), rs(p.get("position_value")))),
            ("If stopped out", "−%s net" % rs(p.get("loss_at_stop"))),
            ("If target hit", "+%s net" % rs(p.get("gain_at_target"))),
            ("Reward : risk", "%.2f (after charges)" % p["reward_risk"] if p.get("reward_risk") is not None else "—"),
            ("Charges", "%s round trip at target" % rs(p.get("charges_at_target"))),
            ("Breakeven", rs(p.get("breakeven"))),
            ("Exit by", p.get("exit_by", "—").replace("T", " "))]
    html = "<div class='it-plan'>%s<table>%s</table></div>" % (
        head, "".join("<tr><td>%s</td><td>%s</td></tr>" % r for r in rows))
    st.markdown(html, unsafe_allow_html=True)
    for b in p["blocks"]:
        st.error(b.replace("Rs ", "₹"), icon="⛔")
    for w in p["warnings"]:
        st.warning(w.replace("Rs ", "₹"), icon="⚠️")
    if p.get("entry_rule"):
        st.markdown("**How to place it in Groww**  \n%s  \n%s" % (p["entry_rule"], p["exit_rule"]))
    if p.get("qty_limited_by"):
        st.caption("Quantity limited by: %s." % p["qty_limited_by"])


def page_stock():
    nav('stock')
    demo_banner()
    status = market_line()
    st.title("Stock lookup")
    idx = search_index()
    chosen = None
    preset = st.session_state.pop("open_symbol", None)
    options = [label(i) for i in idx]
    if idx:
        default = None
        if preset:
            default = next((k for k, i in enumerate(idx) if i["symbol"] == preset[0] and i["exchange"] == preset[1]), None)
        pick = st.selectbox("Search by name or symbol", options, index=default, placeholder="e.g. Reliance, TCS, HDFC Bank")
        if pick:
            chosen = idx[options.index(pick)]
    with st.expander("Not in the list? Type the NSE/BSE symbol"):
        c1, c2 = st.columns([3, 1])
        raw = c1.text_input("Symbol", placeholder="TATAMOTORS or 500570", label_visibility="collapsed")
        ex = c2.selectbox("Exchange", ["NSE", "BSE"], label_visibility="collapsed")
        if raw.strip():
            chosen = it._instrument(raw.strip(), "", ex)
    if preset and not chosen:
        chosen = it._instrument(preset[0], "", preset[1])
    if not chosen:
        st.info("Pick a stock to see its chart, trend, setups, a trade plan for your ₹%s account and its likely price ranges."
                % it.money(it.DEFAULT_CONFIG["capital"])[:-3])
        return

    try:
        with st.spinner("Loading %s…" % chosen["symbol"]):
            bars, meta = daily(chosen["yahoo"], chosen["exchange"])
            qmeta, qbars = quote(chosen["yahoo"], chosen["exchange"])
    except it.FetchError as e:
        st.error("Couldn't load %s: %s. Check the symbol and exchange." % (chosen["yahoo"], e))
        return
    if len(bars) < 30:
        st.error("Not enough price history for %s yet." % chosen["symbol"])
        return
    price = qmeta.get("price") or bars[-1]["c"]
    prev = qmeta.get("prev_close") or (bars[-2]["c"] if len(bars) > 1 else None)
    stats = stock_stats(bars)
    name = chosen["name"] or chosen["symbol"]
    st.subheader("%s" % name)
    c1, c2, c3 = st.columns(3)
    c1.metric("Price", rs(price), pct((price / prev - 1) * 100) if prev else None)
    c2.metric("52-week range", "%s – %s" % (it.money(stats["lo52"])[:-3], it.money(stats["hi52"])[:-3]))
    c3.metric("Trend", stats["trend"])
    st.markdown("<div class='it-note'>%.1f%% below its 52-week high · %s</div>" % (
        -stats["from_hi"], "above its %s-day averages" % "/".join(stats["above"]) if stats["above"] else "below its 20, 50 and 200-day averages"),
        unsafe_allow_html=True)
    st.markdown("<div class='it-note'>%s · %s · data from Yahoo Finance, can lag the exchange by a few minutes</div>"
                % (chosen["yahoo"], "as of %s" % (qbars[-1]["t"][11:16] + " IST" if qbars else bars[-1]["t"][:10])),
                unsafe_allow_html=True)

    view = st.segmented_control("Chart", ["Today", "1M", "6M", "1Y", "2Y"], default="6M", label_visibility="collapsed")
    if view == "Today":
        today = it.session_bars(qbars) if qbars else []
        if today:
            st.plotly_chart(candle_chart(today, show_sma=False, height=320), width="stretch", config=CHART_CFG)
        else:
            st.caption("No candles yet today.")
    else:
        n = {"1M": 22, "6M": 126, "1Y": 252, "2Y": 520}[view or "6M"]
        st.plotly_chart(candle_chart(bars[-max(n, 30) - 200:], height=360).update_xaxes(
            range=[bars[-min(n, len(bars))]["t"], bars[-1]["t"]]), width="stretch", config=CHART_CFG)

    t1, t2, t3, t4 = st.tabs(["Numbers", "Setups & plan", "Price ranges", "Odds"])
    with t1:
        a, b, c, d = st.columns(4)
        a.metric("1 week", pct(stats["r1w"]))
        b.metric("1 month", pct(stats["r1m"]))
        c.metric("3 months", pct(stats["r3m"]))
        d.metric("1 year", pct(stats["r1y"]))
        a, b, c, d = st.columns(4)
        rsi_v = stats["rsi"]
        a.metric("RSI (14)", "—" if rsi_v is None else "%.0f%s" % (rsi_v, " · oversold" if rsi_v < 30 else " · overbought" if rsi_v > 70 else ""))
        b.metric("Daily move (ATR)", "%.2f%%" % stats["atr_pct"] if stats["atr_pct"] else "—")
        c.metric("Traded / day", "₹%.0f cr" % stats["value_cr"] if stats["value_cr"] else "—")
        d.metric("Volume vs avg", "%.1f×" % stats["vol_ratio"] if stats["vol_ratio"] else "—")
        a, b, c = st.columns(3)
        a.metric("20-day avg", rs(stats["s20"]))
        b.metric("50-day avg", rs(stats["s50"]))
        c.metric("200-day avg", rs(stats["s200"]))
        if not chosen.get("intraday_allowed", True):
            st.warning("Trade-to-trade stock: delivery only, no intraday.")

    cfg, acct = rules()
    with t2:
        hits = scan.daily_setups(chosen, bars, dict(cfg, min_avg_traded_value_cr=0, min_price=0),
                                 set(scan.SWING + scan.POSITIONAL))
        hits += scan.premarket_setups(chosen, bars, dict(cfg, min_avg_traded_value_cr=0, min_price=0))
        orb = None
        if status["state"] == "open" and chosen.get("intraday_allowed", True):
            orb = scan.orb_status(chosen, intraday(chosen["yahoo"], chosen["exchange"])[0],
                                  window=tuple(cfg["intraday_entry_window"].split("-")))
        if hits:
            st.markdown("**Setups on the %s close**" % bars[-1]["t"][:10])
            for h in hits:
                st.markdown("- **%s** (%s): %s. Buy above **%s**, wrong below **%s**."
                            % (h["setup"].replace("_", " "), h["mode"], h["why"], it.money(h["trigger"]), it.money(h["stop_ref"])))
        else:
            st.caption("No scanner setup on the last close. You can still plan your own levels below.")
        if orb and orb.get("status") not in (None, "not_ready"):
            st.markdown("**Opening range today:** %s – %s · **%s**%s" % (
                it.money(orb["or_low"]), it.money(orb["or_high"]), orb["status"],
                " at %s (%s)" % (orb["entry_time"], it.money(orb["entry_price"])) if orb["status"] == "triggered" else
                " · %.2f%% below the high" % orb["distance_pct"] if orb["status"] == "waiting" else ""))

        st.markdown("---")
        st.markdown("**Trade plan**")
        choices = ["%s · %s" % (h["setup"].replace("_", " "), h["mode"]) for h in hits]
        if orb and orb.get("trigger"):
            choices.insert(0, "opening range · intraday")
        choices.append("my own levels")
        pick = st.selectbox("Based on", choices, key="plan_src_%s" % chosen["symbol"])
        atr_now = scan.context(bars)["atr_now"] or bars[-1]["c"] * 0.02
        if pick.startswith("opening range"):
            src = {"trigger": orb["trigger"], "stop_ref": orb["stop_ref"], "mode": "intraday", "setup": "orb"}
        elif pick == "my own levels":
            src = {"trigger": it.tick_round(bars[-1]["h"] + 0.05, mode="up"),
                   "stop_ref": it.tick_round(bars[-1]["c"] - 1.5 * atr_now, mode="down"), "mode": "swing", "setup": "manual"}
        else:
            src = hits[choices.index(pick) - (1 if orb and orb.get("trigger") else 0)]
        modes = ["intraday", "swing", "positional"]
        default_mode = src["mode"].split("/")[0] if src["mode"].split("/")[0] in modes else "swing"
        c1, c2, c3 = st.columns(3)
        key = "%s_%s" % (chosen["symbol"], pick)
        mode = c1.selectbox("Mode", modes, index=modes.index(default_mode), key="m_" + key)
        entry = c2.number_input("Entry above", value=float(src["trigger"]), step=0.05, format="%.2f", key="e_" + key)
        stop = c3.number_input("Stop loss", value=float(src["stop_ref"]), step=0.05, format="%.2f", key="s_" + key)
        tgt = st.number_input("Target (0 = automatic: %sR)" % it.MODES[mode]["rr"], value=0.0, step=0.05,
                              format="%.2f", key="t_" + key)
        p = it.build_plan(chosen["symbol"], entry, stop, mode, target=tgt or None, exchange=chosen["exchange"],
                          cfg=cfg, account=acct, intraday_allowed=chosen.get("intraday_allowed", True), setup=src["setup"])
        plan_card(p)
        st.session_state["last_plan"] = p

    with t3:
        r = it.price_ranges([b["c"] for b in bars])
        if not r:
            st.caption("Not enough history for ranges.")
        else:
            st.markdown("Where the price could be, based on replaying this stock's own last %d trading days of moves "
                        "%s times. **This is a range of possibilities, not a prediction.**" % (r["days_used"], it.money(r["sims"])[:-3]))
            st.plotly_chart(fan_chart(bars, r["ranges"]), width="stretch", config=CHART_CFG)
            rows = []
            for h, lab in ((5, "1 week"), (21, "1 month"), (63, "3 months")):
                x = r["ranges"][h]
                rows.append({"In": lab, "Middle": it.money(x["p50"]), "Low": it.money(x["p10"]),
                             "High": it.money(x["p90"]), "Up?": "%.0f%%" % x["prob_up"]})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("Low and High are the 10% and 90% marks: 8 in 10 replays ended between them. "
                       "Up? is the share of replays that ended above today's price.")
            hr = it.range_hit_rate([b["c"] for b in bars])
            if hr:
                verdict = ("well calibrated" if 70 <= hr["hit_rate"] <= 90 else
                           "too narrow: the real price escaped more often than it should" if hr["hit_rate"] < 70 else
                           "a bit wide")
                st.info("**Honesty check:** at %d past dates, the 1-month 80%% range held the real price %d times (%.0f%%). "
                        "80%% would be perfect, so for this stock the ranges look %s."
                        % (hr["checks"], hr["inside"], hr["hit_rate"], verdict), icon="🧭")
            st.caption("Ranges assume the next months move like the last two years. Results, news, or a market crash "
                       "can push the price outside them.")

    with t4:
        p = st.session_state.get("last_plan")
        if not p or p["symbol"] != chosen["symbol"] or not p.get("target"):
            st.caption("Make a plan in the Setups & plan tab first.")
        else:
            if p["mode"] == "intraday":
                ib = it.completed_bars(intraday(chosen["yahoo"], chosen["exchange"], "60d")[0], "5m")
                rets = it.log_returns([b["c"] for b in it.session_bars(ib)])
                start = max(it.now_ist(), dt.datetime.combine(it.now_ist().date(), dt.time(9, 30), it.IST))
                flat = dt.datetime.combine(start.date(), it.parse_hhmm(cfg["intraday_flat_by"]), it.IST)
                steps = max(1, int((flat - start).total_seconds() // 300))
                basis = "%d five-minute moves from the last 60 days, %d candles until %s" % (len(rets), steps, cfg["intraday_flat_by"])
            else:
                rets = it.log_returns([b["c"] for b in bars[-500:]])
                steps = it.MODES[p["mode"]]["max_days"]
                basis = "daily moves over the last 2 years, %d trading days to the exit date" % steps
            o = it.path_odds(rets, p["entry"], p["stop"], p["target"], steps)
            if o:
                a, b, c = st.columns(3)
                a.metric("Target first", "%.0f%%" % o["target_first"])
                b.metric("Stop first", "%.0f%%" % o["stop_first"])
                c.metric("Neither by exit", "%.0f%%" % o["neither"])
                verdict = ("leans your way" if o["expected_r"] > 0.15 else
                           "close to a coin flip" if o["expected_r"] > -0.15 else "leans against you")
                st.markdown("Average result **%+.2fR** per trade before charges: **%s**." % (o["expected_r"], verdict))
                st.caption("Replayed %s times from %s, using closes only. Past moves, not a promise."
                           % (it.money(o["sims"])[:-3], basis))


# --------------------------------------------------------------------------
# Scanner page
# --------------------------------------------------------------------------

def page_scanner():
    nav('scanner')
    demo_banner()
    s = market_line()
    st.title("Scanner")
    c1, c2 = st.columns(2)
    uni = c1.selectbox("Stocks", list(UNIVERSE_LABELS), index=1, format_func=UNIVERSE_LABELS.get)
    kinds = {"swing": "Swing setups (daily)", "positional": "Positional setups (daily)",
             "premarket": "Intraday candidates (pre-market)", "orb": "Opening-range breakouts (live)"}
    kind = c2.selectbox("Scan", list(kinds), format_func=kinds.get, index=3 if s["state"] == "open" else 0)
    if uni in ("nse_all", "bse_all", "all"):
        st.caption("Large list: the first scan can take several minutes. Later scans are faster.")
    if not st.button("Run scan", type="primary", width="stretch") and "scan_result" not in st.session_state:
        return
    try:
        with st.spinner("Scanning %s…" % UNIVERSE_LABELS[uni]):
            stamp = it.last_session_close().isoformat()
            if kind in ("swing", "positional"):
                hits, errors, n, src = run_daily_scan(uni, kind, stamp)
            elif kind == "premarket":
                hits, errors, n, src = run_premarket_scan(uni, stamp)
            else:
                if not it.is_trading_day(it.now_ist().date()):
                    st.info("Market closed today: no opening range. Try a daily scan.")
                    return
                minute = it.now_ist().strftime("%H:%M")
                hits, errors, n, src = run_orb_scan(uni, 15, tuple(it.DEFAULT_CONFIG["intraday_entry_window"].split("-")), minute)
    except RuntimeError as e:
        st.error("Couldn't load the %s list. %s" % (UNIVERSE_LABELS[uni], e))
        return
    st.session_state["scan_result"] = True
    st.caption("%d stocks scanned (%s list) · %d hits%s" % (n, src, len(hits), " · %d failed to load" % len(errors) if errors else ""))
    if not hits:
        st.info("No setups right now.")
        return
    if kind == "orb":
        order = {"triggered": 0, "waiting": 1, "not_ready": 2, "expired": 3, "failed": 4}
        hits = sorted(hits, key=lambda r: (order[r["status"]], r.get("distance_pct") or 0))
        df = pd.DataFrame([{"Stock": h["symbol"], "Status": h["status"], "Detail":
                            ("%s at %s" % (it.money(h["entry_price"]), h["entry_time"]) if h["status"] == "triggered" else
                             "%.2f%% below" % h["distance_pct"] if h["status"] == "waiting" else h.get("note", "")),
                            "Range high": h.get("or_high"), "Range low": h.get("or_low"), "Last": h.get("last"),
                            "Gap %": h.get("gap_pct"), "Rel vol": h.get("rvol")} for h in hits])
    else:
        df = pd.DataFrame([{"Stock": h["symbol"], "Setup": h["setup"].replace("_", " "),
                            "Buy above": h["trigger"], "Wrong below": h["stop_ref"], "Mode": h["mode"], "Close": h["close"],
                            "Risk %": h.get("risk_pct"), "RSI": h.get("rsi"), "Vol ×": h.get("vol_ratio"),
                            "₹ cr/day": h.get("avg_value_cr")} for h in hits])
    st.dataframe(df, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(df)))
    sym = st.selectbox("Open a stock", [h["symbol"] for h in hits], index=None, placeholder="Tap to plan one")
    if sym:
        h = next(x for x in hits if x["symbol"] == sym)
        st.session_state["open_symbol"] = (h["symbol"], h["exchange"])
        st.switch_page(PAGES["stock"])


# --------------------------------------------------------------------------
# Market page
# --------------------------------------------------------------------------

def page_market():
    nav('market')
    demo_banner()
    s = market_line()
    st.title("Market")
    cols = st.columns(2)
    for col, (sym, name) in zip(cols, (("^NSEI", "Nifty 50"), ("^NSEBANK", "Bank Nifty"))):
        try:
            meta, bars = quote(sym, "NSE")
        except it.FetchError as e:
            col.error("%s: %s" % (name, e))
            continue
        price, prev = meta.get("price"), meta.get("prev_close")
        col.metric(name, it.money(price) if price else "—", pct((price / prev - 1) * 100) if price and prev else None)
        today = it.session_bars(bars)
        if today:
            today = [b for b in today if b["t"][:10] == today[-1]["t"][:10]]
            df = pd.DataFrame(today)
            df["t"] = pd.to_datetime(df["t"])
            color = UP if price and prev and price >= prev else DOWN
            fig = go.Figure(go.Scatter(x=df["t"], y=df["c"], mode="lines", line=dict(color=color, width=1.8)))
            if prev:
                fig.add_hline(y=prev, line=dict(color="#94a3b8", width=1, dash="dot"))
            fig.update_layout(height=170, margin=dict(l=0, r=0, t=0, b=0), yaxis=dict(side="right", tickformat=",.0f"),
                              xaxis=dict(tickformat="%H:%M"))
            col.plotly_chart(fig, width="stretch", config=CHART_CFG)

    st.subheader("Nifty 50 movers")
    try:
        items, _ = universe("nifty50")
    except RuntimeError as e:
        st.error(str(e))
        items = []
    if items:
        rows = []

        def one(i):
            m, _ = quote(i["yahoo"], i["exchange"])
            if m.get("price") and m.get("prev_close"):
                return {"Stock": i["symbol"], "Price": m["price"], "Change %": round((m["price"] / m["prev_close"] - 1) * 100, 2)}
        rows, _ = scan.run_parallel(lambda i: one(i), items, 8)
        if rows:
            df = pd.DataFrame(rows).sort_values("Change %", ascending=False)
            up = (df["Change %"] > 0).sum()
            st.caption("%d up · %d down" % (up, len(df) - up))
            df = df[["Stock", "Change %", "Price"]]
            st.markdown("**Top gainers**")
            gain, loss = df[df["Change %"] > 0].head(5), df[df["Change %"] < 0].tail(5).iloc[::-1]
            st.dataframe(gain, hide_index=True, width="stretch") if len(gain) else st.caption("None up today.")
            st.markdown("**Top losers**")
            st.dataframe(loss, hide_index=True, width="stretch") if len(loss) else st.caption("None down today.")

    st.subheader("Upcoming holidays")
    today = it.now_ist().date().isoformat()
    up = [(d, n) for d, n in sorted(it.HOLIDAYS.items()) if d >= today][:5]
    for d, n in up:
        st.markdown("- **%s** · %s" % (dt.date.fromisoformat(d).strftime("%a %d %b"), n))
    for d, t in it.MUHURAT.items():
        if d >= today:
            st.markdown("- **%s** · Muhurat trading (%s)" % (dt.date.fromisoformat(d).strftime("%a %d %b"),
                                                             t or "timings not announced yet"))


# --------------------------------------------------------------------------
# Rules page
# --------------------------------------------------------------------------

def page_rules():
    nav('rules')
    demo_banner()
    st.title("My rules")
    st.caption("These shape every plan. They're kept for this visit only; the defaults are your agreed plan.")
    d = it.DEFAULT_CONFIG
    ss = st.session_state
    c1, c2 = st.columns(2)
    ss["capital"] = c1.number_input("Capital (₹)", 1000, 10_000_000, int(ss.get("capital", d["capital"])), 1000)
    ss["max_drawdown"] = c2.number_input("Stop everything after losing (₹)", 100, int(ss["capital"]),
                                         int(min(ss.get("max_drawdown", d["max_drawdown"]), ss["capital"])), 100)
    c1, c2 = st.columns(2)
    ss["risk_per_trade"] = c1.number_input("Risk per trade (₹, charges included)", 10, int(ss["max_drawdown"]),
                                           int(min(ss.get("risk_per_trade", d["risk_per_trade"]), ss["max_drawdown"])), 10)
    ss["min_reward_risk"] = c2.number_input("Minimum reward : risk", 1.0, 5.0, float(ss.get("min_reward_risk", d["min_reward_risk"])), 0.1)
    c1, c2 = st.columns(2)
    ss["loss_so_far"] = c1.number_input("Losses so far (₹, net)", 0.0, 1e7, float(ss.get("loss_so_far", 0.0)), 50.0)
    ss["gain_so_far"] = c2.number_input("Profits so far (₹, net)", 0.0, 1e7, float(ss.get("gain_so_far", 0.0)), 50.0)
    cfg, acct = rules()
    errs = it.validate_config(cfg)
    for e in errs:
        st.error(e)
    a, b, c = st.columns(3)
    a.metric("Equity", rs(acct["equity"]))
    b.metric("Loss limit left", rs(max(0, acct["loss_limit"] - acct["loss_used"])))
    c.metric("Next trade may risk", rs(acct["risk_per_trade_now"]))
    if acct["halted"]:
        st.error("STOP: %s" % acct["halt_reason"])
    st.markdown("""
**Fixed rules**
- Trades per day: %d · open positions: %d
- One overnight position: at most %d%% of equity · intraday: cash only, no leverage
- Skip a trade if its charges are over %d%% of its risk
- Intraday entries %s, square off by %s
- Charges: Groww (rates checked %s)
""" % (d["max_trades_per_day"], d["max_open_positions"], d["max_position_pct"], d["max_charges_pct_of_risk"],
       d["intraday_entry_window"], d["intraday_flat_by"], it.GROWW["as_of"]))
    st.caption("Not investment advice. These are plans for you to check and place yourself.")


PAGES = {
    "stock": st.Page(page_stock, title="Stock", icon="🔎", url_path="stock", default=True),
    "scanner": st.Page(page_scanner, title="Scanner", icon="📡", url_path="scanner"),
    "market": st.Page(page_market, title="Market", icon="📊", url_path="market"),
    "rules": st.Page(page_rules, title="My rules", icon="⚙️", url_path="rules"),
}

st.navigation(list(PAGES.values()), position="hidden").run()
