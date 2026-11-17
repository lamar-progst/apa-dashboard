# TradesByJay MT5 Bot — H4 Trend + H1 Liquidity Sweep + M15 Wick Entry

This is the demo-only MT5 scanner and execution bot used by the TradesByJay dashboard.

## Exact strategy

1. **H4 trend:** confirmed swing structure defines direction. Higher highs + higher lows = bullish; lower highs + lower lows = bearish. Mixed structure = no trade. No EMAs.
2. **H1 liquidity sweep:** in the H4 direction, a completed H1 candle must wick through an older confirmed swing level and close back through that level. The sweep extreme becomes the invalidation point.
3. **M15 entry:** wait for price to return to the swept H1 level and print a long-wick rejection candle in the H4 direction. The M15 candle must close directionally and meet the configured wick-to-range ratio.
4. **Execution:** market order at the current quote. Stop loss is beyond the H1 sweep extreme (plus the configured point buffer). Take profit is the configured reward-to-risk multiple (default 2.0R).
5. **Invalidation:** invalidate if an M15 candle closes beyond the sweep extreme, H4 trend no longer agrees, or the setup expires. A sweep is not re-armed after it has been invalidated.

There are no EMA, FVG, BOS, or M5 entry conditions in this strategy.

## Install (Windows)

Keep the MT5 desktop terminal open and logged into a **demo account**. Use a Python version supported by MetaTrader5:

```powershell
py -3.14 -m pip install MetaTrader5
```

Save the bot script and its sibling `.env` file in the same folder, then run:

```powershell
py -3.14 .\TRADESBYJAY_BOT.py
```

The bot refuses to start on a live account and stops if the connected account changes away from demo.

## Local .env settings

Create a plain-text `.env` beside the script. Example:

```dotenv
APA_SIGNAL_SYMBOLS=XAUUSD,Volatility 10 Index,Volatility 25 Index,Volatility 50 Index,Volatility 75 Index,Volatility 100 Index
RISK_USD=1
SVP_RR=2.0
SCAN_SECONDS=5
MAX_POSITIONS=4
DRY_RUN=false
TREND_SWING=2
SWEEP_SWING=3
SWEEP_LOOKBACK_H1=12
SETUP_EXPIRY_H1=24
WICK_RATIO=0.5
SL_BUFFER_POINTS=0
MAX_SPREAD_POINTS=0
APA_AUTO_SYMBOL_KEYWORDS=STEP
TRADE_BUY=true
TRADE_SELL=true
APA_WEB_GITHUB_TOKEN=YOUR_REPOSITORY_TOKEN
GITHUB_REPO=lamar-progst/apa-dashboard
GITHUB_BRANCH=main
PUBLISH_SECONDS=30
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID
```

The bot matches configured symbols to the broker's actual symbol names. It can also auto-add broker symbols matching `APA_AUTO_SYMBOL_KEYWORDS` (default: `STEP`). Only symbols successfully found and scanned by MT5 are published.

`RISK_USD` is a fixed cash-risk budget per trade, not a percentage. If the broker's minimum lot would exceed that budget, the bot skips the trade rather than increasing volume. `MAX_POSITIONS` limits total open positions plus pending orders. `DRY_RUN=true` sends setup notifications without sending orders.

## Website live feed

The website reads `state.json` from this repository's `main` branch. Set `APA_WEB_GITHUB_TOKEN` in the local `.env` file to a GitHub token with repository Contents read/write access. The token stays local and must never be committed or pasted into the website. The bot publishes current market snapshots and open/pending trades approximately every `PUBLISH_SECONDS` seconds (default 30).

The website's strategy journey displays H4 trend → H1 liquidity sweep → M15 wick-rejection entry. Candle charts use the completed M15 candles published by the bot.

## Telegram and Discord alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for Telegram. Optional Discord webhook variables supported by the script include `DISCORD_WEBHOOK_URL`, `MARKET_TIME_DISCORD_WEBHOOK_URL`, `BOT_STATUS_DISCORD_WEBHOOK_URL`, `XAUUSD_DISCORD_WEBHOOK_URL`, `VIX_DISCORD_WEBHOOK_URL`, `SKEW_DISCORD_WEBHOOK_URL`, `STEP_DISCORD_WEBHOOK_URL`, and the individual `VIX_*_WEBHOOK` variables.

The bot sends alerts for setup armed, setup invalidated, demo bot status, and order signals. Keep credentials in the local `.env` file only.

## Safety

- Demo accounts only; live account execution is blocked at startup and during the scan loop.
- This is not a profitability guarantee. Forward-test on demo and verify broker symbol specifications, spread, contract size, and order results.
- Never commit `.env`, tokens, webhook URLs, or other credentials to GitHub.
