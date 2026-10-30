#!/usr/bin/env python3
"""India Options: a buy-only CE/PE plan for an intraday idea on an F&O stock or index.

  python3 options.py RELIANCE --kind CE --entry 1164 --stop 1160
  python3 options.py NIFTY --kind PE --entry 25790 --stop 25830 --strike ITM1
  python3 options.py RELIANCE --orb            # use today's opening range (CE above the high, PE below the low)
  python3 options.py lots                      # refresh F&O lot sizes

Never places orders and never sells options.
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indtrader as it  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="options")
    p.add_argument("symbol")
    p.add_argument("--kind", choices=["CE", "PE"], default="CE")
    p.add_argument("--entry", type=float, help="stock/index level that triggers the buy")
    p.add_argument("--stop", type=float, help="stock/index level that ends the idea")
    p.add_argument("--target", type=float)
    p.add_argument("--strike", choices=["ATM", "ITM1", "OTM1"], default="ATM")
    p.add_argument("--expiry", help="YYYY-MM-DD; default the nearest that isn't today")
    p.add_argument("--orb", action="store_true", help="levels from today's 15-minute opening range")
    p.add_argument("--no-live", action="store_true", help="skip NSE's option chain, estimate premiums")
    a = p.parse_args(argv)
    if a.symbol.lower() == "lots":
        lots = it.load_fo_lots(refresh=True)
        print("%d F&O lot sizes saved in trader/universe/fo_lots.json" % len(lots))
        return
    sym = a.symbol.upper()
    cfg = it.load_config()
    lots = it.load_fo_lots()
    lot = it.lot_size(sym, lots)
    ysym = it.INDEX_OPTIONS[sym]["yahoo"] if sym in it.INDEX_OPTIONS else sym
    daily = it.completed_bars(it.fetch_bars(ysym, "NSE", "1d", "2y")["bars"], "1d")
    intra = it.fetch_bars(ysym, "NSE", "5m", "5d")
    spot = intra["meta"].get("price") or daily[-1]["c"]
    entry, stop = a.entry, a.stop
    if a.orb:
        today = it.now_ist().date().isoformat()
        day = [b for b in it.session_bars(it.completed_bars(intra["bars"], "5m")) if b["t"][:10] == today]
        rng = [b for b in day if b["t"][11:16] < "09:30"]
        if len(rng) < 3:
            raise SystemExit("The opening range (09:15-09:30) isn't complete yet.")
        hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
        mid = (hi + lo) / 2
        entry, stop = (hi, mid) if a.kind == "CE" else (lo, mid)
    if entry is None or stop is None:
        raise SystemExit("Give --entry and --stop (stock/index levels), or --orb.")
    chain = None if a.no_live or not lot else it.nse_option_chain(sym)
    plan = it.option_plan(sym, spot, [b["c"] for b in daily], a.kind, entry, stop, a.target, a.strike,
                          dt.date.fromisoformat(a.expiry) if a.expiry else None, lot=lot, chain=chain, cfg=cfg)
    if not plan.get("contract"):
        raise SystemExit("\n".join(plan["blocks"]))
    print("%s  (%s premium, spot %.2f)" % (plan["contract"], plan["premium_source"], spot))
    print("  status       %s" % ("FITS YOUR RULES" if plan["status"] == "ok" else "DOESN'T FIT"))
    print("  buy at       ~%.2f when %s closes %s %.2f" % (plan["premium_entry"], sym, "above" if a.kind == "CE" else "below", entry))
    print("  stop at      ~%.2f (%s back at %.2f)  -> lose Rs %s" % (plan["premium_stop"], sym, stop, it.money(plan["loss_at_stop"])))
    print("  sell at      ~%.2f (%s at %.2f)       -> make Rs %s" % (plan["premium_target"], sym, plan["u_target"], it.money(plan["gain_at_target"])))
    print("  size         %d lot(s) x %d = %d units, cost Rs %s; lots that fit your risk: %d"
          % (plan["lots_shown"], plan["lot"], plan["qty"], it.money(plan["cost"]), plan["lots"]))
    print("  delta %.2f, volatility used %.1f%%, expiry %s (%d days), breakeven at expiry %.2f"
          % (plan["delta"], plan["iv_used"], plan["expiry"], plan["days_to_expiry"], plan["breakeven_at_expiry"]))
    for b in plan["blocks"]:
        print("  BLOCK: " + b)
    for w in plan["warnings"]:
        print("  note:  " + w)
    print("  " + plan["entry_rule"])
    print("  " + plan["exit_rule"])


if __name__ == "__main__":
    main()
