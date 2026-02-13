import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from lunchable import LunchMoney, TransactionInsertObject, TransactionUpdateObject
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manual_tx_bot")

CONFIRM_CALLBACK = "confirm_tx"
CANCEL_CALLBACK = "cancel_tx"
MANUAL_ACCOUNT_TYPES = {"cash", "credit"}


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    lunch_money_token: str
    deepseek_api_key: str
    timezone: str
    default_currency: str
    default_account_id: int | None
    account_token_map: dict[str, int]


@dataclass
class PendingTransaction:
    chat_id: int
    original_text: str
    date: datetime
    amount: float
    currency: str
    payee: str
    account_id: int
    account_name: str
    category_id: int | None
    category_name: str | None
    is_received: bool


_pending: Dict[int, PendingTransaction] = {}


def load_config() -> Config:
    load_dotenv()

    default_account_raw = os.getenv("DEFAULT_ACCOUNT_ID", "").strip()
    default_account_id = int(default_account_raw) if default_account_raw.isdigit() else None
    account_token_map = _parse_account_token_map(os.getenv("ACCOUNT_TOKEN_MAP", ""))

    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        lunch_money_token=os.getenv("LUNCH_MONEY_TOKEN", "").strip(),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        timezone=os.getenv("TIMEZONE", "UTC").strip() or "UTC",
        default_currency=os.getenv("DEFAULT_CURRENCY", "USD").strip() or "USD",
        default_account_id=default_account_id,
        account_token_map=account_token_map,
    )


def validate_config(config: Config) -> None:
    missing = []
    if not config.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.lunch_money_token:
        missing.append("LUNCH_MONEY_TOKEN")
    if not config.deepseek_api_key:
        missing.append("DEEPSEEK_API_KEY")

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def get_client(token: str) -> LunchMoney:
    return LunchMoney(access_token=token)


def list_manual_accounts(client: LunchMoney):
    assets = client.get_assets()
    return [asset for asset in assets if asset.type_name in MANUAL_ACCOUNT_TYPES]


def list_categories(client: LunchMoney):
    categories = client.get_categories()
    return [category for category in categories if not category.is_group]


def _account_label(account) -> str:
    return account.display_name or account.name


def _parse_account_token_map(raw: str) -> dict[str, int]:
    cleaned = raw.strip()
    if not cleaned:
        return {}

    # Preferred format: JSON object {"2831":1234,"9912":5678}
    if cleaned.startswith("{"):
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Invalid ACCOUNT_TOKEN_MAP JSON; expected object like {\"2831\":1234}")
            return {}

        if not isinstance(payload, dict):
            logger.warning("Invalid ACCOUNT_TOKEN_MAP JSON; expected object mapping token -> account_id")
            return {}

        parsed: dict[str, int] = {}
        for token, account_id in payload.items():
            token_str = str(token).strip()
            account_id_str = str(account_id).strip()
            if token_str and account_id_str.isdigit():
                parsed[token_str] = int(account_id_str)
            else:
                logger.warning("Skipping invalid ACCOUNT_TOKEN_MAP entry: %s -> %s", token, account_id)
        return parsed

    # Fallback format: 2831:1234,9912:5678
    parsed: dict[str, int] = {}
    for item in cleaned.split(","):
        pair = item.strip()
        if not pair:
            continue
        if ":" not in pair:
            logger.warning("Skipping invalid ACCOUNT_TOKEN_MAP item (missing colon): %s", pair)
            continue
        token, account_id_raw = pair.split(":", 1)
        token = token.strip()
        account_id_raw = account_id_raw.strip()
        if not token or not account_id_raw.isdigit():
            logger.warning("Skipping invalid ACCOUNT_TOKEN_MAP item: %s", pair)
            continue
        parsed[token] = int(account_id_raw)

    return parsed


def _find_text_tokens(text: str) -> set[str]:
    # Capture common "card ending 1234" variants while also allowing plain 4-digit token matches.
    found: set[str] = set(re.findall(r"(?<!\d)(\d{4})(?!\d)", text))

    for pattern in (
        r"(?:ending|ends\s+with|last\s*4|last4)\D{0,8}(\d{4})",
        r"(?:card|visa|mastercard|amex|debit|credit|acct|account)\D{0,24}(\d{4})",
        r"(?:x{2,}|\*{2,}|#{2,}|•{2,}|XX+)\s*(\d{4})",
    ):
        found.update(match for match in re.findall(pattern, text, flags=re.IGNORECASE))

    return found


def _normalize(value: str) -> str:
    # Normalize case, spacing, and punctuation so fuzzy account/category matching is more reliable.
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return " ".join(normalized.strip().split())


