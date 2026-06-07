from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CHART_PATH = ROOT / "kaspa-chart.png"
KST = timezone(timedelta(hours=9))
IMAGE_SCALE = 1.3
TRANSACTION_BUCKET_SECONDS = 5 * 60
TOCATA_HARDFORK_AT = datetime(2026, 6, 30, 16, 15, tzinfo=timezone.utc)


@dataclass
class Ticker:
    exchange: str
    pair: str
    last: float
    bid: float | None
    ask: float | None
    base_volume: float | None
    quote_volume: float | None
    unit: str
    ok: bool = True
    error: str = ""
    volume_change_pct: float | None = None
    price_change_pct: float | None = None
    market_rank: int | None = None
    futures_quote_volume: float | None = None
    futures_last: float | None = None
    futures_only: bool = False


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class HashratePoint:
    ts: int
    ths: float


@dataclass
class TransactionPoint:
    ts: int
    count: int


@dataclass
class MarketHistoryStats:
    previous_volume: float | None
    previous_close: float | None


@dataclass
class FearGreedIndex:
    value: int
    classification: str


@dataclass
class GlobalRanks:
    coinmarketcap: int | None = None
    coingecko: int | None = None


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def telegram_chat_id() -> str:
    chat_id = env("TELEGRAM_CHAT_ID")
    if chat_id.isdigit() and len(chat_id) >= 10:
        return f"-100{chat_id}"
    return chat_id


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"message_id": None, "history": []}
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state: dict[str, Any]) -> None:
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_PATH)


