"""
formatters.py

Design rule: TICKER + MEXC SYMBOL always on line 1 of every message.

Changes vs previous version:
  - _money(v, currency) replaces hardcoded _usd() — shows HKD, EUR, CNY etc.
  - _eps_fmt(v, currency) replaces hardcoded _eps()
  - format_intraday_reminder() added for 2h/1h/30m/5m/1m alerts
  - format_pre_earnings() shows exact time in both UTC and ET
  - format_upcoming_calendar() shows exact time per entry
"""

from datetime import timezone
from typing import Optional
from zoneinfo import ZoneInfo

from earnings import EarningsReport, IntradayReminder, UpcomingEarnings
from news import NewsItem
from tickers import TICKER_TO_MEXC

ET = ZoneInfo("America/New_York")


# ─── Currency-aware money formatters ─────────────────────────────────────────

# Currencies that use a symbol prefix vs those that use the ISO code
_CURRENCY_SYMBOLS = {
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "JPY": "¥",
    "CNY": "¥",
    "HKD": None,   # show as "HKD 6.44B"
    "AUD": "A$",
    "CAD": "C$",
    "CHF": "CHF ",
    "KRW": "₩",
    "INR": "₹",
    "SEK": None,
    "DKK": None,
    "NOK": None,
    "SGD": "S$",
    "TWD": None,
}

def _currency_prefix(currency: str) -> str:
    """Return the display prefix for a currency e.g. '$', 'HKD ', '€'."""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    if symbol is None:
        return f"{currency.upper()} "
    return symbol


def _money(v: Optional[float], currency: str = "USD") -> str:
    """Format a large financial value with correct currency."""
    if v is None:
        return "N/A"
    prefix = _currency_prefix(currency)
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:  return f"{sign}{prefix}{a/1e9:.2f}B"
    if a >= 1e6:  return f"{sign}{prefix}{a/1e6:.2f}M"
    return f"{sign}{prefix}{a:,.2f}"


def _eps_fmt(v: Optional[float], currency: str = "USD") -> str:
    """Format EPS with correct currency symbol."""
    if v is None:
        return "N/A"
    prefix = _currency_prefix(currency)
    return f"{prefix}{v:.4f}"


# ─── Other helpers ────────────────────────────────────────────────────────────

def _mexc(ticker: str) -> str:
    return "  ".join(f"<code>{s}</code>" for s in TICKER_TO_MEXC.get(ticker, []))

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

def _fmt_exact_time(dt) -> str:
    """Show time in both ET and UTC so it's unambiguous."""
    if dt is None:
        return "TBD"
    et_str  = dt.astimezone(ET).strftime("%H:%M ET")
    utc_str = dt.astimezone(timezone.utc).strftime("%H:%M UTC")
    return f"{et_str}  ({utc_str})"


# ─── Pre-earnings countdown (day milestones) ─────────────────────────────────

_MILESTONES = {
    7: ("🗓", "EARNINGS IN 1 WEEK",  "Plan your direction now."),
    3: ("⏰", "EARNINGS IN 3 DAYS",  "Prepare your entry."),
    1: ("🔔", "EARNINGS TOMORROW",   "Final check — ready to act."),
    0: ("🚨", "EARNINGS TODAY",      "Last chance to position on MEXC!"),
}

def format_pre_earnings(a: UpcomingEarnings) -> str:
    icon, title, cta = _MILESTONES.get(a.milestone, ("📅", "EARNINGS SOON", ""))
    lines = [
        f"{icon} <b>{a.ticker}</b>  {_mexc(a.ticker)}",
        f"<b>{title}</b>",
        "",
        f"📆 Date:        <b>{a.earnings_date}</b>",
        f"⏳ Days until:  <b>{a.days_until}</b>" if a.days_until > 0 else "",
        f"🕐 Time:        {_fmt_exact_time(a.exact_time)}",
        f"📋 Session:     {_time_label(a.report_time)}",
        "",
        f"📐 <b>Analyst Estimates</b>  <i>({a.currency})</i>",
        f"   EPS:      {_eps_fmt(a.eps_estimate, a.currency)}",
        f"   Revenue:  {_money(a.revenue_estimate, a.currency)}",
        "",
        f"💡 <i>{cta}</i>",
    ]
    return "\n".join(l for l in lines if l != "")


