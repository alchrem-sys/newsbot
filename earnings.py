"""
earnings.py — fully async.

All Finnhub/yfinance calls run via asyncio.to_thread().
Rate limiting uses asyncio.sleep() not time.sleep() so the event loop
stays free for bot commands during every API wait.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import finnhub
import yfinance as yf

_FINNHUB_DELAY = 2.0
ET = ZoneInfo("America/New_York")

from config import FINNHUB_API_KEY, EARNINGS_LOOKBACK_DAYS, PRE_EARNINGS_ALERT_DAYS
from models import EarningsReport, IntradayReminder, UpcomingEarnings
from storage import is_seen, mark_seen, set_cache, get_cache
from tickers import EQUITY_TICKERS, TICKER_TO_MEXC

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)

INTRADAY_MINUTES = [120, 60, 30, 5, 1]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _derive_time(date_str: str, code: str) -> Optional[datetime]:
    mapping = {"bmo": (8, 0), "amc": (16, 5)}
    if code.lower() not in mapping:
        return None
    try:
        h, m = mapping[code.lower()]
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).astimezone(timezone.utc)
    except Exception:
        return None


# ─── Async Finnhub wrappers ───────────────────────────────────────────────────

# ─── Async Finnhub wrappers ───────────────────────────────────────────────────

async def _fh_fetch(ticker: str, fetch_fn) -> any:
    """
    Single choke point for ALL Finnhub calls.
    2s sleep between calls — free tier allows 30 calls/min.
    Returns None on 429 so callers fall back to cached data.
    """
    await asyncio.sleep(_FINNHUB_DELAY)
    try:
        return await asyncio.to_thread(fetch_fn)
    except Exception as e:
        if "429" in str(e):
            logger.warning(f"Finnhub 429 [{ticker}] — using cached data")
        else:
            logger.warning(f"Finnhub [{ticker}]: {e}")
        return None


async def _fh_calendar(ticker: str, days_ahead: int = 90) -> list[dict]:
    cache_key = f"fh_cal:{ticker}"
    cached = get_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    def _call():
        today = datetime.now(timezone.utc)
        data = _fh.earnings_calendar(
            _from=today.strftime("%Y-%m-%d"),
            to=(today + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
            symbol=ticker, international=False,
        )
        return data.get("earningsCalendar", []) if data else []

    result = await _fh_fetch(ticker, _call)
    entries = result if isinstance(result, list) else []
    if entries:  # only cache non-empty — empty may mean 429, not truly no data
        set_cache(cache_key, json.dumps(entries), ttl_seconds=14400)  # 4h
    return entries


async def _fh_surprises(ticker: str) -> list[dict]:
    cache_key = f"fh_surp:{ticker}"
    cached = get_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    result = await _fh_fetch(ticker, lambda: _fh.company_earnings(ticker, limit=4) or [])
    entries = result if isinstance(result, list) else []
    if entries:
        set_cache(cache_key, json.dumps(entries), ttl_seconds=14400)  # 4h
    return entries


async def _yf_fetch(ticker: str, fetch_fn) -> any:
    """
    Single choke point for ALL yfinance calls.
    2s sleep before every network call (Yahoo allows ~1 req/2s).
    Returns None on 429 or any error — callers fall back to cache/default.
    """
    await asyncio.sleep(2.0)
    try:
        return await asyncio.to_thread(fetch_fn)
    except Exception as e:
        if "429" in str(e):
            logger.warning(f"yfinance 429 [{ticker}] — will use cached/default value")
        else:
            logger.warning(f"yfinance [{ticker}]: {e}")
        return None


async def _yf_currency(ticker: str) -> str:
    cache_key = f"currency:{ticker}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    result = await _yf_fetch(ticker, lambda: (
        yf.Ticker(ticker).info.get("financialCurrency") or
        yf.Ticker(ticker).info.get("currency") or "USD"
    ))
    currency = result if isinstance(result, str) else "USD"
    set_cache(cache_key, currency, ttl_seconds=86400)   # 24h
    return currency


async def _yf_financials(ticker: str) -> dict:
    cache_key = f"financials:{ticker}"
    cached = get_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    def _call():
        stock = yf.Ticker(ticker)
        qs = stock.quarterly_income_stmt
        if qs is None or qs.empty:
            return {}
        latest = qs.iloc[:, 0]
        def safe(kw):
            for label in latest.index:
                if kw in label.lower():
                    val = latest[label]
                    if val is not None and val == val:
                        return float(val)
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
        set_cache(cache_key, json.dumps(result), ttl_seconds=43200)  # 12h
    return result or {}


async def _yf_earnings_dates(ticker: str) -> list[str]:
    cache_key = f"yf_dates:{ticker}"
    cached = get_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    def _call():
        dates = []
        ed = yf.Ticker(ticker).earnings_dates
        if ed is None or ed.empty:
            return dates
        today = datetime.now(timezone.utc).date()
        for idx in ed.index:
            try:
                d = idx.astimezone(ET).date()
            except Exception:
                d = idx.date()
            if d >= today:
                dates.append(str(d))
        return dates

    result = await _yf_fetch(ticker, _call)
    dates = result if isinstance(result, list) else []
    set_cache(cache_key, json.dumps(dates), ttl_seconds=14400)  # 4h
    return dates


async def _yf_exact_time_for_date(ticker: str, date_str: str) -> Optional[datetime]:
    cache_key = f"exact_time:{ticker}:{date_str}"
    cached = get_cache(cache_key)
    if cached:
        try:
            return datetime.fromisoformat(cached) if cached != "null" else None
        except Exception:
            pass

    def _call():
        ed = yf.Ticker(ticker).earnings_dates
        if ed is None or ed.empty:
            return None
        for idx in ed.index:
            try:
                idx_et = idx.astimezone(ET)
            except Exception:
                idx_et = idx
            if str(idx_et.date()) == date_str:
                return idx.astimezone(timezone.utc)
        return None

    result = await _yf_fetch(ticker, _call)
    set_cache(cache_key, result.isoformat() if result else "null", ttl_seconds=14400)  # 4h
    return result


async def _get_earnings_info(ticker: str) -> Optional[dict]:
    """
    Fetch the most up-to-date earnings date by cross-referencing TWO independent
    data providers: Finnhub and Yahoo Finance (via yfinance).

    The NKE problem explained:
      Finnhub may still cache the old date (Mar 18) for a day or two after a
      reschedule. yfinance (Yahoo Finance) often refreshes faster.
      If the two sources disagree, we take the LATER date — reschedules always
      push the date forward, never backward, so the later date is always correct.

    Logic:
      1. Fetch Finnhub calendar  → gets nearest future date + bmo/amc + estimates
      2. Fetch yfinance dates     → independent source, separate data provider
      3. If they agree            → confirmed, use it
      4. If they disagree         → take the LATER date (that's the rescheduled one)
      5. For exact time           → yfinance earnings_dates if available,
                                    else derive from bmo/amc convention
    """
    today = datetime.now(timezone.utc).date()

    # ── Step 1: Finnhub ───────────────────────────────────────────────────────
    fh_entries = await _fh_calendar(ticker, days_ahead=90)
    fh_future = [
        e for e in fh_entries
        if _parse_date(e.get("date", "")) and _parse_date(e["date"]) >= today
    ]
    fh_date: Optional[str] = None
    fh_entry: Optional[dict] = None
    if fh_future:
        fh_entry = sorted(fh_future, key=lambda e: e["date"])[0]
        fh_date  = fh_entry["date"]

    # ── Step 2: yfinance (Yahoo Finance) ─────────────────────────────────────
    yf_dates = await _yf_earnings_dates(ticker)
    yf_date: Optional[str] = yf_dates[0] if yf_dates else None

    # ── Step 3: Resolve the confirmed date ───────────────────────────────────
    if fh_date and yf_date:
        # Both sources have data — take the LATER one
        # Reschedules always push forward, so the later date is always the correct one
        confirmed_date = max(fh_date, yf_date)
        if fh_date != yf_date:
            logger.info(
                f"Date mismatch [{ticker}]: Finnhub={fh_date} Yahoo={yf_date} "
                f"→ using {confirmed_date}"
            )
    elif fh_date:
        confirmed_date = fh_date   # Only Finnhub available
    elif yf_date:
        confirmed_date = yf_date   # Only Yahoo available
    else:
        return None                # Neither source has data

    # ── Step 4: Get report time code (bmo/amc) from Finnhub if available ─────
    # Even if we used yfinance's later date, try to get bmo/amc from Finnhub
    report_time = ""
    eps_estimate = None
    revenue_estimate = None
    if fh_entry:
        # If Finnhub entry matches confirmed date, use its metadata
        if fh_entry["date"] == confirmed_date:
            report_time      = fh_entry.get("hour", "")
            eps_estimate     = fh_entry.get("epsEstimate")
            revenue_estimate = fh_entry.get("revenueEstimate")
        else:
            # Finnhub had a stale date — look for a matching entry at confirmed_date
            matching = [e for e in fh_entries if e.get("date") == confirmed_date]
            if matching:
                report_time      = matching[0].get("hour", "")
                eps_estimate     = matching[0].get("epsEstimate")
                revenue_estimate = matching[0].get("revenueEstimate")

    # ── Step 5: Get exact time ────────────────────────────────────────────────
    exact_time = await _yf_exact_time_for_date(ticker, confirmed_date)
    if exact_time is None:
        # Fall back to convention: bmo=08:00 ET, amc=16:05 ET
        exact_time = _derive_time(confirmed_date, report_time)

    return {
        "date":             confirmed_date,
        "report_time":      report_time,
        "exact_time":       exact_time,
        "eps_estimate":     eps_estimate,
        "revenue_estimate": revenue_estimate,
    }


# ─── Job 1: Day-level pre-earnings ───────────────────────────────────────────

async def check_upcoming_earnings_alerts() -> list[UpcomingEarnings]:
    alerts: list[UpcomingEarnings] = []
    today = datetime.now(timezone.utc).date()

    for ticker in EQUITY_TICKERS:
        info = await _get_earnings_info(ticker)
        if not info:
            continue
        earnings_date = _parse_date(info["date"])
        if not earnings_date:
            continue
        days_until = (earnings_date - today).days
        currency = await _yf_currency(ticker)

        for milestone in PRE_EARNINGS_ALERT_DAYS:
            if days_until != milestone:
                continue
            alert = UpcomingEarnings(
                ticker=ticker, earnings_date=info["date"], days_until=days_until,
                eps_estimate=info.get("eps_estimate"),
                revenue_estimate=info.get("revenue_estimate"),
                report_time=info.get("report_time", ""), milestone=milestone,
                exact_time=info.get("exact_time"), currency=currency,
            )
            if is_seen("pre_earnings", alert.uid):
                continue
            mark_seen("pre_earnings", alert.uid)
            alerts.append(alert)
            logger.info(f"Pre-earnings: {ticker} in {milestone}d ({info['date']})")

    return alerts


# ─── Job 2: Intraday reminders ────────────────────────────────────────────────

async def check_intraday_reminders() -> list[IntradayReminder]:
    reminders: list[IntradayReminder] = []
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    for ticker in EQUITY_TICKERS:
        info = await _get_earnings_info(ticker)
        if not info or _parse_date(info["date"]) != today:
            continue
        exact_time = info.get("exact_time")
        if exact_time is None:
            continue
        minutes_to_go = (exact_time - now_utc).total_seconds() / 60
        if minutes_to_go < 0:
            continue
        currency = await _yf_currency(ticker)

        for window in INTRADAY_MINUTES:
            if window <= minutes_to_go < window + 5:
                reminder = IntradayReminder(
                    ticker=ticker, earnings_date=info["date"], exact_time=exact_time,
                    minutes_before=window, eps_estimate=info.get("eps_estimate"),
                    revenue_estimate=info.get("revenue_estimate"),
                    report_time=info.get("report_time", ""), currency=currency,
                )
                if is_seen("intraday", reminder.uid):
                    continue
                mark_seen("intraday", reminder.uid)
                reminders.append(reminder)
                logger.info(f"Intraday: {ticker} in {window}min")

    return reminders


# ─── Job 3: Post-earnings results ─────────────────────────────────────────────

async def check_new_earnings() -> list[EarningsReport]:
    reports: list[EarningsReport] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=EARNINGS_LOOKBACK_DAYS)

    for ticker in EQUITY_TICKERS:
        for entry in await _fh_surprises(ticker):
            report_date = entry.get("date", "")
            period = entry.get("period", "")
            try:
                if datetime.strptime(report_date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc) < cutoff:
                    continue
            except ValueError:
                continue

            r = EarningsReport(
                ticker=ticker, period=period, report_date=report_date,
                eps_actual=entry.get("actual"), eps_estimate=entry.get("estimate"),
                eps_surprise_pct=entry.get("surprisePercent"),
                revenue=None, net_income=None, gross_profit=None,
            )
            if is_seen("earnings", r.uid):
                continue

            fin = await _yf_financials(ticker)
            r.revenue      = fin.get("revenue")
            r.net_income   = fin.get("net_income")
            r.gross_profit = fin.get("gross_profit")
            r.currency     = fin.get("currency", "USD")

            mark_seen("earnings", r.uid)
            reports.append(r)
            logger.info(f"Earnings result: {ticker} period={period}")

    return reports


# ─── Full calendar (/upcoming command) ───────────────────────────────────────

CALENDAR_CACHE_KEY = "earnings_calendar_14d"
CALENDAR_CACHE_TTL = 60 * 60 * 5  # 5h — longer than the 4h job interval, merge handles freshness


async def get_full_earnings_calendar(days_ahead: int = 14) -> list[dict]:
    """
    Returns the earnings calendar for the next N days.

    Reads from Upstash cache first — response is instant (<100ms).
    Cache is written by pre_earnings_job() every 30 min so data is always fresh.
    Falls back to a live fetch only if the cache is empty (e.g. first boot).
    """

    # ── Try cache first ───────────────────────────────────────────────────────
    cached = get_cache(CALENDAR_CACHE_KEY)
    if cached:
        try:
            data = json.loads(cached)
            # Recompute days_until from today since cache may be a few minutes old
            today = datetime.now(timezone.utc).date()
            for item in data:
                d = _parse_date(item["date"])
                item["days_until"] = (d - today).days if d else 0
                # exact_time was stored as ISO string — restore to datetime
                if item.get("exact_time_iso"):
                    try:
                        item["exact_time"] = datetime.fromisoformat(item["exact_time_iso"])
                    except Exception:
                        item["exact_time"] = None
            return [i for i in data if 0 <= i["days_until"] <= days_ahead]
        except Exception as e:
            logger.warning(f"Cache parse error: {e} — falling back to live fetch")

    # ── Cache miss: live fetch (only happens on first boot) ───────────────────
    logger.info("Calendar cache miss — doing live fetch")
    return await _fetch_calendar_live(days_ahead)


async def _fetch_calendar_live(days_ahead: int = 14) -> list[dict]:
    """
    Live fetch from Finnhub. Merges new results with the previous cache snapshot
    so that tickers which got 429'd keep their last known data instead of
    disappearing from the calendar — preventing the "info is changing" problem.
    """
    today = datetime.now(timezone.utc).date()

    # Load previous snapshot keyed by ticker so we can fall back to it
    prev_by_ticker: dict[str, dict] = {}
    cached = get_cache(CALENDAR_CACHE_KEY)
    if cached:
        try:
            for item in json.loads(cached):
                prev_by_ticker[item["ticker"]] = item
        except Exception:
            pass

    # Fetch each ticker — collect which ones actually returned data
    fresh_by_ticker: dict[str, dict] = {}
    for ticker in EQUITY_TICKERS:
        entries = await _fh_calendar(ticker, days_ahead=days_ahead)
        for entry in entries:
            raw_date = entry.get("date", "")
            d = _parse_date(raw_date)
            if not d:
                continue
            days_until = (d - today).days
            if 0 <= days_until <= days_ahead:
                exact_time = _derive_time(raw_date, entry.get("hour", ""))
                fresh_by_ticker[ticker] = {
                    "ticker":           ticker,
                    "date":             raw_date,
                    "days_until":       days_until,
                    "eps_estimate":     entry.get("epsEstimate"),
                    "revenue_estimate": entry.get("revenueEstimate"),
                    "report_time":      entry.get("hour", ""),
                    "exact_time":       exact_time,
                    "exact_time_iso":   exact_time.isoformat() if exact_time else None,
                    "mexc_symbols":     TICKER_TO_MEXC.get(ticker, []),
                }

    # Merge: fresh data wins; fall back to previous for any ticker that got 429'd
    merged: dict[str, dict] = {}
    for ticker in EQUITY_TICKERS:
        if ticker in fresh_by_ticker:
            merged[ticker] = fresh_by_ticker[ticker]
        elif ticker in prev_by_ticker:
            # Keep previous entry but recompute days_until so it stays accurate
            item = dict(prev_by_ticker[ticker])
            d = _parse_date(item.get("date", ""))
            if d:
                item["days_until"] = (d - today).days
                if 0 <= item["days_until"] <= days_ahead:
                    merged[ticker] = item
                    logger.debug(f"Calendar fallback to cached data for {ticker}")

    results = sorted(merged.values(), key=lambda x: x["date"])

    # Only write to cache if we got a reasonably complete result
    # (at least 50% of equity tickers responded — protects against mass 429)
    coverage = len(fresh_by_ticker) / max(len(EQUITY_TICKERS), 1)
    if coverage >= 0.5:
        try:
            set_cache(CALENDAR_CACHE_KEY, json.dumps(results, default=str),
                      ttl_seconds=CALENDAR_CACHE_TTL)
            logger.info(f"Calendar cache updated: {len(fresh_by_ticker)} fresh, "
                        f"{len(merged) - len(fresh_by_ticker)} from prev cache")
        except Exception as e:
            logger.warning(f"Failed to write calendar cache: {e}")
    else:
        logger.warning(f"Calendar coverage only {coverage:.0%} — keeping previous cache")

    return results
