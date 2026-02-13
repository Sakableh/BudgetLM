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

## Proxmox + Debian + Dockge (GitHub Deploy)

These steps assume you have Docker + Dockge running on your Debian VM/CT in Proxmox.

1. SSH into your Debian host.
2. Create the app directory:

```
sudo mkdir -p /opt/BudgetLM
sudo chown $USER:$USER /opt/BudgetLM
```

3. Clone your repo into `/opt/BudgetLM`:

```
git clone https://github.com/Sakableh/BudgetLM.git /opt/BudgetLM
```

4. Go to the bot folder:

```
cd /opt/BudgetLM/manual_tx_only_bot
```

5. Create and edit `.env`:

```
cp .env.example .env
nano .env
```

Recommended values (example):
```
TIMEZONE=Asia/Singapore
DEFAULT_CURRENCY=BND
```

6. Open Dockge UI:
   - Click “Create Stack”
   - Choose the folder: `/opt/BudgetLM/manual_tx_only_bot`
   - Dockge will detect `docker-compose.yml`
   - Click “Deploy”

7. Verify logs in Dockge (stack → Logs) or via SSH:

```
docker compose logs -f
```

Notes:
- Do not commit `.env` to GitHub.
- No ports need to be exposed (long polling).

## Usage

Send a message like:

```
Lunch 12.50 yesterday cash at Subway
```

The bot replies with a summary and inline buttons to confirm or cancel.
You can send `/accounts` to list account names and IDs from Lunch Money.

Behavior notes:
- Notes are not parsed or sent.
- Confirmed transactions are inserted as `uncleared`, so they still require approval/review in Lunch Money.

## Environment

- TELEGRAM_BOT_TOKEN
- LUNCH_MONEY_TOKEN
- DEEPSEEK_API_KEY
- TIMEZONE (default UTC)
- DEFAULT_CURRENCY (default USD)
- DEFAULT_ACCOUNT_ID (optional, fallback account)

## Troubleshooting

If you get:

`Could not match an account. Include one of your account names in the message, or set DEFAULT_ACCOUNT_ID.`

Use one of these fixes:
- Include the account name in your message (for example: `Lunch 12.50 cash`).
- Send `/accounts` in Telegram and copy either:
  - the exact account name into your transaction text, or
  - the account ID into `.env` as `DEFAULT_ACCOUNT_ID=123456`.
