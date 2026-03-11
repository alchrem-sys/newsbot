"""
earnings.py

TWO jobs:
  1. check_upcoming_earnings_alerts()  ← MAIN PURPOSE
     Fires BEFORE the report date so you can position on MEXC.
     Milestones: 7 days / 3 days / 1 day / day-of (configurable in config.py)
     Each milestone fires exactly once per ticker per earnings date (Upstash dedup).

  2. check_new_earnings()
     Fires AFTER the report is published with actual EPS, revenue, and surprise %.

Sources:
  - Finnhub earnings_calendar  → scheduled dates + analyst estimates
  - Finnhub company_earnings   → actual vs estimate after report
  - yfinance quarterly_income_stmt → revenue, net income, gross profit
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import finnhub
import yfinance as yf

from config import FINNHUB_API_KEY, EARNINGS_LOOKBACK_DAYS, PRE_EARNINGS_ALERT_DAYS
from storage import is_seen, mark_seen
from tickers import EQUITY_TICKERS, TICKER_TO_MEXC

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)


# ─── Data classes ─────────────────────────────────────────────────────────────

class UpcomingEarnings:
    def __init__(self, ticker: str, earnings_date: str, days_until: int,
                 eps_estimate: Optional[float], revenue_estimate: Optional[float],
                 report_time: str, milestone: int):
        self.ticker = ticker
        self.earnings_date = earnings_date
        self.days_until = days_until
        self.eps_estimate = eps_estimate
        self.revenue_estimate = revenue_estimate
        self.report_time = report_time  # "bmo" | "amc" | ""
        self.milestone = milestone
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self) -> str:
        return hashlib.md5(
            f"pre_{self.ticker}_{self.earnings_date}_{self.milestone}".encode()
        ).hexdigest()


class EarningsReport:
    def __init__(self, ticker: str, period: str, report_date: str,
                 eps_actual: Optional[float], eps_estimate: Optional[float],
                 eps_surprise_pct: Optional[float], revenue: Optional[float],
                 net_income: Optional[float], gross_profit: Optional[float]):
        self.ticker = ticker
        self.period = period
        self.report_date = report_date
        self.eps_actual = eps_actual
        self.eps_estimate = eps_estimate
        self.eps_surprise_pct = eps_surprise_pct
        self.revenue = revenue
        self.net_income = net_income
        self.gross_profit = gross_profit
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self) -> str:
        return hashlib.md5(
            f"result_{self.ticker}_{self.period}_{self.report_date}".encode()
        ).hexdigest()


# ─── Fetchers ─────────────────────────────────────────────────────────────────

def _get_calendar(ticker: str, days_ahead: int = 14) -> list[dict]:
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


def _get_surprises(ticker: str) -> list[dict]:
    try:
        return _fh.company_earnings(ticker, limit=4) or []
    except Exception as e:
        logger.warning(f"Finnhub surprises [{ticker}]: {e}")
        return []


def _get_financials(ticker: str) -> dict:
    try:
        qs = yf.Ticker(ticker).quarterly_income_stmt
        if qs is None or qs.empty:
            return {}
        latest = qs.iloc[:, 0]

        def safe(keyword: str) -> Optional[float]:
            for label in latest.index:
                if keyword in label.lower():
                    val = latest[label]
                    if val is not None and val == val:  # not NaN
                        return float(val)
            return None

        return {
            "revenue":      safe("total revenue"),
            "net_income":   safe("net income"),
            "gross_profit": safe("gross profit"),
        }
    except Exception as e:
        logger.warning(f"yfinance financials [{ticker}]: {e}")
        return {}


# ─── Pre-earnings countdown (main purpose) ────────────────────────────────────

def check_upcoming_earnings_alerts() -> list[UpcomingEarnings]:
    """
    Fire alerts BEFORE the earnings date so you can open a position on MEXC.
    Checks every ticker and fires once per milestone per earnings date.
    """
    alerts: list[UpcomingEarnings] = []
    today = datetime.now(timezone.utc).date()
    max_days = max(PRE_EARNINGS_ALERT_DAYS) + 1

    for ticker in EQUITY_TICKERS:
        for entry in _get_calendar(ticker, days_ahead=max_days):
            raw_date = entry.get("date", "")
            if not raw_date:
                continue
            try:
                earnings_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue

            days_until = (earnings_date - today).days

            for milestone in PRE_EARNINGS_ALERT_DAYS:
                if days_until != milestone:
                    continue

                alert = UpcomingEarnings(
                    ticker=ticker,
                    earnings_date=raw_date,
                    days_until=days_until,
                    eps_estimate=entry.get("epsEstimate"),
                    revenue_estimate=entry.get("revenueEstimate"),
                    report_time=entry.get("hour", ""),
                    milestone=milestone,
                )

                if is_seen("pre_earnings", alert.uid):
                    continue

                mark_seen("pre_earnings", alert.uid)
                alerts.append(alert)
                logger.info(f"Pre-earnings: {ticker} in {milestone}d ({raw_date})")

    return alerts


# ─── Post-earnings results ────────────────────────────────────────────────────

def check_new_earnings() -> list[EarningsReport]:
    """Fire after the report is published with actual results."""
    reports: list[EarningsReport] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=EARNINGS_LOOKBACK_DAYS)

    for ticker in EQUITY_TICKERS:
        for entry in _get_surprises(ticker):
            report_date = entry.get("date", "")
            period = entry.get("period", "")
            try:
                if datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
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
            r.revenue = fin.get("revenue")
            r.net_income = fin.get("net_income")
            r.gross_profit = fin.get("gross_profit")

            mark_seen("earnings", r.uid)
            reports.append(r)
            logger.info(f"Earnings result: {ticker} period={period}")

    return reports


# ─── Full calendar for /upcoming command ─────────────────────────────────────

def get_full_earnings_calendar(days_ahead: int = 14) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    results = []
    for ticker in EQUITY_TICKERS:
        for entry in _get_calendar(ticker, days_ahead=days_ahead):
            raw_date = entry.get("date", "")
            try:
                days_until = (datetime.strptime(raw_date, "%Y-%m-%d").date() - today).days
                if 0 <= days_until <= days_ahead:
                    results.append({
                        "ticker":           ticker,
                        "date":             raw_date,
                        "days_until":       days_until,
                        "eps_estimate":     entry.get("epsEstimate"),
                        "revenue_estimate": entry.get("revenueEstimate"),
                        "report_time":      entry.get("hour", ""),
                        "mexc_symbols":     TICKER_TO_MEXC.get(ticker, []),
                    })
            except ValueError:
                pass
    return sorted(results, key=lambda x: x["date"])
