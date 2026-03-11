import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str       = os.getenv("TELEGRAM_BOT_TOKEN", "")

# For supergroups the chat ID must be negative and prefixed with -100
# e.g. https://t.me/c/3692217852/43933 → chat_id=-1003692217852, thread_id=43933
_raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
TELEGRAM_CHAT_ID: int = int(_raw_chat_id) if int(_raw_chat_id) < 0 else -int(f"100{_raw_chat_id}")

# Message thread (topic) ID for supergroups. Set to 0 for regular groups/DMs.
TELEGRAM_THREAD_ID: int = int(os.getenv("TELEGRAM_THREAD_ID", "0"))

FINNHUB_API_KEY: str          = os.getenv("FINNHUB_API_KEY", "")

UPSTASH_REDIS_REST_URL: str   = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN: str = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

MEXC_TICKER_URL: str          = "https://futures.mexc.com/api/v1/contract/ticker"

CHECK_INTERVAL_MINUTES: int   = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
NEWS_LOOKBACK_HOURS: int      = int(os.getenv("NEWS_LOOKBACK_HOURS", "1"))
EARNINGS_LOOKBACK_DAYS: int   = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "7"))
MAX_NEWS_PER_TICKER: int      = int(os.getenv("MAX_NEWS_PER_TICKER", "3"))

PRE_EARNINGS_ALERT_DAYS: list[int] = [7, 3, 1, 0]
