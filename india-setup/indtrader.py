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
    "max_position_pct": 50,        # no single position over this % of equity
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
            % (host, urllib.parse.quote(ysym, safe="."), rng, interval))


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
    ysym = symbol if symbol.upper().endswith((".NS", ".BO")) else yahoo_symbol(symbol, exchange)
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
