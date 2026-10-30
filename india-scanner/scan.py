#!/usr/bin/env python3
"""India Scanner: finds setups across an NSE/BSE universe. Never places orders.

Three scans:
  daily      after 15:30 or before 09:15. Swing and positional setups from
             completed daily candles: 20-day breakout, 52-week breakout, bull
             flag, pullback to the 20-day average, reclaim of the 20-day
             average, stage-2 trend, oversold, oversold bounce.
  premarket  before 09:15. Intraday candidates for today: narrow-range (NR7)
             and inside days in liquid, moving stocks, with yesterday's high
             and low as the levels to watch.
  orb        from 09:30. Opening-range breakouts on completed 5-minute candles:
             triggered (with time and price), waiting (how far), or failed.

  python3 scan.py daily [--universe nifty200] [--mode swing|positional|all] [--setups a,b] [--top 25]
  python3 scan.py premarket [--universe nifty200] [--top 25]
  python3 scan.py orb [--universe nifty200 | --watchlist] [--minutes 15] [--top 25]

Results print as a table and go to trader/scans/<scan>-<date>.json and
trader/watchlist.json (the latest of each scan) for india-plan.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indtrader as it  # noqa: E402

IST = it.IST

SWING = ("breakout_20d", "bull_flag", "pullback_20sma", "reclaim_20sma", "oversold_bounce", "oversold")
POSITIONAL = ("breakout_52w", "stage2_trend", "pullback_20sma")
SETUP_TEXT = {
    "breakout_20d": "closed at a 20-day closing high on 1.5x+ volume",
    "breakout_52w": "closed above its 52-week high on 1.5x+ volume",
    "bull_flag": "10%+ run, then a tight pullback on lighter volume",
    "pullback_20sma": "uptrend, dipped to the 20-day average and closed green",
    "reclaim_20sma": "closed back above the 20-day average",
    "stage2_trend": "above rising 50/150/200-day averages, near its 52-week high",
    "oversold": "RSI under 30 while above the 200-day average",
    "oversold_bounce": "RSI was under 30, today closed above yesterday's high",
    "nr7": "narrowest range of the last 7 days",
    "inside_day": "today's range inside yesterday's",
    "orb": "opening-range breakout",
}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def daily_bars(inst, fetch=None, now=None):
    """~2 years of completed daily candles, cached until the next session closes."""
    cache = it.path("cache", "daily", inst["yahoo"].replace("&", "_") + ".json")
    cached = it.read_json(cache)
    fresh_after = it.last_session_close(now)
    if cached and dt.datetime.fromisoformat(cached["fetched"]) >= fresh_after:
        return cached["bars"]
    r = it.fetch_bars(inst["yahoo"], inst["exchange"], "1d", "2y", fetch=fetch)
    bars = it.completed_bars(r["bars"], "1d", now)
    it.write_json(cache, {"fetched": (now or it.now_ist()).isoformat(timespec="minutes"), "bars": bars})
    return bars


def run_parallel(fn, items, workers):
    """fn(item) for every item, a few at a time. Returns (results, errors)."""
    results, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(fn, i): i for i in items}
        done = 0
        for f in cf.as_completed(futs):
            done += 1
            if len(items) > 50 and done % 100 == 0:
                sys.stderr.write("  %d / %d\n" % (done, len(items)))
            try:
                r = f.result()
                if r:
                    results.extend(r if isinstance(r, list) else [r])
            except it.FetchError as e:
                errors.append("%s: %s" % (futs[f]["symbol"], e))
    return results, errors


# --------------------------------------------------------------------------
# Daily setups
# --------------------------------------------------------------------------

def context(bars):
    """Indicators for the last completed day."""
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    s20, s50, s150, s200 = it.sma(c, 20), it.sma(c, 50), it.sma(c, 150), it.sma(c, 200)
    a = it.atr(bars, 14)
    r = it.rsi(c, 14)
    vavg = it.sma(v, 20)
    last = bars[-1]
    value_cr = None
    if len(bars) >= 20:
        value_cr = sum(b["c"] * b["v"] for b in bars[-20:]) / 20 / 1e7
    prior_vavg = vavg[-2] if len(vavg) > 1 else None
    return {
        "c": c, "s20": s20, "s50": s50, "s150": s150, "s200": s200, "atr": a, "rsi": r,
        "last": last, "atr_now": a[-1], "value_cr": value_cr,
        "vol_ratio": (last["v"] / prior_vavg) if prior_vavg else None,
    }


def _level(x, mode):
    return it.tick_round(x, mode=mode)


def hit(name, inst, bars, ctx, trigger, stop_ref, mode, score, extra=None):
    last = ctx["last"]
    h = {
        "symbol": inst["symbol"], "exchange": inst["exchange"], "name": inst.get("name", ""),
        "setup": name, "mode": mode, "why": SETUP_TEXT[name],
        "date": last["t"][:10], "close": last["c"],
        "trigger": _level(trigger, "up"),           # buy only above this
        "stop_ref": _level(stop_ref, "down"),       # where the idea is wrong
        "atr": round(ctx["atr_now"], 2) if ctx["atr_now"] else None,
        "rsi": round(ctx["rsi"][-1], 1) if ctx["rsi"][-1] is not None else None,
        "vol_ratio": round(ctx["vol_ratio"], 2) if ctx["vol_ratio"] else None,
        "avg_value_cr": round(ctx["value_cr"], 1) if ctx["value_cr"] else None,
        "intraday_allowed": inst.get("intraday_allowed", True),
        "score": round(score, 2),
    }
    if h["trigger"] > 0:
        h["risk_pct"] = round((h["trigger"] - h["stop_ref"]) / h["trigger"] * 100, 2)
    h.update(extra or {})
    return h


def find_bull_flag(bars):
    """A 10%+ pole of 3-15 days, then a 3-8 day flag that holds the top half and dries up.

    Returns (flag_high, flag_low, pole_gain) or None.
    """
    n = len(bars)
    for f in range(3, 9):
        p = n - f - 1                       # last bar of the pole
        if p < 16:
            continue
        flag = bars[p + 1:]
        top = bars[p]
        best = None
        for k in range(3, 16):
            start = p - k
            if start < 0:
                break
            base = min(b["l"] for b in bars[start:p + 1])
            gain = top["c"] / base - 1 if base > 0 else 0
            if gain >= 0.10 and (best is None or gain > best[0]):
                best = (gain, base, start)
        if not best:
            continue
        gain, base, start = best
        pole = bars[start:p + 1]
        fh = max(b["h"] for b in flag)
        fl = min(b["l"] for b in flag)
        height = top["c"] - base
        if fh > max(b["h"] for b in pole) * 1.005:
            continue                        # flag made a new high: not a flag
        if fl < top["c"] - 0.5 * height:
            continue                        # gave back more than half the pole
        if (fh - fl) / top["c"] > 0.12:
            continue                        # too loose
        if sum(b["v"] for b in flag) / len(flag) >= sum(b["v"] for b in pole) / len(pole):
            continue                        # volume did not dry up
        if bars[-1]["c"] > fh:
            continue                        # already broke out
        return fh, fl, gain
    return None


def daily_setups(inst, bars, cfg, wanted):
    """Every wanted setup that fires on the last completed day."""
    if len(bars) < 60:
        return []
    ctx = context(bars)
    last, prev = bars[-1], bars[-2]
    c, s20, s50, s150, s200, a = ctx["c"], ctx["s20"], ctx["s50"], ctx["s150"], ctx["s200"], ctx["atr_now"]
    if not a or last["c"] < cfg["min_price"]:
        return []
    if ctx["value_cr"] is None or ctx["value_cr"] < cfg["min_avg_traded_value_cr"]:
        return []
    out = []
    vr = ctx["vol_ratio"] or 0
    lows5 = min(b["l"] for b in bars[-5:])

    if "breakout_20d" in wanted and last["c"] > max(c[-21:-1]) and vr >= 1.5:
        out.append(hit("breakout_20d", inst, bars, ctx, last["h"] + 0.05,
                       max(lows5, last["c"] - 2 * a), "swing", vr))

    if "breakout_52w" in wanted and len(bars) >= 253 and last["c"] > max(b["h"] for b in bars[-253:-1]) and vr >= 1.5:
        out.append(hit("breakout_52w", inst, bars, ctx, last["h"] + 0.05,
                       max(min(b["l"] for b in bars[-10:]), last["c"] - 3 * a), "positional", vr))

    if "bull_flag" in wanted:
        fl = find_bull_flag(bars)
        if fl:
            fh, flo, gain = fl
            out.append(hit("bull_flag", inst, bars, ctx, fh + 0.05, flo, "swing", gain * 10,
                           {"pole_gain_pct": round(gain * 100, 1)}))

    up = s50[-1] and s200[-1] and last["c"] > s50[-1] > s200[-1]
    if "pullback_20sma" in wanted and up and s20[-1] and last["l"] <= s20[-1] * 1.01 \
            and last["c"] > s20[-1] and last["c"] > last["o"] and s50[-1] > s50[-6]:
        out.append(hit("pullback_20sma", inst, bars, ctx, last["h"] + 0.05,
                       min(last["l"], s20[-1]) - 0.1 * a, "swing/positional", last["c"] / s20[-1]))

    if "reclaim_20sma" in wanted and s20[-2] and c[-2] < s20[-2] and last["c"] > s20[-1] and vr >= 1.0:
        out.append(hit("reclaim_20sma", inst, bars, ctx, last["h"] + 0.05,
                       min(b["l"] for b in bars[-3:]), "swing", vr))

    if "stage2_trend" in wanted and len(bars) >= 253 and s200[-21] and s150[-1]:
        hi52 = max(b["h"] for b in bars[-252:])
        lo52 = min(b["l"] for b in bars[-252:])
        if last["c"] > s50[-1] > s150[-1] > s200[-1] and s200[-1] > s200[-21] \
                and last["c"] >= 1.3 * lo52 and last["c"] >= 0.75 * hi52:
            pivot = max(b["h"] for b in bars[-10:])
            out.append(hit("stage2_trend", inst, bars, ctx, pivot + 0.05,
                           max(min(b["l"] for b in bars[-10:]), pivot - 3 * a), "positional",
                           last["c"] / hi52, {"pct_from_52w_high": round((last["c"] / hi52 - 1) * 100, 1)}))

    r = ctx["rsi"]
    if "oversold" in wanted and r[-1] is not None and r[-1] < 30 and s200[-1] and last["c"] > s200[-1]:
        out.append(hit("oversold", inst, bars, ctx, last["h"] + 0.05, lows5, "swing", 30 - r[-1],
                       {"note": "wait for a close above the trigger; don't buy a falling stock"}))

    if "oversold_bounce" in wanted and None not in r[-5:-1] and min(r[-5:-1]) < 30 and last["c"] > prev["h"]:
        out.append(hit("oversold_bounce", inst, bars, ctx, last["h"] + 0.05, lows5, "swing", 30 - min(r[-5:-1])))
    return out


def premarket_setups(inst, bars, cfg):
    """Intraday candidates: liquid, moving stocks coiled after a quiet day."""
    if len(bars) < 30 or not inst.get("intraday_allowed", True):
        return []
    ctx = context(bars)
    last, prev = bars[-1], bars[-2]
    a = ctx["atr_now"]
    if not a or last["c"] < cfg["min_price"] or (ctx["value_cr"] or 0) < 2 * cfg["min_avg_traded_value_cr"]:
        return []
    atr_pct = a / last["c"] * 100
    if atr_pct < 1.5:
        return []                       # moves too little to pay the charges
    out = []
    ranges = [b["h"] - b["l"] for b in bars[-7:]]
    extra = {"prev_high": last["h"], "prev_low": last["l"], "prev_close": last["c"], "atr_pct": round(atr_pct, 2)}
    mid = (last["h"] + last["l"]) / 2
    if ranges[-1] == min(ranges):
        out.append(hit("nr7", inst, bars, ctx, last["h"] + 0.05, mid, "intraday", atr_pct, extra))
    elif last["h"] <= prev["h"] and last["l"] >= prev["l"]:
        out.append(hit("inside_day", inst, bars, ctx, last["h"] + 0.05, mid, "intraday", atr_pct, extra))
    return out


# --------------------------------------------------------------------------
# Opening-range breakout
# --------------------------------------------------------------------------

def orb_status(inst, bars, minutes=15, now=None, window=("09:30", "11:00")):
    """Opening range from 09:15 for `minutes`, then the first completed 5-minute close above it.

    bars: 5-minute bars over several days (for relative volume), oldest first.
    """
    now = (now or it.now_ist()).astimezone(IST)
    today = now.date().isoformat()
    done = it.completed_bars(bars, "5m", now)
    day = it.session_bars(done, today)
    or_end = (dt.datetime.combine(now.date(), it.MARKET_OPEN, IST) + dt.timedelta(minutes=minutes)).time()
    rng = [b for b in day if dt.datetime.fromisoformat(b["t"]).time() < or_end]
    need = minutes // 5
    base = {"symbol": inst["symbol"], "exchange": inst["exchange"], "name": inst.get("name", ""),
            "setup": "orb", "mode": "intraday", "date": today, "or_minutes": minutes,
            "intraday_allowed": inst.get("intraday_allowed", True)}
    if len(rng) < need:
        base.update(status="not_ready", note="opening range completes at %s" % or_end.strftime("%H:%M"))
        return base
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    mid = (hi + lo) / 2
    days = sorted({b["t"][:10] for b in done if b["t"][:10] < today})
    prev_close = None
    if days:
        prev = it.session_bars(done, days[-1])
        prev_close = prev[-1]["c"] if prev else None
    past_or_vol = []
    for d in days[-10:]:
        pr = [b for b in it.session_bars(done, d) if dt.datetime.fromisoformat(b["t"]).time() < or_end]
        if len(pr) >= need:
            past_or_vol.append(sum(b["v"] for b in pr))
    or_vol = sum(b["v"] for b in rng)
    rvol = or_vol / (sum(past_or_vol) / len(past_or_vol)) if past_or_vol and sum(past_or_vol) else None
    base.update({
        "or_high": round(hi, 2), "or_low": round(lo, 2), "or_mid": round(mid, 2),
        "or_range_pct": round((hi - lo) / lo * 100, 2) if lo else None,
        "gap_pct": round((rng[0]["o"] / prev_close - 1) * 100, 2) if prev_close else None,
        "rvol": round(rvol, 2) if rvol else None,
        "trigger": it.tick_round(hi, mode="up"), "stop_ref": it.tick_round(mid, mode="down"),
    })
    w0, w1 = [it.parse_hhmm(x) for x in window]
    after = [b for b in day if dt.datetime.fromisoformat(b["t"]).time() >= or_end]
    last_c = after[-1]["c"] if after else rng[-1]["c"]
    base["last"] = last_c
    for b in after:
        bt = dt.datetime.fromisoformat(b["t"])
        close_t = (bt + dt.timedelta(minutes=5)).time()
        if b["c"] < lo:
            base.update(status="failed", note="closed below the range low at %s" % close_t.strftime("%H:%M"))
            return base
        if b["c"] > hi:
            if not (w0 <= close_t <= w1):
                base.update(status="expired", note="first close above the range came at %s, outside %s-%s"
                            % (close_t.strftime("%H:%M"), window[0], window[1]))
                return base
            base.update(status="triggered", entry_time=close_t.strftime("%H:%M"), entry_price=b["c"],
                        pct_above=round((b["c"] / hi - 1) * 100, 2))
            return base
    if now.time() > w1:
        base.update(status="expired", note="no close above the range by %s" % window[1])
    else:
        base.update(status="waiting", distance_pct=round((hi / last_c - 1) * 100, 2))
    return base


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def save(kind, data):
    stamp = it.now_ist().strftime("%Y-%m-%d")
    it.write_json(it.path("scans", "%s-%s.json" % (kind, stamp)), data)
    wl = it.read_json(it.path("watchlist.json"), {}) or {}
    wl[kind] = data
    it.write_json(it.path("watchlist.json"), wl)


def fmt(x, d=2):
    return "-" if x is None else ("%.*f" % (d, x))


def print_daily(hits, top):
    if not hits:
        print("No setups today.")
        return
    print("%-12s %-15s %-10s %9s %9s %9s %6s %5s %5s %7s" %
          ("symbol", "setup", "mode", "close", "trigger", "stop_ref", "risk%", "RSI", "vol x", "Rs cr/d"))
    for h in hits[:top]:
        print("%-12s %-15s %-10s %9s %9s %9s %6s %5s %5s %7s" % (
            h["symbol"][:12], h["setup"], h["mode"], fmt(h["close"]), fmt(h["trigger"]), fmt(h["stop_ref"]),
            fmt(h.get("risk_pct")), fmt(h.get("rsi"), 0), fmt(h.get("vol_ratio"), 1), fmt(h.get("avg_value_cr"), 0)))
    if len(hits) > top:
        print("... %d more in trader/watchlist.json" % (len(hits) - top))


def print_orb(rows, top):
    order = {"triggered": 0, "waiting": 1, "not_ready": 2, "expired": 3, "failed": 4}
    rows.sort(key=lambda r: (order[r["status"]], r.get("distance_pct") or 0, -(r.get("rvol") or 0)))
    print("%-12s %-10s %9s %9s %9s %6s %6s %s" % ("symbol", "status", "or_high", "or_low", "last", "gap%", "rvol", "detail"))
    for r in rows[:top]:
        if r["status"] == "triggered":
            d = "closed %.2f at %s (+%.2f%%)" % (r["entry_price"], r["entry_time"], r["pct_above"])
        elif r["status"] == "waiting":
            d = "%.2f%% below the range high" % r["distance_pct"]
        else:
            d = r.get("note", "")
        print("%-12s %-10s %9s %9s %9s %6s %6s %s" % (r["symbol"][:12], r["status"], fmt(r.get("or_high")),
              fmt(r.get("or_low")), fmt(r.get("last")), fmt(r.get("gap_pct")), fmt(r.get("rvol"), 1), d))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def affordable(cfg):
    st = it.account_state(cfg)
    return st["equity"] * cfg["max_position_pct"] / 100.0


def cmd_daily(a, cfg):
    wanted = set(SWING if a.mode == "swing" else POSITIONAL if a.mode == "positional" else SWING + POSITIONAL)
    if a.setups:
        wanted = set(a.setups.split(","))
        bad = wanted - set(SWING + POSITIONAL)
        if bad:
            raise SystemExit("unknown setups: %s" % ", ".join(sorted(bad)))
    items = it.load_universe(a.universe or cfg["universe"])
    cap = affordable(cfg)

    def one(inst):
        hs = daily_setups(inst, daily_bars(inst), cfg, wanted)
        return [h for h in hs if h["close"] <= cap]
    print("Daily scan: %d stocks in %s (%s setups)..." % (len(items), a.universe or cfg["universe"], a.mode),
          file=sys.stderr)
    hits, errors = run_parallel(one, items, cfg["scan_workers"])
    hits.sort(key=lambda h: (-h["score"]))
    asof = max((h["date"] for h in hits), default=None)
    print("Setups on the %s close (%d stocks, %d hits; stocks above Rs %s skipped as too big for one position):"
          % (asof or "last", len(items), len(hits), it.money(cap)))
    print_daily(hits, a.top)
    if errors:
        print("%d stocks could not be fetched (first: %s)" % (len(errors), errors[0]))
    save("daily", {"generated": it.now_ist().isoformat(timespec="minutes"), "universe": a.universe or cfg["universe"],
                   "mode": a.mode, "asof": asof, "hits": hits, "errors": errors[:50]})


def cmd_premarket(a, cfg):
    items = [i for i in it.load_universe(a.universe or cfg["universe"]) if i.get("intraday_allowed", True)]
    cap = affordable(cfg)

    def one(inst):
        return [h for h in premarket_setups(inst, daily_bars(inst), cfg) if h["close"] <= cap]
    hits, errors = run_parallel(one, items, cfg["scan_workers"])
    hits.sort(key=lambda h: -h["score"])
    print("Intraday candidates for %s (watch above yesterday's high; opening range from 09:15):"
          % it.next_trading_day(it.last_session_close().date()) if it.market_status()["state"] != "open"
          else "Intraday candidates (based on yesterday):")
    print_daily(hits, a.top)
    if errors:
        print("%d stocks could not be fetched (first: %s)" % (len(errors), errors[0]))
    save("premarket", {"generated": it.now_ist().isoformat(timespec="minutes"),
                       "universe": a.universe or cfg["universe"], "hits": hits, "errors": errors[:50]})


def cmd_orb(a, cfg):
    st = it.market_status(cfg=cfg)
    if not it.is_trading_day(it.now_ist().date(), cfg):
        raise SystemExit("Market closed today (%s). Run `scan.py premarket` to plan the next session." % st["reason"])
    if a.watchlist:
        wl = it.read_json(it.path("watchlist.json"), {}) or {}
        syms = {(h["symbol"], h["exchange"]) for k in ("premarket", "daily") for h in (wl.get(k) or {}).get("hits", [])}
        items = [it._instrument(s, "", ex) for s, ex in sorted(syms)]
        if not items:
            raise SystemExit("watchlist.json is empty: run `scan.py premarket` first or pass --universe")
    else:
        items = [i for i in it.load_universe(a.universe or cfg["universe"]) if i.get("intraday_allowed", True)]
    win = tuple(cfg["intraday_entry_window"].split("-"))

    def one(inst):
        r = it.fetch_bars(inst["yahoo"], inst["exchange"], "5m", "5d")
        return orb_status(inst, r["bars"], a.minutes, window=win)
    rows, errors = run_parallel(one, items, cfg["scan_workers"])
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("Opening-range breakouts at %s IST (%d-min range, entries %s-%s): %s"
          % (it.now_ist().strftime("%H:%M"), a.minutes, win[0], win[1],
             ", ".join("%s %d" % kv for kv in sorted(counts.items()))))
    print_orb(rows, a.top)
    if errors:
        print("%d stocks could not be fetched (first: %s)" % (len(errors), errors[0]))
    save("orb", {"generated": it.now_ist().isoformat(timespec="minutes"), "minutes": a.minutes,
                 "hits": [r for r in rows if r["status"] in ("triggered", "waiting")], "all": rows})


def main(argv=None):
    p = argparse.ArgumentParser(prog="scan", description="India Scanner")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("daily"); s.add_argument("--universe"); s.add_argument("--setups")
    s.add_argument("--mode", choices=["swing", "positional", "all"], default="all")
    s.add_argument("--top", type=int, default=25); s.set_defaults(fn=cmd_daily)
    s = sub.add_parser("premarket"); s.add_argument("--universe"); s.add_argument("--top", type=int, default=25)
    s.set_defaults(fn=cmd_premarket)
    s = sub.add_parser("orb"); s.add_argument("--universe"); s.add_argument("--watchlist", action="store_true")
    s.add_argument("--minutes", type=int, choices=[5, 15, 30], default=15); s.add_argument("--top", type=int, default=25)
    s.set_defaults(fn=cmd_orb)
    a = p.parse_args(argv)
    cfg = it.load_config()
    errs = it.validate_config(cfg)
    if errs:
        raise SystemExit("fix trader/config.json first:\n  " + "\n  ".join(errs))
    a.fn(a, cfg)


if __name__ == "__main__":
    main()
