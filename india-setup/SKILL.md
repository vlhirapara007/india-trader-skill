---
name: india-setup
description: >-
  India Trader setup and basics. Sets your rules (capital, the overall loss
  limit, risk per trade, trades per day), says if the NSE/BSE market is open
  in IST with the 2026 holidays and Muhurat trading, works out Groww's full
  charges (brokerage, STT, exchange, SEBI, IPFT, stamp duty, DP, GST) and the
  breakeven price for any trade, loads stock universes (Nifty 50, Nifty 200,
  Bank Nifty, all NSE, all BSE, all of India) and fetches free NSE/BSE price
  bars. Shows equity, how much of the loss limit is used and what the next
  trade may risk. Use when the user says "set up india trader", "set my
  rules", "is the market open", "NSE holiday", "what are the charges",
  "breakeven", "how much can I risk", "my account", "load nifty 200", or
  before any other india-* skill on first use.
---

# India Setup

The base of the India Trader pack. Every other `india-*` skill reads the files
this one writes. **Nothing in this pack places orders.** Each skill hands the
user a complete plan to place themselves in their broker's app.

All files go in `trader/` in the current folder (or `$TRADER_DIR`). The tool is
`indtrader.py`, which sits next to this file. Run it with `python3` (3.8+, no
installs needed).

## First run

1. Show the user the default rules and confirm them or change them:
   `python3 indtrader.py config`
   - capital Rs 20,000. max_drawdown Rs 2,000 (10%). When losses reach this,
     every skill refuses new trades (floor Rs 18,000).
   - risk_per_trade Rs 200 (1%): the loss if the stop fills, **charges included**.
   - max_trades_per_day 3, max_open_positions 4, max_position_pct 50.
   - min_reward_risk 1.5. max_charges_pct_of_risk 25: a trade whose round-trip
     charges are more than a quarter of its risk is skipped.
   - broker groww, exchange NSE, universe nifty200.
   - intraday_entry_window 09:30-11:00, intraday_flat_by 15:10 (square off
     yourself before the broker's auto square-off and its Rs 50 fee).
   Change one: `python3 indtrader.py config --set risk_per_trade=150 universe=nifty50`
2. Check that data works from their machine:
   `python3 indtrader.py status` and `python3 indtrader.py bars RELIANCE --interval 5m --range 1d`.

## Commands

| Ask | Run |
|---|---|
| Is the market open? | `indtrader.py status` |
| Holidays | `indtrader.py calendar --year 2026` |
| My account / how much can I risk | `indtrader.py account` |
| Charges and breakeven | `indtrader.py charges --buy 250 --sell 255 --qty 40 --product intraday` (`--product delivery` for swing, positional and long-term) |
| Load a universe | `indtrader.py universe nifty200` (`nifty50`, `banknifty`, `nse_all`, `bse_all`, `all`, or `file:mylist.txt`) |
| Price bars | `indtrader.py bars TCS --interval 1d --range 1y` (`--exchange BSE` for .BO) |

Universes are cached for 7 days in `trader/universe/`. If NSE, BSE or
niftyindices blocks the script, the error says which URL to open in a browser.
Save the file, then load it with `indtrader.py universe <name> --file <saved file>`.

## The journal: trader/journal.json

The loss limit is only as good as the journal. Whenever the user takes a trade
or closes one, add or update its entry (the later skills do this for them):

```json
[{"id": "2026-11-02-RELIANCE-1", "symbol": "RELIANCE", "exchange": "NSE",
  "mode": "intraday", "entry_date": "2026-11-02", "qty": 6,
  "entry": 1490.40, "stop": 1460.00, "target": 1536.00,
  "risk": 196.50, "status": "open"},
 {"id": "2026-10-28-TCS-1", "status": "closed", "net_pnl": -184.20, "...": "..."}]
```

`risk` is the most an open trade can still lose, charges included. `net_pnl`
is after all charges. `account` counts open risk against the limit, so open
trades can never add up to more than the room left.

## How to talk about results

- Money is in rupees with Indian grouping (Rs 1,23,456.50). Times are IST.
- Always give the net figure after charges, and the breakeven price.
- If `account` says STOP, say so first and plainly. Don't offer to work around it.
- Charge rates are Groww's, checked 2026-10-30 (`GROWW["as_of"]` in
  indtrader.py). If the user says Groww changed its prices, update the table
  and its `as_of` date.
- The 2027 holiday list isn't built in yet. In 2027, `status` warns and treats
  weekdays as open. Add the holidays under `extra_holidays` in config.json
  once NSE publishes them.
