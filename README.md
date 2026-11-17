# TradesByJay Trading Dashboard

The TradesByJay Market Dashboard is hosted with GitHub Pages:

https://lamar-progst.github.io/apa-dashboard/

## Live market feed
The browser reads `state.json` from this repository. The local MT5 bot publishes verified market snapshots and new FVG setups to that file using a private GitHub token stored only in the bot's local `config.json`.

## MT5 bot
See [bot/README.md](bot/README.md) for installation, configuration, dashboard connection, and Telegram alerts.

The strategy is higher-timeframe support/resistance (H1 or H4) with 5-minute displacement Fair Value Gap entries. The public website does not contain MT5 credentials or private bot tokens.

## Supabase configuration
The existing Supabase project hosts access-request handling and TradesByJay chat.

- Add `OPENAI_API_KEY` under Supabase Edge Function secrets for chat responses.
- Add `TRADESBYJAY_ADMIN_KEY` under Supabase Edge Function secrets for the access-admin page. Generate a strong random value locally and enter the same value in the admin page when reviewing requests.
- The chat model defaults to `gpt-6-luna`; optionally override it with `TRADESBYJAY_AI_MODEL`.

Never paste these secrets into the public website or commit them to GitHub.

## Security
Never commit `bot/config.json`, GitHub tokens, Telegram bot tokens, OpenAI API keys, admin keys, or MT5 credentials.
