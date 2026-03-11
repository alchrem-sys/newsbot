"""
models.py — All shared data classes live here.

Previously NewsItem was in news.py and imported by formatters.py,
while news.py was also being pulled into formatters.py — circular import.
Fix: both news.py and formatters.py import from models.py only.
"""

import hashlib
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from tickers import TICKER_TO_MEXC

ET = ZoneInfo("America/New_York")


class NewsItem:
    def __init__(self, ticker: str, headline: str, summary: str,
                 url: str, source: str, published_at: str):
        self.ticker = ticker
        self.headline = headline
        self.summary = summary
        self.url = url
        self.source = source
        self.published_at = published_at
        self.sentiment = "neutral"
        self.importance = 0
        self.category = ""
        self.mexc_symbols = TICKER_TO_MEXC.get(ticker, [])

    @property
    def uid(self) -> str:
        raw = self.url if self.url else self.headline
        return hashlib.md5(raw.encode()).hexdigest()


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
