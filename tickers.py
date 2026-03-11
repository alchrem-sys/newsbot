# MEXC perpetual symbol → real Yahoo Finance / Finnhub ticker
MEXC_TO_TICKER: dict[str, str] = {
    "COINUSDT":   "COIN",
    "FIGUSDT":    "FIG",
    "TSLAUSDT":   "TSLA",
    "CVNAUSDT":   "CVNA",
    "NVDAUSDT":   "NVDA",
    "NAS100USDT": "QQQ",
    "AMATUSDT":   "AMAT",
    "SP500USDT":  "SPY",
    "MSTRUSDT":   "MSTR",
    "GOOGLUSDT":  "GOOGL",
    "QCOMUSDT":   "QCOM",
    "HK50USDT":   "^HSI",
    "CRMUSDT":    "CRM",
    "AMZNUSDT":   "AMZN",
    "FUTUUSDT":   "FUTU",
    "AAPLUSDT":   "AAPL",
    "MUUSDT":     "MU",
    "SHOPUSDT":   "SHOP",
    "WMTUSDT":    "WMT",
    "MSFTUSDT":   "MSFT",
    "US30USDT":   "DIA",
    "QQQUSDT":    "QQQ",
    "CSCOUSDT":   "CSCO",
    "HOODUSDT":   "HOOD",
    "KOUSDT":     "KO",
    "VZUSDT":     "VZ",
    "INTCUSDT":   "INTC",
    "GEUSDT":     "GE",
    "JNJUSDT":    "JNJ",
    "MAUSDT":     "MA",
    "AMDUSDT":    "AMD",
    "METAUSDT":   "META",
    "RDDTUSDT":   "RDDT",
    "SPOTUSDT":   "SPOT",
    "NFLXUSDT":   "NFLX",
    "ORCLUSDT":   "ORCL",
    "ASMLUSDT":   "ASML",
    "PEPUSDT":    "PEP",
    "ACNUSDT":    "ACN",
    "XOMUSDT":    "XOM",
    "VUSDT":      "V",
    "NKEUSDT":    "NKE",
    "SMCIUSDT":   "SMCI",
    "UNHUSDT":    "UNH",
    "NOWUSDT":    "NOW",
    "GSUSDT":     "GS",
    "LLYUSDT":    "LLY",
    "LRCXUSDT":   "LRCX",
    "IBMUSDT":    "IBM",
    "COSTUSDT":   "COST",
    "BAUSDT":     "BA",
    "JDUSDT":     "JD",
    "JPMUSDT":    "JPM",
}

# Indices/ETFs — skip earnings for these
INDEX_TICKERS = {"QQQ", "SPY", "DIA", "^HSI"}

# Unique real tickers
ALL_TICKERS: list[str] = sorted(set(MEXC_TO_TICKER.values()))

# Tickers with real earnings reports
EQUITY_TICKERS: list[str] = [t for t in ALL_TICKERS if t not in INDEX_TICKERS]

# Reverse map: real ticker → list of MEXC symbols
TICKER_TO_MEXC: dict[str, list[str]] = {}
for _mexc, _ticker in MEXC_TO_TICKER.items():
    TICKER_TO_MEXC.setdefault(_ticker, []).append(_mexc)
