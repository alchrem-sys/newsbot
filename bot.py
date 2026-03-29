"""
bot.py — MEXC Earnings Telegram Bot (single file, earnings only)

Run: python bot.py

Async Redis (upstash_redis.asyncio) — commands respond instantly,
background jobs never block the event loop.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import finnhub
import yfinance as yf
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from upstash_redis.asyncio import Redis

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_cid              = os.getenv("TELEGRAM_CHAT_ID", "0")
TELEGRAM_CHAT_ID      = int(_raw_cid) if int(_raw_cid) < 0 else -int(f"100{_raw_cid}")
TELEGRAM_THREAD_ID    = int(os.getenv("TELEGRAM_THREAD_ID", "0"))
FINNHUB_API_KEY       = os.getenv("FINNHUB_API_KEY", "")
UPSTASH_URL           = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN         = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
MEXC_TICKER_URL       = "https://futures.mexc.com/api/v1/contract/ticker"
EARNINGS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "7"))
PRE_EARNINGS_ALERT_DAYS = [14, 7, 3, 1, 0]  # alert at 14d, 7d, 3d, 1d, day-of
INTRADAY_MINUTES      = [120, 60, 30, 5, 1]
ET                    = ZoneInfo("America/New_York")
FH_DELAY              = 2.0
YF_DELAY              = 2.0
FH_CAL_CACHE_TTL      = 3600    # 1h — picks up newly announced dates quickly
CALENDAR_CACHE_TTL    = 4500    # 75min — slightly longer than the 1h job interval


# ══════════════════════════════════════════════════════════════
# TICKERS
# ══════════════════════════════════════════════════════════════

MEXC_TO_TICKER: dict[str, str] = {
    "COINUSDT": "COIN", "FIGUSDT": "FIG", "TSLAUSDT": "TSLA", "CVNAUSDT": "CVNA",
    "NVDAUSDT": "NVDA", "NAS100USDT": "QQQ", "AMATUSDT": "AMAT", "SP500USDT": "SPY",
    "MSTRUSDT": "MSTR", "GOOGLUSDT": "GOOGL", "QCOMUSDT": "QCOM", "HK50USDT": "^HSI",
    "CRMUSDT": "CRM", "AMZNUSDT": "AMZN", "FUTUUSDT": "FUTU", "AAPLUSDT": "AAPL",
    "MUUSDT": "MU", "SHOPUSDT": "SHOP", "WMTUSDT": "WMT", "MSFTUSDT": "MSFT",
    "US30USDT": "DIA", "QQQUSDT": "QQQ", "CSCOUSDT": "CSCO", "HOODUSDT": "HOOD",
    "KOUSDT": "KO", "VZUSDT": "VZ", "INTCUSDT": "INTC", "GEUSDT": "GE",
    "JNJUSDT": "JNJ", "MAUSDT": "MA", "AMDUSDT": "AMD", "METAUSDT": "META",
    "RDDTUSDT": "RDDT", "SPOTUSDT": "SPOT", "NFLXUSDT": "NFLX", "ORCLUSDT": "ORCL",
    "ASMLUSDT": "ASML", "PEPUSDT": "PEP", "ACNUSDT": "ACN", "XOMUSDT": "XOM",
    "VUSDT": "V", "NKEUSDT": "NKE", "SMCIUSDT": "SMCI", "UNHUSDT": "UNH",
    "NOWUSDT": "NOW", "GSUSDT": "GS", "LLYUSDT": "LLY", "LRCXUSDT": "LRCX",
    "IBMUSDT": "IBM", "COSTUSDT": "COST", "BAUSDT": "BA", "JDUSDT": "JD",
    "JPMUSDT": "JPM",
}
INDEX_TICKERS   = {"QQQ", "SPY", "DIA", "^HSI"}
ALL_TICKERS     = sorted(set(MEXC_TO_TICKER.values()))
EQUITY_TICKERS  = [t for t in ALL_TICKERS if t not in INDEX_TICKERS]
TICKER_TO_MEXC: dict[str, list[str]] = {}
for _m, _t in MEXC_TO_TICKER.items():
    TICKER_TO_MEXC.setdefault(_t, []).append(_m)


# ══════════════════════════════════════════════════════════════
# ASYNC REDIS  — never blocks the event loop
# ══════════════════════════════════════════════════════════════

_redis: Optional[Redis] = None

def _r() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
    return _redis

async def is_seen(ns: str, uid: str) -> bool:
    try:
        return bool(await _r().sismember(f"mexcbot:{ns}", uid))
    except Exception:
        return False   # fail-open: better to resend than miss

async def mark_seen(ns: str, uid: str) -> None:
    try:
        await _r().sadd(f"mexcbot:{ns}", uid)
        await _r().expire(f"mexcbot:{ns}", 60 * 60 * 24 * 90)
    except Exception:
        pass

async def get_setting(key: str) -> Optional[str]:
    try:
        v = await _r().get(f"mexcbot:setting:{key}")
        return str(v) if v is not None else None
    except Exception:
        return None

async def set_setting(key: str, val: str) -> None:
    try:
        await _r().set(f"mexcbot:setting:{key}", val)
    except Exception:
        pass

async def get_cache(key: str) -> Optional[str]:
    try:
        v = await _r().get(f"mexcbot:cache:{key}")
        return str(v) if v is not None else None
    except Exception:
        return None

async def set_cache(key: str, val: str, ttl: int = 3600) -> None:
    try:
        await _r().set(f"mexcbot:cache:{key}", val, ex=ttl)
    except Exception:
        pass

async def redis_ping() -> bool:
    try:
        await _r().ping()
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════════════════════

class UpcomingEarnings:
    def __init__(self, ticker, earnings_date, days_until, eps_estimate,
                 revenue_estimate, report_time, milestone,
                 exact_time=None, currency="USD"):
        self.ticker = ticker
        self.earnings_date = earnings_date
        self.days_until = days_until
        self.eps_estimate = eps_estimate
        self.revenue_estimate = revenue_estimate
        self.report_time = report_time
        self.milestone = milestone
        self.exact_time = exact_time
        self.currency = currency
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self):
        return hashlib.md5(
            f"pre_{self.ticker}_{self.earnings_date}_{self.milestone}".encode()
        ).hexdigest()


class IntradayReminder:
    def __init__(self, ticker, earnings_date, exact_time, minutes_before,
                 eps_estimate, revenue_estimate, report_time, currency="USD"):
        self.ticker = ticker
        self.earnings_date = earnings_date
        self.exact_time = exact_time
        self.minutes_before = minutes_before
        self.eps_estimate = eps_estimate
        self.revenue_estimate = revenue_estimate
        self.report_time = report_time
        self.currency = currency
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self):
        return hashlib.md5(
            f"intraday_{self.ticker}_{self.earnings_date}_{self.minutes_before}".encode()
        ).hexdigest()


class EarningsReport:
    def __init__(self, ticker, period, report_date, eps_actual, eps_estimate,
                 eps_surprise_pct, revenue, net_income, gross_profit, currency="USD"):
        self.ticker = ticker
        self.period = period
        self.report_date = report_date
        self.eps_actual = eps_actual
        self.eps_estimate = eps_estimate
        self.eps_surprise_pct = eps_surprise_pct
        self.revenue = revenue
        self.net_income = net_income
        self.gross_profit = gross_profit
        self.currency = currency
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self):
        return hashlib.md5(
            f"result_{self.ticker}_{self.period}_{self.report_date}".encode()
        ).hexdigest()


# ══════════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════════

_CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "CNY": "¥",
    "AUD": "A$", "CAD": "C$", "CHF": "CHF ", "KRW": "₩", "INR": "₹",
    "SGD": "S$",
}

def _pfx(currency: str) -> str:
    s = _CURRENCY_SYMBOLS.get(currency.upper())
    return s if s else f"{currency.upper()} "

def _money(v, currency="USD") -> str:
    if v is None: return "N/A"
    p, a, sign = _pfx(currency), abs(v), "-" if v < 0 else ""
    if a >= 1e9: return f"{sign}{p}{a/1e9:.2f}B"
    if a >= 1e6: return f"{sign}{p}{a/1e6:.2f}M"
    return f"{sign}{p}{a:,.2f}"

def _eps(v, currency="USD") -> str:
    return f"{_pfx(currency)}{v:.4f}" if v is not None else "N/A"

def _mexc_str(ticker: str) -> str:
    return "  ".join(f"<code>{s}</code>" for s in TICKER_TO_MEXC.get(ticker, []))

def _surprise_emoji(pct) -> str:
    if pct is None: return "❓"
    if pct >= 10:   return "🚀"
    if pct >= 3:    return "✅"
    if pct >= -3:   return "➡️"
    if pct >= -10:  return "⚠️"
    return "🔴"

def _time_label(code: str) -> str:
    return {"bmo": "☀️ Before Market Open", "amc": "🌙 After Market Close"}.get(
        code.lower() if code else "", "⏱ Time TBD")

def _fmt_time(dt) -> str:
    if dt is None: return "TBD"
    return (f"{dt.astimezone(ET).strftime('%H:%M ET')}"
            f"  ({dt.astimezone(timezone.utc).strftime('%H:%M UTC')})")

_MILESTONES = {
    7: ("🗓", "EARNINGS IN 1 WEEK",  "Plan your direction now."),
    3: ("⏰", "EARNINGS IN 3 DAYS",  "Prepare your entry."),
    1: ("🔔", "EARNINGS TOMORROW",   "Final check — ready to act."),
    0: ("🚨", "EARNINGS TODAY",      "Last chance to position on MEXC!"),
}

def fmt_pre_earnings(a: UpcomingEarnings) -> str:
    icon, title, cta = _MILESTONES.get(a.milestone, ("📅", "EARNINGS SOON", ""))
    lines = [
        f"{icon} <b>{a.ticker}</b>  {_mexc_str(a.ticker)}",
        f"<b>{title}</b>", "",
        f"📆 Date:       <b>{a.earnings_date}</b>",
        f"⏳ Days until: <b>{a.days_until}</b>" if a.days_until > 0 else "",
        f"🕐 Time:       {_fmt_time(a.exact_time)}",
        f"📋 Session:    {_time_label(a.report_time)}", "",
        f"📐 <b>Estimates</b>  <i>({a.currency})</i>",
        f"   EPS:     {_eps(a.eps_estimate, a.currency)}",
        f"   Revenue: {_money(a.revenue_estimate, a.currency)}", "",
        f"💡 <i>{cta}</i>",
    ]
    return "\n".join(l for l in lines if l != "")

_INTRADAY = {
    120: ("⏰", "2 HOURS TO EARNINGS"),
    60:  ("🔔", "1 HOUR TO EARNINGS"),
    30:  ("⚡", "30 MINUTES TO EARNINGS"),
    5:   ("🚨", "5 MINUTES TO EARNINGS"),
    1:   ("🔴", "1 MINUTE — LAST CALL"),
}

def fmt_intraday(r: IntradayReminder) -> str:
    icon, title = _INTRADAY.get(r.minutes_before, ("⏱", f"{r.minutes_before}MIN TO EARNINGS"))
    lines = [
        f"{icon} <b>{r.ticker}</b>  {_mexc_str(r.ticker)}",
        f"<b>{title}</b>", "",
        f"🕐 Announcement: <b>{_fmt_time(r.exact_time)}</b>",
        f"📋 Session:      {_time_label(r.report_time)}", "",
        f"📐 <b>Estimates</b>  <i>({r.currency})</i>",
        f"   EPS:     {_eps(r.eps_estimate, r.currency)}",
        f"   Revenue: {_money(r.revenue_estimate, r.currency)}", "",
        "💡 <i>Position now before the number drops.</i>",
    ]
    return "\n".join(l for l in lines if l != "")

def fmt_earnings(r: EarningsReport) -> str:
    pct, emoji = r.eps_surprise_pct, _surprise_emoji(r.eps_surprise_pct)
    lines = [
        f"📊 <b>{r.ticker}</b>  {_mexc_str(r.ticker)}  {emoji}",
        f"<b>EARNINGS RESULTS</b>  |  Period: {r.period}  |  Filed: {r.report_date}",
        f"<i>Currency: {r.currency}</i>", "",
        f"EPS Actual    <b>{_eps(r.eps_actual, r.currency)}</b>",
        f"EPS Estimate  {_eps(r.eps_estimate, r.currency)}",
        f"EPS Surprise  <b>{f'{pct:+.2f}%' if pct is not None else 'N/A'}</b>  {emoji}", "",
        f"Revenue       {_money(r.revenue, r.currency)}",
        f"Net Income    {_money(r.net_income, r.currency)}",
        f"Gross Profit  {_money(r.gross_profit, r.currency)}",
    ]
    if pct is not None:
        if pct >= 5:    hint = "📈 Strong beat — consider LONG on MEXC"
        elif pct <= -5: hint = "📉 Big miss — consider SHORT on MEXC"
        else:           hint = "➡️ In-line — wait for price reaction"
        lines += ["", f"💡 <i>{hint}</i>"]
    return "\n".join(lines)

def fmt_calendar(items: list[dict]) -> str:
    if not items:
        return "📅 No earnings scheduled in the next 14 days."
    lines = ["📅 <b>Upcoming Earnings — Next 14 Days</b>\n"]
    prev_date = None
    for item in items:
        if item["date"] != prev_date:
            lines.append(f"\n<b>── {item['date']} ──</b>")
            prev_date = item["date"]
        days = item["days_until"]
        badge = "📍 <b>TODAY</b>" if days == 0 else "🔔 <b>TOMORROW</b>" if days == 1 else f"in {days}d"
        mexc = "  ".join(f"<code>{s}</code>" for s in item.get("mexc_symbols", []))
        et = _fmt_time(item.get("exact_time"))
        lines.append(
            f"• <b>{item['ticker']}</b>  {mexc}  {badge}\n"
            f"  🕐 {et}  |  {_time_label(item.get('report_time', ''))}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# API WRAPPERS  — single choke points with caching
# ══════════════════════════════════════════════════════════════

_fh_client = finnhub.Client(api_key=FINNHUB_API_KEY)


async def _fh_fetch(ticker: str, fn) -> any:
    """All Finnhub calls go through here. 2s throttle. Returns None on 429."""
    await asyncio.sleep(FH_DELAY)
    try:
        return await asyncio.to_thread(fn)
    except Exception as e:
        if "429" in str(e):
            logger.warning(f"Finnhub 429 [{ticker}]")
        else:
            logger.warning(f"Finnhub [{ticker}]: {e}")
        return None


async def fh_calendar(ticker: str, days_ahead: int = 90) -> list[dict]:
    cached = await get_cache(f"fh_cal:{ticker}")
    if cached:
        try: return json.loads(cached)
        except: pass
    def _call():
        today = datetime.now(timezone.utc)
        d = _fh_client.earnings_calendar(
            _from=today.strftime("%Y-%m-%d"),
            to=(today + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
            symbol=ticker, international=False,
        )
        return d.get("earningsCalendar", []) if d else []
    result = await _fh_fetch(ticker, _call)
    entries = result if isinstance(result, list) else []
    if entries:
        await set_cache(f"fh_cal:{ticker}", json.dumps(entries), ttl=FH_CAL_CACHE_TTL)
    return entries


async def fh_surprises(ticker: str) -> list[dict]:
    cached = await get_cache(f"fh_surp:{ticker}")
    if cached:
        try: return json.loads(cached)
        except: pass
    result = await _fh_fetch(ticker,
                              lambda: _fh_client.company_earnings(ticker, limit=4) or [])
    entries = result if isinstance(result, list) else []
    if entries:
        await set_cache(f"fh_surp:{ticker}", json.dumps(entries), ttl=14400)
    return entries


async def _yf_fetch(ticker: str, fn) -> any:
    """All yfinance calls go through here. 2s throttle. Returns None on 429."""
    await asyncio.sleep(YF_DELAY)
    try:
        return await asyncio.to_thread(fn)
    except Exception as e:
        if "429" in str(e):
            logger.warning(f"yfinance 429 [{ticker}]")
        else:
            logger.warning(f"yfinance [{ticker}]: {e}")
        return None


async def yf_currency(ticker: str) -> str:
    cached = await get_cache(f"currency:{ticker}")
    if cached:
        return cached
    result = await _yf_fetch(ticker, lambda: (
        yf.Ticker(ticker).info.get("financialCurrency") or
        yf.Ticker(ticker).info.get("currency") or "USD"
    ))
    c = result if isinstance(result, str) else "USD"
    await set_cache(f"currency:{ticker}", c, ttl=86400)
    return c


async def yf_financials(ticker: str) -> dict:
    cached = await get_cache(f"financials:{ticker}")
    if cached:
        try: return json.loads(cached)
        except: pass
    def _call():
        stock = yf.Ticker(ticker)
        qs = stock.quarterly_income_stmt
        if qs is None or qs.empty: return {}
        latest = qs.iloc[:, 0]
        def safe(kw):
            for label in latest.index:
                if kw in label.lower():
                    v = latest[label]
                    if v is not None and v == v: return float(v)
            return None
        info = yf.Ticker(ticker).info
        return {
            "revenue":      safe("total revenue"),
            "net_income":   safe("net income"),
            "gross_profit": safe("gross profit"),
            "currency":     info.get("financialCurrency") or info.get("currency") or "USD",
        }
    result = await _yf_fetch(ticker, _call)
    if result:
        await set_cache(f"financials:{ticker}", json.dumps(result), ttl=43200)
    return result or {}


async def yf_earnings_dates(ticker: str) -> list[str]:
    cached = await get_cache(f"yf_dates:{ticker}")
    if cached:
        try: return json.loads(cached)
        except: pass
    def _call():
        dates, today = [], datetime.now(timezone.utc).date()
        ed = yf.Ticker(ticker).earnings_dates
        if ed is None or ed.empty: return dates
        for idx in ed.index:
            try: d = idx.astimezone(ET).date()
            except: d = idx.date()
            if d >= today: dates.append(str(d))
        return dates
    result = await _yf_fetch(ticker, _call)
    dates = result if isinstance(result, list) else []
    await set_cache(f"yf_dates:{ticker}", json.dumps(dates), ttl=14400)
    return dates


async def yf_exact_time(ticker: str, date_str: str) -> Optional[datetime]:
    cached = await get_cache(f"exact_time:{ticker}:{date_str}")
    if cached:
        try: return datetime.fromisoformat(cached) if cached != "null" else None
        except: pass
    def _call():
        ed = yf.Ticker(ticker).earnings_dates
        if ed is None or ed.empty: return None
        for idx in ed.index:
            try: idx_et = idx.astimezone(ET)
            except: idx_et = idx
            if str(idx_et.date()) == date_str:
                return idx.astimezone(timezone.utc)
        return None
    result = await _yf_fetch(ticker, _call)
    await set_cache(f"exact_time:{ticker}:{date_str}",
                    result.isoformat() if result else "null", ttl=14400)
    return result


def _parse_date(s: str):
    try: return datetime.strptime(s, "%Y-%m-%d").date()
    except: return None

def _derive_time(date_str: str, code: str) -> Optional[datetime]:
    mapping = {"bmo": (8, 0), "amc": (16, 5)}
    if not code or code.lower() not in mapping: return None
    try:
        h, m = mapping[code.lower()]
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).astimezone(timezone.utc)
    except: return None


async def get_earnings_info(ticker: str) -> Optional[dict]:
    """
    Cross-reference Finnhub + Yahoo to get the most up-to-date date.
    Takes the LATER date if they disagree (reschedules always push forward).
    """
    today = datetime.now(timezone.utc).date()

    fh_entries = await fh_calendar(ticker)
    fh_future = [e for e in fh_entries if _parse_date(e.get("date", "")) and
                 _parse_date(e["date"]) >= today]
    fh_date = sorted(fh_future, key=lambda e: e["date"])[0]["date"] if fh_future else None
    fh_entry = sorted(fh_future, key=lambda e: e["date"])[0] if fh_future else None

    yf_dates = await yf_earnings_dates(ticker)
    yf_date = yf_dates[0] if yf_dates else None

    if fh_date and yf_date:
        confirmed = max(fh_date, yf_date)
        if fh_date != yf_date:
            logger.info(f"Date mismatch [{ticker}]: FH={fh_date} YF={yf_date} → {confirmed}")
    elif fh_date:
        confirmed = fh_date
    elif yf_date:
        confirmed = yf_date
    else:
        return None

    report_time = eps_est = rev_est = None
    if fh_entry:
        if fh_entry["date"] == confirmed:
            report_time = fh_entry.get("hour", "")
            eps_est     = fh_entry.get("epsEstimate")
            rev_est     = fh_entry.get("revenueEstimate")
        else:
            match = [e for e in fh_entries if e.get("date") == confirmed]
            if match:
                report_time = match[0].get("hour", "")
                eps_est     = match[0].get("epsEstimate")
                rev_est     = match[0].get("revenueEstimate")

    exact_time = await yf_exact_time(ticker, confirmed)
    if exact_time is None:
        exact_time = _derive_time(confirmed, report_time or "")

    return {
        "date": confirmed, "report_time": report_time or "",
        "exact_time": exact_time, "eps_estimate": eps_est, "revenue_estimate": rev_est,
    }


# ══════════════════════════════════════════════════════════════
# EARNINGS JOBS
# ══════════════════════════════════════════════════════════════

async def check_upcoming_earnings() -> list[UpcomingEarnings]:
    alerts, today = [], datetime.now(timezone.utc).date()
    for ticker in EQUITY_TICKERS:
        info = await get_earnings_info(ticker)
        if not info: continue
        d = _parse_date(info["date"])
        if not d: continue
        days_until = (d - today).days
        currency = await yf_currency(ticker)
        for milestone in PRE_EARNINGS_ALERT_DAYS:
            if days_until != milestone: continue
            alert = UpcomingEarnings(
                ticker=ticker, earnings_date=info["date"], days_until=days_until,
                eps_estimate=info.get("eps_estimate"),
                revenue_estimate=info.get("revenue_estimate"),
                report_time=info.get("report_time", ""), milestone=milestone,
                exact_time=info.get("exact_time"), currency=currency,
            )
            if await is_seen("pre_earnings", alert.uid): continue
            await mark_seen("pre_earnings", alert.uid)
            alerts.append(alert)
    return alerts


async def check_intraday_reminders() -> list[IntradayReminder]:
    reminders, now_utc, today = [], datetime.now(timezone.utc), datetime.now(timezone.utc).date()
    for ticker in EQUITY_TICKERS:
        info = await get_earnings_info(ticker)
        if not info or _parse_date(info["date"]) != today: continue
        exact_time = info.get("exact_time")
        if exact_time is None: continue
        mins = (exact_time - now_utc).total_seconds() / 60
        if mins < 0: continue
        currency = await yf_currency(ticker)
        for window in INTRADAY_MINUTES:
            if window <= mins < window + 5:
                r = IntradayReminder(
                    ticker=ticker, earnings_date=info["date"], exact_time=exact_time,
                    minutes_before=window, eps_estimate=info.get("eps_estimate"),
                    revenue_estimate=info.get("revenue_estimate"),
                    report_time=info.get("report_time", ""), currency=currency,
                )
                if await is_seen("intraday", r.uid): continue
                await mark_seen("intraday", r.uid)
                reminders.append(r)
    return reminders


async def check_new_earnings() -> list[EarningsReport]:
    reports, cutoff = [], datetime.now(timezone.utc) - timedelta(days=EARNINGS_LOOKBACK_DAYS)
    for ticker in EQUITY_TICKERS:
        for entry in await fh_surprises(ticker):
            rd, period = entry.get("date", ""), entry.get("period", "")
            try:
                if datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
                    continue
            except ValueError:
                continue
            r = EarningsReport(
                ticker=ticker, period=period, report_date=rd,
                eps_actual=entry.get("actual"), eps_estimate=entry.get("estimate"),
                eps_surprise_pct=entry.get("surprisePercent"),
                revenue=None, net_income=None, gross_profit=None,
            )
            if await is_seen("earnings", r.uid): continue
            fin = await yf_financials(ticker)
            r.revenue = fin.get("revenue"); r.net_income = fin.get("net_income")
            r.gross_profit = fin.get("gross_profit"); r.currency = fin.get("currency", "USD")
            await mark_seen("earnings", r.uid)
            reports.append(r)
    return reports


CALENDAR_KEY = "earnings_calendar_14d"
CALENDAR_HASH_KEY = "earnings_calendar_hash"

async def fetch_calendar(days_ahead: int = 14) -> list[dict]:
    """
    Read from cache first (instant). Falls back to live fetch only on cache miss.
    """
    cached = await get_cache(CALENDAR_KEY)
    if cached:
        try:
            today = datetime.now(timezone.utc).date()
            data = json.loads(cached)
            for item in data:
                d = _parse_date(item["date"])
                item["days_until"] = (d - today).days if d else 0
                if item.get("exact_time_iso"):
                    try: item["exact_time"] = datetime.fromisoformat(item["exact_time_iso"])
                    except: item["exact_time"] = None
            return [i for i in data if 0 <= i["days_until"] <= days_ahead]
        except Exception as e:
            logger.warning(f"Calendar cache parse error: {e}")
    return await refresh_calendar(days_ahead)


async def refresh_calendar(days_ahead: int = 14) -> list[dict]:
    """
    Fetch from Finnhub. Merges with previous snapshot so 429'd tickers
    keep their last known data instead of disappearing from the calendar.
    """
    today = datetime.now(timezone.utc).date()

    # Load previous snapshot
    prev: dict[str, dict] = {}
    cached = await get_cache(CALENDAR_KEY)
    if cached:
        try:
            for item in json.loads(cached): prev[item["ticker"]] = item
        except: pass

    fresh: dict[str, dict] = {}
    for ticker in EQUITY_TICKERS:
        for entry in await fh_calendar(ticker, days_ahead=days_ahead):
            d = _parse_date(entry.get("date", ""))
            if not d: continue
            days_until = (d - today).days
            if 0 <= days_until <= days_ahead:
                et = _derive_time(entry.get("date", ""), entry.get("hour", ""))
                fresh[ticker] = {
                    "ticker": ticker, "date": entry.get("date", ""),
                    "days_until": days_until,
                    "eps_estimate": entry.get("epsEstimate"),
                    "revenue_estimate": entry.get("revenueEstimate"),
                    "report_time": entry.get("hour", ""),
                    "exact_time": et,
                    "exact_time_iso": et.isoformat() if et else None,
                    "mexc_symbols": TICKER_TO_MEXC.get(ticker, []),
                }

    # Merge fresh + fallback to prev for any 429'd ticker
    merged: dict[str, dict] = {}
    for ticker in EQUITY_TICKERS:
        if ticker in fresh:
            merged[ticker] = fresh[ticker]
        elif ticker in prev:
            item = dict(prev[ticker])
            d = _parse_date(item.get("date", ""))
            if d:
                item["days_until"] = (d - today).days
                if 0 <= item["days_until"] <= days_ahead:
                    merged[ticker] = item

    results = sorted(merged.values(), key=lambda x: x["date"])
    coverage = len(fresh) / max(len(EQUITY_TICKERS), 1)

    if coverage >= 0.5:
        await set_cache(CALENDAR_KEY, json.dumps(results, default=str), ttl=CALENDAR_CACHE_TTL)
        logger.info(f"Calendar: {len(fresh)} fresh, {len(merged)-len(fresh)} from cache")
    else:
        logger.warning(f"Calendar coverage {coverage:.0%} — keeping previous cache")

    return results


# ══════════════════════════════════════════════════════════════
# BOT + SCHEDULER
# ══════════════════════════════════════════════════════════════

bot = Bot(token=TELEGRAM_BOT_TOKEN,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
_last: dict = {"pre": None, "earnings": None}
CATEGORIES = ("pre", "earnings")


async def _muted(cat: str) -> bool:
    return await get_setting(f"mute_{cat}") == "1"


async def _send(text: str, pin: bool = False) -> Optional[int]:
    """Send to the configured chat/thread. Returns message_id."""
    kwargs: dict = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if TELEGRAM_THREAD_ID:
        kwargs["message_thread_id"] = TELEGRAM_THREAD_ID
    msg = await bot.send_message(**kwargs)
    if pin and msg:
        try:
            await bot.pin_chat_message(
                chat_id=TELEGRAM_CHAT_ID,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as e:
            logger.warning(f"Pin failed: {e}")
    return msg.message_id if msg else None


async def _pin_calendar_if_changed(data: list[dict]) -> None:
    """
    Only pins when actual earnings data changed (new ticker, date shift, time change).
    Ignores days_until — that changes every day and would cause re-pin spam.
    """
    stable = json.dumps(
        [{"ticker": i["ticker"], "date": i["date"],
          "report_time": i.get("report_time", ""),
          "exact_time_iso": i.get("exact_time_iso")}
         for i in data],
        sort_keys=True
    )
    new_hash = hashlib.md5(stable.encode()).hexdigest()
    old_hash = await get_setting("pinned_calendar_hash")

    if new_hash == old_hash:
        logger.info("Calendar unchanged — skipping re-pin")
        return

    # Unpin previous message
    old_id = await get_setting("pinned_calendar_msg_id")
    if old_id:
        try:
            await bot.unpin_chat_message(chat_id=TELEGRAM_CHAT_ID, message_id=int(old_id))
        except Exception:
            pass

    new_id = await _send(text, pin=True)
    if new_id:
        await set_setting("pinned_calendar_msg_id", str(new_id))
        await set_setting("pinned_calendar_hash", new_hash)
        logger.info(f"Calendar changed — pinned new message {new_id}")


# ── Scheduler jobs ────────────────────────────────────────────

async def news_job() -> None:
    pass  # removed — earnings only


async def intraday_job() -> None:
    if await _muted("pre"): return
    try:
        for r in await check_intraday_reminders():
            try:
                await _send(fmt_intraday(r))
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.warning(f"Send intraday [{r.ticker}]: {e}")
    except Exception as e:
        logger.error(f"intraday_job: {e}", exc_info=True)


async def pre_earnings_job() -> None:
    if await _muted("pre"): return
    try:
        alerts = await check_upcoming_earnings()
        for a in sorted(alerts, key=lambda x: x.milestone):
            try:
                await _send(fmt_pre_earnings(a))
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.warning(f"Send pre-earnings [{a.ticker}]: {e}")
        _last["pre"] = datetime.now(timezone.utc)
        # Refresh calendar and pin only if something changed
        data = await refresh_calendar()
        await _pin_calendar_if_changed(data)
    except Exception as e:
        logger.error(f"pre_earnings_job: {e}", exc_info=True)


async def earnings_job() -> None:
    if await _muted("earnings"): return
    try:
        for r in await check_new_earnings():
            try:
                await _send(fmt_earnings(r))
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.warning(f"Send earnings [{r.ticker}]: {e}")
        _last["earnings"] = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"earnings_job: {e}", exc_info=True)


# ── Commands ──────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>MEXC Earnings & News Bot</b>\n\n"
        f"Watching <b>{len(ALL_TICKERS)}</b> stocks.\n"
        "news=5min  |  earnings=4h\n\n"
        "/help — commands"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📋 <b>Commands</b>\n\n"
        "/upcoming       — earnings calendar (14 days)\n"
        "/price <i>SYMBOL</i> — live MEXC price  e.g. /price NVDAUSDT\n"
        "/tickers        — full watchlist\n"
        "/status         — bot health\n"
        "/settings       — mute state\n"
        "/mute pre|earnings\n"
        "/unmute pre|earnings"
    )

@dp.message(Command("upcoming"))
async def cmd_upcoming(message: Message) -> None:
    data = await fetch_calendar(days_ahead=14)
    await _pin_calendar_if_changed(data)

@dp.message(Command("price"))
async def cmd_price(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /price <b>SYMBOL</b>\nExample: /price NVDAUSDT")
        return
    symbol = parts[1].upper().strip()
    if symbol not in MEXC_TO_TICKER:
        await message.answer(f"❌ <code>{symbol}</code> not in watchlist.")
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(MEXC_TICKER_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                api_sym = symbol.replace("USDT", "") + "_USDT"
                ticker_data = next(
                    (t for t in data.get("data", []) if t.get("symbol") == api_sym), None
                )
        if not ticker_data:
            await message.answer(f"⚠️ No price data for <code>{symbol}</code>.")
            return
        d = ticker_data
        pct = d.get("priceChangePct")
        arrow = "📈" if pct and float(pct) >= 0 else "📉"
        await message.answer(
            f"💹 <b>MEXC: {symbol}</b>\n"
            f"Price: <b>${d.get('lastPrice','N/A')}</b>  {arrow} "
            f"{float(pct)*100:+.2f}% (24h)\n" if pct else
            f"💹 <b>MEXC: {symbol}</b>\nPrice: <b>${d.get('lastPrice','N/A')}</b>"
        )
    except Exception as e:
        await message.answer(f"⚠️ Error fetching price: {e}")

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
    await set_setting(f"mute_{cat}", "1")
    await message.answer(f"🔇 <b>{cat}</b> alerts muted.")

@dp.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in CATEGORIES:
        await message.answer(f"Usage: /unmute [{'|'.join(CATEGORIES)}]")
        return
    cat = parts[1].lower()
    await set_setting(f"mute_{cat}", "0")
    await message.answer(f"🔔 <b>{cat}</b> alerts resumed.")

@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    lines = ["⚙️ <b>Settings</b>\n"]
    for cat in CATEGORIES:
        icon = "🔇 MUTED" if await _muted(cat) else "🔔 active"
        lines.append(f"  {cat:<12} {icon}")
    await message.answer("\n".join(lines))

@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    def fmt(dt): return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "Not yet run"
    ok = await redis_ping()
    await message.answer(
        "🤖 <b>Bot Status</b>\n\n"
        f"✅ Railway\n{'✅' if ok else '❌'} Upstash Redis\n\n"
        f"⭐ Pre-earnings: {fmt(_last['pre'])}\n"
        f"📊 Results:      {fmt(_last['earnings'])}\n\n"
        f"👀 {len(ALL_TICKERS)} tickers"
    )


# ── Entry point ───────────────────────────────────────────────

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN: raise ValueError("TELEGRAM_BOT_TOKEN not set")
    if not TELEGRAM_CHAT_ID:   raise ValueError("TELEGRAM_CHAT_ID not set")

    now = datetime.now(timezone.utc)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(intraday_job,     "interval", minutes=5,
                      start_date=now + timedelta(minutes=1))
    scheduler.add_job(pre_earnings_job, "interval", hours=1,
                      start_date=now + timedelta(minutes=2))
    scheduler.add_job(earnings_job,     "interval", hours=4,
                      start_date=now + timedelta(minutes=3))
    scheduler.start()
    logger.info("Scheduler started")

    ok = await redis_ping()
    await _send(
        "🚀 <b>MEXC Earnings Bot online!</b>\n"
        f"Railway ✅  Upstash {'✅' if ok else '❌'}\n"
        f"{len(ALL_TICKERS)} tickers  |  intraday=5min  pre-earnings=1h  results=4h\n"
        "/help for commands"
    )

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        try: await _send("🛑 Bot shutting down.")
        except: pass
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
