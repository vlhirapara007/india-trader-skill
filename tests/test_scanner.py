"""Tests for india-scanner/scan.py and the core's candle and indicator helpers."""

import datetime as dt
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCAN_DIR = os.path.join(ROOT, "india-scanner")
sys.path.insert(0, SCAN_DIR)
spec = importlib.util.spec_from_file_location("scan", os.path.join(SCAN_DIR, "scan.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
it = scan.it
IST = it.IST

INST = {"symbol": "TEST", "exchange": "NSE", "name": "Test Ltd", "yahoo": "TEST.NS", "intraday_allowed": True}
CFG = dict(it.DEFAULT_CONFIG)


def days_back(n, end=dt.date(2026, 10, 29)):
    out, d = [], end
    while len(out) < n:
        if it.is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


def daily(closes, vols=None, spread=0.01, opens=None):
    """Daily bars from closes. High/low are close +/- spread; open is the previous close."""
    ds = days_back(len(closes))
    vols = vols or [1_000_000] * len(closes)
    out = []
    for i, (d, c) in enumerate(zip(ds, closes)):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        h = max(o, c) * (1 + spread)
        l = min(o, c) * (1 - spread)
        out.append({"t": "%sT09:15+05:30" % d.isoformat(), "o": round(o, 2), "h": round(h, 2),
                    "l": round(l, 2), "c": round(c, 2), "v": vols[i]})
    return out


def five_min(day, rows):
    """rows: [(o, h, l, c, v)] from 09:15 every 5 minutes."""
    t0 = dt.datetime.combine(day, dt.time(9, 15), IST)
    return [{"t": (t0 + dt.timedelta(minutes=5 * i)).isoformat(timespec="minutes"),
             "o": o, "h": h, "l": l, "c": c, "v": v} for i, (o, h, l, c, v) in enumerate(rows)]


class Candles(unittest.TestCase):
    def test_forming_5m_candle_dropped(self):
        bars = five_min(dt.date(2026, 10, 30), [(1, 1, 1, 1, 10)] * 11)    # 09:15 ... 10:05
        now = dt.datetime(2026, 10, 30, 10, 7, tzinfo=IST)
        done = it.completed_bars(bars, "5m", now)
        self.assertEqual(done[-1]["t"], "2026-10-30T10:00+05:30")            # 10:05 bar still forming
        self.assertEqual(len(done), 10)

    def test_last_candle_of_day_ends_at_close(self):
        b = [{"t": "2026-10-29T15:25+05:30"}]
        self.assertEqual(len(it.completed_bars(b, "5m", dt.datetime(2026, 10, 29, 15, 30, tzinfo=IST))), 1)
        b = [{"t": "2026-10-29T15:15+05:30"}]
        self.assertEqual(len(it.completed_bars(b, "30m", dt.datetime(2026, 10, 29, 15, 30, tzinfo=IST))), 1)

    def test_todays_daily_candle_dropped_while_open(self):
        bars = [{"t": "2026-10-29T09:15+05:30"}, {"t": "2026-10-30T09:15+05:30"}]
        self.assertEqual(len(it.completed_bars(bars, "1d", dt.datetime(2026, 10, 30, 11, 0, tzinfo=IST))), 1)
        self.assertEqual(len(it.completed_bars(bars, "1d", dt.datetime(2026, 10, 30, 15, 31, tzinfo=IST))), 2)

    def test_last_session_close(self):
        f = it.last_session_close
        self.assertEqual(f(dt.datetime(2026, 10, 30, 11, 0, tzinfo=IST)).date(), dt.date(2026, 10, 29))
        self.assertEqual(f(dt.datetime(2026, 10, 30, 16, 0, tzinfo=IST)).date(), dt.date(2026, 10, 30))
        self.assertEqual(f(dt.datetime(2026, 11, 1, 10, 0, tzinfo=IST)).date(), dt.date(2026, 10, 30))
        self.assertEqual(f(dt.datetime(2026, 11, 11, 8, 0, tzinfo=IST)).date(), dt.date(2026, 11, 9))


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(it.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_ema_seeds_with_sma(self):
        e = it.ema([2, 4, 6, 8], 3)
        self.assertEqual(e[:3], [None, None, 4.0])
        self.assertAlmostEqual(e[3], 6.0)

    def test_rsi_extremes(self):
        self.assertEqual(it.rsi(list(range(1, 31)))[-1], 100.0)
        self.assertLess(it.rsi(list(range(30, 0, -1)))[-1], 1)

    def test_atr_constant_range(self):
        bars = [{"h": 11, "l": 9, "c": 10}] * 20
        self.assertAlmostEqual(it.atr(bars, 14)[-1], 2.0)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["TRADER_DIR"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("TRADER_DIR", None)


def wave(n, base=100.0, amp=3.0, drift=0.0):
    """Choppy closes with an optional drift, so RSI/ATR are realistic."""
    import math
    return [base + drift * i + amp * math.sin(i / 2.0) for i in range(n)]


class DailySetups(unittest.TestCase):
    def setups(self, bars, wanted=None):
        return {h["setup"]: h for h in scan.daily_setups(INST, bars, CFG, set(wanted or scan.SWING + scan.POSITIONAL))}

    def test_breakout_20d(self):
        closes = wave(80) + [110]
        vols = [1_000_000] * 80 + [2_500_000]
        h = self.setups(daily(closes, vols))
        self.assertIn("breakout_20d", h)
        b = h["breakout_20d"]
        self.assertGreater(b["trigger"], b["close"])          # buy above the breakout day's high
        self.assertLess(b["stop_ref"], b["close"])
        self.assertEqual(b["mode"], "swing")
        self.assertAlmostEqual(b["vol_ratio"], 2.5, places=1)

    def test_breakout_needs_volume(self):
        closes = wave(80) + [110]
        self.assertNotIn("breakout_20d", self.setups(daily(closes)))

    def test_illiquid_skipped(self):
        closes = wave(80) + [110]
        vols = [1000] * 80 + [2500]                            # Rs 1 lakh a day: far under Rs 5 cr
        self.assertEqual(self.setups(daily(closes, vols)), {})

    def test_penny_skipped(self):
        closes = [x / 10 for x in wave(80)] + [11]
        vols = [100_000_000] * 80 + [300_000_000]
        self.assertEqual(self.setups(daily(closes, vols)), {})

    def test_bull_flag(self):
        flat = [100.0] * 60
        pole = [100 + 2.5 * i for i in range(1, 9)]             # 100 -> 120
        flag = [118.5, 117.5, 118.0, 117.0, 117.6]
        vols = [1_000_000] * 60 + [3_000_000] * 8 + [800_000] * 5
        h = self.setups(daily(flat + pole + flag, vols, spread=0.004))
        self.assertIn("bull_flag", h)
        f = h["bull_flag"]
        self.assertGreaterEqual(f["pole_gain_pct"], 10)
        self.assertGreater(f["trigger"], max(flag))
        self.assertLess(f["stop_ref"], min(flag))

    def test_no_flag_when_volume_does_not_dry_up(self):
        flat = [100.0] * 60
        pole = [100 + 2.5 * i for i in range(1, 9)]
        flag = [118.5, 117.5, 118.0, 117.0, 117.6]
        vols = [1_000_000] * 60 + [3_000_000] * 8 + [4_000_000] * 5
        self.assertNotIn("bull_flag", self.setups(daily(flat + pole + flag, vols, spread=0.004)))

    def test_stage2_trend(self):
        closes = [100 + 0.4 * i for i in range(300)]          # steady uptrend, near its high
        h = self.setups(daily(closes, spread=0.01), {"stage2_trend"})
        self.assertIn("stage2_trend", h)
        self.assertEqual(h["stage2_trend"]["mode"], "positional")

    def test_breakout_52w(self):
        closes = wave(280, base=200, amp=10) + [240]
        vols = [1_000_000] * 280 + [2_000_000]
        self.assertIn("breakout_52w", self.setups(daily(closes, vols), {"breakout_52w"}))

    def test_oversold_bounce(self):
        closes = [100 + 0.3 * i for i in range(220)] + [160 - 3 * i for i in range(1, 9)]
        closes.append(closes[-1] * 1.04)                       # bounce over yesterday's high
        h = self.setups(daily(closes, spread=0.005), {"oversold_bounce"})
        self.assertIn("oversold_bounce", h)

    def test_pullback_to_20sma(self):
        closes = [100 + 0.5 * i for i in range(220)]
        bars = daily(closes, spread=0.003)
        s20 = it.sma([b["c"] for b in bars], 20)[-1]
        last = bars[-1]
        last.update(o=round(s20 * 1.002, 2), l=round(s20 * 0.998, 2), c=round(s20 * 1.01, 2), h=round(s20 * 1.015, 2))
        h = self.setups(bars, {"pullback_20sma"})
        self.assertIn("pullback_20sma", h)
        self.assertLess(h["pullback_20sma"]["stop_ref"], s20)

    def test_short_history_skipped(self):
        self.assertEqual(self.setups(daily(wave(30))), {})


class Premarket(unittest.TestCase):
    def test_nr7(self):
        closes = wave(60, base=500, amp=15)
        bars = daily(closes, vols=[2_000_000] * 60, spread=0.015)
        bars[-1].update(h=bars[-1]["c"] * 1.001, l=bars[-1]["c"] * 0.999)   # tiny range yesterday
        hs = scan.premarket_setups(INST, bars, CFG)
        self.assertEqual([h["setup"] for h in hs], ["nr7"])
        self.assertEqual(hs[0]["mode"], "intraday")
        self.assertIn("prev_high", hs[0])

    def test_no_intraday_for_trade_to_trade(self):
        closes = wave(60, base=500, amp=15)
        bars = daily(closes, vols=[2_000_000] * 60, spread=0.015)
        bars[-1].update(h=bars[-1]["c"] * 1.001, l=bars[-1]["c"] * 0.999)
        self.assertEqual(scan.premarket_setups(dict(INST, intraday_allowed=False), bars, CFG), [])


class OpeningRange(unittest.TestCase):
    D = dt.date(2026, 10, 30)
    PREV = dt.date(2026, 10, 29)

    def bars(self, today_rows):
        prev = five_min(self.PREV, [(99, 100, 98, 99.5, 1000)] * 75)
        return prev + five_min(self.D, today_rows)

    OR = [(100, 101, 99.5, 100.5, 3000), (100.5, 102, 100, 101.5, 3000), (101.5, 101.8, 100.2, 101, 3000)]

    def at(self, h, m):
        return dt.datetime.combine(self.D, dt.time(h, m), IST)

    def test_not_ready(self):
        r = scan.orb_status(INST, self.bars(self.OR[:2]), 15, self.at(9, 26))
        self.assertEqual(r["status"], "not_ready")

    def test_waiting(self):
        rows = self.OR + [(101, 101.7, 100.8, 101.2, 2000)]
        r = scan.orb_status(INST, self.bars(rows), 15, self.at(9, 36))
        self.assertEqual(r["status"], "waiting")
        self.assertEqual((r["or_high"], r["or_low"], r["or_mid"]), (102, 99.5, 100.75))
        self.assertAlmostEqual(r["rvol"], 3.0)                      # 9,000 vs 3,000 on the prior day
        self.assertAlmostEqual(r["gap_pct"], 0.5)                   # opened 100 vs 99.5 close

    def test_triggered_on_close_not_wick(self):
        rows = self.OR + [(101, 102.5, 100.8, 101.9, 2000),           # wick above, close below: no
                          (101.9, 102.6, 101.8, 102.4, 5000)]         # closes above at 09:40
        r = scan.orb_status(INST, self.bars(rows), 15, self.at(9, 46))
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["entry_time"], r["entry_price"]), ("09:40", 102.4))
        self.assertEqual(r["stop_ref"], 100.75)

    def test_forming_bar_ignored(self):
        rows = self.OR + [(101.9, 103, 101.8, 102.8, 0)]             # 09:30 bar, still forming at 09:33
        r = scan.orb_status(INST, self.bars(rows), 15, self.at(9, 33))
        self.assertEqual(r["status"], "waiting")

    def test_failed(self):
        rows = self.OR + [(100, 100.2, 99, 99.2, 4000)]
        r = scan.orb_status(INST, self.bars(rows), 15, self.at(9, 36))
        self.assertEqual(r["status"], "failed")

    def test_expired_late_breakout(self):
        rows = self.OR + [(101, 101.7, 100.8, 101.2, 2000)] * 21 + [(101.5, 102.6, 101.4, 102.5, 4000)]
        r = scan.orb_status(INST, self.bars(rows), 15, self.at(11, 15), window=("09:30", "11:00"))
        self.assertEqual(r["status"], "expired")


class Caching(TempDir):
    def test_daily_cache_until_next_close(self):
        calls = []
        payload = '{"chart":{"result":[{"meta":{},"timestamp":[%d],"indicators":{"quote":[{"open":[1],"high":[1],"low":[1],"close":[1],"volume":[1]}]}}],"error":null}}' \
            % int(dt.datetime(2026, 10, 29, 9, 15, tzinfo=IST).timestamp())

        def fetch(url):
            calls.append(url)
            return payload
        now = dt.datetime(2026, 10, 30, 8, 0, tzinfo=IST)
        scan.daily_bars(INST, fetch, now)
        scan.daily_bars(INST, fetch, now + dt.timedelta(hours=3))           # still before today's close
        self.assertEqual(len(calls), 1)
        scan.daily_bars(INST, fetch, dt.datetime(2026, 10, 30, 16, 0, tzinfo=IST))
        self.assertEqual(len(calls), 2)                                    # a new day closed
        self.assertIn("range=2y&interval=1d", calls[0])


if __name__ == "__main__":
    unittest.main()
