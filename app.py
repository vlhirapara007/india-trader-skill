"""India Trader web API (Flask). Vercel loads `app` from this file; the page lives in public/.

Plans only: nothing here places an order.

Run locally:  pip install flask && flask --app app run      (then open http://127.0.0.1:5000)
Demo prices:  INDTRADER_DEMO=1 flask --app app run
"""

import concurrent.futures as cf
import datetime as dt
import hmac
import math
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "core"))          # the core first, so scan.py reuses this module
sys.path.insert(1, os.path.join(ROOT, "india-scanner"))
sys.path.insert(2, os.path.join(ROOT, "web"))
os.environ.setdefault("TRADER_DIR", "/tmp/indtrader")

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

import indtrader as it  # noqa: E402
import scan  # noqa: E402

DEMO = os.environ.get("INDTRADER_DEMO") == "1"
if DEMO:
    import demo_data
    demo_data.install(it)

PUBLIC = os.path.join(ROOT, "public")
SNAPSHOTS = os.path.join(ROOT, "data", "universe")
PASSWORD = os.environ.get("APP_PASSWORD", "")
SCAN_CHUNK = 60
WORKERS = 12

app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class Cache:
    """In-memory cache for a warm function instance."""

    def __init__(self):
        self.d, self.lock = {}, threading.Lock()

    def get(self, key, ttl, fn):
        now = time.time()
        with self.lock:
            hit = self.d.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self.d[key] = (now, val)
            if len(self.d) > 4000:
                for k in sorted(self.d, key=lambda k: self.d[k][0])[:1000]:
                    self.d.pop(k, None)
        return val


CACHE = Cache()


def bars(ysym, exchange, interval, rng, ttl):
    return CACHE.get(("bars", ysym, interval, rng), ttl, lambda: it.fetch_bars(ysym, exchange, interval, rng))


def daily(inst):
    r = bars(inst["yahoo"], inst["exchange"], "1d", "2y", 900)
    return it.completed_bars(r["bars"], "1d"), r["meta"]


def intraday(inst, rng="5d"):
    return bars(inst["yahoo"], inst["exchange"], "5m", rng, 55)


def reply(data, max_age=60, status=200):
    resp = jsonify(data)
    resp.status_code = status
    if PASSWORD or status != 200:
        resp.headers["Cache-Control"] = "private, no-store"
    else:
        # Vercel's CDN keeps the answer for max_age seconds and serves a stale copy while refreshing
        resp.headers["Cache-Control"] = "public, max-age=0, s-maxage=%d, stale-while-revalidate=%d" % (max_age, max_age * 4)
    return resp


def fail(msg, status=400):
    return reply({"error": msg}, status=status)


