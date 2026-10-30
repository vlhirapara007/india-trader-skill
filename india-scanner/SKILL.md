---
name: india-scanner
description: >-
  India Chart Scanner. Scans Nifty 50, Nifty 200, Bank Nifty, all NSE or all
  BSE stocks for setups and writes the watchlist. Daily scan (after 3:30 PM or
  before 9:15 AM) for swing and positional trades: 20-day and 52-week
  breakouts on volume, bull flags, pullbacks to the 20-day average, 20-day
  reclaims, stage-2 trends, oversold and oversold bounces. Pre-market scan
  for intraday candidates (NR7 and inside days in liquid, moving stocks). Live
  opening-range breakout scan from 9:30 AM on completed 5-minute candles,
  saying triggered, waiting, failed or expired. Skips illiquid, penny and
  trade-to-trade stocks and anything too expensive for the account. Never
  places orders. Use when the user says "scan the market", "find setups",
  "what's breaking out", "swing trades for tomorrow", "positional ideas",
  "intraday stocks for today", "opening range breakout", "bull flags",
  "oversold stocks", "scan nifty 200", "scan all NSE", or at the start or end
  of a trading day.
---

# India Scanner

Finds setups. It does not decide the trade: every hit gives a **trigger**
(buy only above it) and a **stop_ref** (the price where the idea is wrong).
The planning skill turns those into the full plan with quantity and charges.

Tool: `scan.py`, next to this file (with its copy of `indtrader.py`). Run it
from the user's project folder, because everything goes into `trader/`. If
`trader/config.json` doesn't exist yet, run india-setup first, or carry on
with the defaults (₹20,000 capital, ₹200 risk per trade) and say so.

## Which scan, when (IST)

| Time | Scan | Command |
|---|---|---|
| After 15:30, or the evening before | Swing + positional for the next session | `python3 scan.py daily` |
| Weekend | Positional only | `python3 scan.py daily --mode positional` |
| 08:30-09:15 | Intraday candidates for today | `python3 scan.py premarket` |
| 09:31 onward, until the entry window closes (11:00) | Live opening-range breakouts | `python3 scan.py orb` or `python3 scan.py orb --watchlist` |

Options: `--universe nifty50|nifty200|banknifty|nse_all|bse_all|all|file:list.txt`
(default from config: nifty200), `--top 25`, `--setups breakout_20d,bull_flag`,
`--minutes 5|15|30` for the ORB range.

Big universes take time. A first `nse_all` scan downloads 2 years of daily
candles for about 2,000 stocks, which takes several minutes. Later runs the same day
use `trader/cache/`. For `orb`, prefer `--watchlist` (only the premarket and daily
hits) over a large universe, because it re-downloads 5-minute bars on every run. Run
scans in the background when the universe is large and tell the user it's running.

## Setups

Swing (2-10 days, delivery):
- **breakout_20d**: closed at a 20-day closing high on at least 1.5× average volume.
- **bull_flag**: a run of 10% or more, then a 3-8 day pullback that holds the top half on lighter volume. The trigger is the flag high.
- **pullback_20sma**: in an uptrend (price above the 50-day, 50 above 200), dipped to the 20-day average and closed green. Also counts as positional.
- **reclaim_20sma**: closed back above the 20-day average.
- **oversold**: RSI below 30 while still above the 200-day average. Only a watch: buy only above the trigger.
- **oversold_bounce**: RSI was below 30, and today closed above yesterday's high.

Positional (weeks to months, delivery):
- **breakout_52w**: closed above the 52-week high on at least 1.5× volume.
- **stage2_trend**: price above a rising 50 > 150 > 200-day average stack, at least 30% off the 52-week low and within 25% of the high. The trigger is the 10-day high.

Intraday (`premarket`):
- **nr7**: yesterday was the narrowest range of 7 days. **inside_day**: yesterday's range sat inside the day before's.
- Only stocks with ATR of at least 1.5% (enough movement to beat the charges), twice the normal liquidity bar, and no trade-to-trade series.

Opening-range breakout (`orb`): the range is 09:15 to 09:30 (15 min). The trigger
is the first **completed** 5-minute candle that closes above the range high inside
the entry window (09:30-11:00 by default). A candle that only pokes above the
high doesn't count. If a candle closes below the range low first, the setup has
failed. The stop reference is the middle of the range.

## Filters (from trader/config.json)

- `min_price` ₹20, `min_avg_traded_value_cr` ₹5 crore a day (20-day average).
- Stocks priced above `max_position_pct` of equity (₹10,000 at the start) are
  skipped, because one share would be too big a slice of a ₹20,000 account.
- Only completed candles are used. The bar still forming, and today's unfinished
  daily candle, are ignored.

## Reporting to the user

- Lead with the count and the best few hits, each in one line: symbol, setup in plain words,
  trigger, stop_ref, and the risk % between them. A risk % over about 8% for
  swing, or 3% for intraday, won't fit a ₹200 risk sensibly. Say so.
- For `orb`, list `triggered` first with the candle time and price, then
  `waiting` with the distance. Never call a `waiting` stock an entry.
- Say what the scan is based on ("on the 29 Oct close", "as of 09:47 IST"). The
  data is free Yahoo data and can lag the exchange by a few minutes.
- If `trader/account` is halted (the loss limit is reached), say that first: there are no new trades.
- End by offering to plan the best one or two (entry, stop, target, quantity,
  charges), rather than reprinting the whole table.
- These are setups, not recommendations, and many of them fail. Keep that
  framing without lecturing the user.

Results: `trader/scans/<scan>-<date>.json`, and the latest of each scan in `trader/watchlist.json`.
