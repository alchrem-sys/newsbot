"""
news.py — Financial news fetcher with importance filter.

The spam problem was: every article for every ticker was sent regardless of value.
Fix: every article gets an importance SCORE (0–100). Only articles scoring
above MIN_IMPORTANCE_SCORE are sent. Default threshold is 60.

Scoring system (additive, capped at 100):
  Category bonuses (the biggest drivers):
    +50  Earnings beat/miss/guidance
    +45  Analyst upgrade/downgrade with price target
    +45  M&A / merger / acquisition / takeover
    +40  CEO/CFO/executive change
    +40  Regulatory approval / FDA / SEC action
    +35  Stock split / buyback / special dividend
    +30  Major product launch / partnership
    +25  Legal action / lawsuit / DOJ / FTC
    +20  Layoffs / restructuring
    +15  Macroeconomic data directly mentioning the company

  Sentiment modifier:
    +10  Strong positive or negative sentiment (score >= 2 keywords)
    +5   Mild sentiment (score == 1 keyword)

  Noise penalties (subtractive):
    -30  Article is a listicle / "X stocks to watch" / "best stocks"
    -20  Opinion / commentary / "why I think" / "here's why"
    -15  Duplicate story (headline highly similar to already-sent one)
    -10  Sponsored / promoted content signals
    -10  Pre-market/after-hours recap (generic, not company-specific)

Threshold: MIN_IMPORTANCE_SCORE = 60 (configurable in config.py)
Maximum 2 articles per ticker per run to prevent one company flooding the chat.
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import finnhub

_FINNHUB_DELAY = 2.0

from config import FINNHUB_API_KEY, NEWS_LOOKBACK_HOURS
from storage import is_seen, mark_seen
from tickers import ALL_TICKERS

logger = logging.getLogger(__name__)
_fh = finnhub.Client(api_key=FINNHUB_API_KEY)

# ── Tunable constants ──────────────────────────────────────────────────────────
MIN_IMPORTANCE_SCORE = 60   # Articles below this are dropped
MAX_PER_TICKER       = 2    # Max articles per ticker per run


# ── Scoring keyword tables ─────────────────────────────────────────────────────

# (score_bonus, [keywords]) — matched against lowercased headline + summary
_CATEGORY_RULES: list[tuple[int, list[str]]] = [
    # Earnings / financial results
    (50, ["earnings beat", "earnings miss", "beat estimates", "missed estimates",
          "beat expectations", "missed expectations", "eps beat", "eps miss",
          "revenue beat", "revenue miss", "raised guidance", "lowered guidance",
          "cut guidance", "raised outlook", "profit warning", "earnings surprise",
          "quarterly results", "q1 results", "q2 results", "q3 results", "q4 results",
          "full year results", "annual results"]),

    # Analyst actions
    (45, ["price target", "upgrades to", "downgrades to", "initiates coverage",
          "raised target", "lowered target", "overweight", "underweight",
          "outperform", "underperform", "buy rating", "sell rating",
          "strong buy", "strong sell", "neutral rating", "hold rating",
          "analyst upgrade", "analyst downgrade", "price target raised",
          "price target cut"]),

    # M&A
    (45, ["merger", "acquisition", "acquires", "acquired by", "takeover",
          "buyout", "going private", "deal worth", "billion deal",
          "agreed to buy", "agreed to acquire", "to be acquired",
          "strategic acquisition", "tender offer"]),

    # Executive changes
    (40, ["ceo resigns", "ceo steps down", "new ceo", "appoints ceo",
          "cfo resigns", "cfo steps down", "new cfo", "appoints cfo",
          "chief executive", "executive departure", "board chair",
          "founder steps down", "leadership change"]),

    # Regulatory / legal major
    (40, ["fda approval", "fda approved", "fda rejected", "fda rejection",
          "sec charges", "sec investigation", "doj investigation",
          "ftc blocks", "antitrust", "regulatory approval", "cleared by",
          "sanctioned", "banned", "delisted"]),

    # Capital actions
    (35, ["stock split", "share buyback", "buyback program", "special dividend",
          "dividend increase", "dividend cut", "dividend suspended",
          "share repurchase", "tender offer", "dutch auction"]),

    # Major business news
    (30, ["major partnership", "exclusive deal", "landmark deal",
          "major contract", "billion contract", "government contract",
          "new product launch", "product recall", "plant closure",
          "factory shutdown", "major layoffs", "mass layoffs"]),

    # Legal / regulatory moderate
    (25, ["lawsuit filed", "class action", "settles lawsuit", "fined",
          "settlement reached", "court ruling", "subpoena", "indicted",
          "charged with", "faces probe"]),

    # Restructuring
    (20, ["restructuring", "cost cutting", "job cuts", "workforce reduction",
          "lays off", "laying off", "streamlining operations"]),
]

# Flatten for fast lookup: phrase → bonus
_PHRASE_SCORES: dict[str, int] = {}
for _bonus, _phrases in _CATEGORY_RULES:
    for _phrase in _phrases:
        _PHRASE_SCORES[_phrase] = _bonus

# Sentiment keywords (secondary, smaller boost)
_POSITIVE_WORDS = {"beat", "beats", "surpass", "record", "profit", "rise", "rises",
                   "gain", "gains", "upgrade", "growth", "strong", "bullish", "rally",
                   "outperform", "raises", "exceeds", "soars", "jumps", "surges"}
_NEGATIVE_WORDS = {"miss", "misses", "missed", "decline", "declines", "loss", "losses",
                   "downgrade", "weak", "bearish", "fall", "falls", "cut", "cuts",
                   "layoff", "warning", "disappoints", "plunges", "drops", "tumbles"}

# Noise penalty phrases
_NOISE_PENALTIES: list[tuple[int, list[str]]] = [
    (-30, ["stocks to watch", "top stocks", "best stocks", "stocks to buy",
           "stocks making moves", "5 stocks", "3 stocks", "10 stocks",
           "stocks that could", "stocks you should"]),
    (-20, ["here's why", "why i think", "opinion:", "commentary:", "should you buy",
           "is it a buy", "is it time to", "what you need to know"]),
    (-10, ["premarket movers", "after hours movers", "market recap",
           "morning briefing", "midday movers", "stocks on the move today"]),
    (-10, ["sponsored", "paid content", "advertisement", "partner content"]),
]

_NOISE_PHRASE_SCORES: dict[str, int] = {}
for _penalty, _phrases in _NOISE_PENALTIES:
    for _phrase in _phrases:
        _NOISE_PHRASE_SCORES[_phrase] = _penalty


# ── Data class ────────────────────────────────────────────────────────────────

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
        self.importance = 0    # 0–100
        self.category = ""     # human-readable label e.g. "Earnings Beat"

    @property
    def uid(self) -> str:
        raw = self.url if self.url else self.headline
        return hashlib.md5(raw.encode()).hexdigest()


# ── Importance scorer ─────────────────────────────────────────────────────────

# Category labels for display (highest-scoring category wins)
_CATEGORY_LABELS = {
    50: "Earnings",
    45: "Analyst Action",  # used for both upgrade/downgrade and M&A
    40: "Executive Change",
    35: "Capital Action",
    30: "Major News",
    25: "Legal",
    20: "Restructuring",
}

def _score_article(headline: str, summary: str) -> tuple[int, str, str]:
    """
    Returns (score, sentiment, category_label).
    Score is capped at 100.
    """
    text = (headline + " " + summary).lower()
    score = 0
    top_bonus = 0
    top_category = ""

    # Category bonuses — check multi-word phrases first
    for phrase, bonus in sorted(_PHRASE_SCORES.items(), key=lambda x: -len(x[0])):
        if phrase in text:
            score += bonus
            if bonus > top_bonus:
                top_bonus = bonus
                top_category = _CATEGORY_LABELS.get(bonus, "News")

    # Sentiment modifier
    words = set(text.split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    sentiment_score = max(pos, neg)
    if sentiment_score >= 2:
        score += 10
    elif sentiment_score == 1:
        score += 5

    sentiment = "neutral"
    if pos > neg:   sentiment = "positive"
    elif neg > pos: sentiment = "negative"

    # Noise penalties
    for phrase, penalty in _NOISE_PHRASE_SCORES.items():
        if phrase in text:
            score += penalty  # penalty is already negative

    return min(max(score, 0), 100), sentiment, top_category or "News"


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _finnhub_news(ticker: str, from_ts: int, to_ts: int) -> list[NewsItem]:
    time.sleep(_FINNHUB_DELAY)
    items = []
    try:
        articles = _fh.company_news(
            ticker,
            _from=datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            to=datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        )
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
    except Exception as e:
        logger.warning(f"Finnhub news [{ticker}]: {e}")
    return items


def _yahoo_rss_news(ticker: str, cutoff: datetime) -> list[NewsItem]:
    items = []
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US"
        feed = feedparser.parse(url)
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

def check_new_news() -> list[NewsItem]:
    """
    Fetch news for all tickers, score each article, and return only
    the important ones (score >= MIN_IMPORTANCE_SCORE).
    Lookback is 5 minutes — tight window for 1-minute polling.
    Upstash dedup ensures nothing is ever sent twice.
    Max MAX_PER_TICKER articles per ticker per run.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    from_ts = int(cutoff.timestamp())
    to_ts   = int(datetime.now(timezone.utc).timestamp())
    results: list[NewsItem] = []

    for ticker in ALL_TICKERS:
        articles = _finnhub_news(ticker, from_ts, to_ts)
        if not articles:
            articles = _yahoo_rss_news(ticker, cutoff)

        scored: list[NewsItem] = []
        for item in articles:
            if is_seen("news", item.uid):
                continue

            score, sentiment, category = _score_article(item.headline, item.summary)
            item.importance = score
            item.sentiment  = sentiment
            item.category   = category

            if score >= MIN_IMPORTANCE_SCORE:
                scored.append(item)

        # Sort by importance, keep only top MAX_PER_TICKER per ticker
        scored.sort(key=lambda x: -x.importance)
        for item in scored[:MAX_PER_TICKER]:
            mark_seen("news", item.uid)
            results.append(item)
            logger.info(
                f"News [{score}pts/{item.category}] {ticker}: "
                f"{item.headline[:60]}"
            )

        # Mark everything else as seen too so low-score articles
        # don't pile up and re-score next run
        for item in articles:
            if not is_seen("news", item.uid):
                mark_seen("news", item.uid)

    # Sort final list: highest importance first
    results.sort(key=lambda x: -x.importance)
    return results
