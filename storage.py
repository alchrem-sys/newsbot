"""
Upstash Redis storage.
- Deduplication for news and earnings (never send the same alert twice)
- Bot settings (mute/unmute per category)
- Pre-earnings milestone tracking

Free tier: https://upstash.com — 10,000 commands/day, no persistent disk needed.
Works perfectly on Railway (serverless HTTP, no TCP redis port required).
"""

import logging
from typing import Optional
from upstash_redis import Redis
from config import UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN

logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None
_TTL = 60 * 60 * 24 * 90  # 90 days


def _r() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    return _redis


def is_seen(namespace: str, item_id: str) -> bool:
    try:
        return bool(_r().sismember(f"mexcbot:{namespace}", item_id))
    except Exception as e:
        logger.warning(f"Redis is_seen error: {e}")
        return False  # fail open — better to resend than miss


def mark_seen(namespace: str, item_id: str) -> None:
    try:
        _r().sadd(f"mexcbot:{namespace}", item_id)
        _r().expire(f"mexcbot:{namespace}", _TTL)
    except Exception as e:
        logger.warning(f"Redis mark_seen error: {e}")


def get_setting(key: str) -> Optional[str]:
    try:
        val = _r().get(f"mexcbot:setting:{key}")
        return str(val) if val is not None else None
    except Exception as e:
        logger.warning(f"Redis get_setting error: {e}")
        return None


def set_setting(key: str, value: str) -> None:
    try:
        _r().set(f"mexcbot:setting:{key}", value)
    except Exception as e:
        logger.warning(f"Redis set_setting error: {e}")


def health_check() -> bool:
    try:
        _r().ping()
        return True
    except Exception:
        return False