def num(name, default=None, lo=None, hi=None):
    v = request.args.get(name)
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except ValueError:
        raise ValueError("%s must be a number" % name)
    if math.isnan(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError("%s is out of range" % name)
    return x


def instrument(symbol, exchange):
    symbol = (symbol or "").strip().upper()
    exchange = (exchange or "NSE").upper()
    if not symbol or len(symbol) > 30 or exchange not in ("NSE", "BSE"):
        raise ValueError("give a symbol and exchange NSE or BSE")
    if symbol in it.INDEX_OPTIONS:
        ix = it.INDEX_OPTIONS[symbol]
        return {"symbol": symbol, "name": ix["name"], "exchange": "NSE", "series": "INDEX", "isin": "",
                "industry": "", "bse_code": "", "intraday_allowed": True, "yahoo": ix["yahoo"], "index": True}
    for name in ("nifty200", "nse_all", "bse_all"):
        try:
            for i in universe(name)[0]:
                if i["symbol"] == symbol and i["exchange"] == exchange:
                    return i
        except RuntimeError:
            pass
    return it._instrument(symbol, "", exchange)


def universe(name):
    def load():
        try:
            return it.load_universe(name), "live"
        except SystemExit as e:
            snap = it.read_json(os.path.join(SNAPSHOTS, name + ".json"))
            if snap:
                return snap["items"], "saved list from %s" % snap.get("fetched", "?")[:10]
            if name == "all":
                nse, _ = universe("nse_all")
                bse, _ = universe("bse_all")
                return it.merge_all(nse, bse), "saved lists"
            raise RuntimeError(str(e).splitlines()[0])
    return CACHE.get(("universe", name), 6 * 3600, load)


def fo_lots():
    return CACHE.get(("fo_lots",), 6 * 3600,
                     lambda: it.load_fo_lots(snapshot=os.path.join(SNAPSHOTS, "fo_lots.json")))


def fo_info(inst):
    if inst["exchange"] != "NSE":
        return None
    try:
        lots = fo_lots()
    except Exception:
        lots = None
    lot = it.lot_size(inst["symbol"], lots)
    if not lot:
        return None
    return {"lot": lot, "expiries": [e.isoformat() for e in it.expiries(inst["symbol"])],
            "weekly": bool(it.INDEX_OPTIONS.get(inst["symbol"], {}).get("weekly"))}


def intraday_returns(inst, cfg):
    ib = it.completed_bars(intraday(inst, "60d")["bars"], "5m")
    rets = it.log_returns([x["c"] for x in it.session_bars(ib)])
    now = it.now_ist()
    start = max(now, dt.datetime.combine(now.date(), dt.time(9, 30), it.IST))
    flat = dt.datetime.combine(start.date(), it.parse_hhmm(cfg["intraday_flat_by"]), it.IST)
    if start >= flat:   # after hours: plan a whole session
        start = dt.datetime.combine(start.date(), dt.time(9, 30), it.IST)
    steps = max(1, int((flat - start).total_seconds() // 300))
    basis = "%d five-minute moves from the last 60 days, %d candles until %s" % (len(rets), steps, cfg["intraday_flat_by"])
    return rets, steps, basis


def rules_from_query():
    cfg = dict(it.DEFAULT_CONFIG)
    cfg["capital"] = num("capital", cfg["capital"], 1000, 1e9)
    cfg["max_drawdown"] = num("drawdown", cfg["max_drawdown"], 1, cfg["capital"])
    cfg["risk_per_trade"] = num("risk", cfg["risk_per_trade"], 1, cfg["max_drawdown"])
    cfg["min_reward_risk"] = num("minrr", cfg["min_reward_risk"], 0.5, 10)
    journal = []
    loss, gain = num("loss", 0, 0, 1e9), num("gain", 0, 0, 1e9)
    if loss:
        journal.append({"status": "closed", "net_pnl": -loss, "entry_date": "2000-01-01"})
    if gain:
        journal.append({"status": "closed", "net_pnl": gain, "entry_date": "2000-01-01"})
    trades_today = int(num("today", 0, 0, 100))
    today = it.now_ist().date().isoformat()
    journal += [{"status": "closed", "net_pnl": 0, "entry_date": today}] * trades_today
    return cfg, it.account_state(cfg, journal)


def compact(i):
    return {"s": i["symbol"], "n": i["name"], "e": i["exchange"], "i": 1 if i.get("intraday_allowed", True) else 0}


@app.before_request
def guard():
    if not PASSWORD or not request.path.startswith("/api/") or request.path == "/api/ping":
        return None
    key = request.headers.get("X-Key") or request.args.get("key") or ""
    if not hmac.compare_digest(key, PASSWORD):
        return reply({"error": "password needed", "auth": True}, status=401)
    return None


@app.errorhandler(ValueError)
def bad_value(e):
    return fail(str(e), 400)


@app.errorhandler(it.FetchError)
def fetch_failed(e):
    return fail("couldn't get prices right now (%s). Try again in a minute." % e, 502)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/ping")
def ping():
    return reply({"ok": True, "demo": DEMO, "auth": bool(PASSWORD), "version": it.VERSION}, 0)


@app.get("/api/status")
def status():
    s = it.market_status()
    today = it.now_ist().date().isoformat()
    s["holidays"] = [{"date": d, "name": n} for d, n in sorted(it.HOLIDAYS.items()) if d >= today][:6]
    s["muhurat"] = [{"date": d, "time": t} for d, t in it.MUHURAT.items() if d >= today]
    s["demo"] = DEMO
    s["rules"] = {k: it.DEFAULT_CONFIG[k] for k in (
        "capital", "max_drawdown", "risk_per_trade", "min_reward_risk", "max_trades_per_day", "max_open_positions",
        "max_position_pct", "intraday_max_position_pct", "max_charges_pct_of_risk", "intraday_entry_window",
        "intraday_flat_by")}
    s["charges_as_of"] = it.GROWW["as_of"]
    return reply(s, 30)


@app.get("/api/search-index")
def search_index():
    seen, out, sources = set(), [], {}
    for sym, ix in it.INDEX_OPTIONS.items():
        out.append({"s": sym, "n": ix["name"], "e": "NSE", "i": 1, "x": 1})
    for name in ("nifty200", "nse_all", "bse_all"):
        try:
            items, src = universe(name)
        except RuntimeError as e:
            sources[name] = "unavailable: %s" % e
            continue
        sources[name] = src
        for i in items:
            key = i["isin"] or (i["symbol"], i["exchange"])
            if key in seen:
                continue
            seen.add(key)
            out.append(compact(i))
    return reply({"items": out, "sources": sources}, 6 * 3600)


@app.get("/api/universe")
def universe_api():
    name = request.args.get("name", "nifty200")
    if name not in it.UNIVERSES:
        raise ValueError("unknown list")
    try:
        items, src = universe(name)
    except RuntimeError as e:
        return fail("couldn't load the %s list: %s" % (name, e), 502)
    return reply({"name": name, "source": src, "count": len(items)}, 3600)


def stock_stats(b):
    ctx = scan.context(b)
    c = ctx["c"]
    last = b[-1]
    yr = b[-252:]
    hi52, lo52 = max(x["h"] for x in yr), min(x["l"] for x in yr)
    s20, s50, s200 = ctx["s20"][-1], ctx["s50"][-1], ctx["s200"][-1]
    if s50 and s200 and last["c"] > s50 > s200:
        trend = "Uptrend"
    elif s50 and s200 and last["c"] < s50 < s200:
        trend = "Downtrend"
    else:
        trend = "Sideways"
    ret = lambda n: round((c[-1] / c[-1 - n] - 1) * 100, 2) if len(c) > n else None
    rnd = lambda x, d=2: None if x is None else round(x, d)
    return {"hi52": hi52, "lo52": lo52, "from_hi_pct": rnd((last["c"] / hi52 - 1) * 100),
            "rsi": rnd(ctx["rsi"][-1], 1), "atr_pct": rnd(ctx["atr_now"] / last["c"] * 100 if ctx["atr_now"] else None),
            "atr": rnd(ctx["atr_now"]), "value_cr": rnd(ctx["value_cr"], 1), "vol_ratio": rnd(ctx["vol_ratio"]),
            "sma20": rnd(s20), "sma50": rnd(s50), "sma200": rnd(s200), "trend": trend,
            "above": [n for n, v in ((20, s20), (50, s50), (200, s200)) if v and last["c"] > v],
            "r1w": ret(5), "r1m": ret(21), "r3m": ret(63), "r1y": ret(250)}


@app.get("/api/stock")
def stock():
    inst = instrument(request.args.get("symbol"), request.args.get("exchange"))
    b, meta = daily(inst)
    if len(b) < 30:
        return fail("not enough price history for %s yet" % inst["symbol"], 404)
    q = intraday(inst)
    status = it.market_status()
    today = [x for x in it.session_bars(it.completed_bars(q["bars"], "5m"))
             if x["t"][:10] == (q["bars"][-1]["t"][:10] if q["bars"] else "")]
    price = q["meta"].get("price") or b[-1]["c"]
    prev = q["meta"].get("prev_close") or b[-1]["c"]
    loose = dict(it.DEFAULT_CONFIG, min_avg_traded_value_cr=0, min_price=0)
    setups = scan.daily_setups(inst, b, loose, set(scan.SWING + scan.POSITIONAL))
    setups += scan.premarket_setups(inst, b, loose)
    orb = None
    if status["state"] == "open" and inst.get("intraday_allowed", True):
        orb = scan.orb_status(inst, q["bars"], 15, window=tuple(it.DEFAULT_CONFIG["intraday_entry_window"].split("-")))
    closes = [x["c"] for x in b]
    return reply({
        "symbol": inst["symbol"], "exchange": inst["exchange"], "name": inst.get("name") or inst["symbol"],
        "yahoo": inst["yahoo"], "intraday_allowed": inst.get("intraday_allowed", True),
        "price": price, "prev_close": prev, "change_pct": round((price / prev - 1) * 100, 2) if prev else None,
        "asof": (q["bars"][-1]["t"] if q["bars"] else b[-1]["t"]), "last_close_date": b[-1]["t"][:10],
        "daily": [[x["t"][:10], x["o"], x["h"], x["l"], x["c"], x["v"]] for x in b[-520:]],
        "today": [[x["t"], x["o"], x["h"], x["l"], x["c"], x["v"]] for x in today],
        "stats": stock_stats(b), "setups": setups, "orb": orb,
        "ranges": it.price_ranges(closes), "hit_rate": it.range_hit_rate(closes),
        "market": status["state"], "fo": fo_info(inst), "index": bool(inst.get("index")),
    }, 60)


@app.get("/api/plan")
def plan():
    inst = instrument(request.args.get("symbol"), request.args.get("exchange"))
    mode = request.args.get("mode", "swing")
    if mode not in it.MODES:
        raise ValueError("mode must be intraday, swing or positional")
    entry, stop = num("entry", lo=0.05, hi=1e7), num("stop", lo=0.0, hi=1e7)
    if entry is None or stop is None:
        raise ValueError("entry and stop are needed")
    target = num("target", None, 0, 1e7) or None
    cfg, acct = rules_from_query()
    p = it.build_plan(inst["symbol"], entry, stop, mode, target=target, exchange=inst["exchange"], cfg=cfg,
                      account=acct, intraday_allowed=inst.get("intraday_allowed", True),
                      setup=request.args.get("setup", ""))
    p["account"] = acct
    odds = None
    if p.get("target") and p["stop"] < p["entry"]:
        if mode == "intraday":
            rets, steps, basis = intraday_returns(inst, cfg)
        else:
            b, _ = daily(inst)
            rets = it.log_returns([x["c"] for x in b[-500:]])
            steps = it.MODES[mode]["max_days"]
            basis = "daily moves over the last 2 years, %d trading days to the exit date" % steps
        odds = it.path_odds(rets, p["entry"], p["stop"], p["target"], steps)
        if odds:
            odds["basis"] = basis
    p["odds"] = odds
    return reply(p, 30)


@app.get("/api/options")
def options():
    inst = instrument(request.args.get("symbol"), request.args.get("exchange"))
    kind = request.args.get("kind", "CE").upper()
    if kind not in ("CE", "PE"):
        raise ValueError("kind must be CE or PE")
    strike = request.args.get("strike", "ATM").upper()
    if strike not in ("ATM", "ITM1", "OTM1"):
        raise ValueError("strike must be ATM, ITM1 or OTM1")
    entry, stop = num("entry", lo=0.05, hi=1e7), num("stop", lo=0.05, hi=1e7)
    if entry is None or stop is None:
        raise ValueError("entry and stop are needed")
    target = num("target", None, 0, 1e7) or None
    expiry = request.args.get("expiry")
    expiry = dt.date.fromisoformat(expiry) if expiry else None
    cfg, acct = rules_from_query()
    info = fo_info(inst)
    b, _ = daily(inst)
    spot = intraday(inst)["meta"].get("price") or b[-1]["c"]
    chain = None
    if info and request.args.get("live", "1") == "1" and not DEMO:
        chain = CACHE.get(("chain", inst["symbol"]), 60, lambda: it.nse_option_chain(inst["symbol"]))
    p = it.option_plan(inst["symbol"], spot, [x["c"] for x in b], kind, entry, stop, target, strike, expiry,
                       lot=info["lot"] if info else None, chain=chain, cfg=cfg, account=acct, exchange="NSE")
    p["spot"] = spot
    p["account"] = acct
    p["odds"] = None
    if p.get("u_target"):
        rets, steps, basis = intraday_returns(inst, cfg)
        o = it.path_odds(rets, p["u_entry"], p["u_stop"], p["u_target"], steps)
        if o:
            o["basis"] = basis
        p["odds"] = o
    return reply(p, 30)


def scan_one(kind, inst, minutes, window):
    loose = it.DEFAULT_CONFIG
    if kind in ("swing", "positional"):
        wanted = set(scan.SWING if kind == "swing" else scan.POSITIONAL)
        return scan.daily_setups(inst, daily(inst)[0], loose, wanted)
    if kind == "premarket":
        return scan.premarket_setups(inst, daily(inst)[0], loose)
    return [scan.orb_status(inst, intraday(inst)["bars"], minutes, window=window)]


@app.get("/api/scan")
def scan_api():
    name = request.args.get("universe", "nifty200")
    kind = request.args.get("kind", "swing")
    if name not in it.UNIVERSES or kind not in ("swing", "positional", "premarket", "orb"):
        raise ValueError("unknown list or scan")
    offset = int(num("offset", 0, 0, 1e6))
    limit = int(num("limit", SCAN_CHUNK, 1, 120))
    try:
        items, src = universe(name)
    except RuntimeError as e:
        return fail("couldn't load the %s list: %s" % (name, e), 502)
    if kind in ("premarket", "orb"):
        items = [i for i in items if i.get("intraday_allowed", True)]
    cap = it.DEFAULT_CONFIG["capital"] * it.DEFAULT_CONFIG["max_position_pct"] / 100.0
    chunk = items[offset:offset + limit]
    window = tuple(it.DEFAULT_CONFIG["intraday_entry_window"].split("-"))
    hits, errors = [], []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(scan_one, kind, i, 15, window): i for i in chunk}
        for f in cf.as_completed(futs):
            try:
                for h in f.result() or []:
                    if kind == "orb" or h.get("close", 0) <= cap:
                        hits.append(h)
            except Exception as e:  # one bad stock never sinks the scan
                errors.append("%s: %s" % (futs[f]["symbol"], e))
    return reply({"universe": name, "kind": kind, "source": src, "total": len(items), "offset": offset,
                  "next": offset + limit if offset + limit < len(items) else None,
                  "hits": hits, "errors": errors[:20], "error_count": len(errors)},
                 55 if kind == "orb" else 900)


@app.get("/api/market")
def market():
    out = {"indices": [], "movers": []}
    for sym, name in (("^NSEI", "Nifty 50"), ("^NSEBANK", "Bank Nifty")):
        try:
            r = bars(sym, "NSE", "5m", "1d", 55)
        except it.FetchError as e:
            out["indices"].append({"name": name, "error": str(e)})
            continue
        day = it.session_bars(r["bars"])
        day = [x for x in day if day and x["t"][:10] == day[-1]["t"][:10]]
        out["indices"].append({"name": name, "symbol": sym, "price": r["meta"].get("price"),
                               "prev_close": r["meta"].get("prev_close"),
                               "line": [[x["t"], x["c"]] for x in day]})
    try:
        items, _ = universe("nifty50")
    except RuntimeError as e:
        out["movers_error"] = str(e)
        items = []

    def one(i):
        try:
            r = bars(i["yahoo"], i["exchange"], "5m", "1d", 55)
        except it.FetchError:
            return None
        m = r["meta"]
        if m.get("price") and m.get("prev_close"):
            return {"symbol": i["symbol"], "exchange": i["exchange"], "price": m["price"],
                    "change_pct": round((m["price"] / m["prev_close"] - 1) * 100, 2)}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(one, items):
            if r:
                out["movers"].append(r)
    out["movers"].sort(key=lambda r: -r["change_pct"])
    out["status"] = it.market_status()
    return reply(out, 55)


@app.get("/api/charges")
def charges():
    product = request.args.get("product", "intraday")
    exchange = request.args.get("exchange", "NSE")
    rt = it.round_trip(num("buy", lo=0.01), num("sell", lo=0.01), int(num("qty", lo=1, hi=1e7)), product, exchange)
    return reply(rt, 3600)


# --------------------------------------------------------------------------
# The page (Vercel serves public/ from its CDN; these routes cover local runs)
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(PUBLIC, "index.html")


@app.get("/<path:path>")
def static_files(path):
    if path.startswith("api/"):
        return fail("no such API", 404)
    full = os.path.join(PUBLIC, path)
    if os.path.isfile(full):
        return send_from_directory(PUBLIC, path)
    return send_from_directory(PUBLIC, "index.html")
