"""
earnings.py — fully async.

All Finnhub/yfinance calls run via asyncio.to_thread().
Rate limiting uses asyncio.sleep() not time.sleep() so the event loop
stays free for bot commands during every API wait.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import finnhub
import yfinance as yf

_FINNHUB_DELAY = 2.0
ET = ZoneInfo("America/New_York")

from config import FINNHUB_API_KEY, EARNINGS_LOOKBACK_DAYS, PRE_EARNINGS_ALERT_DAYS
from storage import is_seen, mark_seen
from tickers import EQUITY_TICKERS, TICKER_TO_MEXC

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)

INTRADAY_MINUTES = [120, 60, 30, 5, 1]


# ─── Data classes ─────────────────────────────────────────────────────────────

class UpcomingEarnings:
    def __init__(self, ticker: str, earnings_date: str, days_until: int,
                 eps_estimate: Optional[float], revenue_estimate: Optional[float],
                 report_time: str, milestone: int,
                 exact_time: Optional[datetime] = None, currency: str = "USD"):
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
    def uid(self) -> str:
        return hashlib.md5(
            f"pre_{self.ticker}_{self.earnings_date}_{self.milestone}".encode()
        ).hexdigest()


class IntradayReminder:
    def __init__(self, ticker: str, earnings_date: str, exact_time: datetime,
                 minutes_before: int, eps_estimate: Optional[float],
                 revenue_estimate: Optional[float], report_time: str,
                 currency: str = "USD"):
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
    def uid(self) -> str:
        return hashlib.md5(
            f"intraday_{self.ticker}_{self.earnings_date}_{self.minutes_before}".encode()
        ).hexdigest()


class EarningsReport:
    def __init__(self, ticker: str, period: str, report_date: str,
                 eps_actual: Optional[float], eps_estimate: Optional[float],
                 eps_surprise_pct: Optional[float], revenue: Optional[float],
                 net_income: Optional[float], gross_profit: Optional[float],
                 currency: str = "USD"):
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
    def uid(self) -> str:
        return hashlib.md5(
            f"result_{self.ticker}_{self.period}_{self.report_date}".encode()
        ).hexdigest()


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

async def _fh_calendar(ticker: str, days_ahead: int = 90) -> list[dict]:
    await asyncio.sleep(_FINNHUB_DELAY)
    def _call():
        today = datetime.now(timezone.utc)
        data = _fh.earnings_calendar(
            _from=today.strftime("%Y-%m-%d"),
            to=(today + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
            symbol=ticker, international=False,
        )
        return data.get("earningsCalendar", []) if data else []
    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.warning(f"Finnhub calendar [{ticker}]: {e}")
        return []


async def _fh_surprises(ticker: str) -> list[dict]:
    await asyncio.sleep(_FINNHUB_DELAY)
    def _call():
        return _fh.company_earnings(ticker, limit=4) or []
    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.warning(f"Finnhub surprises [{ticker}]: {e}")
        return []


async def _yf_currency(ticker: str) -> str:
    def _call():
        try:
            info = yf.Ticker(ticker).info
            return info.get("financialCurrency") or info.get("currency") or "USD"
        except Exception:
            return "USD"
    return await asyncio.to_thread(_call)


async def _yf_financials(ticker: str) -> dict:
    def _call():
        try:
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
            currency = info.get("financialCurrency") or info.get("currency") or "USD"
            return {
                "revenue":      safe("total revenue"),
                "net_income":   safe("net income"),
                "gross_profit": safe("gross profit"),
                "currency":     currency,
            }
        except Exception as e:
            logger.warning(f"yfinance financials [{ticker}]: {e}")
            return {}
    return await asyncio.to_thread(_call)


async def _yf_earnings_dates(ticker: str) -> list[str]:
    """
    Fetch upcoming earnings dates from yfinance earnings_dates DataFrame.
    Returns a list of date strings "YYYY-MM-DD", newest-first.
    This hits Yahoo Finance directly — a completely separate data provider
    from Finnhub, so the two can be cross-checked against each other.
    """
    def _call():
        dates = []
        try:
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
        except Exception:
            pass
        return dates
    return await asyncio.to_thread(_call)


async def _yf_exact_time_for_date(ticker: str, date_str: str) -> Optional[datetime]:
    """
    Return the exact tz-aware UTC time from yfinance for a specific date.
    Returns None if yfinance has no time data for that date.
    """
    def _call():
        try:
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
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_call)


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

async def get_full_earnings_calendar(days_ahead: int = 14) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    results = []
    for ticker in EQUITY_TICKERS:
        for entry in await _fh_calendar(ticker, days_ahead=days_ahead):
            raw_date = entry.get("date", "")
            d = _parse_date(raw_date)
            if not d:
                continue
            days_until = (d - today).days
            if 0 <= days_until <= days_ahead:
                results.append({
                    "ticker":           ticker,
                    "date":             raw_date,
                    "days_until":       days_until,
                    "eps_estimate":     entry.get("epsEstimate"),
                    "revenue_estimate": entry.get("revenueEstimate"),
                    "report_time":      entry.get("hour", ""),
                    "exact_time":       _derive_time(raw_date, entry.get("hour", "")),
                    "mexc_symbols":     TICKER_TO_MEXC.get(ticker, []),
                })
    return sorted(results, key=lambda x: x["date"])
