"""
main.py — MEXC Earnings & News Telegram Bot
Hosted on Railway | State stored in Upstash Redis

Scheduler (every 30 min, staggered):
  pre_earnings_job   → alerts BEFORE report date so you can position on MEXC
  earnings_job       → full results after announcement
  news_job           → breaking news with ticker + MEXC symbol first

Commands:
  /start             welcome
  /help              command list
  /upcoming          earnings calendar, next 14 days
  /price AAPLUSDT    live MEXC futures price
  /tickers           full watchlist
  /status            bot health + last check times
  /mute [pre|earnings|news]    pause a category
  /unmute [pre|earnings|news]  resume
  /settings          view current mute state
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CHECK_INTERVAL_MINUTES
from earnings import check_upcoming_earnings_alerts, check_new_earnings, get_full_earnings_calendar
from formatters import format_pre_earnings, format_earnings, format_news, format_upcoming_calendar, pack_messages
from mexc_price import get_mexc_price, format_mexc_price
from news import check_new_news
from storage import get_setting, set_setting, health_check
from tickers import MEXC_TO_TICKER, ALL_TICKERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_last: dict = {"pre": None, "earnings": None, "news": None}
CATEGORIES = ("pre", "earnings", "news")


# ─── Mute helpers ─────────────────────────────────────────────────────────────

def _muted(cat: str) -> bool:
    return get_setting(f"mute_{cat}") == "1"

def _set_mute(cat: str, on: bool) -> None:
    set_setting(f"mute_{cat}", "1" if on else "0")


# ─── Scheduler jobs ───────────────────────────────────────────────────────────

async def pre_earnings_job() -> None:
    if _muted("pre"):
        return
    logger.info("Running pre-earnings check...")
    try:
        alerts = check_upcoming_earnings_alerts()
        if alerts:
            alerts.sort(key=lambda a: a.milestone)
            for batch in pack_messages([format_pre_earnings(a) for a in alerts]):
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"⚡ <b>{len(alerts)} earnings alert(s)</b>\n\n" + batch
                )
        _last["pre"] = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"pre_earnings_job: {e}", exc_info=True)


async def earnings_job() -> None:
    if _muted("earnings"):
        return
    logger.info("Running post-earnings check...")
    try:
        reports = check_new_earnings()
        if reports:
            for batch in pack_messages([format_earnings(r) for r in reports]):
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"📊 <b>{len(reports)} earnings result(s)</b>\n\n" + batch
                )
        _last["earnings"] = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"earnings_job: {e}", exc_info=True)


async def news_job() -> None:
    if _muted("news"):
        return
    logger.info("Running news check...")
    try:
        items = check_new_news()
        if items:
            for batch in pack_messages([format_news(i) for i in items]):
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"📡 <b>{len(items)} new article(s)</b>\n\n" + batch
                )
        _last["news"] = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"news_job: {e}", exc_info=True)


# ─── Commands ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>MEXC Earnings & News Bot</b>\n\n"
        f"Monitoring <b>{len(ALL_TICKERS)}</b> stocks on MEXC.\n"
        f"Alerts every <b>{CHECK_INTERVAL_MINUTES} min</b>.\n\n"
        "⭐ Pre-earnings alerts fire at <b>7d / 3d / 1d / day-of</b> "
        "so you can position before the announcement.\n\n"
        "/help — all commands"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📋 <b>Commands</b>\n\n"
        "/upcoming          — earnings calendar (14 days)\n"
        "/price <i>SYMBOL</i>    — live MEXC price  e.g. /price NVDAUSDT\n"
        "/tickers           — full watchlist\n"
        "/status            — bot health\n"
        "/settings          — mute state\n"
        "/mute pre          — pause countdown alerts\n"
        "/mute earnings     — pause results alerts\n"
        "/mute news         — pause news alerts\n"
        "/unmute <i>category</i>  — resume"
    )


@dp.message(Command("upcoming"))
async def cmd_upcoming(message: Message) -> None:
    await message.answer("🔍 Fetching earnings calendar…")
    data = get_full_earnings_calendar(days_ahead=14)
    await message.answer(format_upcoming_calendar(data))


@dp.message(Command("price"))
async def cmd_price(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /price <b>SYMBOL</b>\nExample: /price NVDAUSDT")
        return
    symbol = parts[1].upper().strip()
    if symbol not in MEXC_TO_TICKER:
        await message.answer(f"❌ <code>{symbol}</code> not in watchlist. Use /tickers.")
        return
    data = await get_mexc_price(symbol)
    if not data:
        await message.answer(f"⚠️ Could not fetch price for <code>{symbol}</code>.")
        return
    await message.answer(format_mexc_price(symbol, data))


@dp.message(Command("tickers"))
async def cmd_tickers(message: Message) -> None:
    lines = ["📋 <b>MEXC Watchlist</b>\n"]
    for mexc, real in sorted(MEXC_TO_TICKER.items()):
        lines.append(f"<code>{mexc:<20}</code> → <b>{real}</b>")
    await message.answer("\n".join(lines))


@dp.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in CATEGORIES:
        await message.answer(f"Usage: /mute [{'|'.join(CATEGORIES)}]")
        return
    cat = parts[1].lower()
    _set_mute(cat, True)
    await message.answer(f"🔇 <b>{cat}</b> alerts muted.")


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in CATEGORIES:
        await message.answer(f"Usage: /unmute [{'|'.join(CATEGORIES)}]")
        return
    cat = parts[1].lower()
    _set_mute(cat, False)
    await message.answer(f"🔔 <b>{cat}</b> alerts resumed.")


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    lines = ["⚙️ <b>Settings (Upstash)</b>\n"]
    for cat in CATEGORIES:
        icon = "🔇 MUTED" if _muted(cat) else "🔔 active"
        lines.append(f"  {cat:<12} {icon}")
    lines += ["", f"Check interval: every {CHECK_INTERVAL_MINUTES} min"]
    await message.answer("\n".join(lines))


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    def fmt(dt) -> str:
        return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "Not yet run"
    redis_ok = health_check()
    await message.answer(
        "🤖 <b>Bot Status</b>\n\n"
        f"✅ Running on Railway\n"
        f"{'✅' if redis_ok else '❌'} Upstash Redis: {'ok' if redis_ok else 'ERROR'}\n\n"
        f"⭐ Last pre-earnings:  {fmt(_last['pre'])}\n"
        f"📊 Last results:       {fmt(_last['earnings'])}\n"
        f"📰 Last news:          {fmt(_last['news'])}\n\n"
        f"⏱ Interval: {CHECK_INTERVAL_MINUTES} min  |  👀 {len(ALL_TICKERS)} tickers"
    )


# ─── Startup / shutdown ───────────────────────────────────────────────────────

async def on_startup() -> None:
    redis_ok = health_check()
    logger.info(f"Startup | Redis: {'ok' if redis_ok else 'FAILED'}")

    # Run all checks immediately so you get alerts right away
    await pre_earnings_job()
    await earnings_job()
    await news_job()

    await bot.send_message(
        TELEGRAM_CHAT_ID,
        "🚀 <b>MEXC Bot is online!</b>\n"
        f"Railway ✅  |  Upstash {'✅' if redis_ok else '❌'}\n"
        f"Watching {len(ALL_TICKERS)} tickers | Alerts every {CHECK_INTERVAL_MINUTES} min\n"
        "⭐ Pre-earnings: 7d / 3d / 1d / day-of\n"
        "/help for commands"
    )


async def on_shutdown() -> None:
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, "🛑 Bot shutting down.")
    except Exception:
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID is not set")

    now = datetime.now(timezone.utc)
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Stagger jobs by 3 min each so APIs aren't hammered simultaneously
    scheduler.add_job(pre_earnings_job, "interval", minutes=CHECK_INTERVAL_MINUTES,
                      start_date=now)
    scheduler.add_job(earnings_job,     "interval", minutes=CHECK_INTERVAL_MINUTES,
                      start_date=now + timedelta(minutes=3))
    scheduler.add_job(news_job,         "interval", minutes=CHECK_INTERVAL_MINUTES,
                      start_date=now + timedelta(minutes=6))

    scheduler.start()
    logger.info(f"Scheduler started — every {CHECK_INTERVAL_MINUTES} min")

    await on_startup()

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await on_shutdown()
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
