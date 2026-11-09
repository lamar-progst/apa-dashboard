# S.V.P. APA MT5 Bot

This is the local MT5 execution service for the restored S.V.P. Market Room.

## Safety defaults
- Starts in scan-only mode; no orders are sent.
- Demo-only account guard is enabled.
- Live trading is blocked unless demo_only is disabled AND allow_live_trading is explicitly enabled.
- Signals use closed candles only.
- Risk sizing uses MT5 order_calc_profit and the broker's volume step/min/max.
- A 1% per-trade risk target and 3% aggregate bot risk ceiling are configured by default.

## Install on Windows
Use Python 3.14 (MetaTrader5 does not currently install on every Python version):
```powershell
py -3.14 -m pip install -r requirements.txt
Copy-Item config.example.json config.json
# Generate a private token, then paste it into config.json as api_token:
py -3.14 -c "import secrets; print(secrets.token_urlsafe(32))"
py -3.14 svp_apa_mt5_bot.py
```
Keep the MT5 desktop terminal open and logged in. Edit config.json to match the exact broker symbol names shown in Market Watch.

Before starting, replace `CHANGE_THIS_LOCAL_TOKEN` in config.json with the generated private token. The bot refuses dashboard control commands while the placeholder remains. The local API accepts browser requests only from the S.V.P. GitHub Pages origin or localhost.\n\n## First test
1. Leave auto_execute=false.
2. Start the script and open http://127.0.0.1:8765/status on the same computer.
3. Confirm the account, symbols, closed-candle scan and rejections look correct.
4. Test on a demo account before considering any live execution.

## Execution
Only after demo testing, set auto_execute=true. The default demo_only=true guard will still reject a non-demo account. Live execution requires a deliberate config change to demo_only=false and allow_live_trading=true. Never put the API token or MT5 credentials in the public website or repository.

The local API is bound to 127.0.0.1. POST commands require the Bearer token configured in config.json. The web dashboard connection UI still needs to be wired to this local service in a follow-up integration step.

## Strategy implementation
- H4: HH/HL or LL/LH structure with EMA context; neutral if unclear.
- H1: relevant swing/liquidity location; sweep is optional.
- M15: close beyond recent structure (BOS).
- M5: closed-candle precision shift.
- Stop: protected M5 swing; target: opposing H1 swing.
- Candlestick catalogue and other patterns remain context/education, not standalone entries.

This is a first executable baseline, not a profitability claim. Backtest and forward-test each broker symbol. Broker naming, filling modes, stop distances and contract specifications differ.
