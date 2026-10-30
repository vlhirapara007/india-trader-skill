"""Tests for core/indtrader.py. Run: python3 -m unittest discover -s tests"""

import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")

spec = importlib.util.spec_from_file_location("indtrader", os.path.join(ROOT, "core", "indtrader.py"))
it = importlib.util.module_from_spec(spec)
spec.loader.exec_module(it)


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class TempTraderDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["TRADER_DIR"] = self.tmp
        os.environ.pop("TRADER_NOW", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("TRADER_DIR", None)
        os.environ.pop("TRADER_NOW", None)


class Calendar(unittest.TestCase):
    def at(self, s):
        return dt.datetime.fromisoformat(s).replace(tzinfo=it.IST)

    def test_holidays_are_weekdays_in_2026(self):
        self.assertEqual(len(it.HOLIDAYS), 16)
        for d in it.HOLIDAYS:
            self.assertTrue(d.startswith("2026"))
            self.assertLess(dt.date.fromisoformat(d).weekday(), 5, d)

    def test_sessions(self):
        cases = {
            "2026-10-29T08:59": "closed", "2026-10-29T09:00": "pre-open",
            "2026-10-29T09:10": "pre-open-matching", "2026-10-29T09:15": "open",
            "2026-10-29T15:29": "open", "2026-10-29T15:30": "post-close",
            "2026-10-31T11:00": "closed", "2026-11-10T11:00": "closed",
        }
        for t, state in cases.items():
            self.assertEqual(it.market_status(self.at(t))["state"], state, t)

    def test_holiday_reason_and_next_open(self):
        s = it.market_status(self.at("2026-11-10T10:00"))
        self.assertIn("Diwali", s["reason"])
        self.assertEqual(s["next_open"], "2026-11-11T09:15+05:30")

    def test_friday_after_close_opens_monday(self):
        s = it.market_status(self.at("2026-10-30T16:00"))
        self.assertEqual(s["next_open"], "2026-11-02T09:15+05:30")

    def test_muhurat_sunday_flagged(self):
        s = it.market_status(self.at("2026-11-08T18:00"))
        self.assertEqual(s["state"], "closed")
        self.assertIn("muhurat", s)

    def test_unknown_year_warns(self):
        s = it.market_status(self.at("2027-01-04T10:00"))
        self.assertFalse(s["calendar_known"])
        self.assertIn("warning", s)
        cfg = dict(it.DEFAULT_CONFIG, extra_holidays={"2027-01-26": "Republic Day"})
        self.assertFalse(it.is_trading_day(dt.date(2027, 1, 26), cfg))
        self.assertTrue(it.market_status(self.at("2027-01-04T10:00"), cfg)["calendar_known"])

    def test_trading_day_walks(self):
        self.assertEqual(it.next_trading_day(dt.date(2026, 11, 9)), dt.date(2026, 11, 11))
        self.assertEqual(it.previous_trading_day(dt.date(2026, 11, 11)), dt.date(2026, 11, 9))
        self.assertEqual(it.trading_days_between(dt.date(2026, 11, 6), dt.date(2026, 11, 13)), 4)

    def test_fake_clock(self):
        os.environ["TRADER_NOW"] = "2026-11-02T09:31"
        try:
            self.assertEqual(it.now_ist().isoformat(), "2026-11-02T09:31:00+05:30")
        finally:
            del os.environ["TRADER_NOW"]


class Charges(unittest.TestCase):
    def test_intraday_round_trip(self):
        rt = it.round_trip(250, 255, 40, "intraday", "NSE")
        b, s = rt["buy"], rt["sell"]
        self.assertEqual(b["brokerage"], 10.0)        # 0.1% of 10,000 is under the Rs 20 cap
        self.assertEqual(b["stt"], 0.0)               # intraday STT is sell side only
        self.assertEqual(s["stt"], 2.55)              # 0.025% of 10,200
        self.assertEqual(b["stamp_duty"], 0.3)        # 0.003% buy side
        self.assertEqual(s["stamp_duty"], 0.0)
        self.assertEqual(s["dp"], 0.0)
        self.assertEqual(rt["gross_pnl"], 200.0)
        self.assertAlmostEqual(rt["net_pnl"], rt["gross_pnl"] - rt["charges"], places=2)
        self.assertGreater(rt["breakeven"], 250)
        self.assertLess(rt["breakeven"], 251)

    def test_brokerage_cap_and_minimum(self):
        self.assertEqual(it.order_charges(5000, 10, "buy")["brokerage"], 20.0)   # 0.1% of 50,000 = 50 -> cap 20
        self.assertEqual(it.order_charges(100, 10, "buy")["brokerage"], 5.0)     # 0.1% of 1,000 = 1 -> min 5
        self.assertEqual(it.order_charges(2, 1, "buy")["brokerage"], 2.0)        # never more than the order

    def test_delivery_has_stt_both_sides_and_dp_on_sell(self):
        rt = it.round_trip(1000, 1100, 10, "delivery", "NSE")
        self.assertEqual(rt["buy"]["stt"], 10.0)
        self.assertEqual(rt["sell"]["stt"], 11.0)
        self.assertEqual(rt["buy"]["stamp_duty"], 1.5)
        self.assertEqual(rt["sell"]["dp"], 20.0)
        self.assertEqual(rt["buy"]["dp"], 0.0)

    def test_gst_base(self):
        c = it.order_charges(1000, 10, "sell", "delivery", "NSE")
        base = c["brokerage"] + c["dp"] + c["exchange_txn"] + c["sebi"] + c["ipft"]
        self.assertAlmostEqual(c["gst"], round(base * 0.18, 2), places=2)

    def test_bse_rates(self):
        c = it.order_charges(1000, 10, "buy", "intraday", "BSE")
        self.assertEqual(c["exchange_txn"], 0.38)
        self.assertEqual(c["ipft"], 0.0)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            it.order_charges(100, 1, "short")
        with self.assertRaises(ValueError):
            it.order_charges(100, 1, "buy", "mtf")

    def test_breakeven_really_breaks_even(self):
        rt = it.round_trip(512.35, 512.35, 19, "intraday", "NSE")
        be = it.round_trip(512.35, rt["breakeven"], 19, "intraday", "NSE")
        self.assertGreaterEqual(be["net_pnl"], -0.05)


class ConfigAndAccount(TempTraderDir):
    def test_defaults_match_the_plan(self):
        cfg = it.load_config()
        self.assertEqual((cfg["capital"], cfg["max_drawdown"], cfg["risk_per_trade"]), (20000, 2000, 200))
        self.assertEqual(it.validate_config(cfg), [])

    def test_set_and_validate(self):
        it.main(["config", "--set", "risk_per_trade=300", "universe=nifty50"])
        cfg = it.load_config()
        self.assertEqual(cfg["risk_per_trade"], 300)
        with self.assertRaises(SystemExit):
            it.main(["config", "--set", "risk_per_trade=5000"])
        with self.assertRaises(SystemExit):
            it.main(["config", "--set", "nonsense=1"])

    def test_typo_key_reported(self):
        it.write_json(it.path("config.json"), {"captial": 5})
        self.assertTrue(any("captial" in e for e in it.validate_config(it.load_config())))

    def test_account_fresh(self):
        st = it.account_state(today=dt.date(2026, 11, 2))
        self.assertEqual(st["equity"], 20000)
        self.assertEqual(st["floor"], 18000)
        self.assertEqual(st["risk_per_trade_now"], 200)
        self.assertFalse(st["halted"])

    def test_losses_shrink_room_then_halt(self):
        j = [{"status": "closed", "net_pnl": -1900, "entry_date": "2026-10-20"}]
        st = it.account_state(journal=j, today=dt.date(2026, 11, 2))
        self.assertEqual(st["room_left"], 100)
        self.assertEqual(st["risk_per_trade_now"], 100)   # never more than what is left
        j.append({"status": "closed", "net_pnl": -150, "entry_date": "2026-10-21"})
        st = it.account_state(journal=j, today=dt.date(2026, 11, 2))
        self.assertTrue(st["halted"])
        self.assertIn("loss limit", st["halt_reason"])

    def test_profits_do_not_raise_the_limit(self):
        j = [{"status": "closed", "net_pnl": 3000, "entry_date": "2026-10-20"}]
        st = it.account_state(journal=j, today=dt.date(2026, 11, 2))
        self.assertEqual(st["equity"], 23000)
        self.assertEqual(st["loss_used"], 0)
        self.assertEqual(st["risk_per_trade_now"], 200)

    def test_open_risk_counts(self):
        j = [{"status": "open", "risk": 200, "entry_date": "2026-11-02"} for _ in range(4)]
        st = it.account_state(journal=j, today=dt.date(2026, 11, 2))
        self.assertTrue(st["halted"])          # 4 trades today hits max 3/day first
        self.assertEqual(st["open_risk"], 800)

    def test_money_format(self):
        self.assertEqual(it.money(123456.5), "1,23,456.50")
        self.assertEqual(it.money(20000), "20,000.00")
        self.assertEqual(it.money(-999), "-999.00")
        self.assertEqual(it.money(12345678), "1,23,45,678.00")


class Universes(TempTraderDir):
    def test_index_csv(self):
        items = it.parse_index_csv(fixture("ind_nifty50list.csv"))
        self.assertEqual([i["symbol"] for i in items], ["RELIANCE", "HDFCBANK", "M&M"])
        self.assertEqual(items[2]["yahoo"], "M&M.NS")
        self.assertTrue(all(i["intraday_allowed"] for i in items))

    def test_nse_equity_csv_bom_spaces_and_series(self):
        items = it.parse_nse_equity_csv(fixture("EQUITY_L.csv"))
        self.assertEqual([i["symbol"] for i in items], ["RELIANCE", "SOMEBE"])   # bond series dropped
        self.assertEqual(items[0]["isin"], "INE002A01018")
        self.assertFalse(items[1]["intraday_allowed"])                          # BE is trade-to-trade

    def test_bse_json(self):
        items = it.parse_bse_json(fixture("bse_scrips.json"))
        self.assertEqual([i["symbol"] for i in items], ["RELIANCE", "BSEONLY", "ZGRP"])  # delisted dropped
        self.assertEqual(items[0]["bse_code"], "500325")
        self.assertEqual(items[0]["yahoo"], "RELIANCE.BO")
        self.assertFalse(items[2]["intraday_allowed"])                          # Z group

    def test_all_merges_by_isin(self):
        nse = it.parse_nse_equity_csv(fixture("EQUITY_L.csv"))
        bse = it.parse_bse_json(fixture("bse_scrips.json"))
        allx = it.merge_all(nse, bse)
        self.assertEqual([(i["symbol"], i["exchange"]) for i in allx],
                         [("RELIANCE", "NSE"), ("SOMEBE", "NSE"), ("BSEONLY", "BSE"), ("ZGRP", "BSE")])

    def test_load_caches_and_falls_back(self):
        calls = []

        def fetch(name):
            calls.append(name)
            return fixture("ind_nifty50list.csv")
        self.assertEqual(len(it.load_universe("nifty50", fetch=fetch)), 3)
        self.assertEqual(len(it.load_universe("nifty50", fetch=fetch)), 3)
        self.assertEqual(calls, ["nifty50"])                 # second call used the cache

        def broken(name):
            raise it.FetchError("blocked")
        self.assertEqual(len(it.load_universe("nifty50", refresh=True, fetch=broken)), 3)  # stale copy
        with self.assertRaises(SystemExit) as cm:
            it.load_universe("banknifty", fetch=broken)
        self.assertIn("--file", str(cm.exception))           # tells you how to load it by hand

    def test_load_from_saved_file(self):
        items = it.load_universe("nse_all", from_file=os.path.join(FIX, "EQUITY_L.csv"))
        self.assertEqual(len(items), 2)
        self.assertTrue(os.path.exists(it.path("universe", "nse_all.json")))

    def test_all_universe(self):
        src = {"nse_all": fixture("EQUITY_L.csv"), "bse_all": fixture("bse_scrips.json")}
        self.assertEqual(len(it.load_universe("all", fetch=src.get)), 4)

    def test_custom_list(self):
        items = it.parse_custom_list("RELIANCE, TCS\n# comment\nBSE:500325")
        self.assertEqual([i["yahoo"] for i in items], ["RELIANCE.NS", "TCS.NS", "500325.BO"])

    def test_unknown_universe(self):
        with self.assertRaises(SystemExit):
            it.load_universe("sensex500")


class Prices(unittest.TestCase):
    def test_parse_drops_gaps_and_uses_ist(self):
        r = it.parse_yahoo_chart(fixture("yahoo-RELIANCE.NS-5m.json"))
        self.assertEqual(len(r["bars"]), 3)
        self.assertEqual(r["bars"][0]["t"], "2026-10-29T09:15+05:30")
        self.assertEqual(r["bars"][-1]["c"], 1490.4)
        self.assertEqual(r["meta"]["currency"], "INR")

    def test_error_payload(self):
        with self.assertRaises(it.FetchError):
            it.parse_yahoo_chart(fixture("yahoo-error.json"))

    def test_fetch_falls_back_to_second_host(self):
        seen = []

        def fetch(url):
            seen.append(url)
            if "query2" in url:
                raise it.FetchError("502")
            return fixture("yahoo-RELIANCE.NS-5m.json")
        r = it.fetch_bars("RELIANCE", "NSE", "5m", "1d", fetch=fetch)
        self.assertEqual(len(r["bars"]), 3)
        self.assertIn("query1", seen[-1])
        self.assertIn("/RELIANCE.NS?", seen[0])

    def test_url_encodes_ampersand(self):
        self.assertIn("/M%26M.NS?", it.yahoo_chart_url("M&M.NS"))

    def test_session_bars(self):
        bars = [{"t": "2026-10-29T09:10+05:30"}, {"t": "2026-10-29T09:15+05:30"},
                {"t": "2026-10-29T15:25+05:30"}, {"t": "2026-10-29T15:30+05:30"},
                {"t": "2026-10-30T09:15+05:30"}]
        self.assertEqual(len(it.session_bars(bars, "2026-10-29")), 2)

    def test_tick_round(self):
        self.assertEqual(it.tick_round(100.03), 100.05)
        self.assertEqual(it.tick_round(100.07, mode="down"), 100.05)
        self.assertEqual(it.tick_round(100.01, mode="up"), 100.05)
        self.assertEqual(it.tick_round(100.05, mode="down"), 100.05)


class Copies(unittest.TestCase):
    def test_every_skill_carries_the_same_core(self):
        with open(os.path.join(ROOT, "core", "indtrader.py"), "rb") as fh:
            core = fh.read()
        for d in sorted(os.listdir(ROOT)):
            p = os.path.join(ROOT, d, "indtrader.py")
            if d.startswith("india-") and os.path.isdir(os.path.join(ROOT, d)):
                self.assertTrue(os.path.exists(p), "%s has no indtrader.py: run python3 tools/build.py" % d)
                with open(p, "rb") as fh:
                    self.assertEqual(fh.read(), core, "%s/indtrader.py is stale: run python3 tools/build.py" % d)


if __name__ == "__main__":
    unittest.main()
