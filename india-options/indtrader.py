#!/usr/bin/env python3
"""indtrader: the shared core of the India Trader skill pack.

One file, standard library only, Python 3.8+. Every skill folder carries an
identical copy (tools/build.py syncs it), so each skill still works when it is
uploaded on its own.

What lives here
  * India time: IST (UTC+05:30, no daylight saving), NSE/BSE sessions,
    the 2026 trading holidays and Muhurat trading.
  * Your rules: trader/config.json (capital, the overall loss limit, risk per
    trade) and the account state worked out from trader/journal.json.
  * Groww's charges: brokerage, STT, exchange fees, SEBI fee, IPFT, stamp
    duty, DP charge and GST, for intraday and delivery, NSE or BSE.
  * Stock universes: Nifty 50, Nifty 200, Bank Nifty, all NSE, all BSE, all.
  * Prices: free Yahoo Finance chart bars for NSE (.NS) and BSE (.BO).

Nothing here places an order. Every plan is for you to place yourself.

Command line (run from your project folder; everything is written to ./trader):
  python3 indtrader.py status                 market open/closed right now (IST)
  python3 indtrader.py calendar [--year 2026] trading holidays
  python3 indtrader.py config [--set k=v ...] show or change your rules
  python3 indtrader.py account                equity, loss limit used, risk per trade
  python3 indtrader.py charges --buy 250 --sell 255 --qty 40 [--product intraday|delivery] [--exchange NSE|BSE]
  python3 indtrader.py universe nifty200 [--refresh] [--file path]
  python3 indtrader.py bars RELIANCE [--exchange NSE] [--interval 5m] [--range 5d]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Time and the market calendar
# --------------------------------------------------------------------------

# India has had no daylight saving since 1945, so a fixed offset is exact and
# needs no tz database (zoneinfo is missing on Python 3.8 and on Windows).
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

PREOPEN_START = dt.time(9, 0)    # pre-open order entry 09:00-09:08
PREOPEN_END = dt.time(9, 8)
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

# NSE equity segment trading holidays (weekdays only). BSE follows the same
# list for equities. Source: NSE circular, as listed by ClearTax
# (cleartax.in/s/nse-holidays-2026), checked 2026-10-30.
HOLIDAYS = {
    "2026-01-15": "Municipal Corporation elections (Maharashtra)",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
HOLIDAY_YEARS = {2026}

# Muhurat trading: a one-hour evening session on Diwali, usually a Sunday.
# Timings are announced by NSE a few days before; None until then.
MUHURAT = {
    "2026-11-08": None,
}


def now_ist():
    """The current time in IST. TRADER_NOW=2026-11-02T09:31 overrides it (tests, replays)."""
    fake = os.environ.get("TRADER_NOW")
    if fake:
        t = dt.datetime.fromisoformat(fake)
        return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
    return dt.datetime.now(IST)


def to_ist(ts):
    """Unix seconds -> aware IST datetime."""
    return dt.datetime.fromtimestamp(ts, IST)


def parse_hhmm(s):
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


def holidays(cfg=None):
    """The holiday table plus any extra_holidays from config (for years not yet built in)."""
    out = dict(HOLIDAYS)
    if cfg:
        for d, name in (cfg.get("extra_holidays") or {}).items():
            out[d] = name
    return out


def calendar_known(d, cfg=None):
    years = set(HOLIDAY_YEARS)
    if cfg:
        years |= {int(k[:4]) for k in (cfg.get("extra_holidays") or {})}
    return d.year in years


def is_trading_day(d, cfg=None):
    """Weekdays that are not exchange holidays. Muhurat Sundays are not regular days."""
    if isinstance(d, dt.datetime):
        d = d.date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in holidays(cfg)


def next_trading_day(d, cfg=None):
    d = (d.date() if isinstance(d, dt.datetime) else d) + dt.timedelta(days=1)
    while not is_trading_day(d, cfg):
        d += dt.timedelta(days=1)
    return d


def previous_trading_day(d, cfg=None):
    d = (d.date() if isinstance(d, dt.datetime) else d) - dt.timedelta(days=1)
    while not is_trading_day(d, cfg):
        d -= dt.timedelta(days=1)
    return d


def last_session_close(t=None, cfg=None):
    """When the most recent completed session closed (15:30 IST)."""
    t = (t or now_ist()).astimezone(IST)
    d = t.date()
    if is_trading_day(d, cfg) and t.time() >= MARKET_CLOSE:
        return dt.datetime.combine(d, MARKET_CLOSE, IST)
    return dt.datetime.combine(previous_trading_day(d, cfg), MARKET_CLOSE, IST)


def trading_days_between(a, b, cfg=None):
    """Trading days after a, up to and including b."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if is_trading_day(d, cfg):
            n += 1
    return n


def market_status(t=None, cfg=None):
    """Where the market is right now, in plain words.

    Returns a dict: state (closed / pre-open / pre-open-matching / open / post-close),
    reason, the next open, and whether the holiday list covers this year.
    """
    t = (t or now_ist()).astimezone(IST)
    d, clock = t.date(), t.time()
    known = calendar_known(d, cfg)
    out = {"now": t.isoformat(timespec="minutes"), "date": d.isoformat(),
           "calendar_known": known}
    if d.isoformat() in MUHURAT:
        out["muhurat"] = MUHURAT[d.isoformat()] or "timings not announced yet"
    if not is_trading_day(d, cfg):
        name = holidays(cfg).get(d.isoformat())
        out["state"] = "closed"
        out["reason"] = ("holiday: " + name) if name else "weekend"
        nd = next_trading_day(d, cfg)
        out["next_open"] = dt.datetime.combine(nd, MARKET_OPEN, IST).isoformat(timespec="minutes")
        return out
    if clock < PREOPEN_START:
        out["state"], out["reason"] = "closed", "before pre-open (09:00)"
        out["next_open"] = dt.datetime.combine(d, MARKET_OPEN, IST).isoformat(timespec="minutes")
    elif clock < PREOPEN_END:
        out["state"], out["reason"] = "pre-open", "pre-open order entry 09:00-09:08"
        out["next_open"] = dt.datetime.combine(d, MARKET_OPEN, IST).isoformat(timespec="minutes")
    elif clock < MARKET_OPEN:
        out["state"], out["reason"] = "pre-open-matching", "pre-open matching, opens 09:15"
        out["next_open"] = dt.datetime.combine(d, MARKET_OPEN, IST).isoformat(timespec="minutes")
    elif clock < MARKET_CLOSE:
        out["state"], out["reason"] = "open", "normal market 09:15-15:30"
        out["closes"] = dt.datetime.combine(d, MARKET_CLOSE, IST).isoformat(timespec="minutes")
    else:
        out["state"], out["reason"] = "post-close", "closed for the day at 15:30"
        nd = next_trading_day(d, cfg)
        out["next_open"] = dt.datetime.combine(nd, MARKET_OPEN, IST).isoformat(timespec="minutes")
    if not known:
        out["warning"] = ("the %d holiday list is not built in yet: weekdays are assumed open. "
                          "Add them to trader/config.json as extra_holidays." % d.year)
    return out


# --------------------------------------------------------------------------
# Files, config and your rules
# --------------------------------------------------------------------------

def trader_dir():
    d = os.environ.get("TRADER_DIR") or os.path.join(os.getcwd(), "trader")
    os.makedirs(d, exist_ok=True)
    return d


def path(*parts):
    return os.path.join(trader_dir(), *parts)


def read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise SystemExit("%s is not valid JSON (%s). Fix or delete it." % (p, e))


def write_json(p, data):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)


