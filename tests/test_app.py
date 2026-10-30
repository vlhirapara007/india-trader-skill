"""Smoke test for the web app in demo mode (skipped if Streamlit isn't installed)."""

import os
import unittest

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # the skills themselves need no packages
    AppTest = None

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "streamlit_app.py")


@unittest.skipIf(AppTest is None, "streamlit not installed")
class App(unittest.TestCase):
    def setUp(self):
        os.environ.update(INDTRADER_DEMO="1", TRADER_NOW="2026-10-30T10:20", TRADER_DIR="/tmp/indtrader-apptest")

    def tearDown(self):
        for k in ("INDTRADER_DEMO", "TRADER_NOW"):
            os.environ.pop(k, None)

    def test_stock_page_with_a_stock(self):
        at = AppTest.from_file(APP, default_timeout=180)
        at.run()
        self.assertFalse(at.exception)
        at.selectbox[0].select_index(0).run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any("Fits your rules" in m.value or "Blocked" in m.value for m in at.markdown))
