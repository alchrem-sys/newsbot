"""
earnings.py

THREE jobs:
  1. check_upcoming_earnings_alerts()
     Day-level milestones: 7d / 3d / 1d / day-of

  2. check_intraday_reminders()
     Minute-level reminders: 2h / 1h / 30min / 5min / 1min before exact time

  3. check_new_earnings()
     Full results after announcement (EPS actual vs estimate, revenue, etc.)

Fixes:
  - Exact announcement time from yfinance earnings_dates (tz-aware)
  - Dates cross-referenced: Finnhub calendar (authoritative) + yfinance (exact time)
    avoids stale/rescheduled dates like NKE
  - Currency per-ticker via yfinance financialCurrency
    FUTU → HKD, ASML → EUR, not hardcoded $
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import finnhub
import yfinance as yf

_FINNHUB_DELAY = 2.0  # 30 calls/min free tier — 2s gap keeps us safe
ET = ZoneInfo("America/New_York")  # All earnings times are in ET

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
        self.exact_time = exact_time    # tz-aware UTC, or None if unknown
        self.currency = currency
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self) -> str:
        return hashlib.md5(
            f"pre_{self.ticker}_{self.earnings_date}_{self.milestone}".encode()
        ).hexdigest()


class IntradayReminder:
    """Fires on the day of earnings at 2h/1h/30m/5m/1m before announcement."""
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


def _derive_time_from_code(date_str: str, code: str) -> Optional[datetime]:
    """
    bmo → 08:00 ET (typical pre-market release time)
    amc → 16:05 ET (typical post-close release time)
    Returns UTC datetime.
    """
    mapping = {"bmo": (8, 0), "amc": (16, 5)}
    if code.lower() not in mapping:
        return None
    try:
        h, m = mapping[code.lower()]
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).astimezone(timezone.utc)
    except Exception:
        return None


# ─── Currency ─────────────────────────────────────────────────────────────────

def _get_currency(ticker: str) -> str:
    """
    Fetch the financial reporting currency for a ticker.
    financialCurrency is the currency used in income statements (e.g. HKD for FUTU).
    Falls back to the trading currency, then USD.
    """
    try:
        info = yf.Ticker(ticker).info
        return info.get("financialCurrency") or info.get("currency") or "USD"
    except Exception as e:
        logger.debug(f"Currency [{ticker}]: {e}")
        return "USD"


# ─── Reliable date fetching ───────────────────────────────────────────────────

def _get_finnhub_calendar(ticker: str, days_ahead: int = 90) -> list[dict]:
    time.sleep(_FINNHUB_DELAY)
    try:
        today = datetime.now(timezone.utc)
        data = _fh.earnings_calendar(
            _from=today.strftime("%Y-%m-%d"),
            to=(today + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
            symbol=ticker,
            international=False,
        )
        return data.get("earningsCalendar", []) if data else []
    except Exception as e:
        logger.warning(f"Finnhub calendar [{ticker}]: {e}")
        return []


def _get_reliable_earnings_info(ticker: str) -> Optional[dict]:
    """
    Cross-reference Finnhub + yfinance to get the most accurate earnings date + time.

    Why this matters:
      - yfinance .calendar property lags behind rescheduled dates (e.g. NKE)
      - Finnhub calendar endpoint is updated within hours of any reschedule
      - yfinance earnings_dates has exact tz-aware times when available

    Strategy:
      1. Finnhub gives us the confirmed date (authoritative)
      2. yfinance earnings_dates gives exact time for that date
      3. If yfinance has no exact time → derive from bmo/amc convention
    """
    # Step 1: Get confirmed date from Finnhub
    entries = _get_finnhub_calendar(ticker, days_ahead=90)
    if not entries:
        return None

    today = datetime.now(timezone.utc).date()
    future = [e for e in entries if _parse_date(e.get("date", "")) and
              _parse_date(e["date"]) >= today]
    if not future:
        return None

    entry = sorted(future, key=lambda e: e["date"])[0]
    confirmed_date = entry["date"]
    report_time_code = entry.get("hour", "")

    # Step 2: Get exact time from yfinance earnings_dates
    exact_time_utc: Optional[datetime] = None
    try:
        ed = yf.Ticker(ticker).earnings_dates
        if ed is not None and not ed.empty:
            for idx in ed.index:
                # idx is tz-aware (ET or UTC depending on yfinance version)
                try:
                    idx_et = idx.astimezone(ET)
                except Exception:
                    idx_et = idx
                if str(idx_et.date()) == confirmed_date:
                    exact_time_utc = idx.astimezone(timezone.utc)
                    break
    except Exception as e:
        logger.debug(f"yfinance earnings_dates [{ticker}]: {e}")

    # Step 3: Fallback to convention time if yfinance had nothing
    if exact_time_utc is None:
        exact_time_utc = _derive_time_from_code(confirmed_date, report_time_code)

    return {
        "date":             confirmed_date,
        "report_time":      report_time_code,
        "exact_time":       exact_time_utc,
        "eps_estimate":     entry.get("epsEstimate"),
        "revenue_estimate": entry.get("revenueEstimate"),
    }


def _get_surprises(ticker: str) -> list[dict]:
    time.sleep(_FINNHUB_DELAY)
    try:
        return _fh.company_earnings(ticker, limit=4) or []
    except Exception as e:
        logger.warning(f"Finnhub surprises [{ticker}]: {e}")
        return []


def _get_financials(ticker: str) -> dict:
    try:
        stock = yf.Ticker(ticker)
        qs = stock.quarterly_income_stmt
        if qs is None or qs.empty:
            return {}
        latest = qs.iloc[:, 0]

        def safe(keyword: str) -> Optional[float]:
            for label in latest.index:
                if keyword in label.lower():
                    val = latest[label]
                    if val is not None and val == val:
                        return float(val)
            return None

        return {
            "revenue":      safe("total revenue"),
            "net_income":   safe("net income"),
            "gross_profit": safe("gross profit"),
            "currency":     _get_currency(ticker),
        }
    except Exception as e:
        logger.warning(f"yfinance financials [{ticker}]: {e}")
        return {}


# ─── Job 1: Day-level pre-earnings alerts ────────────────────────────────────

def check_upcoming_earnings_alerts() -> list[UpcomingEarnings]:
    alerts: list[UpcomingEarnings] = []
    today = datetime.now(timezone.utc).date()

    for ticker in EQUITY_TICKERS:
        info = _get_reliable_earnings_info(ticker)
        if not info:
            continue

        try:
            earnings_date = datetime.strptime(info["date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        days_until = (earnings_date - today).days
        currency = _get_currency(ticker)

        for milestone in PRE_EARNINGS_ALERT_DAYS:
            if days_until != milestone:
                continue

            alert = UpcomingEarnings(
                ticker=ticker,
                earnings_date=info["date"],
                days_until=days_until,
                eps_estimate=info.get("eps_estimate"),
                revenue_estimate=info.get("revenue_estimate"),
                report_time=info.get("report_time", ""),
                milestone=milestone,
                exact_time=info.get("exact_time"),
                currency=currency,
            )

            if is_seen("pre_earnings", alert.uid):
                continue

            mark_seen("pre_earnings", alert.uid)
            alerts.append(alert)
            logger.info(f"Pre-earnings: {ticker} in {milestone}d ({info['date']})")

    return alerts


# ─── Job 2: Intraday reminders (2h/1h/30m/5m/1m) ────────────────────────────

def check_intraday_reminders() -> list[IntradayReminder]:
    """
    Run every 5 minutes. Fires when we enter a reminder window.
    Skips entirely if exact_time is unknown — no false alarms.
    """
    reminders: list[IntradayReminder] = []
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    for ticker in EQUITY_TICKERS:
        info = _get_reliable_earnings_info(ticker)
        if not info:
            continue

        # Only care about today
        if _parse_date(info["date"]) != today:
            continue

        exact_time = info.get("exact_time")
        if exact_time is None:
            continue  # Don't fire without a known time

        minutes_to_go = (exact_time - now_utc).total_seconds() / 60
        if minutes_to_go < 0:
            continue  # Already past

        currency = _get_currency(ticker)

        for window in INTRADAY_MINUTES:
            # Fire if we just entered this window (within a 5-minute scheduler tick)
            if window <= minutes_to_go < window + 5:
                reminder = IntradayReminder(
                    ticker=ticker,
                    earnings_date=info["date"],
                    exact_time=exact_time,
                    minutes_before=window,
                    eps_estimate=info.get("eps_estimate"),
                    revenue_estimate=info.get("revenue_estimate"),
                    report_time=info.get("report_time", ""),
                    currency=currency,
                )

                if is_seen("intraday", reminder.uid):
                    continue

                mark_seen("intraday", reminder.uid)
                reminders.append(reminder)
                logger.info(f"Intraday: {ticker} in {window}min at {exact_time}")

    return reminders


# ─── Job 3: Post-earnings results ────────────────────────────────────────────

def check_new_earnings() -> list[EarningsReport]:
    reports: list[EarningsReport] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=EARNINGS_LOOKBACK_DAYS)

    for ticker in EQUITY_TICKERS:
        for entry in _get_surprises(ticker):
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
                eps_actual=entry.get("actual"),
                eps_estimate=entry.get("estimate"),
                eps_surprise_pct=entry.get("surprisePercent"),
                revenue=None, net_income=None, gross_profit=None,
            )

            if is_seen("earnings", r.uid):
                continue

            fin = _get_financials(ticker)
            r.revenue      = fin.get("revenue")
            r.net_income   = fin.get("net_income")
            r.gross_profit = fin.get("gross_profit")
            r.currency     = fin.get("currency", "USD")

            mark_seen("earnings", r.uid)
            reports.append(r)
            logger.info(f"Earnings result: {ticker} period={period}")

    return reports


# ─── Full calendar for /upcoming command ─────────────────────────────────────

def get_full_earnings_calendar(days_ahead: int = 14) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    results = []
    for ticker in EQUITY_TICKERS:
        for entry in _get_finnhub_calendar(ticker, days_ahead=days_ahead):
            raw_date = entry.get("date", "")
            try:
                days_until = (_parse_date(raw_date) - today).days
                if 0 <= days_until <= days_ahead:
                    results.append({
                        "ticker":           ticker,
                        "date":             raw_date,
                        "days_until":       days_until,
                        "eps_estimate":     entry.get("epsEstimate"),
                        "revenue_estimate": entry.get("revenueEstimate"),
                        "report_time":      entry.get("hour", ""),
                        "exact_time":       _derive_time_from_code(raw_date, entry.get("hour", "")),
                        "mexc_symbols":     TICKER_TO_MEXC.get(ticker, []),
                    })
            except (ValueError, TypeError):
                pass
    return sorted(results, key=lambda x: x["date"])