# ─── Intraday reminders (2h/1h/30m/5m/1m) ───────────────────────────────────

_INTRADAY_LABELS = {
    120: ("⏰", "2 HOURS TO EARNINGS"),
    60:  ("🔔", "1 HOUR TO EARNINGS"),
    30:  ("⚡", "30 MINUTES TO EARNINGS"),
    5:   ("🚨", "5 MINUTES TO EARNINGS"),
    1:   ("🔴", "1 MINUTE TO EARNINGS — LAST CALL"),
}

def format_intraday_reminder(r: IntradayReminder) -> str:
    icon, title = _INTRADAY_LABELS.get(r.minutes_before, ("⏱", f"{r.minutes_before}MIN TO EARNINGS"))
    lines = [
        # ── Ticker + MEXC always first ──
        f"{icon} <b>{r.ticker}</b>  {_mexc(r.ticker)}",
        f"<b>{title}</b>",
        "",
        f"🕐 Announcement:  <b>{_fmt_exact_time(r.exact_time)}</b>",
        f"📋 Session:       {_time_label(r.report_time)}",
        "",
        f"📐 <b>Estimates</b>  <i>({r.currency})</i>",
        f"   EPS:      {_eps_fmt(r.eps_estimate, r.currency)}",
        f"   Revenue:  {_money(r.revenue_estimate, r.currency)}",
        "",
        "💡 <i>Position now before the number drops.</i>",
    ]
    return "\n".join(l for l in lines if l != "")


# ─── Post-earnings results ────────────────────────────────────────────────────

def format_earnings(r: EarningsReport) -> str:
    pct = r.eps_surprise_pct
    emoji = _surprise_emoji(pct)
    lines = [
        f"📊 <b>{r.ticker}</b>  {_mexc(r.ticker)}  {emoji}",
        f"<b>EARNINGS RESULTS</b>  |  Period: {r.period}  |  Filed: {r.report_date}",
        f"<i>Currency: {r.currency}</i>",
        "",
        f"EPS Actual      <b>{_eps_fmt(r.eps_actual, r.currency)}</b>",
        f"EPS Estimate    {_eps_fmt(r.eps_estimate, r.currency)}",
        f"EPS Surprise    <b>{f'{pct:+.2f}%' if pct is not None else 'N/A'}</b>  {emoji}",
        "",
        f"Revenue         {_money(r.revenue, r.currency)}",
        f"Net Income      {_money(r.net_income, r.currency)}",
        f"Gross Profit    {_money(r.gross_profit, r.currency)}",
    ]
    if pct is not None:
        if pct >= 5:    hint = "📈 Strong beat — consider LONG on MEXC"
        elif pct <= -5: hint = "📉 Big miss — consider SHORT on MEXC"
        else:           hint = "➡️ In-line — wait for price reaction"
        lines += ["", f"💡 <i>{hint}</i>"]
    return "\n".join(lines)


# ─── News ─────────────────────────────────────────────────────────────────────

def format_news(item: NewsItem) -> str:
    emoji = _sentiment_emoji(item.sentiment)
    lines = [
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
        exact = _fmt_exact_time(item.get("exact_time"))
        lines.append(
            f"• <b>{item['ticker']}</b>  {mexc_str}  {badge}\n"
            f"  🕐 {exact}  |  {_time_label(item.get('report_time', ''))}"
        )
    return "\n".join(lines)


# ─── Batch packer ─────────────────────────────────────────────────────────────

def pack_messages(texts: list[str], limit: int = 4000) -> list[str]:
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
