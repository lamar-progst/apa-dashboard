# TradesByJay MT5 Bot — HTF Support & Resistance + M5 FVG

This is the local MT5 scanner/execution service used by the TradesByJay dashboard.

## Strategy
1. Select H1 or H4 as the higher timeframe.
2. Identify support from the lowest-low candle and resistance from the highest-high candle in the configured HTF lookback. The candle's full range is the zone.
3. Wait for price to interact with support (bullish) or resistance (bearish).
4. Detect a completed three-candle M5 displacement FVG. The middle candle must close in the direction of the gap.
5. Optionally require an M5 liquidity sweep (off by default).
6. Alert on the setup and, when automatic execution is deliberately enabled, place a limit order at the FVG 50% midpoint.
7. SL is beyond the FVG origin candle with a spread buffer. TP defaults to 2R.
8. Cancel a tracked pending order if a completed M5 candle closes through the far side of the FVG.

## Install (Windows / PowerShell)

Use a Python version supported by the MetaTrader5 package and keep the MT5 desktop terminal open and logged into a **demo account**.

```powershell
py -3.14 -m pip install -r requirements.txt
Copy-Item config.example.json config.json
py -3.14 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the generated value into `api_token` in `config.json`.

## Dashboard live publishing

The website reads `state.json` from GitHub Pages. To publish fresh scanner data, create a GitHub fine-grained personal access token limited to this repository with **Contents: Read and write** permission. Put it in the local `github_token` field in `config.json`. Never commit `config.json` or share this token.

The bot updates `state.json` approximately every `publish_seconds` (default 30 seconds). The website polls that feed and displays real scanner results. Without this token, the local MT5 scanner still works, but remote website visitors will not receive fresh market data.

## Telegram alerts

Optional: set `telegram_bot_token` and `telegram_chat_id` in the local config. A new confirmed FVG setup triggers a Telegram message. Keep these credentials out of GitHub.

## Start

```powershell
py -3.14 tradesbyjay_fvg_mt5_bot.py
```

The local dashboard API listens on `http://127.0.0.1:8765/status`. On the TradesByJay website, use the Bot Control panel to connect, start, pause, or request a scan. Use the same `api_token` configured locally.

## Safety defaults
- `auto_execute=true`: automatic limit orders are enabled by default on demo.
- `demo_only=true`: non-demo accounts are rejected at startup.
- `allow_live_trading=false`: live trading remains blocked.
- Set `auto_execute=false` if you want alerts and scanning without orders.
- `risk_percent=1.0`, `risk_reward=2.0`.
- Broker symbol names and pip sizes vary. Verify each symbol's contract specification and set `pip_size_overrides` where needed.

This software is not a profitability guarantee. Forward-test on demo and inspect every broker-specific order result before considering any change to execution settings.