def match_account(account_name: str | None, accounts) -> tuple[int, str] | None:
    if not account_name:
        return None

    trimmed = account_name.strip()
    if trimmed.isdigit():
        account_id = int(trimmed)
        for acct in accounts:
            if acct.id == account_id:
                return acct.id, _account_label(acct)

    needle = _normalize(account_name)
    if not needle:
        return None

    needle_tokens = set(needle.split())
    exact = None
    contains = None
    contained_by = None
    token_best = None
    token_best_score = 0
    token_tie = False

    for acct in accounts:
        name = _account_label(acct)
        normalized = _normalize(name)
        if normalized == needle:
            exact = acct
            break
        if needle in normalized:
            contains = contains or acct
        if normalized in needle:
            contained_by = contained_by or acct

        overlap = len(set(normalized.split()) & needle_tokens)
        if overlap > token_best_score:
            token_best = acct
            token_best_score = overlap
            token_tie = False
        elif overlap and overlap == token_best_score:
            token_tie = True

    token_match = token_best if token_best_score > 0 and not token_tie else None
    match = exact or contains or contained_by or token_match
    if not match:
        return None

    return match.id, _account_label(match)


def resolve_account(account_name: str | None, accounts, default_account_id: int | None) -> tuple[int, str] | None:
    matched_account = match_account(account_name, accounts)
    if matched_account:
        return matched_account

    if default_account_id is not None:
        by_default_id = next((acct for acct in accounts if acct.id == default_account_id), None)
        if by_default_id:
            return by_default_id.id, _account_label(by_default_id)

    if len(accounts) == 1:
        only = accounts[0]
        return only.id, _account_label(only)

    return None


def resolve_account_from_text_tokens(text: str, accounts, token_map: dict[str, int]) -> tuple[int, str] | None:
    if not token_map:
        return None

    text_tokens = _find_text_tokens(text)
    if not text_tokens:
        return None

    mapped_ids = {account_id for token, account_id in token_map.items() if token in text_tokens}
    if not mapped_ids:
        return None

    if len(mapped_ids) > 1:
        logger.info("Multiple ACCOUNT_TOKEN_MAP matches found in text; skipping map-based account selection")
        return None

    account_id = next(iter(mapped_ids))
    account = next((acct for acct in accounts if acct.id == account_id), None)
    if account is None:
        logger.warning(
            "ACCOUNT_TOKEN_MAP matched account_id %s, but it is not a manual cash/credit account in Lunch Money",
            account_id,
        )
        return None

    return account.id, _account_label(account)


def format_account_options(accounts, *, limit: int = 10) -> str:
    options = [f"{_account_label(acct)} (id {acct.id})" for acct in accounts[:limit]]
    remaining = len(accounts) - limit
    if remaining > 0:
        options.append(f"...and {remaining} more")
    return ", ".join(options)


def match_category(category_name: str | None, categories) -> tuple[int, str] | None:
    if not category_name:
        return None

    needle = _normalize(category_name)
    if not needle:
        return None

    exact = None
    contains = None
    for category in categories:
        normalized = _normalize(category.name)
        if normalized == needle:
            exact = category
            break
        if needle in normalized:
            contains = contains or category

    match = exact or contains
    if not match:
        return None

    return match.id, match.name


def _safe_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _safe_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    return str(value)


def _tzinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except Exception:
        logger.exception("Invalid timezone %s, falling back to UTC", timezone)
        return ZoneInfo("UTC")


