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
| `india-options` | ready | Buy-only call (CE) or put (PE) plan for an intraday idea on an F&O stock, Nifty or Bank Nifty: strike, expiry, premium, stop and target on the premium, lots that fit your risk after F&O charges, odds |
| `india-plan` | next | Turns a setup into a full trade plan: entry, stop, target, exit time, quantity after charges (the logic is in the core and already used by the web app) |
| `india-timemachine` | planned | Replays the plan 10,000 times from the stock's own bars and on every past day the setup fired |
| `india-risk` | planned | PASS / BLOCK against your rules before you place anything |
| `india-news` | planned | NSE/BSE corporate announcements and headlines for your watchlist |
| `india-results` | planned | Quarterly results analyst: revenue, margins, profit, EPS, YoY, for long-term picks |
| `india-insider` | planned | SEBI insider-trading disclosures, bulk and block deals, promoter pledges |
| `india-portfolio` | planned | Holdings, P&L, stop hits, results dates, the trade journal |

## Web app (phone and desktop)

A website on the same code, hosted free on Vercel: `app.py` (a small Flask API over the core) and `public/` (the page). It installs on your phone like an app.

- **Stock**: search any NSE/BSE stock to see its chart with 20/50/200-day averages, trend, RSI, 52-week position, which setups fire, and a trade plan for your rules drawn as a price ladder: target, buy-above, stop, quantity, net profit or loss after Groww charges, breakeven, exit time, how to place it in Groww, and the odds of target-first vs stop-first.
- **Price ranges** for 1 week, 1 month and 3 months, with a check of how often past ranges held. These are ranges of possibility, **not predictions**.
- **Options**: for F&O stocks, Nifty and Bank Nifty, a call (CE) or put (PE) plan on the same intraday levels: contract, expiry, premium, stop and target premiums, lots that fit your risk, and Groww's F&O charges. Buy only, never option selling. Premiums are live when NSE's option chain answers and estimated otherwise (the page says which).
- **Scan**: swing, positional, pre-market and live opening-range scans over Nifty 50 / 200, Bank Nifty, all NSE, all BSE. Tap a hit to plan it.
- **Market**: Nifty 50 and Bank Nifty today, the biggest Nifty 50 movers, market holidays.
- **Rules**: capital, loss limit, risk per trade, losses and profits so far, saved on your phone.

### Deploy on Vercel (free)

1. Sign in at **vercel.com** with GitHub (vlhirapara007) and click **Add New → Project**.
2. Import `vlhirapara007/india-trader-skill` (grant Vercel access to it if asked). Leave every setting as it is and click **Deploy**. Vercel finds `app.py` (Flask) and `public/` by itself; `vercel.json` puts the server in Mumbai (`bom1`).
3. Optional but recommended: Vercel URLs are public, so add a password. In **Settings → Environment Variables** add `APP_PASSWORD`, then redeploy. The app asks for it once on each phone.
4. Open the URL on your phone and choose **Add to Home Screen**.

Every push to `main` redeploys automatically.

**Staying inside the free plan.** Vercel's Hobby plan includes, per month and shared by every project on your account: 1,000,000 function calls, 4 CPU-hours, 360 GB-hours of memory and 100 GB of data. Going over pauses the feature for up to 30 days. This app is built to stay small: it asks for 1 GB of memory, answers are cached on Vercel's CDN (60 seconds for prices, 15 minutes for daily scans), search engines are told not to index it, and the password stops strangers from using it. Personal daily use (stock lookups, a few Nifty 200 scans) comes to roughly 5–10 GB-hours a month. Check **Usage** in the Vercel dashboard after the first week.

If the deployed app can't download a stock list (NSE sometimes blocks cloud servers), run
`python3 tools/snapshot_universes.py` on your computer, then commit and push `data/universe/`.
The app falls back to those files. Searching by exact symbol always works, even without a list.

Run it locally: `pip install flask && flask --app app run`, then open http://127.0.0.1:5000.
Demo mode with made-up prices (no internet needed): `INDTRADER_DEMO=1 flask --app app run`.

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
