"""mexc_price.py — Live prices from MEXC Futures API."""

import logging
from typing import Optional
import aiohttp
from config import MEXC_TICKER_URL

logger = logging.getLogger(__name__)


async def get_mexc_price(mexc_symbol: str) -> Optional[dict]:
    """Fetch live price for a single MEXC symbol e.g. AAPLUSDT."""
    base = mexc_symbol.replace("USDT", "")
    api_symbol = f"{base}_USDT"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(MEXC_TICKER_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                for ticker in data.get("data", []):
                    if ticker.get("symbol") == api_symbol:
                        return ticker
    except Exception as e:
        logger.error(f"MEXC price fetch error: {e}")
    return None


def format_mexc_price(symbol: str, d: dict) -> str:
    last = d.get("lastPrice", "N/A")
    pct  = d.get("priceChangePct")
    high = d.get("high24h", "N/A")
    low  = d.get("low24h", "N/A")
    vol  = d.get("volume24h", "N/A")
    chg_str = f"{float(pct)*100:+.2f}%" if pct else "N/A"
    arrow = "📈" if pct and float(pct) >= 0 else "📉"
    return (
        f"💹 <b>MEXC: {symbol}</b>\n"
        f"Price: <b>${last}</b>  {arrow} {chg_str} (24h)\n"
        f"High: ${high}  |  Low: ${low}\n"
        f"Volume: {vol}"
    )
