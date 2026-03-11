"""
news.py — Financial news fetcher, fully async.

Key fix: all Finnhub/yfinance calls run via asyncio.to_thread() so the
event loop (and bot commands) are never blocked by API sleeps.

Rate limiting is done with asyncio.sleep() not time.sleep(), which
yields control back to the event loop during the wait.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import finnhub

from config import FINNHUB_API_KEY
from storage import is_seen, mark_seen
from models import NewsItem
from tickers import ALL_TICKERS

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)

# Finnhub free tier: 30 calls/min → 2s between calls
# Using asyncio.sleep so the event loop is NOT blocked during the wait
_FINNHUB_DELAY = 2.0

MIN_IMPORTANCE_SCORE = 60
MAX_PER_TICKER = 2

# ── Scoring tables ────────────────────────────────────────────────────────────

_CATEGORY_RULES: list[tuple[int, list[str]]] = [
    (50, ["earnings beat", "earnings miss", "beat estimates", "missed estimates",
          "beat expectations", "missed expectations", "eps beat", "eps miss",
          "revenue beat", "revenue miss", "raised guidance", "lowered guidance",
          "cut guidance", "raised outlook", "profit warning", "earnings surprise",
          "quarterly results", "q1 results", "q2 results", "q3 results", "q4 results",
          "full year results", "annual results"]),
    (45, ["price target", "upgrades to", "downgrades to", "initiates coverage",
          "raised target", "lowered target", "overweight", "underweight",
          "outperform", "underperform", "buy rating", "sell rating",
          "strong buy", "strong sell", "neutral rating", "hold rating",
          "analyst upgrade", "analyst downgrade", "price target raised",
          "price target cut"]),
    (45, ["merger", "acquisition", "acquires", "acquired by", "takeover",
          "buyout", "going private", "deal worth", "billion deal",
          "agreed to buy", "agreed to acquire", "to be acquired",
          "strategic acquisition", "tender offer"]),
    (40, ["ceo resigns", "ceo steps down", "new ceo", "appoints ceo",
          "cfo resigns", "cfo steps down", "new cfo", "appoints cfo",
          "chief executive", "executive departure", "leadership change"]),
    (40, ["fda approval", "fda approved", "fda rejected", "sec charges",
          "sec investigation", "doj investigation", "ftc blocks", "antitrust",
          "regulatory approval", "sanctioned", "banned", "delisted"]),
    (35, ["stock split", "share buyback", "buyback program", "special dividend",
          "dividend increase", "dividend cut", "dividend suspended",
          "share repurchase"]),
    (30, ["major partnership", "exclusive deal", "major contract",
          "billion contract", "government contract", "new product launch",
          "product recall", "plant closure", "major layoffs", "mass layoffs"]),
    (25, ["lawsuit filed", "class action", "settles lawsuit", "fined",
          "settlement reached", "court ruling", "subpoena", "indicted"]),
    (20, ["restructuring", "cost cutting", "job cuts", "workforce reduction",
          "lays off", "laying off"]),
]

_PHRASE_SCORES: dict[str, int] = {}
_CATEGORY_LABELS: dict[int, str] = {
    50: "Earnings", 45: "Analyst / M&A", 40: "Executive / Regulatory",
    35: "Capital Action", 30: "Major News", 25: "Legal", 20: "Restructuring",
}
for _bonus, _phrases in _CATEGORY_RULES:
    for _phrase in _phrases:
        _PHRASE_SCORES[_phrase] = _bonus

_POSITIVE_WORDS = {"beat","beats","surpass","record","profit","rise","rises","gain",
                   "gains","upgrade","growth","strong","bullish","rally","outperform",
                   "raises","exceeds","soars","jumps","surges"}
_NEGATIVE_WORDS = {"miss","misses","missed","decline","declines","loss","losses",
                   "downgrade","weak","bearish","fall","falls","cut","cuts","layoff",
                   "warning","disappoints","plunges","drops","tumbles"}

_NOISE_PENALTIES: dict[str, int] = {}
for _penalty, _phrases in [
    (-30, ["stocks to watch","top stocks","best stocks","stocks to buy",
           "5 stocks","3 stocks","10 stocks","stocks that could"]),
    (-20, ["here's why","why i think","opinion:","should you buy","is it a buy"]),
    (-10, ["premarket movers","after hours movers","market recap",
           "morning briefing","midday movers"]),
    (-10, ["sponsored","paid content","advertisement"]),
]:
    for _phrase in _phrases:
        _NOISE_PENALTIES[_phrase] = _penalty


# ── Scorer ────────────────────────────────────────────────────────────────────

def _score(headline: str, summary: str) -> tuple[int, str, str]:
    text = (headline + " " + summary).lower()
    score, top_bonus, top_cat = 0, 0, ""

    for phrase, bonus in sorted(_PHRASE_SCORES.items(), key=lambda x: -len(x[0])):
        if phrase in text:
            score += bonus
            if bonus > top_bonus:
                top_bonus = bonus
                top_cat = _CATEGORY_LABELS.get(bonus, "News")

    words = set(text.split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    score += 10 if max(pos, neg) >= 2 else (5 if max(pos, neg) == 1 else 0)

    sentiment = "positive" if pos > neg else ("negative" if neg > pos else "neutral")

    for phrase, penalty in _NOISE_PENALTIES.items():
        if phrase in text:
            score += penalty

    return min(max(score, 0), 100), sentiment, top_cat or "News"


# ── Async fetchers ────────────────────────────────────────────────────────────

async def _fetch_finnhub(ticker: str, from_ts: int, to_ts: int) -> list[NewsItem]:
    """
    Fetch Finnhub news for a ticker. Cached for 5 minutes (matching the job interval)
    so rapid re-runs never hit the API twice for the same window.
    """
    import json
    from storage import get_cache, set_cache

    cache_key = f"fh_news:{ticker}:{from_ts // 300}"  # bucket by 5-min window
    cached = get_cache(cache_key)
    if cached:
        try:
            raw = json.loads(cached)
            items = []
            for r in raw:
                item = NewsItem(ticker=r["ticker"], headline=r["headline"],
                                summary=r["summary"], url=r["url"],
                                source=r["source"], published_at=r["published_at"])
                items.append(item)
            return items
        except Exception:
            pass

    await asyncio.sleep(_FINNHUB_DELAY)

    def _sync_call():
        return _fh.company_news(
            ticker,
            _from=datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            to=datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        )

    items = []
    try:
        articles = await asyncio.to_thread(_sync_call)
        for art in (articles or []):
            pub = datetime.fromtimestamp(art.get("datetime", 0), tz=timezone.utc)
            items.append(NewsItem(
                ticker=ticker,
                headline=art.get("headline", ""),
                summary=art.get("summary", "")[:300],
                url=art.get("url", ""),
                source=art.get("source", "Finnhub"),
                published_at=pub.strftime("%Y-%m-%d %H:%M UTC"),
            ))
        # Cache even empty results to prevent hammering on quiet tickers
        serialized = json.dumps([{
            "ticker": i.ticker, "headline": i.headline, "summary": i.summary,
            "url": i.url, "source": i.source, "published_at": i.published_at,
        } for i in items])
        set_cache(cache_key, serialized, ttl_seconds=300)  # 5 min
    except Exception as e:
        if "429" in str(e):
            logger.warning(f"Finnhub 429 news [{ticker}] — skipping")
        else:
            logger.warning(f"Finnhub news [{ticker}]: {e}")
    return items


async def _fetch_yahoo_rss(ticker: str, cutoff: datetime) -> list[NewsItem]:
    """Runs feedparser in a thread — never blocks the event loop."""
    def _sync_call():
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US"
        return feedparser.parse(url)

    items = []
    try:
        feed = await asyncio.to_thread(_sync_call)
        for entry in feed.entries:
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
                summary=entry.get("summary", "")[:300],
                url=entry.get("link", ""),
                source="Yahoo Finance",
                published_at=pub_str,
            ))
    except Exception as e:
        logger.warning(f"Yahoo RSS [{ticker}]: {e}")
    return items


# ── Main check ────────────────────────────────────────────────────────────────

async def check_new_news() -> list[NewsItem]:
    """
    Async — runs every 1 minute with a 5-minute lookback.
    All blocking calls are offloaded to threads via asyncio.to_thread().
    The event loop stays free for bot commands throughout the entire sweep.
    """
    cutoff  = datetime.now(timezone.utc) - timedelta(minutes=5)
    from_ts = int(cutoff.timestamp())
    to_ts   = int(datetime.now(timezone.utc).timestamp())
    results: list[NewsItem] = []

    for ticker in ALL_TICKERS:
        articles = await _fetch_finnhub(ticker, from_ts, to_ts)
        if not articles:
            articles = await _fetch_yahoo_rss(ticker, cutoff)

        scored: list[NewsItem] = []
        for item in articles:
            if is_seen("news", item.uid):
                continue
            s, sentiment, category = _score(item.headline, item.summary)
            item.importance = s
            item.sentiment  = sentiment
            item.category   = category
            if s >= MIN_IMPORTANCE_SCORE:
                scored.append(item)

        scored.sort(key=lambda x: -x.importance)
        for item in scored[:MAX_PER_TICKER]:
            mark_seen("news", item.uid)
            results.append(item)
            logger.info(f"News [{item.importance}pts] {ticker}: {item.headline[:60]}")

        # Mark low-score articles seen so they don't accumulate
        for item in articles:
            if not is_seen("news", item.uid):
                mark_seen("news", item.uid)

    results.sort(key=lambda x: -x.importance)
    return results
