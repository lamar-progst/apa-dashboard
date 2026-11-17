# TradesByJay Trading Dashboard

The TradesByJay Market Dashboard is hosted with GitHub Pages:

https://lamar-progst.github.io/apa-dashboard/

## Live market feed
The browser reads `state.json` from this repository. The local MT5 bot publishes verified market snapshots and new FVG setups to that file using a private GitHub token stored only in the bot's local `config.json`.

## MT5 bot
See [bot/README.md](bot/README.md) for installation, configuration, dashboard connection, and Telegram alerts.

The strategy is higher-timeframe support/resistance (H1 or H4) with 5-minute displacement Fair Value Gap entries. The public website does not contain MT5 credentials or private bot tokens.

## Security
Never commit `bot/config.json`, GitHub tokens, Telegram bot tokens, or MT5 credentials.
