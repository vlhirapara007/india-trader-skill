#!/usr/bin/env python3
"""Save today's stock lists into data/universe/ so the web app has them even when
NSE/BSE block its cloud server. Run on your own computer, then commit and push:

  python3 tools/snapshot_universes.py
  git add data/universe && git commit -m "Refresh stock lists" && git push
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "core"))
import indtrader as it  # noqa: E402

out = os.path.join(ROOT, "data", "universe")
for name in ("nifty50", "nifty200", "banknifty", "nse_all", "bse_all"):
    try:
        items = it.load_universe(name, refresh=True)
    except SystemExit as e:
        print("%-10s FAILED: %s" % (name, str(e).splitlines()[0]))
        continue
    it.write_json(os.path.join(out, name + ".json"),
                  {"name": name, "fetched": it.now_ist().isoformat(timespec="minutes"), "items": items})
    print("%-10s %5d stocks" % (name, len(items)))

lots = it.load_fo_lots(refresh=True)
it.write_json(os.path.join(out, "fo_lots.json"), {"fetched": it.now_ist().isoformat(timespec="minutes"), "lots": lots})
print("%-10s %5d F&O lot sizes" % ("fo_lots", len(lots)))
