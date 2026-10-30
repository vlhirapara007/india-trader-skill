"""Synthetic market data for testing the app without the internet.

Turned on only by INDTRADER_DEMO=1. Every page then shows a "demo data" banner.
Prices are generated, not real.
"""

import datetime as dt
import json
import math
import random
import zlib

DEMO_STOCKS = [
    ("RELIANCE", "Reliance Industries Ltd.", 1160), ("HDFCBANK", "HDFC Bank Ltd.", 1710),
    ("TCS", "Tata Consultancy Services Ltd.", 3050), ("INFY", "Infosys Ltd.", 1480),
    ("ICICIBANK", "ICICI Bank Ltd.", 1290), ("SBIN", "State Bank of India", 820),
    ("ITC", "ITC Ltd.", 410), ("TATAMOTORS", "Tata Motors Ltd.", 690),
    ("M&M", "Mahindra & Mahindra Ltd.", 3120), ("AXISBANK", "Axis Bank Ltd.", 1150),
]


def _seed(sym):
    return zlib.crc32(sym.encode())


def universe_csv():
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for i, (s, n, _) in enumerate(DEMO_STOCKS):
        rows.append('"%s",Demo,%s,EQ,INE%06dA0101' % (n, s, i))
    return "\n".join(rows)


def _payload(meta, rows):
    return json.dumps({"chart": {"result": [{
        "meta": meta, "timestamp": [r[0] for r in rows],
        "indicators": {"quote": [{k: [r[i] for r in rows] for i, k in
                                  ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume"))}]}}],
        "error": None}})


def chart(url, now, IST, is_trading_day):
    """A Yahoo chart payload for any URL the core builds."""
    path = url.split("/chart/")[1]
    ysym = path.split("?")[0].replace("%26", "&").replace("%5E", "^")
    sym = ysym.split(".")[0]
    base = dict((s, p) for s, _, p in DEMO_STOCKS).get(sym, 22000 if sym == "^NSEI" else 51000 if sym == "^NSEBANK" else 500)
    rng = random.Random(_seed(sym))
    drift = rng.uniform(-0.0004, 0.0012)
    vol = rng.uniform(0.010, 0.022)
    days, d = [], now.date() - dt.timedelta(days=760)
    while d <= now.date():
        if is_trading_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    closes, c = [], base * 0.7
    for i, _ in enumerate(days):
        c *= math.exp(drift + vol * rng.gauss(0, 1))
        closes.append(c)
    scale = base / closes[-2]
    closes = [x * scale for x in closes]
    if "interval=1d" in url:
        rows = []
        for d, c in zip(days, closes):
            o = c * (1 + rng.gauss(0, vol / 3))
            h, l = max(o, c) * (1 + abs(rng.gauss(0, vol / 2))), min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            ts = int(dt.datetime.combine(d, dt.time(9, 15), IST).timestamp())
            rows.append((ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(rng.uniform(2e6, 8e6))))
        meta = {"symbol": ysym, "currency": "INR", "exchangeName": "NSI", "regularMarketPrice": rows[-1][4],
                "chartPreviousClose": rows[-2][4], "dataGranularity": "1d", "range": "2y"}
        return _payload(meta, rows)
    rows = []
    for d, c0 in zip(days[-5:], closes[-6:-1]):
        t0 = dt.datetime.combine(d, dt.time(9, 15), IST)
        c = c0
        for k in range(75):
            t = t0 + dt.timedelta(minutes=5 * k)
            if t > now:
                break
            o = c
            c = c * math.exp(vol / 9 * rng.gauss(0, 1) + (0.0012 if (sym in ("TCS", "ITC") and d == now.date() and 3 <= k <= 6) else 0))
            h, l = max(o, c) * (1 + abs(rng.gauss(0, vol / 20))), min(o, c) * (1 - abs(rng.gauss(0, vol / 20)))
            rows.append((int(t.timestamp()), round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(rng.uniform(2e4, 9e4))))
    meta = {"symbol": ysym, "currency": "INR", "exchangeName": "NSI",
            "regularMarketPrice": rows[-1][4] if rows else base, "chartPreviousClose": closes[-2],
            "dataGranularity": "5m", "range": "5d"}
    return _payload(meta, rows)


def install(it):
    """Route the core's HTTP through the generator."""
    def fake_get(url, headers=None, timeout=20, retries=2, pause=1.5):
        if "niftyindices" in url or "EQUITY_L" in url:
            return universe_csv() if "niftyindices" in url else "SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\n" + "\n".join(
                "%s,%s,EQ,INE%06dA0101" % (s, n.replace(",", ""), i) for i, (s, n, _) in enumerate(DEMO_STOCKS))
        if "fo_mktlots" in url:
            return "UNDERLYING,SYMBOL,NOV-26\nNIFTY 50,NIFTY,65\nNIFTY BANK,BANKNIFTY,30\n" + "\n".join(
                "%s,%s,%d" % (n.replace(",", ""), s, lot) for (s, n, _), lot in zip(DEMO_STOCKS, (500, 550, 175, 400, 700, 750, 1600, 800, 200, 625)))
        if "bseindia" in url:
            raise it.FetchError("demo: no BSE list")
        return chart(url, it.now_ist(), it.IST, it.is_trading_day)
    it.http_get = fake_get