DEFAULT_CONFIG = {
    "capital": 20000,              # starting account, Rs
    "max_drawdown": 2000,          # stop all new trades once losses reach this (10%)
    "risk_per_trade": 200,         # Rs lost if the stop fills, charges included (1%)
    "max_trades_per_day": 3,
    "max_open_positions": 4,       # across intraday, swing and positional
    "max_position_pct": 50,        # no single overnight position over this % of equity
    "intraday_max_position_pct": 100,  # intraday is closed the same day; still cash only, no leverage
    "min_reward_risk": 1.5,        # target must pay at least this many R after charges
    "max_charges_pct_of_risk": 25, # skip trades whose round-trip charges exceed this % of risk
    "broker": "groww",
    "exchange": "NSE",             # NSE or BSE, preferred when a stock is on both
    "universe": "nifty200",
    "intraday_entry_window": "09:30-11:00",
    "intraday_flat_by": "15:10",   # square off yourself before the broker's auto square-off
    "min_price": 20,               # skip penny stocks
    "min_avg_traded_value_cr": 5,  # 20-day average daily traded value, Rs crore (liquidity)
    "scan_workers": 8,             # parallel downloads while scanning
    "extra_holidays": {},          # {"2027-01-26": "Republic Day"} for years not built in
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = read_json(path("config.json"), {}) or {}
    unknown = sorted(set(user) - set(DEFAULT_CONFIG))
    cfg.update(user)
    cfg["_unknown_keys"] = unknown
    return cfg


def save_config(cfg):
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    write_json(path("config.json"), clean)


def coerce(key, raw):
    """Turn a --set value into the type the default has."""
    base = DEFAULT_CONFIG.get(key)
    if isinstance(base, bool):
        return raw.lower() in ("1", "true", "yes", "y")
    if isinstance(base, int) and not isinstance(base, bool):
        return int(float(raw)) if float(raw).is_integer() else float(raw)
    if isinstance(base, float):
        return float(raw)
    if isinstance(base, dict):
        return json.loads(raw)
    return raw


def validate_config(cfg):
    """Plain-word problems with the rules, empty when fine."""
    errs = []
    if cfg["capital"] <= 0:
        errs.append("capital must be above 0")
    if not 0 < cfg["max_drawdown"] <= cfg["capital"]:
        errs.append("max_drawdown must be above 0 and no more than capital")
    if not 0 < cfg["risk_per_trade"] <= cfg["max_drawdown"]:
        errs.append("risk_per_trade must be above 0 and no more than max_drawdown")
    if cfg["exchange"] not in ("NSE", "BSE"):
        errs.append("exchange must be NSE or BSE")
    if str(cfg.get("broker", "")).lower() not in BROKERS:
        errs.append("broker %r has no charge table yet (known: %s)" % (cfg.get("broker"), ", ".join(sorted(BROKERS))))
    for k in ("intraday_flat_by",):
        try:
            t = parse_hhmm(cfg[k])
            if not MARKET_OPEN < t <= MARKET_CLOSE:
                errs.append("%s must be inside market hours" % k)
        except Exception:
            errs.append("%s must look like 15:10" % k)
    try:
        a, b = [parse_hhmm(x) for x in cfg["intraday_entry_window"].split("-")]
        if not MARKET_OPEN <= a < b <= MARKET_CLOSE:
            errs.append("intraday_entry_window must be inside market hours, start before end")
    except Exception:
        errs.append("intraday_entry_window must look like 09:30-11:00")
    if cfg.get("_unknown_keys"):
        errs.append("unknown keys in config.json (typo?): " + ", ".join(cfg["_unknown_keys"]))
    return errs


def load_journal():
    """trader/journal.json: the trades you actually took. See india-setup SKILL.md for the shape."""
    return read_json(path("journal.json"), []) or []


def account_state(cfg=None, journal=None, today=None):
    """Equity, how much of the loss limit is used, and what the next trade may risk.

    Closed trades count by their net P&L (after charges). Open trades count by
    the most they can still lose (their risk at the stop), so two open trades
    can never together break the limit.
    """
    cfg = cfg or load_config()
    journal = load_journal() if journal is None else journal
    today = today or now_ist().date()
    realized = sum(float(t.get("net_pnl") or 0) for t in journal if t.get("status") == "closed")
    open_trades = [t for t in journal if t.get("status") == "open"]
    open_risk = sum(max(0.0, float(t.get("risk") or 0)) for t in open_trades)
    trades_today = sum(1 for t in journal if str(t.get("entry_date", "")) == today.isoformat())
    equity = cfg["capital"] + realized
    loss_used = max(0.0, -realized)
    room = cfg["max_drawdown"] - loss_used - open_risk
    risk_now = max(0.0, min(cfg["risk_per_trade"], room))
    halted, why = False, ""
    if loss_used >= cfg["max_drawdown"]:
        halted, why = True, "loss limit reached: Rs %s lost of Rs %s. No new trades." % (
            money(loss_used), money(cfg["max_drawdown"]))
    elif room <= 0:
        halted, why = True, "open trades already use the rest of the loss limit"
    elif trades_today >= cfg["max_trades_per_day"]:
        halted, why = True, "already %d trades today (max %d)" % (trades_today, cfg["max_trades_per_day"])
    elif len(open_trades) >= cfg["max_open_positions"]:
        halted, why = True, "%d positions open (max %d)" % (len(open_trades), cfg["max_open_positions"])
    return {
        "capital": cfg["capital"],
        "equity": round(equity, 2),
        "floor": cfg["capital"] - cfg["max_drawdown"],
        "realized_pnl": round(realized, 2),
        "loss_limit": cfg["max_drawdown"],
        "loss_used": round(loss_used, 2),
        "open_positions": len(open_trades),
        "open_risk": round(open_risk, 2),
        "room_left": round(max(0.0, room), 2),
        "risk_per_trade_now": round(risk_now, 2),
        "trades_today": trades_today,
        "halted": halted,
        "halt_reason": why,
    }


def money(x):
    """Indian digit grouping: 123456.5 -> 1,23,456.50"""
    neg = x < 0
    s = "%.2f" % abs(x)
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + "." + frac


# --------------------------------------------------------------------------
# Charges
# --------------------------------------------------------------------------

# Groww equity charges, from groww.in/pricing/stocks, checked 2026-10-30.
# Percentages are of turnover (price x quantity) on that side.
GROWW = {
    "as_of": "2026-10-30",
    "source": "https://groww.in/pricing/stocks",
    "brokerage_pct": 0.1, "brokerage_cap": 20.0, "brokerage_min": 5.0,   # per executed order
    "stt": {"intraday": {"buy": 0.0, "sell": 0.025}, "delivery": {"buy": 0.1, "sell": 0.1}},
    "stamp": {"intraday": 0.003, "delivery": 0.015},                      # buy side only
    "exchange_txn": {"NSE": 0.00297, "BSE": 0.00375},                      # both sides
    "sebi": 0.0001,                                                        # both sides
    "ipft": {"NSE": 0.0001, "BSE": 0.0},                                   # both sides
    "dp_per_sell": 20.0,   # delivery sells: depository 3.50 + Groww 16.50, per stock per day
    "dp_free_below": 100.0,  # no Groww DP part when the sell is under Rs 100
    "dp_depository_part": 3.5,
    "gst_pct": 18.0,       # on brokerage, DP, exchange, SEBI and IPFT charges
    "auto_squareoff_fee": 50.0,
}

BROKERS = {"groww": GROWW}


def _pct(value, pct):
    return value * pct / 100.0


def order_charges(price, qty, side, product="intraday", exchange="NSE", rates=None):
    """Every charge on ONE executed order. side: buy/sell; product: intraday/delivery."""
    r = rates or GROWW
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if product not in ("intraday", "delivery"):
        raise ValueError("product must be intraday or delivery")
    if exchange not in ("NSE", "BSE"):
        raise ValueError("exchange must be NSE or BSE")
    v = float(price) * int(qty)
    brokerage = 0.0
    if v > 0:
        brokerage = max(r["brokerage_min"], min(r["brokerage_cap"], _pct(v, r["brokerage_pct"])))
        brokerage = min(brokerage, v)  # never more than the order itself
    stt = _pct(v, r["stt"][product][side])
    stamp = _pct(v, r["stamp"][product]) if side == "buy" else 0.0
    exch = _pct(v, r["exchange_txn"][exchange])
    sebi = _pct(v, r["sebi"])
    ipft = _pct(v, r["ipft"][exchange])
    dp = 0.0
    if product == "delivery" and side == "sell" and v > 0:
        dp = r["dp_per_sell"] if v >= r["dp_free_below"] else r["dp_depository_part"]
    gst = _pct(brokerage + dp + exch + sebi + ipft, r["gst_pct"])
    parts = {"brokerage": brokerage, "stt": stt, "stamp_duty": stamp, "exchange_txn": exch,
             "sebi": sebi, "ipft": ipft, "dp": dp, "gst": gst}
    parts = {k: round(x, 2) for k, x in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    parts["turnover"] = round(v, 2)
    return parts


def round_trip(entry, exit_price, qty, product="intraday", exchange="NSE", rates=None):
    """Buy at entry, sell at exit_price: gross P&L, all charges, net P&L, breakeven."""
    b = order_charges(entry, qty, "buy", product, exchange, rates)
    s = order_charges(exit_price, qty, "sell", product, exchange, rates)
    gross = (float(exit_price) - float(entry)) * int(qty)
    charges = round(b["total"] + s["total"], 2)
    # breakeven sell price: smallest price where net >= 0 (charges move a little with price)
    be = float(entry)
    if qty:
        be = float(entry) + charges / int(qty)
        for _ in range(3):
            c = b["total"] + order_charges(be, qty, "sell", product, exchange, rates)["total"]
            be = float(entry) + c / int(qty)
    return {"qty": int(qty), "entry": float(entry), "exit": float(exit_price),
            "gross_pnl": round(gross, 2), "charges": charges,
            "net_pnl": round(gross - charges, 2), "buy": b, "sell": s,
            "breakeven": round(be, 2)}


def broker_rates(cfg=None):
    name = str((cfg or {}).get("broker", "groww")).lower()
    if name not in BROKERS:
        raise SystemExit("No charge table for broker %r yet. Known: %s" % (name, ", ".join(BROKERS)))
    return BROKERS[name]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class FetchError(Exception):
    pass


def http_get(url, headers=None, timeout=20, retries=2, pause=1.5):
    """GET a URL and return the body as text. Retries on 5xx / network errors."""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-IN,en;q=0.9"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(enc, errors="replace")
        except urllib.error.HTTPError as e:
            last = "%s answered HTTP %d" % (url, e.code)
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, OSError) as e:
            last = "%s: %s" % (url, getattr(e, "reason", e))
        if attempt < retries:
            time.sleep(pause * (attempt + 1))
    raise FetchError(last or url)


# --------------------------------------------------------------------------
# Stock universes
# --------------------------------------------------------------------------

UNIVERSE_SOURCES = {
    "nifty50": "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "nifty200": "https://niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "banknifty": "https://niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
    "nse_all": "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "bse_all": ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
                "?Group=&Scripcode=&industry=&segment=Equity&status=Active"),
}
UNIVERSES = ("nifty50", "nifty200", "banknifty", "nse_all", "bse_all", "all")
BSE_HEADERS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
               "Accept": "application/json, text/plain, */*"}
