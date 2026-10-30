"""Tests for the web API (app.py) in demo mode. Skipped if Flask isn't installed."""

import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import flask  # noqa: F401
except ImportError:
    flask = None


@unittest.skipIf(flask is None, "flask not installed")
class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(INDTRADER_DEMO="1", TRADER_NOW="2026-10-30T10:20", TRADER_DIR="/tmp/indtrader-webtest")
        os.environ.pop("APP_PASSWORD", None)
        sys.path.insert(0, ROOT)
        cls.mod = importlib.import_module("app")
        cls.c = cls.mod.app.test_client()

    @classmethod
    def tearDownClass(cls):
        for k in ("INDTRADER_DEMO", "TRADER_NOW"):
            os.environ.pop(k, None)

    def get(self, url, code=200):
        r = self.c.get(url)
        self.assertEqual(r.status_code, code, r.get_data(as_text=True)[:300])
        return r.get_json()

    def test_status(self):
        s = self.get("/api/status")
        self.assertEqual(s["state"], "open")
        self.assertEqual(s["rules"]["capital"], 20000)

    def test_search_index(self):
        items = self.get("/api/search-index")["items"]
        self.assertIn("RELIANCE", [i["s"] for i in items])

    def test_stock(self):
        s = self.get("/api/stock?symbol=RELIANCE&exchange=NSE")
        self.assertEqual(s["name"], "Reliance Industries Ltd.")
        self.assertGreater(len(s["daily"]), 300)
        self.assertIn(21, {int(k) for k in s["ranges"]["ranges"]})
        self.assertIn("trend", s["stats"])
        self.assertIsNotNone(s["orb"])                      # the market is open in the fake clock

    def test_plan_and_odds(self):
        p = self.get("/api/plan?symbol=TCS&exchange=NSE&mode=swing&entry=3050&stop=2950")
        self.assertIn(p["status"], ("ok", "blocked"))
        self.assertIn("target_first", p["odds"])
        p = self.get("/api/plan?symbol=ITC&exchange=NSE&mode=swing&entry=410&stop=395&capital=50000&drawdown=5000&risk=500")
        self.assertEqual(p["risk_allowed"], 500)
        p = self.get("/api/plan?symbol=ITC&exchange=NSE&mode=swing&entry=410&stop=395&loss=2000")
        self.assertTrue(any("loss limit" in b for b in p["blocks"]))

    def test_bad_input(self):
        self.get("/api/plan?symbol=ITC&mode=swing&entry=abc&stop=1", 400)
        self.get("/api/plan?symbol=ITC&mode=yolo&entry=10&stop=9", 400)
        self.get("/api/stock?symbol=", 400)

    def test_scan_chunks(self):
        r = self.get("/api/scan?universe=nifty50&kind=orb&limit=4")
        self.assertEqual(r["total"], 10)
        self.assertEqual(r["next"], 4)
        self.assertEqual(len(r["hits"]), 4)
        r = self.get("/api/scan?universe=nifty50&kind=swing&offset=8&limit=4")
        self.assertIsNone(r["next"])

    def test_market(self):
        m = self.get("/api/market")
        self.assertEqual([i["name"] for i in m["indices"]], ["Nifty 50", "Bank Nifty"])
        self.assertEqual(len(m["movers"]), 10)

    def test_cdn_cache_header(self):
        r = self.c.get("/api/status")
        self.assertIn("s-maxage", r.headers["Cache-Control"])

    def test_password(self):
        self.mod.PASSWORD = "secret"
        try:
            self.assertEqual(self.c.get("/api/status").status_code, 401)
            r = self.c.get("/api/status", headers={"X-Key": "secret"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("no-store", r.headers["Cache-Control"])
            self.assertEqual(self.c.get("/api/ping").status_code, 200)
        finally:
            self.mod.PASSWORD = ""