def _parse_date(value: str | None, timezone: str) -> datetime:
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            logger.info("Invalid date format from AI: %s", value)

    now = datetime.now(_tzinfo(timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_transaction_text(
    *,
    text: str,
    deepseek_api_key: str,
    timezone: str,
    default_currency: str,
    account_names: list[str],
    category_names: list[str],
) -> dict:
    now = datetime.now(_tzinfo(timezone))
    today = now.strftime("%Y-%m-%d")

    schema_prompt = {
        "date": "YYYY-MM-DD or null",
        "amount": "number",
        "currency": "string",
        "payee": "string",
        "account": "string or null",
        "category": "string or null",
        "is_received": "boolean",
        "confidence": "number between 0 and 1",
        "missing_fields": ["date", "amount", "payee", "account"],
    }

    account_hint = ", ".join(account_names[:20]) if account_names else "(none)"
    category_hint = ", ".join(category_names[:50]) if category_names else "(none)"

    system_prompt = (
        "You are a transaction parser. Output a JSON object only. "
        "Use the schema below and do not add extra keys."
    )

    guidance = (
        f"Today is {today} in the user's timezone ({timezone}). "
        "If no date is mentioned, use today. "
        "Return amount as a positive number. Use is_received=true for income. "
        f"Default currency is {default_currency} if not specified. "
        "If you are unsure about a field, set it to null and include it in missing_fields. "
        "Account must match one of the provided account names when possible. "
        "Category should match a provided category name when possible."
    )

    user_prompt = (
        f"Text: {text}\n\n"
        f"Accounts: {account_hint}\n\n"
        f"Categories: {category_hint}\n\n"
        f"Schema: {json.dumps(schema_prompt)}\n\n"
        f"Rules: {guidance}"
    )

    client = OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("DeepSeek returned empty response")

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.exception("Failed to decode DeepSeek JSON")
        raise RuntimeError("DeepSeek response was not valid JSON") from exc


def set_pending(chat_id: int, pending: PendingTransaction) -> None:
    _pending[chat_id] = pending


def get_pending(chat_id: int) -> PendingTransaction | None:
    return _pending.get(chat_id)


def clear_pending(chat_id: int) -> None:
    _pending.pop(chat_id, None)


def build_summary(pending: PendingTransaction) -> str:
    lines = [
        "Proposed transaction:",
        f"Date: {pending.date.date().isoformat()}",
        f"Payee: {pending.payee}",
        f"Amount: {pending.amount:.2f} {pending.currency}",
        f"Type: {'Income' if pending.is_received else 'Expense'}",
        f"Account: {pending.account_name}",
        f"Category: {pending.category_name or 'Uncategorized'}",
        "Lunch Money status: Uncleared (requires approval/review)",
        "",
        f"Original text: {pending.original_text}",
    ]
    return "\n".join(lines)


def insert_transaction(client: LunchMoney, pending: PendingTransaction) -> tuple[int, bool]:
    signed_amount = -abs(pending.amount) if pending.is_received else abs(pending.amount)
    tx_object = TransactionInsertObject(
        date=pending.date,
        category_id=pending.category_id,
        payee=pending.payee,
        amount=signed_amount,
        currency=pending.currency.lower(),
        status=TransactionInsertObject.StatusEnum.uncleared,
        asset_id=pending.account_id,
    )

    tx_ids = client.insert_transactions(tx_object, apply_rules=False)
    if not tx_ids:
        raise RuntimeError("Lunch Money did not return a transaction id")

    tx_id = int(tx_ids[0])

    # Force newly-created manual transactions to stay unreviewed.
    status_update = TransactionUpdateObject(status=TransactionUpdateObject.StatusEnum.uncleared)
    try:
        client.update_transaction(transaction_id=tx_id, transaction=status_update)
        return tx_id, True
    except Exception:
        logger.exception("Inserted transaction %s but failed to force uncleared status", tx_id)
        return tx_id, False


async def handle_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Send a transaction like: 'Lunch 12.50 yesterday cash at Subway'. "
        "I will parse it and ask you to confirm before saving. "
        "Use /accounts to see account names and IDs. "
        "Use /accountmap to verify card-token mappings."
    )


async def handle_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    config = context.bot_data.get("config")
    if not isinstance(config, Config):
        await update.message.reply_text("Bot configuration error. Check server logs.")
        return

    client = get_client(config.lunch_money_token)
    accounts = list_manual_accounts(client)
    if not accounts:
        await update.message.reply_text(
            "No manual accounts found in Lunch Money. Add a cash or credit account before using this bot."
        )
        return

    lines = ["Manual accounts:"]
    lines.extend(f"- {_account_label(acct)} (id {acct.id})" for acct in accounts)
    await update.message.reply_text("\n".join(lines))


async def handle_accountmap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    config = context.bot_data.get("config")
    if not isinstance(config, Config):
        await update.message.reply_text("Bot configuration error. Check server logs.")
        return

    if not config.account_token_map:
        await update.message.reply_text(
            "ACCOUNT_TOKEN_MAP is empty. Configure it in .env, for example: ACCOUNT_TOKEN_MAP=2831:1234,9912:5678"
        )
        return

    client = get_client(config.lunch_money_token)
    accounts = list_manual_accounts(client)
    account_by_id = {acct.id: _account_label(acct) for acct in accounts}

    lines = ["Configured account token map:"]
    for token, account_id in sorted(config.account_token_map.items()):
        account_label = account_by_id.get(account_id)
        if account_label:
            lines.append(f"- {token} -> {account_label} (id {account_id})")
        else:
            lines.append(f"- {token} -> id {account_id} (not found in manual cash/credit accounts)")

    await update.message.reply_text("\n".join(lines))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    config = context.bot_data.get("config")
    if not isinstance(config, Config):
        await update.message.reply_text("Bot configuration error. Check server logs.")
        return

    text = update.message.text.strip()
    if not text:
        return

    client = get_client(config.lunch_money_token)
    accounts = list_manual_accounts(client)
    categories = list_categories(client)

    if not accounts:
        await update.message.reply_text(
            "No manual accounts found in Lunch Money. Add a cash or credit account before using this bot."
        )
        return

    account_names = [_account_label(acct) for acct in accounts]
    category_names = [category.name for category in categories]

    try:
        result = parse_transaction_text(
            text=text,
            deepseek_api_key=config.deepseek_api_key,
            timezone=config.timezone,
            default_currency=config.default_currency,
            account_names=account_names,
            category_names=category_names,
        )
    except Exception as exc:
        logger.exception("Failed to parse transaction")
        await update.message.reply_text(f"Failed to parse transaction: {exc}")
        return

    payee = _safe_str(result.get("payee"))
    amount = _safe_float(result.get("amount"))
    currency = _safe_str(result.get("currency")) or config.default_currency
    account_name = _safe_str(result.get("account"))
    category_name = _safe_str(result.get("category"))
    is_received = bool(result.get("is_received"))

    if not payee:
        await update.message.reply_text("Missing payee. Please include who the transaction was with.")
        return
    if amount is None:
        await update.message.reply_text("Missing amount. Please include the amount.")
        return

    parsed_date = _parse_date(_safe_str(result.get("date")), config.timezone)

    mapped_account = resolve_account_from_text_tokens(text, accounts, config.account_token_map)
    matched_account = mapped_account or resolve_account(account_name, accounts, config.default_account_id)

    if matched_account is None:
        await update.message.reply_text(
            "Could not match an account. Include one of your account names in the message, "
            "or set DEFAULT_ACCOUNT_ID.\n"
            f"Available accounts: {format_account_options(accounts)}"
        )
        return

    matched_category = match_category(category_name, categories)

    pending = PendingTransaction(
        chat_id=update.effective_chat.id,
        original_text=text,
        date=parsed_date,
        amount=abs(amount),
        currency=currency.upper(),
        payee=payee,
        account_id=matched_account[0],
        account_name=matched_account[1],
        category_id=matched_category[0] if matched_category else None,
        category_name=matched_category[1] if matched_category else None,
        is_received=is_received,
    )

    set_pending(update.effective_chat.id, pending)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Confirm", callback_data=CONFIRM_CALLBACK)],
            [InlineKeyboardButton("Cancel", callback_data=CANCEL_CALLBACK)],
        ]
    )

    await update.message.reply_text(build_summary(pending), reply_markup=keyboard)


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return

    config = context.bot_data.get("config")
    if not isinstance(config, Config):
        await update.callback_query.answer("Bot configuration error")
        return

    pending = get_pending(update.effective_chat.id)
    if not pending:
        await update.callback_query.answer("No pending transaction")
        return

    client = get_client(config.lunch_money_token)

    try:
        tx_id, forced_unreviewed = insert_transaction(client, pending)
    except Exception:
        logger.exception("Failed to insert transaction")
        await update.callback_query.answer("Failed to save transaction. Check logs.", show_alert=True)
        return

    clear_pending(update.effective_chat.id)

    await update.callback_query.answer("Saved")
    if forced_unreviewed:
        saved_text = f"Saved transaction in Lunch Money (id {tx_id}) as uncleared for approval/review."
    else:
        saved_text = (
            f"Saved transaction in Lunch Money (id {tx_id}), but it may still appear reviewed.\n"
            "In Lunch Money, disable auto-review settings for manual creation/updates and check Rules."
        )

    if update.callback_query.message:
        await update.callback_query.message.reply_text(saved_text)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=saved_text,
        )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return

    clear_pending(update.effective_chat.id)
    await update.callback_query.answer("Cancelled")
    if update.callback_query.message:
        await update.callback_query.message.reply_text("Cancelled. Send a new transaction when ready.")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="Cancelled. Send a new transaction when ready."
        )


async def main() -> None:
    config = load_config()
    validate_config(config)

    application = Application.builder().token(config.telegram_bot_token).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("help", handle_start))
    application.add_handler(CommandHandler("accounts", handle_accounts))
    application.add_handler(CommandHandler("accountmap", handle_accountmap))
    application.add_handler(CallbackQueryHandler(handle_confirm, pattern=f"^{CONFIRM_CALLBACK}$"))
    application.add_handler(CallbackQueryHandler(handle_cancel, pattern=f"^{CANCEL_CALLBACK}$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot starting")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
