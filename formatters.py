"""
formatters.py

Design rule: TICKER + MEXC SYMBOL are always the first line of every message
so you can decide whether to act before reading a single word of the content.
"""

from typing import Optional
from earnings import EarningsReport, UpcomingEarnings
from news import NewsItem
from tickers import TICKER_TO_MEXC


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _usd(v: Optional[float]) -> str:
    if v is None: return "N/A"
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1e9: return f"{s}${a/1e9:.2f}B"
    if a >= 1e6: return f"{s}${a/1e6:.2f}M"
    return f"{s}${a:,.2f}"

def _eps(v: Optional[float]) -> str:
    return f"${v:.4f}" if v is not None else "N/A"

def _mexc(ticker: str) -> str:
    syms = TICKER_TO_MEXC.get(ticker, [])
    return "  ".join(f"<code>{s}</code>" for s in syms)

def _surprise_emoji(pct: Optional[float]) -> str:
    if pct is None:  return "❓"
    if pct >= 10:    return "🚀"
    if pct >= 3:     return "✅"
    if pct >= -3:    return "➡️"
    if pct >= -10:   return "⚠️"
    return "🔴"

def _sentiment_emoji(s: str) -> str:
    return {"positive": "📈", "negative": "📉", "neutral": "📰"}.get(s, "📰")

def _time_label(code: str) -> str:
    return {"bmo": "☀️ Before Market Open", "amc": "🌙 After Market Close"}.get(
        code.lower(), "⏱ Time TBD"
    )


# ─── Pre-earnings countdown (the main alert) ──────────────────────────────────

_MILESTONES = {
    7: ("🗓", "EARNINGS IN 1 WEEK",  "Plan your direction now."),
    3: ("⏰", "EARNINGS IN 3 DAYS",  "Prepare your entry."),
    1: ("🔔", "EARNINGS TOMORROW",   "Final check — ready to act."),
    0: ("🚨", "EARNINGS TODAY",      "Last chance to position on MEXC!"),
}

def format_pre_earnings(a: UpcomingEarnings) -> str:
    icon, title, cta = _MILESTONES.get(a.milestone, ("📅", "EARNINGS SOON", ""))
    lines = [
        # ── Always first: ticker + MEXC symbol ──
        f"{icon} <b>{a.ticker}</b>  {_mexc(a.ticker)}",
        f"<b>{title}</b>",
        "",
        f"📆 Date:           <b>{a.earnings_date}</b>",
        f"⏳ Days until:     <b>{a.days_until}</b>" if a.days_until > 0 else "",
        f"🕐 Report time:    {_time_label(a.report_time)}",
        "",
        "📐 <b>Analyst Estimates</b>",
        f"   EPS estimate:      {_eps(a.eps_estimate)}",
        f"   Revenue estimate:  {_usd(a.revenue_estimate)}",
        "",
        f"💡 <i>{cta}</i>",
    ]
    return "\n".join(l for l in lines if l != "")


# ─── Post-earnings results ────────────────────────────────────────────────────

def format_earnings(r: EarningsReport) -> str:
    pct = r.eps_surprise_pct
    emoji = _surprise_emoji(pct)
    lines = [
        # ── Always first: ticker + MEXC symbol ──
        f"📊 <b>{r.ticker}</b>  {_mexc(r.ticker)}  {emoji}",
        f"<b>EARNINGS RESULTS</b>  |  Period: {r.period}  |  Filed: {r.report_date}",
        "",
        f"EPS Actual      <b>{_eps(r.eps_actual)}</b>",
        f"EPS Estimate    {_eps(r.eps_estimate)}",
        f"EPS Surprise    <b>{f'{pct:+.2f}%' if pct is not None else 'N/A'}</b>  {emoji}",
        "",
        f"Revenue         {_usd(r.revenue)}",
        f"Net Income      {_usd(r.net_income)}",
        f"Gross Profit    {_usd(r.gross_profit)}",
    ]
    if pct is not None:
        if pct >= 5:    hint = "📈 Strong beat — consider LONG on MEXC"
        elif pct <= -5: hint = "📉 Big miss — consider SHORT on MEXC"
        else:           hint = "➡️ In-line — wait for the price reaction"
        lines += ["", f"💡 <i>{hint}</i>"]
    return "\n".join(lines)


# ─── News ─────────────────────────────────────────────────────────────────────

def format_news(item: NewsItem) -> str:
    emoji = _sentiment_emoji(item.sentiment)
    lines = [
        # ── Always first: ticker + MEXC symbol + sentiment ──
        f"{emoji} <b>{item.ticker}</b>  {_mexc(item.ticker)}  [{item.sentiment.upper()}]",
        "",
        f"<b>{item.headline}</b>",
        "",
        item.summary or "",
        "",
        f"🕐 {item.published_at}  |  {item.source}",
    ]
    if item.url:
        lines.append(f'<a href="{item.url}">Read full article →</a>')
    return "\n".join(l for l in lines if l is not None)


# ─── Upcoming calendar ────────────────────────────────────────────────────────

def format_upcoming_calendar(items: list[dict]) -> str:
    if not items:
        return "📅 No earnings scheduled in the next 14 days for your watchlist."
    lines = ["📅 <b>Upcoming Earnings — Next 14 Days</b>\n"]
    prev_date = None
    for item in items:
        if item["date"] != prev_date:
            lines.append(f"\n<b>── {item['date']} ──</b>")
            prev_date = item["date"]
        days = item["days_until"]
        badge = "📍 <b>TODAY</b>" if days == 0 else "🔔 <b>TOMORROW</b>" if days == 1 else f"in {days}d"
        mexc_str = "  ".join(f"<code>{s}</code>" for s in item.get("mexc_symbols", []))
        lines.append(
            f"• <b>{item['ticker']}</b>  {mexc_str}  {badge}\n"
            f"  {_time_label(item.get('report_time',''))}  |  EPS est: {_eps(item.get('eps_estimate'))}"
        )
    return "\n".join(lines)


# ─── Batch packer ─────────────────────────────────────────────────────────────

def pack_messages(texts: list[str], limit: int = 4000) -> list[str]:
    """Pack multiple formatted messages into fewest possible Telegram sends."""
    batches, current, length = [], [], 0
    sep = "\n\n" + "─" * 30 + "\n\n"
    for text in texts:
        chunk = (sep + text) if current else text
        if length + len(chunk) > limit and current:
            batches.append("".join(current))
            current, length = [text], len(text)
        else:
            current.append(chunk)
            length += len(chunk)
    if current:
        batches.append("".join(current))
    return batches
