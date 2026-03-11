import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: int         = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

FINNHUB_API_KEY: str          = os.getenv("FINNHUB_API_KEY", "")

UPSTASH_REDIS_REST_URL: str   = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN: str = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

MEXC_TICKER_URL: str          = "https://futures.mexc.com/api/v1/contract/ticker"

CHECK_INTERVAL_MINUTES: int   = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
NEWS_LOOKBACK_HOURS: int      = int(os.getenv("NEWS_LOOKBACK_HOURS", "1"))
EARNINGS_LOOKBACK_DAYS: int   = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "7"))
MAX_NEWS_PER_TICKER: int      = int(os.getenv("MAX_NEWS_PER_TICKER", "3"))

# Fire pre-earnings alerts at these milestones (days before report)
PRE_EARNINGS_ALERT_DAYS: list[int] = [7, 3, 1, 0]
