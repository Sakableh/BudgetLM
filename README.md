# Manual Transaction Bot (DeepSeek + Lunch Money)

A tiny Telegram bot that only does manual transactions. Users text a transaction, DeepSeek extracts fields, and the bot asks for confirmation before saving to Lunch Money.

## Setup

1. Create a Telegram bot and get its token.
2. Create a Lunch Money API token.
3. Create a DeepSeek API key.
4. Copy `.env.example` to `.env` and fill values.

## Run

```
cd manual_tx_only_bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Run with Docker (Home Server)

1. Copy `.env.example` to `.env` and fill in your secrets.
2. Build and start the container:

```
cd manual_tx_only_bot
docker compose up -d --build
```

3. Check logs:

```
docker compose logs -f
```

4. Stop the bot:

```
docker compose down
```

Notes:
- This bot uses long polling, so you do not need to expose any ports.
- Keep your `.env` file private.

## Usage

Send a message like:

```
Lunch 12.50 yesterday cash at Subway
```

The bot replies with a summary and inline buttons to confirm or cancel.

## Environment

- TELEGRAM_BOT_TOKEN
- LUNCH_MONEY_TOKEN
- DEEPSEEK_API_KEY
- TIMEZONE (default UTC)
- DEFAULT_CURRENCY (default USD)
- DEFAULT_ACCOUNT_ID (optional, fallback account)