# NSE series you can trade normally. BE/BZ are trade-to-trade (delivery only, no intraday).
NSE_SERIES = {"EQ": "normal", "BE": "trade-to-trade", "BZ": "trade-to-trade", "SM": "SME", "ST": "SME"}
UNIVERSE_MAX_AGE_DAYS = 7


def yahoo_symbol(symbol, exchange):
    """RELIANCE on NSE -> RELIANCE.NS; on BSE -> RELIANCE.BO (or the 6-digit code .BO)."""
    return symbol.strip().upper() + (".NS" if exchange == "NSE" else ".BO")


def _instrument(symbol, name, exchange, series="EQ", isin="", industry="", code=""):
    sym = symbol.strip().upper()
    return {"symbol": sym, "name": " ".join(name.split()), "exchange": exchange,
            "series": series.strip().upper(), "isin": isin.strip().upper(),
            "industry": industry.strip(), "bse_code": str(code).strip(),
            # NSE: only the EQ series trades intraday. BSE groups are checked in parse_bse_json.
            "intraday_allowed": exchange == "BSE" or series.strip().upper() == "EQ",
            "yahoo": yahoo_symbol(sym, exchange)}


def _csv_rows(text):
    text = text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def parse_index_csv(text):
    """niftyindices.com constituent CSV: Company Name, Industry, Symbol, Series, ISIN Code."""
    out = []
    for r in _csv_rows(text):
        if not r.get("Symbol"):
            continue
        out.append(_instrument(r["Symbol"], r.get("Company Name", ""), "NSE",
                               r.get("Series", "EQ"), r.get("ISIN Code", ""), r.get("Industry", "")))
    if not out:
        raise ValueError("no rows with a Symbol column: is this a niftyindices constituent CSV?")
    return out


def parse_nse_equity_csv(text, series=("EQ", "BE", "BZ")):
    """NSE EQUITY_L.csv: SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, ..., ISIN NUMBER."""
    out = []
    for r in _csv_rows(text):
        sym, ser = r.get("SYMBOL", ""), r.get("SERIES", "").upper()
        if not sym or (series and ser not in series):
            continue
        out.append(_instrument(sym, r.get("NAME OF COMPANY", ""), "NSE", ser, r.get("ISIN NUMBER", "")))
    if not out:
        raise ValueError("no SYMBOL rows: is this NSE's EQUITY_L.csv?")
    return out


