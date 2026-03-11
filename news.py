"""
news.py — Financial news fetcher.

Sources:
  1. Finnhub company news (primary)
  2. Yahoo Finance RSS (fallback, no key needed)

Every article gets a sentiment label (positive / negative / neutral)
so you can judge direction at a glance.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import finnhub

from config import FINNHUB_API_KEY, NEWS_LOOKBACK_HOURS, MAX_NEWS_PER_TICKER
from storage import is_seen, mark_seen
from tickers import ALL_TICKERS

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)

_POSITIVE = {"beat","beats","surpass","record","profit","rise","rises","gain","gains",
             "upgrade","buy","growth","strong","bullish","rally","outperform","raises","exceeds"}
_NEGATIVE = {"miss","misses","missed","decline","declines","loss","losses","downgrade",
             "sell","weak","bearish","fall","falls","cut","layoff","lawsuit","warning",
             "below","disappoints","disappointing","investigation","recall"}


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

    @property
    def uid(self) -> str:
        raw = self.url if self.url else self.headline
        return hashlib.md5(raw.encode()).hexdigest()


def _sentiment(text: str) -> str:
    words = set(text.lower().split())
    pos = len(words & _POSITIVE)
    neg = len(words & _NEGATIVE)
    if pos > neg:   return "positive"
    if neg > pos:   return "negative"
    return "neutral"


def _finnhub_news(ticker: str, from_ts: int, to_ts: int) -> list[NewsItem]:
    items = []
    try:
        articles = _fh.company_news(
            ticker,
            _from=datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            to=datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        )
        for art in (articles or [])[:MAX_NEWS_PER_TICKER]:
            pub = datetime.fromtimestamp(art.get("datetime", 0), tz=timezone.utc)
            items.append(NewsItem(
                ticker=ticker,
                headline=art.get("headline", ""),
                summary=art.get("summary", "")[:280],
                url=art.get("url", ""),
                source=art.get("source", "Finnhub"),
                published_at=pub.strftime("%Y-%m-%d %H:%M UTC"),
            ))
    except Exception as e:
        logger.warning(f"Finnhub news [{ticker}]: {e}")
    return items


def _yahoo_rss_news(ticker: str, cutoff: datetime) -> list[NewsItem]:
    items = []
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US"
        feed = feedparser.parse(url)
        count = 0
        for entry in feed.entries:
            if count >= MAX_NEWS_PER_TICKER:
                break
            ps = entry.get("published_parsed")
            if ps:
                pub_dt = datetime(*ps[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M UTC")
            else:
                pub_str = "Unknown"
            items.append(NewsItem(
                ticker=ticker,
                headline=entry.get("title", ""),
                summary=entry.get("summary", "")[:280],
                url=entry.get("link", ""),
                source="Yahoo Finance",
                published_at=pub_str,
            ))
            count += 1
    except Exception as e:
        logger.warning(f"Yahoo RSS [{ticker}]: {e}")
    return items


def check_new_news() -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    from_ts = int(cutoff.timestamp())
    to_ts = int(datetime.now(timezone.utc).timestamp())
    new_items = []

    for ticker in ALL_TICKERS:
        articles = _finnhub_news(ticker, from_ts, to_ts)
        if not articles:
            articles = _yahoo_rss_news(ticker, cutoff)

        for item in articles:
            if is_seen("news", item.uid):
                continue
            item.sentiment = _sentiment(item.headline + " " + item.summary)
            mark_seen("news", item.uid)
            new_items.append(item)

    return new_items
