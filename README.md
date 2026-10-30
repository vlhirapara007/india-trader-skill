# India Trader skill

Claude skills for trading Indian stocks on NSE and BSE: intraday, swing,
positional and long-term, plus a tracker for the portfolio you hold.

**Nothing in this pack places an order.** Every skill gives you a complete
plan: stock, date, entry rule and price, stop loss, target, exit time, quantity,
charges and odds. You place the orders yourself in your broker's app.

Plain Python 3.8+ using only the standard library: nothing to `pip install`
and no API keys. Data comes from free public sources: NSE, BSE, niftyindices
and Yahoo Finance (`.NS` / `.BO`).

## Your rules (defaults)

| Rule | Default |
|---|---|
| Capital | Rs 20,000 |
| Loss limit | Rs 2,000 (10%). Once losses reach it, every skill refuses new trades |
| Risk per trade | Rs 200 (1%), charges included |
| Trades per day / open positions | 3 / 4 |
| Broker charges | Groww (brokerage, STT, exchange, SEBI, IPFT, stamp duty, DP, GST) |
| Universe | Nifty 200 (also Nifty 50, Bank Nifty, all NSE, all BSE, all) |

Change any of them: `python3 india-setup/indtrader.py config --set risk_per_trade=150`

## Skills

| Skill | Status | What it does |
|---|---|---|
| `india-setup` | ready | Rules, IST market hours and holidays, Groww charges and breakeven, universes, price bars, account and loss-limit tracking |
| `india-scanner` | ready | Scans a universe for intraday (opening-range breakout) and swing/positional setups (breakouts, bull flags, pullbacks, oversold bounces) |
| `india-plan` | next | Turns a setup into a full trade plan: entry, stop, target, exit time, quantity after charges (the logic is in the core and already used by the web app) |
| `india-timemachine` | planned | Replays the plan 10,000 times from the stock's own bars and on every past day the setup fired |
| `india-risk` | planned | PASS / BLOCK against your rules before you place anything |
| `india-news` | planned | NSE/BSE corporate announcements and headlines for your watchlist |
| `india-results` | planned | Quarterly results analyst: revenue, margins, profit, EPS, YoY, for long-term picks |
| `india-insider` | planned | SEBI insider-trading disclosures, bulk and block deals, promoter pledges |
| `india-portfolio` | planned | Holdings, P&L, stop hits, results dates, the trade journal |

## Web app (phone and desktop)

`app/streamlit_app.py` is a mobile-friendly website built on the same code:

- **Stock**: search any NSE/BSE stock to see its chart with 20/50/200-day averages, trend, RSI, 52-week position, which setups fire, a full trade plan for your rules (entry, stop, target, exit time, quantity, Groww charges, breakeven), **price ranges** for 1 week, 1 month and 3 months with an honesty check, and the **odds** of target-first vs stop-first.
- **Scan**: swing, positional, pre-market and live opening-range scans over Nifty 50 / 200, Bank Nifty, all NSE, all BSE.
- **Market**: Nifty 50 and Bank Nifty today, Nifty 50 movers, upcoming holidays.
- **Rules**: capital, loss limit, risk per trade, and losses/profits so far.

Price ranges are **not predictions**. They replay the stock's own last two years of daily moves 4,000 times and show where 80% and 50% of those replays ended. The honesty check re-runs this at 25 past dates and counts how often the real price landed inside the range.

### Deploy free on Streamlit Community Cloud

1. Go to **share.streamlit.io** and sign in with GitHub (vlhirapara007).
2. **Create app** → *Deploy a public app from GitHub* → repo `vlhirapara007/india-trader-skill`, branch `main`, main file `app/streamlit_app.py`. Under *Advanced settings* pick Python 3.12.
3. Deploy. Because the repo is private, the app is private too. Add viewers with **Share → invite by email**.
4. Open the link on your phone and *Add to Home Screen*.

If the app can't download a stock list (NSE sometimes blocks cloud servers), run
`python3 tools/snapshot_universes.py` on your computer, then commit and push
`data/universe/`. The app falls back to those files.

Run it locally: `pip install -r requirements.txt && streamlit run app/streamlit_app.py`.
Demo mode with made-up prices (no internet needed): `INDTRADER_DEMO=1 streamlit run app/streamlit_app.py`.

## Install

On claude.ai: download the zips from `dist/` (or run `python3 tools/build.py`)
and upload each one under Settings → Skills.

In Claude Code: `cp -r india-* ~/.claude/skills/`

## Develop

```bash
python3 -m unittest discover -s tests   # tests
python3 tools/build.py                  # copy core/indtrader.py into every skill, build dist/*.zip
```

`core/indtrader.py` is the one source of shared code. Never edit the copies
inside the skill folders; run the build instead.

Inspired by the method in
[Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT).
This is a separate implementation for Indian markets.

Not investment advice. SEBI's 2024 study found about 7 in 10 individual
intraday traders in the cash segment lost money. Paper-trade a setup before
risking money on it.