def parse_bse_json(text):
    """BSE ListofScripData: [{SCRIP_CD, Scrip_Name, Status, GROUP, ISIN_NUMBER, INDUSTRY, scrip_id}]."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("Table") or data.get("data") or []
    out = []
    for r in data:
        if str(r.get("Status", "Active")).strip().lower() != "active":
            continue
        sid = str(r.get("scrip_id") or "").strip()
        code = str(r.get("SCRIP_CD") or "").strip()
        if not (sid or code):
            continue
        group = str(r.get("GROUP") or "").strip().upper()
        inst = _instrument(sid or code, str(r.get("Scrip_Name") or r.get("Issuer_Name") or ""), "BSE",
                           group, str(r.get("ISIN_NUMBER") or ""), str(r.get("INDUSTRY") or ""), code)
        # BSE T / Z / ZP groups settle trade-to-trade: no intraday
        inst["intraday_allowed"] = group not in ("T", "Z", "ZP", "TS", "MT")
        if not sid:
            inst["yahoo"] = code + ".BO"
        out.append(inst)
    if not out:
        raise ValueError("no active scrips in the BSE list")
    return out


def merge_all(nse, bse):
    """All of India: every NSE stock, plus BSE stocks not on NSE (matched by ISIN)."""
    seen = {i["isin"] for i in nse if i["isin"]}
    extra = [i for i in bse if not i["isin"] or i["isin"] not in seen]
    return nse + extra


def parse_custom_list(text, exchange="NSE"):
    """A plain list, one symbol per line or comma separated; 'BSE:500325' style allowed."""
    out = []
    body = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for tok in body.replace(",", "\n").split():
        tok = tok.strip()
        if not tok:
            continue
        ex = exchange
        if ":" in tok:
            ex, tok = tok.split(":", 1)
            ex = ex.upper()
        out.append(_instrument(tok, "", ex))
    return out


def _fetch_universe_raw(name):
    url = UNIVERSE_SOURCES[name]
    headers = BSE_HEADERS if name == "bse_all" else {"Referer": "https://www.nseindia.com/"}
    return http_get(url, headers=headers, timeout=40)


def _parse_universe(name, text):
    if name in ("nifty50", "nifty200", "banknifty"):
        return parse_index_csv(text)
    if name == "nse_all":
        return parse_nse_equity_csv(text)
    if name == "bse_all":
        return parse_bse_json(text)
    raise ValueError(name)


def load_universe(name, refresh=False, fetch=None, from_file=None):
    """The list of stocks for a universe name, cached in trader/universe/<name>.json for 7 days.

    from_file: a CSV/JSON you downloaded yourself in a browser, when the site
    blocks scripts. fetch: an injectable function(name) -> text for tests.
    """
    name = name.lower()
    if name.startswith("file:"):
        with open(name[5:], encoding="utf-8") as fh:
            return parse_custom_list(fh.read())
    if name not in UNIVERSES:
        raise SystemExit("unknown universe %r. Choose: %s, or file:<path>" % (name, ", ".join(UNIVERSES)))
    if name == "all":
        nse = load_universe("nse_all", refresh, fetch)
        bse = load_universe("bse_all", refresh, fetch)
        return merge_all(nse, bse)
    cache = path("universe", name + ".json")
    if from_file:
        with open(from_file, encoding="utf-8-sig") as fh:
            items = _parse_universe(name, fh.read())
        write_json(cache, {"name": name, "fetched": now_ist().isoformat(timespec="minutes"),
                           "source": "file:" + os.path.abspath(from_file), "items": items})
        return items
    cached = read_json(cache)
    if cached and not refresh:
        age = now_ist() - dt.datetime.fromisoformat(cached["fetched"])
        if age.days < UNIVERSE_MAX_AGE_DAYS:
            return cached["items"]
    try:
        text = (fetch or _fetch_universe_raw)(name)
        items = _parse_universe(name, text)
    except (FetchError, ValueError, json.JSONDecodeError) as e:
        if cached:
            sys.stderr.write("warning: could not refresh %s (%s); using the copy from %s\n"
                             % (name, e, cached["fetched"]))
            return cached["items"]
        raise SystemExit(
            "Could not download the %s list (%s).\nOpen %s in your browser, save the file, then run:\n"
            "  python3 indtrader.py universe %s --file <saved file>" % (name, e, UNIVERSE_SOURCES[name], name))
    write_json(cache, {"name": name, "fetched": now_ist().isoformat(timespec="minutes"),
                       "source": UNIVERSE_SOURCES[name], "items": items})
    return items


# --------------------------------------------------------------------------
# Prices: Yahoo Finance chart bars
# --------------------------------------------------------------------------

YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
# Yahoo's limits on how far back each bar size goes
YAHOO_MAX_RANGE = {"1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d", "30m": "60d",
                   "60m": "730d", "1h": "730d", "1d": "max", "1wk": "max", "1mo": "max"}


def yahoo_chart_url(ysym, interval="5m", rng="5d", host=YAHOO_HOSTS[0]):
    return ("https://%s/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false"
            % (host, urllib.parse.quote(ysym, safe=".^"), rng, interval))


def parse_yahoo_chart(text):
    """Yahoo chart JSON -> {"meta": {...}, "bars": [{t, o, h, l, c, v}]}, t in IST, gaps dropped."""
    data = json.loads(text)
    chart = data.get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise FetchError("Yahoo: %s" % (err.get("description") or err.get("code") or err))
    res = (chart.get("result") or [None])[0]
    if not res:
        raise FetchError("Yahoo returned no data")
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        if None in (o, h, l, c):
            continue
        bars.append({"t": to_ist(t).isoformat(timespec="minutes"), "o": round(o, 2), "h": round(h, 2),
                     "l": round(l, 2), "c": round(c, 2), "v": int(v)})
    return {"meta": {"symbol": meta.get("symbol"), "currency": meta.get("currency"),
                     "exchange": meta.get("exchangeName"), "price": meta.get("regularMarketPrice"),
                     "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
                     "interval": meta.get("dataGranularity"), "range": meta.get("range")},
            "bars": bars}


def fetch_bars(symbol, exchange="NSE", interval="5m", rng="5d", fetch=None):
    """Bars for one stock. symbol may already carry .NS/.BO."""
    if symbol.startswith("^") or symbol.upper().endswith((".NS", ".BO")):
        ysym = symbol                     # an index (^NSEI) or already a Yahoo symbol
    else:
        ysym = yahoo_symbol(symbol, exchange)
    last = None
    for host in YAHOO_HOSTS:
        try:
            text = (fetch or http_get)(yahoo_chart_url(ysym, interval, rng, host))
            return parse_yahoo_chart(text)
        except FetchError as e:
            last = e
    raise FetchError("%s: %s" % (ysym, last))


def session_bars(bars, day=None):
    """Only the bars inside 09:15-15:30 IST, optionally for one date (YYYY-MM-DD)."""
    out = []
    for b in bars:
        t = dt.datetime.fromisoformat(b["t"])
        if day and t.date().isoformat() != day:
            continue
        if MARKET_OPEN <= t.time() < MARKET_CLOSE:
            out.append(b)
    return out


INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60}


def completed_bars(bars, interval, now=None):
    """Drop bars that have not finished yet.

    Yahoo includes the candle that is still forming (often with volume 0), and
    during market hours the daily series includes today's unfinished day.
    A rule like "a 5-minute candle CLOSES above X" must never see those.
    """
    now = (now or now_ist()).astimezone(IST)
    out = []
    for b in bars:
        t = dt.datetime.fromisoformat(b["t"]).astimezone(IST)
        if interval in INTERVAL_MINUTES:
            end = t + dt.timedelta(minutes=INTERVAL_MINUTES[interval])
            # the last candle of the day ends at 15:30 even if it started at 15:25
            close_dt = dt.datetime.combine(t.date(), MARKET_CLOSE, IST)
            if end > close_dt:
                end = close_dt
        elif interval == "1d":
            end = dt.datetime.combine(t.date(), MARKET_CLOSE, IST)
        else:
            out.append(b)
            continue
        if end <= now:
            out.append(b)
    return out


# --------------------------------------------------------------------------
# Indicators (plain lists, oldest first; None until there is enough data)
# --------------------------------------------------------------------------

def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(xs, n):
    out, k, e = [], 2.0 / (n + 1), None
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(xs[: n]) / n if i == n - 1 else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    """Wilder's average true range over bar dicts {h, l, c}."""
    out = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(bars) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def tick_round(price, tick=0.05, mode="nearest"):
    """NSE/BSE equities move in 5-paise ticks for most stocks. mode: nearest, down or up."""
    n = float(price) / tick
    if mode == "down":
        k = math.floor(n + 1e-9)
    elif mode == "up":
        k = math.ceil(n - 1e-9)
    else:
        k = round(n)
    return round(k * tick, 2)


# --------------------------------------------------------------------------
# Trade plans
# --------------------------------------------------------------------------

MODES = {
    # product, how long the trade may live, and what ends it
    "intraday": {"product": "intraday", "max_days": 0, "rr": 2.0},
    "swing": {"product": "delivery", "max_days": 10, "rr": 2.5},
    "positional": {"product": "delivery", "max_days": 60, "rr": 3.0},
}


def plan_date(t=None, cfg=None):
    """The session a new plan is for: today until 15:30 on a trading day, else the next one."""
    t = (t or now_ist()).astimezone(IST)
    d = t.date()
    if is_trading_day(d, cfg) and t.time() < MARKET_CLOSE:
        return d
    return next_trading_day(d, cfg)


def add_trading_days(d, n, cfg=None):
    for _ in range(n):
        d = next_trading_day(d, cfg)
    return d


