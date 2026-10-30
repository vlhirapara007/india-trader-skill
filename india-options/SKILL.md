---
name: india-options
description: >-
  India Options planner. Turns an intraday idea on an NSE F&O stock, Nifty or
  Bank Nifty into a buy-only option plan: whether to buy a call (CE) or a put
  (PE), which strike (at, in or out of the money) and expiry (Nifty weekly on
  Tuesday, stocks and Bank Nifty on the month's last Tuesday, moved earlier
  for holidays), the premium to pay, where to stop and take profit on the
  premium, how many lots fit the user's risk after Groww's F&O charges, and
  the odds from the stock's own past moves. Uses NSE's live option chain when
  it answers, otherwise estimates premiums and says so. Never sells options
  and never places orders. Use when the user says "which call should I buy",
  "CE or PE", "put option on", "option for this breakout", "Nifty options
  today", "bank nifty call", "strike price", "how many lots", or after an
  opening-range breakout or breakdown on an F&O stock.
---

# India Options

Buy-only plans for calls (CE) and puts (PE). Tool: `options.py` next to this
file (with its copy of `indtrader.py`). Run it from the user's project folder.

## How a plan is built

1. **Direction comes from the stock or index, not the option.** A CE is for a
   rise: buy when a 5-minute candle of the stock **closes above** the trigger
   (usually the 09:15-09:30 opening-range high). A PE is for a fall: buy when
   a candle **closes below** the range low. `--orb` takes both levels from
   today's opening range, and the stop is the middle of the range.
2. **Strike:** ATM by default (`--strike ITM1` is 1 strike in the money, which
   moves more with the price and decays less; `OTM1` is cheaper but needs a
   bigger move). Stock strike gaps are approximated from the price, so tell the
   user to pick the exact strike in Groww.
3. **Expiry:** the nearest one that isn't today, because premiums melt on expiry day.
4. **Premium at entry, stop and target:** from NSE's live chain (premium and IV)
   when it answers, else Black-Scholes with the stock's own 20/60-day volatility.
   Say plainly when it's an estimate: real premiums are often higher.
5. **Lots:** the most whole lots whose loss at the stop, after Groww's F&O
   charges (₹20 an order, 0.15% STT on the sell premium, exchange, SEBI, IPFT,
   stamp, GST), fits the risk allowed now. Lot sizes come from NSE's
   fo_mktlots.csv (`python3 options.py lots`); Nifty 65, Bank Nifty 30 from Jan 2026.

## What to tell the user

- Lead with the contract (e.g. "RELIANCE 23 Nov 1160 CE"), whether it **fits
  their rules**, the premium to pay, the stop and target premiums, and the
  rupee loss and gain for the lots shown.
- With a small account the honest answer is often **"doesn't fit"**. With
  ₹200 risk per trade, one lot of most stock or index options loses ₹500 to
  ₹1,500 on a normal intraday stop. Show the risk one lot needs. Don't shrink
  the stop to make it fit, and don't suggest option selling.
- Put the time rule first: out by 15:10, never carried overnight on an intraday plan.
- One line, once: SEBI found about 9 in 10 individual F&O traders lost money,
  and buying options can lose the whole premium.