def http_json(url: str, timeout: float = 8.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kaspa-info-telegram/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_coinone() -> Ticker:
    payload = http_json("https://api.coinone.co.kr/public/v2/ticker_new/KRW/KAS")
    ticker = payload["tickers"][0]
    asks = ticker.get("best_asks") or []
    bids = ticker.get("best_bids") or []
    return Ticker(
        exchange="Coinone",
        pair="KAS/KRW",
        last=float(ticker["last"]),
        bid=as_float(bids[0].get("price") if bids else None),
        ask=as_float(asks[0].get("price") if asks else None),
        base_volume=as_float(ticker.get("target_volume")),
        quote_volume=as_float(ticker.get("quote_volume")),
        unit="KRW",
    )


def fetch_usdt_krw() -> float:
    payload = http_json("https://api.coinone.co.kr/public/v2/ticker_new/KRW/USDT")
    ticker = payload["tickers"][0]
    return float(ticker["last"])


def fetch_fear_greed_index() -> FearGreedIndex:
    payload = http_json("https://api.alternative.me/fng/?limit=1&format=json")
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError("fear greed data missing")
    row = rows[0]
    return FearGreedIndex(
        value=int(row["value"]),
        classification=str(row.get("value_classification") or ""),
    )


def fetch_coinmarketcap_rank() -> int:
    api_key = env("CMC_API_KEY") or env("COINMARKETCAP_API_KEY")
    if api_key:
        request = urllib.request.Request(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol=KAS",
            headers={
                "Accept": "application/json",
                "User-Agent": "kaspa-info-telegram/1.0",
                "X-CMC_PRO_API_KEY": api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=8.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return int(payload["data"]["KAS"]["cmc_rank"])

    request = urllib.request.Request(
        "https://coinmarketcap.com/currencies/kaspa/",
        headers={
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0 kaspa-info-telegram/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=12.0) as response:
        body = response.read().decode("utf-8", errors="replace")
    match = re.search(r'"rank":(\d+)', body)
    if not match:
        raise RuntimeError("coinmarketcap rank missing")
    return int(match.group(1))


def fetch_coingecko_rank() -> int:
    payload = http_json(
        "https://api.coingecko.com/api/v3/coins/kaspa?"
        "localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
    )
    return int(payload["market_cap_rank"])


def fetch_global_ranks() -> GlobalRanks:
    ranks = GlobalRanks()
    try:
        ranks.coinmarketcap = fetch_coinmarketcap_rank()
    except Exception as exc:
        print(f"coinmarketcap rank fallback: {exc}")
    try:
        ranks.coingecko = fetch_coingecko_rank()
    except Exception as exc:
        print(f"coingecko rank fallback: {exc}")
    return ranks


def fetch_gate() -> Ticker:
    payload = http_json("https://api.gateio.ws/api/v4/spot/tickers?currency_pair=KAS_USDT")
    ticker = payload[0]
    return Ticker(
        exchange="Gate",
        pair="KAS/USDT",
        last=float(ticker["last"]),
        bid=as_float(ticker.get("highest_bid")),
        ask=as_float(ticker.get("lowest_ask")),
        base_volume=as_float(ticker.get("base_volume")),
        quote_volume=as_float(ticker.get("quote_volume")),
        unit="USDT",
    )


def fetch_mexc() -> Ticker:
    ticker = http_json("https://api.mexc.com/api/v3/ticker/24hr?symbol=KASUSDT")
    return Ticker(
        exchange="MEXC",
        pair="KAS/USDT",
        last=float(ticker["lastPrice"]),
        bid=as_float(ticker.get("bidPrice")),
        ask=as_float(ticker.get("askPrice")),
        base_volume=as_float(ticker.get("volume")),
        quote_volume=as_float(ticker.get("quoteVolume")),
        unit="USDT",
    )


def fetch_kucoin() -> Ticker:
    payload = http_json("https://api.kucoin.com/api/v1/market/stats?symbol=KAS-USDT")
    ticker = payload["data"]
    return Ticker(
        exchange="KuCoin",
        pair="KAS/USDT",
        last=float(ticker["last"]),
        bid=as_float(ticker.get("buy")),
        ask=as_float(ticker.get("sell")),
        base_volume=as_float(ticker.get("vol")),
        quote_volume=as_float(ticker.get("volValue")),
        unit="USDT",
    )


def fetch_bybit() -> Ticker:
    payload = http_json("https://api.bybit.com/v5/market/tickers?category=spot&symbol=KASUSDT")
    ticker = payload["result"]["list"][0]
    return Ticker(
        exchange="Bybit",
        pair="KAS/USDT",
        last=float(ticker["lastPrice"]),
        bid=as_float(ticker.get("bid1Price")),
        ask=as_float(ticker.get("ask1Price")),
        base_volume=as_float(ticker.get("volume24h")),
        quote_volume=as_float(ticker.get("turnover24h")),
        unit="USDT",
    )


def fetch_bitget() -> Ticker:
    payload = http_json("https://api.bitget.com/api/v2/spot/market/tickers?symbol=KASUSDT")
    ticker = payload["data"][0]
    return Ticker(
        exchange="Bitget",
        pair="KAS/USDT",
        last=float(ticker["lastPr"]),
        bid=as_float(ticker.get("bidPr")),
        ask=as_float(ticker.get("askPr")),
        base_volume=as_float(ticker.get("baseVolume")),
        quote_volume=as_float(ticker.get("quoteVolume") or ticker.get("usdtVolume")),
        unit="USDT",
    )


def fetch_kraken() -> Ticker:
    payload = http_json("https://api.kraken.com/0/public/Ticker?pair=KASUSD")
    ticker = payload["result"]["KASUSD"]
    last = float(ticker["c"][0])
    base_volume = as_float(ticker["v"][1])
    return Ticker(
        exchange="Kraken",
        pair="KAS/USD",
        last=last,
        bid=as_float(ticker["b"][0]),
        ask=as_float(ticker["a"][0]),
        base_volume=base_volume,
        quote_volume=base_volume * last if base_volume is not None else None,
        unit="USD",
    )


def fetch_htx() -> Ticker:
    payload = http_json("https://api.huobi.pro/market/detail/merged?symbol=kasusdt")
    ticker = payload["tick"]
    return Ticker(
        exchange="HTX",
        pair="KAS/USDT",
        last=float(ticker["close"]),
        bid=as_float((ticker.get("bid") or [None])[0]),
        ask=as_float((ticker.get("ask") or [None])[0]),
        base_volume=as_float(ticker.get("amount")),
        quote_volume=as_float(ticker.get("vol")),
        unit="USDT",
    )


FETCHERS: list[Callable[[], Ticker]] = [
    fetch_coinone,
    fetch_gate,
    fetch_mexc,
    fetch_kucoin,
    fetch_bybit,
    fetch_bitget,
    fetch_kraken,
    fetch_htx,
]


def previous_market_stats(candles: list[tuple[int, float, float]]) -> MarketHistoryStats:
    candles = sorted((ts, close, volume) for ts, close, volume in candles if close > 0)
    if not candles:
        return MarketHistoryStats(previous_volume=None, previous_close=None)
    latest_ts = max(ts for ts, _, _ in candles)
    previous_end = latest_ts - 24 * 60 * 60
    previous_start = previous_end - 24 * 60 * 60
    previous_volume = sum(volume for ts, _, volume in candles if previous_start <= ts < previous_end and volume > 0)
    previous_close = None
    for ts, close, _ in candles:
        if ts <= previous_end:
            previous_close = close
    return MarketHistoryStats(previous_volume=previous_volume or None, previous_close=previous_close)


def fetch_gate_market_history() -> MarketHistoryStats:
    payload = http_json(
        "https://api.gateio.ws/api/v4/spot/candlesticks?"
        "currency_pair=KAS_USDT&interval=1h&limit=48"
    )
    return previous_market_stats([(int(row[0]), float(row[2]), float(row[1])) for row in payload])


def fetch_mexc_market_history() -> MarketHistoryStats:
    payload = http_json("https://api.mexc.com/api/v3/klines?symbol=KASUSDT&interval=60m&limit=48")
    return previous_market_stats([(int(int(row[0]) / 1000), float(row[4]), float(row[7])) for row in payload])


def fetch_kucoin_market_history() -> MarketHistoryStats:
    now = int(time.time())
    payload = http_json(
        "https://api.kucoin.com/api/v1/market/candles?"
        f"symbol=KAS-USDT&type=1hour&startAt={now - 49 * 60 * 60}&endAt={now}"
    )
    return previous_market_stats([(int(row[0]), float(row[2]), float(row[6])) for row in payload["data"]])


def fetch_bybit_market_history() -> MarketHistoryStats:
    payload = http_json("https://api.bybit.com/v5/market/kline?category=spot&symbol=KASUSDT&interval=60&limit=48")
    rows = payload["result"]["list"]
    return previous_market_stats([(int(int(row[0]) / 1000), float(row[4]), float(row[6])) for row in rows])


def fetch_bitget_market_history() -> MarketHistoryStats:
    payload = http_json("https://api.bitget.com/api/v2/spot/market/candles?symbol=KASUSDT&granularity=1h&limit=48")
    return previous_market_stats([(int(int(row[0]) / 1000), float(row[4]), float(row[6])) for row in payload["data"]])


def fetch_kraken_market_history() -> MarketHistoryStats:
    since = int(time.time()) - 49 * 60 * 60
    payload = http_json(f"https://api.kraken.com/0/public/OHLC?pair=KASUSD&interval=60&since={since}")
    rows = payload["result"].get("KASUSD", [])
    return previous_market_stats([(int(row[0]), float(row[4]), float(row[6]) * float(row[4])) for row in rows])


def fetch_htx_market_history() -> MarketHistoryStats:
    payload = http_json("https://api.huobi.pro/market/history/kline?symbol=kasusdt&period=60min&size=48")
    return previous_market_stats([(int(row["id"]), float(row["close"]), float(row["vol"])) for row in payload["data"]])


MARKET_HISTORY_FETCHERS: dict[str, Callable[[], MarketHistoryStats]] = {
    "Gate": fetch_gate_market_history,
    "MEXC": fetch_mexc_market_history,
    "KuCoin": fetch_kucoin_market_history,
    "Bybit": fetch_bybit_market_history,
    "Bitget": fetch_bitget_market_history,
    "Kraken": fetch_kraken_market_history,
    "HTX": fetch_htx_market_history,
}


def rank_symbol_by_volume(
    rows: list[tuple[str, float | None]],
    target_symbol: str,
) -> int | None:
    volumes = [(symbol, volume) for symbol, volume in rows if volume is not None]
    if not volumes:
        return None
    ranked = sorted(volumes, key=lambda item: item[1], reverse=True)
    for index, (symbol, _) in enumerate(ranked, start=1):
        if symbol == target_symbol:
            return index
    return None


def fetch_gate_market_rank() -> int | None:
    payload = http_json("https://api.gateio.ws/api/v4/spot/tickers")
    return rank_symbol_by_volume(
        [
            (row.get("currency_pair", ""), as_float(row.get("quote_volume")))
            for row in payload
            if str(row.get("currency_pair", "")).endswith("_USDT")
        ],
        "KAS_USDT",
    )


def fetch_mexc_market_rank() -> int | None:
    payload = http_json("https://api.mexc.com/api/v3/ticker/24hr")
    return rank_symbol_by_volume(
        [
            (row.get("symbol", ""), as_float(row.get("quoteVolume")))
            for row in payload
            if str(row.get("symbol", "")).endswith("USDT")
        ],
        "KASUSDT",
    )


def fetch_kucoin_market_rank() -> int | None:
    payload = http_json("https://api.kucoin.com/api/v1/market/allTickers")
    rows = payload.get("data", {}).get("ticker", [])
    return rank_symbol_by_volume(
        [
            (row.get("symbol", ""), as_float(row.get("volValue")))
            for row in rows
            if str(row.get("symbol", "")).endswith("-USDT")
        ],
        "KAS-USDT",
    )


def fetch_bybit_market_rank() -> int | None:
    payload = http_json("https://api.bybit.com/v5/market/tickers?category=spot")
    rows = payload.get("result", {}).get("list", [])
    return rank_symbol_by_volume(
        [
            (row.get("symbol", ""), as_float(row.get("turnover24h")))
            for row in rows
            if str(row.get("symbol", "")).endswith("USDT")
        ],
        "KASUSDT",
    )


def fetch_bitget_market_rank() -> int | None:
    payload = http_json("https://api.bitget.com/api/v2/spot/market/tickers")
    rows = payload.get("data", [])
    return rank_symbol_by_volume(
        [
            (row.get("symbol", ""), as_float(row.get("quoteVolume") or row.get("usdtVolume")))
            for row in rows
            if str(row.get("symbol", "")).endswith("USDT")
        ],
        "KASUSDT",
    )


def fetch_kraken_market_rank() -> int | None:
    pairs_payload = http_json("https://api.kraken.com/0/public/AssetPairs")
    ticker_payload = http_json("https://api.kraken.com/0/public/Ticker")
    tickers = ticker_payload.get("result", {})
    rows = []
    for pair_key, pair in pairs_payload.get("result", {}).items():
        if not str(pair.get("wsname", "")).endswith("/USD"):
            continue
        ticker = tickers.get(pair_key)
        if not ticker:
            continue
        last = as_float((ticker.get("c") or [None])[0])
        base_volume = as_float((ticker.get("v") or [None, None])[1])
        rows.append((pair_key, base_volume * last if base_volume is not None and last is not None else None))
    return rank_symbol_by_volume(rows, "KASUSD")


def fetch_htx_market_rank() -> int | None:
    payload = http_json("https://api.huobi.pro/market/tickers")
    return rank_symbol_by_volume(
        [
            (row.get("symbol", ""), as_float(row.get("vol")))
            for row in payload.get("data", [])
            if str(row.get("symbol", "")).endswith("usdt")
        ],
        "kasusdt",
    )


MARKET_RANK_FETCHERS: dict[str, Callable[[], int | None]] = {
    "Gate": fetch_gate_market_rank,
    "MEXC": fetch_mexc_market_rank,
    "KuCoin": fetch_kucoin_market_rank,
    "Bybit": fetch_bybit_market_rank,
    "Bitget": fetch_bitget_market_rank,
    "Kraken": fetch_kraken_market_rank,
    "HTX": fetch_htx_market_rank,
}


def futures_ticker(
    exchange: str,
    last: float | None,
    quote_volume: float | None,
    market_rank: int | None = None,
    price_change_pct: float | None = None,
) -> Ticker:
    return Ticker(
        exchange=exchange,
        pair="KAS/USDT PERP",
        last=last or 0.0,
        bid=None,
        ask=None,
        base_volume=None,
        quote_volume=None,
        unit="USDT",
        futures_quote_volume=quote_volume,
        futures_last=last,
        futures_only=True,
        market_rank=market_rank,
        price_change_pct=price_change_pct,
    )


def fetch_gate_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=KAS_USDT")
    ticker = payload[0]
    return as_float(ticker.get("last")), as_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle"))


def fetch_mexc_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://contract.mexc.com/api/v1/contract/ticker?symbol=KAS_USDT")
    ticker = payload.get("data", {})
    return as_float(ticker.get("lastPrice")), as_float(ticker.get("amount24"))


def fetch_kucoin_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://api-futures.kucoin.com/api/v1/contracts/active")
    for ticker in payload.get("data", []):
        if ticker.get("symbol") == "KASUSDTM":
            return as_float(ticker.get("lastTradePrice")), as_float(ticker.get("turnoverOf24h"))
    raise RuntimeError("KASUSDTM not found")


def fetch_bybit_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://api.bybit.com/v5/market/tickers?category=linear&symbol=KASUSDT")
    ticker = payload.get("result", {}).get("list", [{}])[0]
    return as_float(ticker.get("lastPrice")), as_float(ticker.get("turnover24h"))


def fetch_bitget_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://api.bitget.com/api/v2/mix/market/ticker?symbol=KASUSDT&productType=USDT-FUTURES")
    ticker = payload.get("data", [{}])[0]
    return as_float(ticker.get("lastPr")), as_float(ticker.get("quoteVolume") or ticker.get("usdtVolume"))


def fetch_htx_futures() -> tuple[float | None, float | None]:
    payload = http_json("https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code=KAS-USDT")
    ticker = payload.get("tick", {})
    return as_float(ticker.get("close")), as_float(ticker.get("trade_turnover"))


def fetch_binance_futures() -> Ticker:
    payload = http_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    rank = rank_symbol_by_volume(
        [
            (ticker.get("symbol", ""), as_float(ticker.get("quoteVolume")))
            for ticker in payload
            if str(ticker.get("symbol", "")).endswith("USDT")
        ],
        "KASUSDT",
    )
    ticker = next((ticker for ticker in payload if ticker.get("symbol") == "KASUSDT"), {})
    return futures_ticker(
        "Binance",
        as_float(ticker.get("lastPrice")),
        as_float(ticker.get("quoteVolume")),
        market_rank=rank,
        price_change_pct=as_float(ticker.get("priceChangePercent")),
    )


def fetch_coinbase_futures() -> Ticker:
    payload = http_json("https://api.international.coinbase.com/api/v1/instruments")
    rank = rank_symbol_by_volume(
        [
            (ticker.get("symbol", ""), as_float(ticker.get("notional_24hr")))
            for ticker in payload
            if ticker.get("type") == "PERP"
        ],
        "KAS-PERP",
    )
    ticker = next((ticker for ticker in payload if ticker.get("symbol") == "KAS-PERP"), {})
    quote = ticker.get("quote") or {}
    return futures_ticker(
        "Coinbase",
        as_float(quote.get("best_ask_price") or quote.get("best_bid_price") or ticker.get("reference_price")),
        as_float(ticker.get("notional_24hr")),
        market_rank=rank,
    )


def fetch_echobit_futures() -> Ticker:
    payload = http_json("https://www.echobit.com/mainapi/exchange/all/tickers")
    rows = payload.get("data", [])
    rank = rank_symbol_by_volume(
        [
            (ticker.get("s", ""), as_float(ticker.get("qv")))
            for ticker in rows
            if str(ticker.get("s", "")).endswith("-SWAP-USDT")
        ],
        "KAS-SWAP-USDT",
    )
    ticker = next((ticker for ticker in rows if ticker.get("s") == "KAS-SWAP-USDT"), {})
    if not ticker:
        raise RuntimeError("KAS-SWAP-USDT not found")
    price_change = as_float(ticker.get("m"))
    return futures_ticker(
        "Echobit",
        as_float(ticker.get("c")),
        as_float(ticker.get("qv")),
        market_rank=rank,
        price_change_pct=price_change * 100 if price_change is not None else None,
    )


def fetch_bitmart_futures() -> Ticker:
    payload = http_json("https://api-cloud-v2.bitmart.com/contract/public/details")
    rows = payload.get("data", {}).get("symbols", [])
    if not rows:
        raise RuntimeError("KASUSDT not found")
    rank = rank_symbol_by_volume(
        [
            (ticker.get("symbol", ""), as_float(ticker.get("turnover_24h")))
            for ticker in rows
            if str(ticker.get("symbol", "")).endswith("USDT")
        ],
        "KASUSDT",
    )
    ticker = next((ticker for ticker in rows if ticker.get("symbol") == "KASUSDT"), {})
    if not ticker:
        raise RuntimeError("KASUSDT not found")
    price_change = as_float(ticker.get("change_24h"))
    return futures_ticker(
        "BitMart",
        as_float(ticker.get("last_price")),
        as_float(ticker.get("turnover_24h")),
        market_rank=rank,
        price_change_pct=price_change * 100 if price_change is not None else None,
    )


FUTURES_FETCHERS: dict[str, Callable[[], tuple[float | None, float | None]]] = {
    "Gate": fetch_gate_futures,
    "MEXC": fetch_mexc_futures,
    "KuCoin": fetch_kucoin_futures,
    "Bybit": fetch_bybit_futures,
    "Bitget": fetch_bitget_futures,
    "HTX": fetch_htx_futures,
}


FUTURES_ONLY_FETCHERS: list[Callable[[], Ticker]] = [
    fetch_binance_futures,
    fetch_coinbase_futures,
    fetch_echobit_futures,
    fetch_bitmart_futures,
]


def apply_market_changes(tickers: list[Ticker]) -> None:
    for ticker in tickers:
        if not ticker.ok:
            continue
        fetcher = MARKET_HISTORY_FETCHERS.get(ticker.exchange)
        if not fetcher:
            continue
        try:
            stats = fetcher()
            quote_volume = quote_volume_value(ticker)
            if quote_volume is not None and stats.previous_volume:
                ticker.volume_change_pct = ((quote_volume - stats.previous_volume) / stats.previous_volume) * 100
            if stats.previous_close:
                ticker.price_change_pct = ((ticker.last - stats.previous_close) / stats.previous_close) * 100
        except Exception as exc:
            ticker.error = ticker.error or f"market change: {str(exc)[:80]}"


def apply_market_ranks(tickers: list[Ticker]) -> None:
    for ticker in tickers:
        if not ticker.ok:
            continue
        fetcher = MARKET_RANK_FETCHERS.get(ticker.exchange)
        if not fetcher:
            continue
        try:
            ticker.market_rank = fetcher()
        except Exception as exc:
            ticker.error = ticker.error or f"market rank: {str(exc)[:80]}"


def apply_futures_volumes(tickers: list[Ticker]) -> None:
    for ticker in tickers:
        if not ticker.ok:
            continue
        fetcher = FUTURES_FETCHERS.get(ticker.exchange)
        if not fetcher:
            continue
        try:
            futures_last, futures_volume = fetcher()
            ticker.futures_last = futures_last
            ticker.futures_quote_volume = futures_volume
        except Exception as exc:
            ticker.error = ticker.error or f"futures: {str(exc)[:80]}"


def fetch_futures_only_tickers() -> list[Ticker]:
    tickers: list[Ticker] = []
    for fetcher in FUTURES_ONLY_FETCHERS:
        try:
            tickers.append(fetcher())
        except Exception as exc:
            exchange = fetcher.__name__.replace("fetch_", "").replace("_futures", "").title()
            tickers.append(
                futures_ticker(
                    exchange=exchange,
                    last=None,
                    quote_volume=None,
                )
            )
            tickers[-1].error = str(exc)[:120]
    return tickers


def fetch_gate_candles(currency_pair: str = "KAS_USDT", hours: int = 24, interval: str = "5m") -> list[Candle]:
    limit = min(max(hours * 12, 1), 1000)
    payload = http_json(
        "https://api.gateio.ws/api/v4/spot/candlesticks?"
        f"currency_pair={urllib.parse.quote(currency_pair)}&interval={interval}&limit={limit}"
    )
    candles: list[Candle] = []
    for row in payload:
        # Gate: [timestamp, quote_volume, close, high, low, open, base_volume, finished]
        candles.append(
            Candle(
                ts=int(row[0]),
                open=float(row[5]),
                high=float(row[3]),
                low=float(row[4]),
                close=float(row[2]),
                volume=float(row[6]),
            )
        )
    return sorted(candles, key=lambda candle: candle.ts)


def fetch_hashrate() -> float | None:
    payload = http_json("https://api.kaspa.org/info/hashrate")
    return as_float(payload.get("hashrate"))


def fetch_hashrate_history(hours: int = 24) -> list[HashratePoint]:
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    points: list[HashratePoint] = []
    for day in days:
        payload = http_json(f"https://api.kaspa.org/info/hashrate/history/{day}?resolution=15m")
        for row in payload:
            hashrate_kh = as_float(row.get("hashrate_kh"))
            timestamp_ms = row.get("timestamp")
            if hashrate_kh is None or timestamp_ms is None:
                continue
            points.append(HashratePoint(ts=int(int(timestamp_ms) / 1000), ths=hashrate_kh / 1_000_000_000))

    if not points:
        return []
    latest_ts = max(point.ts for point in points)
    cutoff = latest_ts - hours * 60 * 60
    return sorted((point for point in points if point.ts >= cutoff), key=lambda point: point.ts)


def fetch_transaction_counts(hours: int = 24) -> list[TransactionPoint]:
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    points: list[TransactionPoint] = []
    for day in days:
        payload = http_json(f"https://api.kaspa.org/transactions/count/{day}")
        for row in payload:
            timestamp_ms = row.get("timestamp")
            count = row.get("regular")
            if timestamp_ms is None or count is None:
                continue
            points.append(TransactionPoint(ts=int(int(timestamp_ms) / 1000), count=int(count)))

    if not points:
        return []
    latest_ts = max(point.ts for point in points)
    cutoff = latest_ts - hours * 60 * 60
    return sorted((point for point in points if point.ts >= cutoff), key=lambda point: point.ts)


def fetch_latest_blue_score() -> int:
    blocks = http_json("https://api.kaspa.org/blocks-from-bluescore?blueScoreLt=999999999999&includeTransactions=false")
    return max(int(block["header"]["blueScore"]) for block in blocks)


def fetch_virtual_chain(blue_score_gte: int) -> list[dict[str, Any]]:
    return http_json(
        "https://api.kaspa.org/virtual-chain?"
        f"blueScoreGte={blue_score_gte}&limit=100&includeCoinbase=false",
        timeout=20,
    )


def update_transaction_5m_history(state: dict[str, Any], hours: int = 24) -> list[TransactionPoint]:
    latest_blue_score = fetch_latest_blue_score()
    last_blue_score = as_float(state.get("last_transaction_blue_score"))
    if state.get("transaction_source") != "virtual_chain_5m":
        bucket_counts: dict[int, int] = {}
        last_blue_score = None
    else:
        bucket_counts = {
            int(point["ts"]): int(point["count"])
            for point in state.get("transaction_5m_buckets", [])
            if point.get("ts") is not None and point.get("count") is not None
        }

    if last_blue_score is None or latest_blue_score - int(last_blue_score) > 6_000:
        last_blue_score = max(0, latest_blue_score - 3_000)

    start_blue_score = (int(last_blue_score) + 1) // 100 * 100
    max_seen_blue_score = int(last_blue_score)
    pages = 0
    while start_blue_score <= latest_blue_score and pages < 80:
        for block in fetch_virtual_chain(start_blue_score):
            blue_score = int(block.get("blue_score") or 0)
            if blue_score <= int(last_blue_score) or blue_score > latest_blue_score:
                continue
            max_seen_blue_score = max(max_seen_blue_score, blue_score)
            timestamp_ms = block.get("timestamp")
            if timestamp_ms is None:
                continue
            tx_count = sum(1 for tx in block.get("transactions") or [] if tx.get("is_accepted", True))
            if tx_count <= 0:
                continue
            ts = int(int(timestamp_ms) / 1000)
            bucket_ts = ts - (ts % TRANSACTION_BUCKET_SECONDS)
            bucket_counts[bucket_ts] = bucket_counts.get(bucket_ts, 0) + tx_count
        start_blue_score += 100
        pages += 1

    cutoff = int(time.time()) - hours * 60 * 60
    points = [
        {"ts": bucket_ts, "count": count}
        for bucket_ts, count in sorted(bucket_counts.items())
        if bucket_ts >= cutoff and count > 0
    ]
    state["transaction_source"] = "virtual_chain_5m"
    state["last_transaction_blue_score"] = max_seen_blue_score
    state["transaction_5m_buckets"] = points
    return [TransactionPoint(ts=point["ts"], count=point["count"]) for point in points]


def fetch_tickers() -> list[Ticker]:
    tickers: list[Ticker] = []
    for fetcher in FETCHERS:
        try:
            tickers.append(fetcher())
        except Exception as exc:
            tickers.append(
                Ticker(
                    exchange=fetcher.__name__.replace("fetch_", "").title(),
                    pair="KAS",
                    last=0,
                    bid=None,
                    ask=None,
                    base_volume=None,
                    quote_volume=None,
                    unit="",
                    ok=False,
                    error=str(exc)[:120],
                )
            )
    apply_market_changes(tickers)
    apply_market_ranks(tickers)
    apply_futures_volumes(tickers)
    tickers.extend(fetch_futures_only_tickers())
    return tickers


def history_as_candles(history: list[dict[str, Any]]) -> list[Candle]:
    candles: list[Candle] = []
    for point in history:
        price = as_float(point.get("price"))
        ts = point.get("ts")
        if price is None or ts is None:
            continue
        candles.append(Candle(ts=int(ts), open=price, high=price, low=price, close=price, volume=0))
    return candles


def update_history(state: dict[str, Any], tickers: list[Ticker], max_points: int = 288) -> list[dict[str, Any]]:
    usd_prices = [
        ticker.last
        for ticker in tickers
        if ticker.ok and not ticker.futures_only and ticker.unit in {"USDT", "USD"} and ticker.last > 0
    ]
    if not usd_prices:
        return state.get("history", [])

    point = {"ts": int(time.time()), "price": sum(usd_prices) / len(usd_prices)}
    history = list(state.get("history", []))
    history.append(point)
    state["history"] = history[-max_points:]
    return state["history"]


def fmt_price(ticker: Ticker) -> str:
    if not ticker.ok:
        return "error"
    if ticker.unit == "KRW":
        return f"{ticker.last:,.2f} KRW"
    return f"{ticker.last:.6f} USDT"


def price_value(ticker: Ticker) -> str:
    if not ticker.ok:
        return "error"
    if ticker.unit == "KRW":
        return f"{ticker.last:,.2f}"
    return f"{ticker.last:.6f}"


def fmt_volume(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {unit}"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K {unit}"
    return f"{value:.2f} {unit}"


def compact_volume(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def quote_volume_millions(value: float | None) -> str:
    if value is None:
        return "-"
    if 0 < value < 1_000:
        return "<0.001M"
    if 0 < value < 10_000:
        return f"{value / 1_000_000:.3f}M"
    return f"{value / 1_000_000:.2f}M"


def compact_count(value: int | float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def fmt_hashrate(ths: float | None) -> str:
    if ths is None:
        return "-"
    if ths >= 1_000_000:
        return f"{ths / 1_000_000:.2f} EH/s"
    if ths >= 1_000:
        return f"{ths / 1_000:.2f} PH/s"
    return f"{ths:.2f} TH/s"


def fmt_btc_price(price: float | None) -> str:
    if price is None:
        return "-"
    return f"{price:,.0f} USDT"


def fmt_countdown(target: datetime, now: datetime) -> str:
    remaining_seconds = int((target - now.astimezone(timezone.utc)).total_seconds())
    if remaining_seconds <= 0:
        return "started"
    days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"D-{days} {hours:02d}:{minutes:02d}:{seconds:02d}"


def fmt_fear_greed(index: FearGreedIndex | None) -> str:
    if index is None:
        return "-"
    labels = {
        "Extreme Fear": "극단공포",
        "Fear": "공포",
        "Neutral": "중립",
        "Greed": "탐욕",
        "Extreme Greed": "극단탐욕",
    }
    classification = labels.get(index.classification, index.classification)
    return f"{index.value} {classification}".strip()


def fmt_global_ranks(ranks: GlobalRanks | None) -> str:
    if ranks is None:
        return "-"
    cmc = f"#{ranks.coinmarketcap}" if ranks.coinmarketcap is not None else "-"
    cg = f"#{ranks.coingecko}" if ranks.coingecko is not None else "-"
    return f"CMC {cmc} / CG {cg}"


def quote_volume_value(ticker: Ticker) -> float | None:
    if ticker.quote_volume is not None:
        return ticker.quote_volume
    if ticker.base_volume is not None:
        return ticker.base_volume * ticker.last
    return None


def market_tickers(tickers: list[Ticker]) -> list[Ticker]:
    return sorted(
        [ticker for ticker in tickers if ticker.exchange != "Coinone"],
        key=lambda ticker: (ticker.ok, quote_volume_value(ticker) or 0),
        reverse=True,
    )


def market_price(ticker: Ticker) -> str:
    if not ticker.ok:
        return "error"
    if ticker.futures_only and ticker.futures_last is not None:
        return f"{ticker.futures_last:.4f}"
    return f"{ticker.last:.4f}"


def market_volume(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return quote_volume_millions(quote_volume_value(ticker))


def market_futures_volume(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return quote_volume_millions(ticker.futures_quote_volume)


def market_rank(ticker: Ticker) -> str:
    if not ticker.ok or ticker.market_rank is None:
        return "-"
    return f"#{ticker.market_rank}"


def volume_change_value(ticker: Ticker) -> str:
    return change_value(ticker.volume_change_pct)


def price_change_value(ticker: Ticker) -> str:
    return change_value(ticker.price_change_pct)


def change_value(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.1f}%"


def volume_change_color(ticker: Ticker) -> str:
    return change_color(ticker.volume_change_pct)


def price_change_color(ticker: Ticker) -> str:
    return change_color(ticker.price_change_pct)


def change_color(value: float | None) -> str:
    if value is None:
        return "#9aa4b2"
    if value >= 0:
        return "#37d67a"
    return "#ff6b6b"


def caption_volume(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return quote_volume_millions(quote_volume_value(ticker))


def caption_total_volume(value: float) -> str:
    return quote_volume_millions(value)


def caption_change(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.0f}%"


def caption_exchange(exchange: str) -> str:
    aliases = {
        "KuCoin": "KuC",
        "Kraken": "Krkn",
        "Bitget": "Bitgt",
    }
    return aliases.get(exchange, exchange)[:5]


def caption_row(exchange: str, price: str, price_change: str, volume: str, volume_change: str) -> str:
    return f"{exchange[:5]:<5} {price:>6} {price_change:>5} {volume:>5} {volume_change:>5}"


def display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_left(text: str, width: int) -> str:
    return " " * max(0, width - display_width(text)) + text


def pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def caption_summary_row(label: str, value: str) -> str:
    return f"{pad_right(label, 9)} {pad_left(value, 18)}"


def render_caption(
    tickers: list[Ticker],
    hashrate_ths: float | None = None,
    display_dt: datetime | None = None,
    usdt_krw: float | None = None,
    fear_greed: FearGreedIndex | None = None,
    global_ranks: GlobalRanks | None = None,
) -> str:
    display_dt = display_dt or datetime.now(KST).replace(second=0, microsecond=0)
    now = display_dt.strftime("%Y-%m-%d %H:%M:%S KST")
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if not ticker.futures_only and ticker.unit in {"USDT", "USD"}]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)

    summary_rows = []
    if avg_usdt:
        summary_rows.append(caption_summary_row("Avg", f"{avg_usdt:.6f} USDT"))
    if coinone:
        summary_rows.append(caption_summary_row("KRW", f"{coinone.last:,.2f} KRW"))
    if usdt_krw is None and avg_usdt and coinone:
        usdt_krw = coinone.last / avg_usdt
    if usdt_krw:
        summary_rows.append(caption_summary_row("USDT/KRW", f"{usdt_krw:,.2f} KRW"))
    if fear_greed is not None:
        summary_rows.append(caption_summary_row("F&G", fmt_fear_greed(fear_greed)))
    if global_ranks is not None and (global_ranks.coinmarketcap is not None or global_ranks.coingecko is not None):
        summary_rows.append(caption_summary_row("Ranks", fmt_global_ranks(global_ranks)))
    summary_rows.append(caption_summary_row("Tocata", fmt_countdown(TOCATA_HARDFORK_AT, display_dt)))
    if hashrate_ths is not None:
        summary_rows.append(caption_summary_row("Hashrate", fmt_hashrate(hashrate_ths)))

    lines = ["<b>KASPA 실시간 티커</b>", f"<code>{html.escape(now)}</code>"]
    if summary_rows:
        lines.append("<b>Market / Network</b>")
        for row in summary_rows:
            lines.append(f"<code>{html.escape(row)}</code>")

    lines.append("")
    lines.append(f"<code>{html.escape(caption_row('Exch', 'Price', 'P24h', 'Vol$', 'V24h'))}</code>")
    total_volume = 0.0
    for ticker in [ticker for ticker in market_tickers(tickers) if not ticker.futures_only]:
        if not ticker.ok:
            lines.append(f"<code>{html.escape(caption_row(caption_exchange(ticker.exchange), 'error', '-', '-', '-'))}</code>")
            continue
        quote_volume = quote_volume_value(ticker)
        if quote_volume is not None:
            total_volume += quote_volume
        lines.append(
            f"<code>{html.escape(caption_row(caption_exchange(ticker.exchange), market_price(ticker), caption_change(ticker.price_change_pct), caption_volume(ticker), caption_change(ticker.volume_change_pct)))}</code>"
        )
    lines.append(f"<code>{html.escape(caption_row('Total', '', '', caption_total_volume(total_volume), ''))}</code>")

    return "\n".join(lines)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def render_chart(
    candles: list[Candle],
    tickers: list[Ticker],
    hashrate_points: list[HashratePoint] | None = None,
    hashrate_ths: float | None = None,
    btc_candles: list[Candle] | None = None,
    transaction_points: list[TransactionPoint] | None = None,
    display_dt: datetime | None = None,
) -> bytes:
    width, height = 1600, 1080
    image = Image.new("RGB", (width, height), "#10131a")
    draw = ImageDraw.Draw(image)
    title_font = load_font(50, bold=True)
    text_font = load_font(31)
    small_font = load_font(23)

    draw.rectangle((0, 0, width, 110), fill="#171b24")
    draw.text((48, 28), "KASPA LIVE TICKER", fill="#f3f6fb", font=title_font)
    draw.text((width - 500, 32), "24H / 5M CANDLES", fill="#9aa4b2", font=text_font)
    display_dt = display_dt or datetime.now(KST).replace(second=0, microsecond=0)
    draw.text((width - 235, 72), display_dt.strftime("%H:%M:%S KST"), fill="#9aa4b2", font=small_font)

    chart_box = (84, 142, 1516, 590)
    draw.rounded_rectangle(chart_box, radius=12, fill="#0b0e13", outline="#2b3342", width=2)

    for index in range(1, 5):
        y = chart_box[1] + index * ((chart_box[3] - chart_box[1]) / 5)
        draw.line((chart_box[0] + 1, y, chart_box[2] - 1, y), fill="#1d2532", width=1)

    if len(candles) >= 2:
        lows = [candle.low for candle in candles]
        highs = [candle.high for candle in candles]
        min_price, max_price = min(lows), max(highs)
        padding = (max_price - min_price) * 0.08 or 0.0001
        min_price -= padding
        max_price += padding

        def y_for(price: float) -> float:
            return chart_box[3] - 22 - ((price - min_price) / (max_price - min_price)) * (chart_box[3] - chart_box[1] - 44)

        inner_left = chart_box[0] + 30
        label_left = chart_box[2] - 360
        inner_right = label_left - 28
        step = (inner_right - inner_left) / max(len(candles), 1)
        candle_width = max(2, min(9, int(step * 0.68)))
        candle_start_ts = candles[0].ts
        candle_end_ts = candles[-1].ts

        def x_for_ts(ts: int) -> float:
            span = max(candle_end_ts - candle_start_ts, 1)
            return inner_left + ((ts - candle_start_ts) / span) * (inner_right - inner_left)

        def scaled_line_points(points: list[tuple[int, float]]) -> list[tuple[float, float]]:
            values = [value for _, value in points]
            min_value, max_value = min(values), max(values)
            value_padding = (max_value - min_value) * 0.12 or max_value * 0.03 or 1
            min_value -= value_padding
            max_value += value_padding

            def line_y_for(value: float) -> float:
                return chart_box[3] - 22 - ((value - min_value) / (max_value - min_value)) * (
                    chart_box[3] - chart_box[1] - 44
                )

            return [(x_for_ts(ts), line_y_for(value)) for ts, value in points]

        end_label_slots: list[tuple[float, float]] = []

        def draw_end_label(x: float, y: float, text: str, fill: str) -> None:
            pad_x, pad_y = 8, 5
            bbox = draw.textbbox((0, 0), text, font=small_font)
            label_width = bbox[2] - bbox[0] + pad_x * 2
            label_height = bbox[3] - bbox[1] + pad_y * 2
            label_x = label_left
            label_y = max(chart_box[1] + 8, min(y - label_height / 2, chart_box[3] - label_height - 8))
            for used_top, used_bottom in end_label_slots:
                if label_y < used_bottom + 6 and label_y + label_height > used_top - 6:
                    label_y = used_bottom + 6
            if label_y + label_height > chart_box[3] - 8:
                label_y = chart_box[3] - label_height - 8
                for used_top, used_bottom in reversed(end_label_slots):
                    if label_y < used_bottom + 6 and label_y + label_height > used_top - 6:
                        label_y = used_top - label_height - 6
            label_y = max(chart_box[1] + 8, min(label_y, chart_box[3] - label_height - 8))
            end_label_slots.append((label_y, label_y + label_height))
            draw.line((x + 6, y, label_x - 8, y), fill=fill, width=1)
            draw.rounded_rectangle(
                (label_x, label_y, label_x + label_width, label_y + label_height),
                radius=5,
                fill="#0b0e13",
                outline=fill,
                width=1,
            )
            draw.text((label_x + pad_x, label_y + pad_y - bbox[1]), text, fill=fill, font=small_font)

        transaction_points = transaction_points or []
        visible_transactions = [
            point
            for point in transaction_points
            if candle_start_ts <= point.ts <= candle_end_ts and point.count > 0
        ]
        if visible_transactions:
            max_transactions = max(point.count for point in visible_transactions) or 1
            transaction_diffs = [
                later.ts - earlier.ts
                for earlier, later in zip(visible_transactions, visible_transactions[1:])
                if later.ts > earlier.ts
            ]
            transaction_bucket_seconds = min(transaction_diffs) if transaction_diffs else TRANSACTION_BUCKET_SECONDS
            bar_slot = (transaction_bucket_seconds / max(candle_end_ts - candle_start_ts, 1)) * (inner_right - inner_left)
            bar_width = max(3, min(20, bar_slot * 0.72))
            max_bar_height = (chart_box[3] - chart_box[1]) * 0.32
            last_transaction = visible_transactions[-1]
            last_transaction_x = x_for_ts(last_transaction.ts + transaction_bucket_seconds // 2)
            last_transaction_height = (last_transaction.count / max_transactions) * max_bar_height
            for point in visible_transactions:
                x = x_for_ts(point.ts + transaction_bucket_seconds // 2)
                bar_height = (point.count / max_transactions) * max_bar_height
                draw.rectangle(
                    (x - bar_width / 2, chart_box[3] - bar_height - 2, x + bar_width / 2, chart_box[3] - 2),
                    fill="#1f2c44",
                )
            tx_text = f"TX {compact_count(last_transaction.count)}/5m  max {compact_count(max_transactions)}"
            tx_bbox = draw.textbbox((0, 0), tx_text, font=small_font)
            tx_pad_x, tx_pad_y = 8, 5
            tx_width = tx_bbox[2] - tx_bbox[0] + tx_pad_x * 2
            tx_height = tx_bbox[3] - tx_bbox[1] + tx_pad_y * 2
            tx_label_x = label_left
            tx_label_y = chart_box[3] - tx_height - 14
            tx_anchor_y = max(
                chart_box[1] + 18,
                min(chart_box[3] - last_transaction_height - 2, chart_box[3] - 18),
            )
            draw.line((last_transaction_x + 6, tx_anchor_y, tx_label_x - 8, tx_label_y + tx_height / 2), fill="#5f7eb6", width=1)
            draw.rounded_rectangle(
                (tx_label_x, tx_label_y, tx_label_x + tx_width, tx_label_y + tx_height),
                radius=5,
                fill="#0b0e13",
                outline="#5f7eb6",
                width=1,
            )
            draw.text(
                (tx_label_x + tx_pad_x, tx_label_y + tx_pad_y - tx_bbox[1]),
                tx_text,
                fill="#9fb8ee",
                font=small_font,
            )

        for index, candle in enumerate(candles):
            x = inner_left + index * step + step / 2
            open_y = y_for(candle.open)
            close_y = y_for(candle.close)
            high_y = y_for(candle.high)
            low_y = y_for(candle.low)
            up = candle.close >= candle.open
            color = "#38d87c" if up else "#ff5c72"
            draw.line((x, high_y, x, low_y), fill=color, width=1)
            top, bottom = min(open_y, close_y), max(open_y, close_y)
            if bottom - top < 2:
                bottom = top + 2
            draw.rectangle((x - candle_width / 2, top, x + candle_width / 2, bottom), fill=color)

        last = candles[-1]
        last_x = inner_left + (len(candles) - 1) * step + step / 2

        hashrate_points = hashrate_points or []
        visible_hashrates = [
            point
            for point in hashrate_points
            if candle_start_ts <= point.ts <= candle_end_ts and point.ths > 0
        ]
        hashrate_label: tuple[float, float, str, str] | None = None
        if len(visible_hashrates) >= 2:
            line_points = scaled_line_points([(point.ts, point.ths) for point in visible_hashrates])
            draw.line(line_points, fill="#ff304f", width=3, joint="curve")
            hashrate_label = (
                line_points[-1][0],
                line_points[-1][1],
                f"Hashrate {fmt_hashrate(hashrate_ths or visible_hashrates[-1].ths)}",
                "#ff7184",
            )

        btc_candles = btc_candles or []
        visible_btc = [
            candle
            for candle in btc_candles
            if candle_start_ts <= candle.ts <= candle_end_ts and candle.close > 0
        ]
        btc_label: tuple[float, float, str, str] | None = None
        if len(visible_btc) >= 2:
            line_points = scaled_line_points([(candle.ts, candle.close) for candle in visible_btc])
            draw.line(line_points, fill="#42a5ff", width=3, joint="curve")
            btc_label = (line_points[-1][0], line_points[-1][1], f"BTC {fmt_btc_price(visible_btc[-1].close)}", "#78bdff")

        draw_end_label(last_x, y_for(last.close), f"KAS {last.close:.6f}", "#f3f6fb")
        if hashrate_label:
            draw_end_label(*hashrate_label)
        if btc_label:
            draw_end_label(*btc_label)

        label_indexes = [0, len(candles) // 4, len(candles) // 2, (len(candles) * 3) // 4, len(candles) - 1]
        for label_index in label_indexes:
            candle = candles[label_index]
            x = inner_left + label_index * step + step / 2
            draw.line((x, chart_box[1] + 1, x, chart_box[3] - 1), fill="#182030", width=1)
            label = datetime.fromtimestamp(candle.ts, tz=KST).strftime("%H:%M")
            draw.text((x - 34, chart_box[3] + 16), label, fill="#9aa4b2", font=small_font)

        draw.text((chart_box[0] + 28, chart_box[1] + 18), f"High {max(highs):.6f}", fill="#9aa4b2", font=small_font)
        draw.text((chart_box[0] + 28, chart_box[3] - 48), f"Low {min(lows):.6f}", fill="#9aa4b2", font=small_font)
    else:
        draw.text((chart_box[0] + 380, chart_box[1] + 160), "collecting candle history...", fill="#9aa4b2", font=text_font)

    def draw_right(x: int, y: int, text: str, fill: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((x - (bbox[2] - bbox[0]), y), text, fill=fill, font=font)

    header_y = 640
    exchange_x = 84
    rank_right_x = 355
    price_right_x = 560
    price_change_right_x = 715
    volume_right_x = 1035
    change_right_x = 1180
    futures_volume_right_x = 1516
    draw.text((exchange_x, header_y), "Exchange", fill="#9aa4b2", font=small_font)
    draw_right(rank_right_x, header_y, "Mkt Rank", "#9aa4b2", small_font)
    draw_right(price_right_x, header_y, "Price", "#9aa4b2", small_font)
    draw_right(price_change_right_x, header_y, "P24h", "#9aa4b2", small_font)
    draw_right(volume_right_x, header_y, "Spot $", "#9aa4b2", small_font)
    draw_right(change_right_x, header_y, "S24h", "#9aa4b2", small_font)
    draw_right(futures_volume_right_x, header_y, "Fut $", "#9aa4b2", small_font)
    draw.line((84, header_y + 32, 1516, header_y + 32), fill="#2b3342", width=1)

    y = 678
    total_volume = 0.0
    total_futures_volume = 0.0
    row_height = 28
    row_font = load_font(24)
    for ticker in market_tickers(tickers):
        color = "#37d67a" if ticker.ok else "#ff6b6b"
        quote_volume = quote_volume_value(ticker) if ticker.ok else None
        futures_quote_volume = ticker.futures_quote_volume if ticker.ok else None
        if quote_volume is not None:
            total_volume += quote_volume
        if futures_quote_volume is not None:
            total_futures_volume += futures_quote_volume
        if (y - 678) // row_height % 2 == 0:
            draw.rectangle((84, y - 1, 1516, y + row_height - 1), fill="#121720")
        draw.text((exchange_x, y + 3), ticker.exchange, fill=color, font=row_font)
        draw_right(rank_right_x, y + 3, market_rank(ticker), "#f3f6fb", row_font)
        draw_right(price_right_x, y + 3, market_price(ticker), "#f3f6fb", row_font)
        draw_right(price_change_right_x, y + 3, price_change_value(ticker), price_change_color(ticker), row_font)
        draw_right(volume_right_x, y + 3, market_volume(ticker), "#f3f6fb", row_font)
        draw_right(change_right_x, y + 3, volume_change_value(ticker), volume_change_color(ticker), row_font)
        draw_right(futures_volume_right_x, y + 3, market_futures_volume(ticker), "#f3f6fb", row_font)
        y += row_height

    draw.line((84, y + 2, 1516, y + 2), fill="#2b3342", width=1)
    draw.text((exchange_x, y + 8), "Total", fill="#9aa4b2", font=row_font)
    draw_right(volume_right_x, y + 8, quote_volume_millions(total_volume), "#f3f6fb", row_font)
    draw_right(futures_volume_right_x, y + 8, quote_volume_millions(total_futures_volume), "#f3f6fb", row_font)

    if IMAGE_SCALE != 1:
        image = image.resize((int(width * IMAGE_SCALE), int(height * IMAGE_SCALE)), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def telegram_api(method: str, fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]] | None = None) -> Any:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    url = f"https://api.telegram.org/bot{token}/{method}"
    if not files:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(url, data=data)
    else:
        boundary = f"----kaspa-{uuid.uuid4().hex}"
        body = io.BytesIO()
        for key, value in fields.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.write(str(value).encode("utf-8"))
            body.write(b"\r\n")
        for key, (filename, content, content_type) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode())
            body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
            body.write(content)
            body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            url,
            data=body.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
            description = payload.get("description", body)
        except json.JSONDecodeError:
            description = body
        raise RuntimeError(f"Telegram {method} failed: {description}") from exc
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload["result"]


def check_telegram_config() -> None:
    bot = telegram_api("getMe", {})
    print(f"bot: @{bot.get('username')} id={bot.get('id')}")

    chat_id = telegram_chat_id()
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    chat = telegram_api("getChat", {"chat_id": chat_id})
    print(f"chat: {chat.get('type')} {chat.get('title') or chat.get('username') or chat.get('id')}")


def send_or_edit_message(state: dict[str, Any], chart_png: bytes, caption: str) -> None:
    chat_id = telegram_chat_id()
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    message_id = state.get("message_id")
    if message_id:
        media = json.dumps(
            {
                "type": "photo",
                "media": "attach://chart",
                "caption": caption,
                "parse_mode": "HTML",
            },
            ensure_ascii=False,
        )
        telegram_api(
            "editMessageMedia",
            {"chat_id": chat_id, "message_id": message_id, "media": media},
            {"chart": ("kaspa-chart.png", chart_png, "image/png")},
        )
        return

    result = telegram_api(
        "sendPhoto",
        {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        {"photo": ("kaspa-chart.png", chart_png, "image/png")},
    )
    state["message_id"] = result["message_id"]
    save_state(state)


def run_once(dry_run: bool = False) -> None:
    display_dt = datetime.now(KST).replace(second=0, microsecond=0)
    state = load_state()
    tickers = fetch_tickers()
    history = update_history(state, tickers)
    hashrate_ths = None
    usdt_krw = None
    fear_greed = None
    global_ranks = None
    hashrate_points: list[HashratePoint] = []
    transaction_points: list[TransactionPoint] = []
    try:
        usdt_krw = fetch_usdt_krw()
    except Exception as exc:
        print(f"usdt/krw fallback: {exc}")
    try:
        fear_greed = fetch_fear_greed_index()
    except Exception as exc:
        print(f"fear greed fallback: {exc}")
    try:
        global_ranks = fetch_global_ranks()
    except Exception as exc:
        print(f"global ranks fallback: {exc}")
    try:
        hashrate_ths = fetch_hashrate()
        hashrate_points = fetch_hashrate_history()
    except Exception as exc:
        print(f"hashrate fallback: {exc}")
    try:
        transaction_points = update_transaction_5m_history(state)
    except Exception as exc:
        print(f"transactions fallback: {exc}")
    caption = render_caption(tickers, hashrate_ths, display_dt, usdt_krw, fear_greed, global_ranks)
    try:
        candles = fetch_gate_candles()
    except Exception as exc:
        print(f"candle fallback: {exc}")
        candles = history_as_candles(history)
    btc_candles: list[Candle] = []
    try:
        btc_candles = fetch_gate_candles("BTC_USDT")
    except Exception as exc:
        print(f"btc fallback: {exc}")
    chart_png = render_chart(candles, tickers, hashrate_points, hashrate_ths, btc_candles, transaction_points, display_dt)
    CHART_PATH.write_bytes(chart_png)

    fingerprint = hashlib.sha256(chart_png + caption.encode("utf-8")).hexdigest()
    if state.get("last_fingerprint") == fingerprint and state.get("message_id"):
        print("skip: no visible change")
        return
    save_state(state)

    if dry_run:
        state["last_fingerprint"] = fingerprint
        save_state(state)
        print(caption)
        print(f"chart: {CHART_PATH}")
        return

    send_or_edit_message(state, chart_png, caption)
    state["last_fingerprint"] = fingerprint
    save_state(state)
    print(f"updated message_id={state.get('message_id')}")


def sleep_until_next_tick(interval: int) -> None:
    now = time.time()
    next_tick = ((int(now) // interval) + 1) * interval
    time.sleep(max(0.2, next_tick - now))


def loop(interval: int, dry_run: bool = False) -> None:
    while True:
        sleep_until_next_tick(interval)
        try:
            run_once(dry_run=dry_run)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"telegram/http error {exc.code}: {body[:300]}")
        except Exception as exc:
            print(f"error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kaspa Telegram live ticker")
    parser.add_argument("--once", action="store_true", help="run one update and exit")
    parser.add_argument("--dry-run", action="store_true", help="render locally without Telegram API calls")
    parser.add_argument("--check-telegram", action="store_true", help="validate bot token and chat access")
    parser.add_argument("--interval", type=int, default=int(env("TICKER_INTERVAL_SECONDS", "60")))
    args = parser.parse_args()

    if args.check_telegram:
        try:
            check_telegram_config()
        except RuntimeError as exc:
            print(f"error: {exc}")
            sys.exit(1)
        return

    if args.once:
        try:
            run_once(dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"error: {exc}")
            sys.exit(1)
    else:
        loop(max(args.interval, 5), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