def build_plan(symbol, entry, stop, mode="swing", target=None, rr=None, exchange=None,
               cfg=None, account=None, intraday_allowed=True, setup="", when=None):
    """A complete plan: entry, stop, target, exit time, quantity, charges, and every check.

    Quantity is the largest whole number of shares whose loss AT THE STOP,
    after all charges, fits the risk allowed right now, and whose cost fits
    the position cap and the cash. Nothing is placed.
    """
    cfg = cfg or load_config()
    account = account or account_state(cfg)
    exchange = exchange or cfg["exchange"]
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    product = MODES[mode]["product"]
    rr = rr or MODES[mode]["rr"]
    rates = broker_rates(cfg)
    entry = tick_round(float(entry), mode="up")
    stop = tick_round(float(stop), mode="down")
    blocks, warns = [], []
    p = {"symbol": symbol.upper(), "exchange": exchange, "mode": mode, "product": product,
         "setup": setup, "entry": entry, "stop": stop, "status": "blocked",
         "blocks": blocks, "warnings": warns}
    if stop >= entry:
        blocks.append("the stop (%.2f) must be below the entry (%.2f)" % (stop, entry))
        return p
    per_share = entry - stop
    target = tick_round(float(target), mode="down") if target else tick_round(entry + rr * per_share, mode="up")
    p["target"] = target
    if target <= entry:
        blocks.append("the target must be above the entry")
        return p
    if mode == "intraday" and not intraday_allowed:
        blocks.append("this stock is trade-to-trade (BE/BZ or BSE T/Z group): no intraday, delivery only")
    if account["halted"]:
        blocks.append(account["halt_reason"])
    risk_now = account["risk_per_trade_now"]
    cap_pct = cfg["intraday_max_position_pct"] if mode == "intraday" else cfg["max_position_pct"]
    cap_value = account["equity"] * cap_pct / 100.0
    cash = account["equity"]
    p["risk_allowed"] = risk_now
    caps = [(int(risk_now // per_share), "risk (Rs %s at the stop, charges included)" % money(risk_now)),
            (int(cap_value // entry), "position cap (%d%% of equity = Rs %s)" % (cap_pct, money(cap_value))),
            (int(cash // entry), "cash (Rs %s)" % money(cash))]
    q, limit = min(caps, key=lambda c: c[0])
    while q > 0 and -round_trip(entry, stop, q, product, exchange, rates)["net_pnl"] > risk_now:
        q -= 1
        limit = caps[0][1]
    p["qty"] = q
    p["qty_limited_by"] = limit
    if q < 1:
        if entry > cap_value:
            blocks.append("one share (Rs %s) is more than the %d%% position cap (Rs %s)"
                          % (money(entry), cap_pct, money(cap_value)))
        else:
            blocks.append("the stop is Rs %.2f a share away: even 1 share loses more than the Rs %s allowed"
                          % (per_share, money(risk_now)))
        return p
    at_stop = round_trip(entry, stop, q, product, exchange, rates)
    at_target = round_trip(entry, target, q, product, exchange, rates)
    loss, gain = -at_stop["net_pnl"], at_target["net_pnl"]
    p.update({
        "position_value": round(entry * q, 2),
        "loss_at_stop": round(loss, 2), "gain_at_target": round(gain, 2),
        "charges_at_stop": at_stop["charges"], "charges_at_target": at_target["charges"],
        "breakeven": at_target["breakeven"] if q else None,
        "reward_risk": round(gain / loss, 2) if loss > 0 else None,
        "risk_pct_of_equity": round(loss / account["equity"] * 100, 2),
        "stop_pct": round(per_share / entry * 100, 2),
    })
    # the lowest target that clears the minimum reward:risk after charges
    need = entry + cfg["min_reward_risk"] * loss / q
    for _ in range(4):
        rt = round_trip(entry, need, q, product, exchange, rates)
        need = entry + (cfg["min_reward_risk"] * loss + rt["charges"]) / q
    p["min_target"] = tick_round(need, mode="up")
    if p["reward_risk"] is not None and p["reward_risk"] < cfg["min_reward_risk"]:
        blocks.append("reward:risk after charges is %.2f, under your minimum %.2f: the target would need to be %.2f or higher"
                      % (p["reward_risk"], cfg["min_reward_risk"], p["min_target"]))
    if loss > 0 and at_stop["charges"] / loss * 100 > cfg["max_charges_pct_of_risk"]:
        blocks.append("charges (Rs %.2f) are %.0f%% of the risk, over your %d%% limit: the move is too small for the size"
                      % (at_stop["charges"], at_stop["charges"] / loss * 100, cfg["max_charges_pct_of_risk"]))
    day = plan_date(when, cfg)
    p["date"] = day.isoformat()
    if mode == "intraday":
        w0, w1 = cfg["intraday_entry_window"].split("-")
        p["entry_rule"] = ("Buy only if a 5-minute candle CLOSES above %.2f between %s and %s. "
                           "Place a stop-loss sell at %.2f and a target sell at %.2f right after the fill."
                           % (entry, w0, w1, stop, target))
        p["exit_rule"] = ("Out at the stop, the target, or %s, whichever comes first. Never hold an "
                          "intraday trade overnight." % cfg["intraday_flat_by"])
        p["exit_by"] = "%sT%s" % (day.isoformat(), cfg["intraday_flat_by"])
    else:
        days = MODES[mode]["max_days"]
        last = add_trading_days(day, days, cfg)
        p["entry_rule"] = ("Buy (delivery/CNC) only above %.2f: a stop-limit buy at %.2f, or buy if a day closes above it. "
                           "Cancel if not filled by %s." % (entry, entry, add_trading_days(day, 2, cfg).isoformat()))
        p["exit_rule"] = ("Sell at the stop %.2f or the target %.2f. If neither has hit by %s, sell at that day's close. "
                          % (stop, target, last.isoformat()) +
                          ("Once the price is 1R up (%.2f), move the stop to the entry." % (entry + per_share)))
        p["exit_by"] = last.isoformat()
        if mode == "positional":
            p["exit_rule"] += " After that, trail the stop under the 20-day average."
    if not calendar_known(day, cfg):
        warns.append("the %d holiday list is not built in: check the exit date against NSE's calendar" % day.year)
    if p["stop_pct"] > (3 if mode == "intraday" else 10 if mode == "swing" else 15):
        warns.append("a %.1f%% stop is wide for %s: the size is small, check the setup still makes sense"
                     % (p["stop_pct"], mode))
    p["status"] = "ok" if not blocks else "blocked"
    return p


# --------------------------------------------------------------------------
# Forecast ranges: what the stock's own history says is likely, never a promise
# --------------------------------------------------------------------------

def log_returns(closes):
    out = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _paths(returns, steps, n, block, rng):
    """n simulated paths of `steps` returns, drawn in blocks of consecutive real returns."""
    m = len(returns)
    block = max(1, min(block, m))
    for _ in range(n):
        path = []
        while len(path) < steps:
            i = rng.randrange(0, m - block + 1)
            path.extend(returns[i:i + block])
        yield path[:steps]


def _quantile(sorted_xs, q):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(sorted_xs) - 1)
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def price_ranges(closes, horizons=(5, 21, 63), n=4000, block=5, lookback=500, seed=7):
    """Bootstrapped price ranges for each horizon (trading days).

    Re-plays the stock's own daily moves from the last `lookback` days in random
    5-day blocks (keeping streaks and volatility clusters). Returns percentiles
    10/25/50/75/90 and the share of paths that end above today's price.
    """
    import random
    rets = log_returns(closes[-(lookback + 1):])
    if len(rets) < 60:
        return None
    rng = random.Random(seed)
    last = closes[-1]
    longest = max(horizons)
    finals = {h: [] for h in horizons}
    for path in _paths(rets, longest, n, block, rng):
        s = 0.0
        for i, r in enumerate(path, 1):
            s += r
            if i in finals:
                finals[i].append(last * math.exp(s))
    out = {}
    for h in horizons:
        xs = sorted(finals[h])
        out[h] = {"p10": round(_quantile(xs, .10), 2), "p25": round(_quantile(xs, .25), 2), "p50": round(_quantile(xs, .50), 2),
                  "p75": round(_quantile(xs, .75), 2), "p90": round(_quantile(xs, .90), 2),
                  "prob_up": round(sum(1 for x in xs if x > last) / len(xs) * 100, 1)}
    return {"last": last, "days_used": len(rets), "sims": n, "ranges": out}


def range_hit_rate(closes, horizon=21, every=10, checks=25, n=800, lookback=250, seed=11):
    """How honest are the ranges? Re-run them at past dates and count how often the
    real price `horizon` days later landed inside the 10-90% band (80% is perfect)."""
    import random
    rng = random.Random(seed)
    hits = total = 0
    end = len(closes) - 1 - horizon
    for k in range(checks):
        i = end - k * every
        if i - lookback < 1:
            break
        rets = log_returns(closes[i - lookback:i + 1])
        finals = sorted(closes[i] * math.exp(sum(p)) for p in _paths(rets, horizon, n, 5, rng))
        lo, hi = _quantile(finals, .10), _quantile(finals, .90)
        total += 1
        if lo <= closes[i + horizon] <= hi:
            hits += 1
    if not total:
        return None
    return {"horizon": horizon, "checks": total, "inside": hits, "hit_rate": round(hits / total * 100, 1)}


def path_odds(returns, start, stop, target, steps, n=4000, block=3, seed=19):
    """Replay `steps` moves n times: how often the target comes first, the stop first, or neither.

    Works both ways: a long idea (target above, stop below) or a short/put idea
    (target below, stop above). Uses closes only, so a stop or target touched
    by a wick inside a bar is not seen: the real odds of a touch are a little
    higher for both.
    """
    import random
    if len(returns) < 30 or steps < 1 or start <= 0 or stop <= 0 or target <= 0:
        return None
    up = target > start
    if up == (stop > start):
        return None                     # target and stop on the same side
    sign = 1.0 if up else -1.0          # a fall is a gain for a put
    rng = random.Random(seed)
    lt, ls = sign * math.log(target / start), sign * math.log(stop / start)
    t = s = 0
    finals = []
    for path in _paths(returns, steps, n, block, rng):
        acc, res = 0.0, None
        for r in path:
            acc += sign * r
            if acc >= lt:
                res = "t"
                break
            if acc <= ls:
                res = "s"
                break
        if res == "t":
            t += 1
        elif res == "s":
            s += 1
        else:
            finals.append(acc)
    neither = n - t - s
    risk = abs(start - stop)
    # R at the close for paths that hit neither: the move in our favour over the risk
    avg_r_open = (sum((math.exp(sign * x) - 1) * start * sign for x in finals) / len(finals) / risk) if finals else 0.0
    rr = abs(target - start) / risk
    exp_r = (t * rr - s * 1.0 + neither * avg_r_open) / n
    return {"target_first": round(t / n * 100, 1), "stop_first": round(s / n * 100, 1),
            "neither": round(neither / n * 100, 1), "expected_r": round(exp_r, 2), "sims": n,
            "direction": "up" if up else "down"}


# --------------------------------------------------------------------------
# Options: buy-only CE/PE plans (never option selling)
# --------------------------------------------------------------------------

# Groww equity options charges, from groww.in/pricing/futures-and-options, checked 2026-10-30.
GROWW_FO = {
    "as_of": "2026-10-30",
    "source": "https://groww.in/pricing/futures-and-options",
    "brokerage_per_order": 20.0,
    "stt_sell_pct": 0.15,                         # on premium, sell side only
    "stamp_buy_pct": 0.003,
    "exchange_txn_pct": {"NSE": 0.03503, "BSE": 0.0325},
    "sebi_pct": 0.0001,
    "ipft_pct": 0.0005,
    "gst_pct": 18.0,
}

# Index options. Lot sizes from January 2026 (NSE revises them; check before trading).
INDEX_OPTIONS = {
    "NIFTY": {"name": "Nifty 50 index", "yahoo": "^NSEI", "lot": 65, "step": 50, "weekly": True},
    "BANKNIFTY": {"name": "Nifty Bank index", "yahoo": "^NSEBANK", "lot": 30, "step": 100, "weekly": False},
}
FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
RISK_FREE = 0.065


def option_order_charges(premium, qty, side, exchange="NSE", rates=None):
    r = rates or GROWW_FO
    v = float(premium) * int(qty)
    brokerage = r["brokerage_per_order"] if v > 0 else 0.0
    stt = v * r["stt_sell_pct"] / 100 if side == "sell" else 0.0
    stamp = v * r["stamp_buy_pct"] / 100 if side == "buy" else 0.0
    exch = v * r["exchange_txn_pct"][exchange] / 100
    sebi = v * r["sebi_pct"] / 100
    ipft = v * r["ipft_pct"] / 100
    gst = (brokerage + exch + sebi + ipft) * r["gst_pct"] / 100
    parts = {"brokerage": brokerage, "stt": stt, "stamp_duty": stamp, "exchange_txn": exch, "sebi": sebi,
             "ipft": ipft, "gst": gst}
    parts = {k: round(x, 2) for k, x in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def option_round_trip(buy, sell, qty, exchange="NSE"):
    b = option_order_charges(buy, qty, "buy", exchange)["total"]
    s = option_order_charges(sell, qty, "sell", exchange)["total"]
    gross = (float(sell) - float(buy)) * int(qty)
    return {"gross": round(gross, 2), "charges": round(b + s, 2), "net": round(gross - b - s, 2)}


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sigma, kind="CE", r=RISK_FREE):
    """Black-Scholes price of a European option. T in years."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_delta(S, K, T, sigma, kind="CE", r=RISK_FREE):
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if kind == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if kind == "CE" else _ncdf(d1) - 1


def hist_vol(closes, n=20):
    """Annualised volatility of the last n daily moves."""
    rets = log_returns(closes[-(n + 1):])
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def strike_step(price, symbol=""):
    """The gap between strikes. Exact for the indices; an approximation for stocks (check in your broker)."""
    if symbol.upper() in INDEX_OPTIONS:
        return INDEX_OPTIONS[symbol.upper()]["step"]
    for limit, step in ((100, 1), (250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50), (10000, 100)):
        if price < limit:
            return step
    return 250


def nearest_strike(price, step):
    return round(round(price / step) * step, 2)


def last_tuesday(year, month):
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    while d.weekday() != 1:
        d -= dt.timedelta(days=1)
    return d


def _on_trading_day(d, cfg=None):
    """Expiry moves to the previous trading day when it falls on a holiday."""
    while not is_trading_day(d, cfg):
        d -= dt.timedelta(days=1)
    return d


def expiries(symbol, today=None, count=3, cfg=None):
    """Upcoming NSE expiries: Nifty weekly on Tuesdays; stocks and Bank Nifty on the month's last Tuesday."""
    today = today or now_ist().date()
    out = []
    if INDEX_OPTIONS.get(symbol.upper(), {}).get("weekly"):
        d = today
        while len(out) < count:
            if d.weekday() == 1:
                e = _on_trading_day(d, cfg)
                if e >= today and e not in out:
                    out.append(e)
            d += dt.timedelta(days=1)
        return out
    y, m = today.year, today.month
    while len(out) < count:
        e = _on_trading_day(last_tuesday(y, m), cfg)
        if e >= today:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def parse_fo_lots(text):
    """NSE fo_mktlots.csv: UNDERLYING, SYMBOL, then one lot-size column per expiry month."""
    out = {}
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if len(row) < 3:
            continue
        sym = row[1].strip().upper()
        if not sym or sym == "SYMBOL":
            continue
        for cell in row[2:]:
            cell = cell.strip()
            if cell.isdigit() and int(cell) > 0:
                out[sym] = int(cell)
                break
    if not out:
        raise ValueError("no lot sizes found: is this NSE's fo_mktlots.csv?")
    return out


def load_fo_lots(refresh=False, fetch=None, from_file=None, snapshot=None):
    """Lot sizes of every F&O stock and index, cached 7 days. Falls back to a saved copy."""
    cache = path("universe", "fo_lots.json")
    if from_file:
        with open(from_file, encoding="utf-8-sig") as fh:
            lots = parse_fo_lots(fh.read())
        write_json(cache, {"fetched": now_ist().isoformat(timespec="minutes"), "lots": lots})
        return lots
    cached = read_json(cache)
    if cached and not refresh and (now_ist() - dt.datetime.fromisoformat(cached["fetched"])).days < 7:
        return cached["lots"]
    try:
        lots = parse_fo_lots((fetch or http_get)(FO_LOTS_URL))
        write_json(cache, {"fetched": now_ist().isoformat(timespec="minutes"), "lots": lots})
        return lots
    except Exception as e:  # blocked, changed format, anything: fall back
        for src in (cached, read_json(snapshot) if snapshot else None):
            if src and src.get("lots"):
                return src["lots"]
        sys.stderr.write("warning: no F&O lot sizes (%s); only Nifty and Bank Nifty are known\n" % e)
        return {k: v["lot"] for k, v in INDEX_OPTIONS.items()}


def lot_size(symbol, lots=None):
    symbol = symbol.upper()
    if lots and symbol in lots:
        return lots[symbol]
    if symbol in INDEX_OPTIONS:
        return INDEX_OPTIONS[symbol]["lot"]
    return None


def nse_option_chain(symbol, fetch_opener=None):
    """Best effort: NSE's own option chain (live premiums and IVs).

    NSE blocks most scripts and cloud servers, so this often fails; callers fall
    back to estimated premiums. Returns {"spot", "rows": [{expiry, strike, CE, PE}]} or None.
    """
    import http.cookiejar
    symbol = symbol.upper()
    is_index = symbol in INDEX_OPTIONS
    opener = fetch_opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9",
         "Referer": "https://www.nseindia.com/option-chain"}
    urls = ["https://www.nseindia.com/api/option-chain-%s?symbol=%s" % ("indices" if is_index else "equities",
                                                                         urllib.parse.quote(symbol))]
    try:
        opener.open(urllib.request.Request("https://www.nseindia.com/option-chain", headers=h), timeout=10).read()
    except Exception:
        return None
    for url in urls:
        try:
            raw = opener.open(urllib.request.Request(url, headers=h), timeout=12).read().decode("utf-8", "replace")
            return parse_option_chain(raw)
        except Exception:
            continue
    return None


def parse_option_chain(text):
    data = json.loads(text)
    rec = data.get("records") or data.get("filtered") or {}
    rows = []
    for r in rec.get("data") or []:
        try:
            exp = dt.datetime.strptime(r.get("expiryDate") or r.get("expiryDates"), "%d-%b-%Y").date()
        except (TypeError, ValueError):
            continue
        row = {"expiry": exp.isoformat(), "strike": float(r["strikePrice"])}
        for k in ("CE", "PE"):
            o = r.get(k)
            if o:
                row[k] = {"ltp": o.get("lastPrice"), "iv": o.get("impliedVolatility"), "oi": o.get("openInterest"),
                          "bid": o.get("bidprice") or o.get("buyPrice1"), "ask": o.get("askPrice") or o.get("sellPrice1"),
                          "volume": o.get("totalTradedVolume")}
        rows.append(row)
    if not rows:
        raise ValueError("empty option chain")
    return {"spot": rec.get("underlyingValue"), "rows": rows}


def option_plan(symbol, spot, closes, kind, u_entry, u_stop, u_target=None, strike_pref="ATM", expiry=None,
                lot=None, chain=None, cfg=None, account=None, exchange="NSE", when=None):
    """A buy-only option plan (CE when you expect a rise, PE for a fall) sized to your risk.

    The stock or index levels decide the trade (buy the CE when price CLOSES
    above u_entry; stop when it goes back to u_stop). The option's premium at
    those levels is worked out from the live chain when NSE answers, otherwise
    estimated with Black-Scholes from the stock's own recent volatility.
    """
    cfg = cfg or load_config()
    account = account or account_state(cfg)
    when = (when or now_ist()).astimezone(IST)
    kind = kind.upper()
    blocks, warns = [], []
    p = {"symbol": symbol.upper(), "kind": kind, "status": "blocked", "blocks": blocks, "warnings": warns,
         "u_entry": round(u_entry, 2), "u_stop": round(u_stop, 2)}
    if kind not in ("CE", "PE"):
        raise ValueError("kind must be CE or PE")
    if lot is None:
        blocks.append("%s has no options on NSE (it isn't in the F&O list)" % symbol.upper())
        return p
    long_ = kind == "CE"
    if long_ and not u_stop < u_entry:
        blocks.append("for a CE the stop (%.2f) must be below the trigger (%.2f)" % (u_stop, u_entry))
        return p
    if not long_ and not u_stop > u_entry:
        blocks.append("for a PE the stop (%.2f) must be above the trigger (%.2f)" % (u_stop, u_entry))
        return p
    risk_u = abs(u_entry - u_stop)
    if u_target is None:
        u_target = u_entry + 2 * risk_u if long_ else u_entry - 2 * risk_u
    p["u_target"] = round(u_target, 2)
    if (u_target - u_entry) * (1 if long_ else -1) <= 0:
        blocks.append("the target must be beyond the trigger in the trade's direction")
        return p
    exps = expiries(symbol, when.date(), 3, cfg)
    if expiry is None:
        # skip today's expiry for a fresh intraday buy: the premium can melt within minutes
        expiry = next((e for e in exps if e > when.date()), exps[0])
    p["expiry"] = expiry.isoformat()
    p["expiries"] = [e.isoformat() for e in exps]
    step = strike_step(spot, symbol)
    atm = nearest_strike(spot, step)
    shift = {"ATM": 0, "ITM1": -1, "OTM1": 1}.get(strike_pref, 0) * step * (1 if long_ else -1)
    K = round(atm + shift, 2)
    p.update({"strike": K, "strike_step": step, "strike_pref": strike_pref, "lot": lot})
    close_dt = dt.datetime.combine(expiry, MARKET_CLOSE, IST)
    T = max((close_dt - when).total_seconds() / (365 * 24 * 3600), 0.5 / 365)
    hv = max(x for x in (hist_vol(closes, 20), hist_vol(closes, 60), 0.12) if x is not None)
    source, sigma, ltp = "estimate", hv, None
    if chain:
        row = next((r for r in chain.get("rows", []) if r["expiry"] == expiry.isoformat() and abs(r["strike"] - K) < 1e-6), None)
        o = row and row.get(kind)
        if o and o.get("ltp"):
            ltp = float(o["ltp"])
            if o.get("iv"):
                sigma = float(o["iv"]) / 100
            source = "live"
    S0 = float(chain.get("spot") or spot) if (chain and source == "live") else spot
    model = lambda S: bs_price(S, K, T, sigma, kind)
    base = model(S0)
    adj = (ltp - base) if ltp is not None else 0.0          # keep the live premium's level, move it with the model
    prem = lambda S: max(0.05, model(S) + adj)
    pe, ps, pt = prem(u_entry), prem(u_stop), prem(u_target)
    pe, ps, pt = tick_round(pe, mode="up"), tick_round(ps, mode="down"), tick_round(pt, mode="down")
    delta = bs_delta(u_entry, K, T, sigma, kind)
    p.update({"premium_now": round(ltp if ltp is not None else base, 2), "premium_source": source,
              "iv_used": round(sigma * 100, 1), "hv20": round((hist_vol(closes, 20) or 0) * 100, 1),
              "premium_entry": pe, "premium_stop": ps, "premium_target": pt, "delta": round(delta, 2),
              "days_to_expiry": (expiry - when.date()).days,
              "contract": "%s %s %s %s" % (symbol.upper(), expiry.strftime("%d %b").lstrip("0"),
                                           ("%g" % K), kind)})
    if account["halted"]:
        blocks.append(account["halt_reason"])
    risk_now = account["risk_per_trade_now"]
    one = option_round_trip(pe, ps, lot, exchange)
    loss_lot = -one["net"]
    cost_lot = pe * lot
    lots_risk = int(risk_now // loss_lot) if loss_lot > 0 else 0
    lots_cash = int(account["equity"] // (cost_lot + 60))
    lots = min(lots_risk, lots_cash)
    p.update({"cost_per_lot": round(cost_lot, 2), "loss_per_lot": round(loss_lot, 2), "lots": lots,
              "risk_allowed": risk_now})
    if lots < 1:
        if lots_cash < 1:
            blocks.append("one lot costs Rs %s (%d x %.2f), more than your Rs %s" % (
                money(cost_lot), lot, pe, money(account["equity"])))
        if lots_risk < 1:
            blocks.append("one lot loses Rs %s if the stop hits (premium %.2f -> %.2f on %d units, with charges): "
                          "more than the Rs %s you allow per trade" % (money(loss_lot), pe, ps, lot, money(risk_now)))
        lots_shown = 1
    else:
        lots_shown = lots
    qty = lots_shown * lot
    at_stop = option_round_trip(pe, ps, qty, exchange)
    at_tgt = option_round_trip(pe, pt, qty, exchange)
    p.update({"qty": qty, "lots_shown": lots_shown, "cost": round(pe * qty, 2),
              "loss_at_stop": round(-at_stop["net"], 2), "gain_at_target": round(at_tgt["net"], 2),
              "charges_round_trip": at_tgt["charges"],
              "reward_risk": round(at_tgt["net"] / -at_stop["net"], 2) if at_stop["net"] < 0 else None,
              "breakeven_at_expiry": round(K + pe if long_ else K - pe, 2)})
    if p["reward_risk"] is not None and p["reward_risk"] < cfg["min_reward_risk"]:
        blocks.append("reward:risk after charges is %.2f, under your minimum %.2f" % (p["reward_risk"], cfg["min_reward_risk"]))
    if -at_stop["net"] > 0 and at_stop["charges"] / -at_stop["net"] * 100 > cfg["max_charges_pct_of_risk"]:
        blocks.append("charges (Rs %.2f) are %.0f%% of the risk, over your %d%% limit"
                      % (at_stop["charges"], at_stop["charges"] / -at_stop["net"] * 100, cfg["max_charges_pct_of_risk"]))
    if source == "estimate":
        warns.append("premiums are estimates (NSE's live option chain didn't answer): check the real price in Groww "
                     "before buying; real premiums are often higher than this estimate")
    if p["days_to_expiry"] <= 1:
        warns.append("expiry is %s: premiums can fall very fast this close to expiry"
                     % ("today" if p["days_to_expiry"] == 0 else "tomorrow"))
    w0, w1 = cfg["intraday_entry_window"].split("-")
    cmp_word = "above" if long_ else "below"
    p["entry_rule"] = ("Buy %s only after a 5-minute candle of %s CLOSES %s %.2f, between %s and %s. "
                       "Expect to pay about %.2f a unit (%d lot%s, %d units, Rs %s)."
                       % (p["contract"], symbol.upper(), cmp_word, u_entry, w0, w1, pe, lots_shown,
                          "" if lots_shown == 1 else "s", qty, money(p["cost"])))
    p["exit_rule"] = ("Sell when %s goes back to %.2f (premium near %.2f) or reaches %.2f (premium near %.2f), "
                      "or at %s at the latest. Set a stop-loss sell on the option right after buying."
                      % (symbol.upper(), u_stop, ps, u_target, pt, cfg["intraday_flat_by"]))
    p["exit_by"] = "%sT%s" % (when.date().isoformat(), cfg["intraday_flat_by"])
    p["status"] = "ok" if not blocks else "blocked"
    return p


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_status(a):
    cfg = load_config()
    s = market_status(cfg=cfg)
    print("%s  %s (%s)" % (s["now"], s["state"].upper(), s["reason"]))
    if "next_open" in s:
        print("next open: " + s["next_open"])
    if "closes" in s:
        print("closes:    " + s["closes"])
    if "muhurat" in s:
        print("Muhurat trading today: " + s["muhurat"])
    if s.get("warning"):
        print("warning: " + s["warning"])


def cmd_calendar(a):
    cfg = load_config()
    year = a.year or now_ist().year
    hs = sorted((d, n) for d, n in holidays(cfg).items() if d.startswith(str(year)))
    if not hs:
        print("No holidays built in for %d. Add them to trader/config.json -> extra_holidays." % year)
        return
    for d, n in hs:
        print("%s  %s  %s" % (d, dt.date.fromisoformat(d).strftime("%a"), n))
    for d, t in MUHURAT.items():
        if d.startswith(str(year)):
            print("%s  %s  Muhurat trading (%s)" % (d, dt.date.fromisoformat(d).strftime("%a"),
                                                   t or "timings not announced yet"))


def cmd_config(a):
    cfg = load_config()
    for kv in a.set or []:
        if "=" not in kv:
            raise SystemExit("--set wants key=value, got %r" % kv)
        k, v = kv.split("=", 1)
        if k not in DEFAULT_CONFIG:
            raise SystemExit("unknown setting %r. Settings: %s" % (k, ", ".join(DEFAULT_CONFIG)))
        cfg[k] = coerce(k, v)
    errs = validate_config(cfg)
    if a.set:
        if errs:
            raise SystemExit("not saved:\n  " + "\n  ".join(errs))
        save_config(cfg)
        print("saved " + path("config.json"))
    _print({k: v for k, v in cfg.items() if not k.startswith("_")})
    for e in errs:
        print("problem: " + e)


def cmd_account(a):
    st = account_state()
    print("Equity        Rs %s  (started Rs %s, floor Rs %s)" % (money(st["equity"]), money(st["capital"]), money(st["floor"])))
    print("Loss limit    Rs %s used of Rs %s; open risk Rs %s; room left Rs %s"
          % (money(st["loss_used"]), money(st["loss_limit"]), money(st["open_risk"]), money(st["room_left"])))
    print("Next trade    may risk Rs %s" % money(st["risk_per_trade_now"]))
    print("Today         %d trades, %d positions open" % (st["trades_today"], st["open_positions"]))
    if st["halted"]:
        print("STOP: " + st["halt_reason"])


def cmd_charges(a):
    cfg = load_config()
    rt = round_trip(a.buy, a.sell, a.qty, a.product, a.exchange or cfg["exchange"], broker_rates(cfg))
    print("%d shares, buy %.2f, sell %.2f, %s on %s (%s rates as of %s)"
          % (a.qty, a.buy, a.sell, a.product, a.exchange or cfg["exchange"], cfg["broker"], broker_rates(cfg)["as_of"]))
    for side in ("buy", "sell"):
        c = rt[side]
        print("  %-4s turnover %10s  charges %8s  (brokerage %s, STT %s, stamp %s, exch %s, SEBI %s, IPFT %s, DP %s, GST %s)"
              % (side, money(c["turnover"]), money(c["total"]), c["brokerage"], c["stt"], c["stamp_duty"],
                 c["exchange_txn"], c["sebi"], c["ipft"], c["dp"], c["gst"]))
    print("Gross Rs %s, charges Rs %s, net Rs %s. Breakeven sell price %.2f"
          % (money(rt["gross_pnl"]), money(rt["charges"]), money(rt["net_pnl"]), rt["breakeven"]))


def cmd_universe(a):
    items = load_universe(a.name, refresh=a.refresh, from_file=a.file)
    by_ex = {}
    for i in items:
        by_ex[i["exchange"]] = by_ex.get(i["exchange"], 0) + 1
    print("%s: %d stocks (%s)" % (a.name, len(items), ", ".join("%s %d" % kv for kv in sorted(by_ex.items()))))
    for i in items[: a.show]:
        print("  %-14s %-6s %-4s %s" % (i["symbol"], i["exchange"], i["series"], i["name"][:50]))
    if len(items) > a.show:
        print("  ... (%d more; full list in trader/universe/)" % (len(items) - a.show))


def cmd_bars(a):
    cfg = load_config()
    r = fetch_bars(a.symbol, a.exchange or cfg["exchange"], a.interval, a.range)
    bars = r["bars"] if a.partial else completed_bars(r["bars"], a.interval)
    r["bars"] = bars
    print("%s %s, %d bars of %s, last price %s, previous close %s"
          % (r["meta"]["symbol"], r["meta"]["exchange"], len(bars), a.interval, r["meta"]["price"], r["meta"]["prev_close"]))
    for b in bars[-a.show:]:
        print("  %s  O %.2f  H %.2f  L %.2f  C %.2f  V %d" % (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))
    if a.save:
        write_json(path("bars", "%s-%s-%s.json" % (r["meta"]["symbol"], a.interval, a.range)), r)


def main(argv=None):
    p = argparse.ArgumentParser(prog="indtrader", description="India Trader core: calendar, rules, charges, universes, prices")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("calendar"); s.add_argument("--year", type=int); s.set_defaults(fn=cmd_calendar)
    s = sub.add_parser("config"); s.add_argument("--set", nargs="*"); s.set_defaults(fn=cmd_config)
    sub.add_parser("account").set_defaults(fn=cmd_account)
    s = sub.add_parser("charges")
    s.add_argument("--buy", type=float, required=True); s.add_argument("--sell", type=float, required=True)
    s.add_argument("--qty", type=int, required=True)
    s.add_argument("--product", choices=["intraday", "delivery"], default="intraday")
    s.add_argument("--exchange", choices=["NSE", "BSE"]); s.set_defaults(fn=cmd_charges)
    s = sub.add_parser("universe"); s.add_argument("name"); s.add_argument("--refresh", action="store_true")
    s.add_argument("--file"); s.add_argument("--show", type=int, default=10); s.set_defaults(fn=cmd_universe)
    s = sub.add_parser("bars"); s.add_argument("symbol"); s.add_argument("--exchange", choices=["NSE", "BSE"])
    s.add_argument("--interval", default="5m"); s.add_argument("--range", default="5d")
    s.add_argument("--show", type=int, default=10); s.add_argument("--save", action="store_true")
    s.add_argument("--partial", action="store_true", help="also show the candle that is still forming")
    s.set_defaults(fn=cmd_bars)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except FetchError as e:
        raise SystemExit("could not fetch: %s" % e)


if __name__ == "__main__":
    main()
