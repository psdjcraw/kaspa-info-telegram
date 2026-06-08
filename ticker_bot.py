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
SETTINGS_PATH = ROOT / "settings.json"
CHART_PATH = ROOT / "kaspa-chart.png"
LOG_PATH = ROOT / "ticker.log"
ERR_LOG_PATH = ROOT / "ticker.err.log"
KST = timezone(timedelta(hours=9))
IMAGE_SCALE = 1.3
TRANSACTION_BUCKET_SECONDS = 60
TOCATA_HARDFORK_AT = datetime(2026, 6, 30, 16, 15, tzinfo=timezone.utc)
MARKET_RANK_CACHE_SECONDS = 60
GLOBAL_RANK_CACHE_SECONDS = 60 * 60
SLOW_INDEX_CACHE_SECONDS = 60
WHALE_WALLET_ADDRESS = "kaspa:qpz2vgvlxhmyhmt22h538pjzmvvd52nuut80y5zulgpvyerlskvvwm7n4uk5a"
SOMPI_PER_KAS = 100_000_000
WHALE_TRANSFER_ALERT_KAS = 1_000_000
MAX_RUNTIME_LOG_BYTES = 512 * 1024
HTTP_METRICS: list[dict[str, Any]] = []
DEFAULT_SETTINGS: dict[str, Any] = {
    "show_aux_panel": True,
    "show_futures_interpretation": True,
    "show_api_quality": True,
}


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
    futures_open_interest: float | None = None
    futures_funding_rate: float | None = None
    futures_only: bool = False
    market_rank_delta: int | None = None
    spot_volume_delta_pct: float | None = None
    futures_volume_delta_pct: float | None = None
    open_interest_delta_pct: float | None = None


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


@dataclass
class MarketDominance:
    bitcoin: float | None = None
    ethereum: float | None = None
    alt_ex_btc_eth: float | None = None
    kaspa: float | None = None


@dataclass
class CryptoCaps:
    total1: float | None = None
    total2: float | None = None
    total3: float | None = None
    kaspa: float | None = None


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


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            settings.update(loaded)
    return settings


def save_state(state: dict[str, Any]) -> None:
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    if STATE_PATH.exists():
        STATE_PATH.with_suffix(".bak").write_bytes(STATE_PATH.read_bytes())
    tmp_path.replace(STATE_PATH)


def trim_runtime_log(path: Path, max_bytes: int = MAX_RUNTIME_LOG_BYTES) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        with path.open("rb") as handle:
            handle.seek(-max_bytes // 2, os.SEEK_END)
            tail = handle.read()
        path.write_bytes(b"[trimmed old log]\n" + tail)
    except OSError as exc:
        print(f"log trim skipped {path.name}: {exc}")


def trim_runtime_logs() -> None:
    trim_runtime_log(LOG_PATH)
    trim_runtime_log(ERR_LOG_PATH)


def cached_value(
    state: dict[str, Any],
    key: str,
    ttl_seconds: int,
    fetcher: Callable[[], Any],
) -> Any:
    now = int(time.time())
    cache = state.setdefault("cache", {})
    item = cache.get(key)
    if isinstance(item, dict) and now - int(item.get("ts", 0)) < ttl_seconds and "value" in item:
        return item["value"]
    try:
        value = fetcher()
    except Exception:
        if isinstance(item, dict) and "value" in item:
            return item["value"]
        raise
    cache[key] = {"ts": now, "value": value}
    return value


def http_json(url: str, timeout: float = 8.0) -> Any:
    started_at = time.perf_counter()
    host = urllib.parse.urlparse(url).netloc
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kaspa-info-telegram/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        HTTP_METRICS.append({"host": host, "ms": int((time.perf_counter() - started_at) * 1000), "ok": False})
        raise
    HTTP_METRICS.append({"host": host, "ms": int((time.perf_counter() - started_at) * 1000), "ok": True})
    return payload


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


def fetch_market_dominance() -> MarketDominance:
    try:
        payload = http_json("https://api.coingecko.com/api/v3/global")
        percentages = payload.get("data", {}).get("market_cap_percentage", {})
        btc = as_float(percentages.get("btc"))
        eth = as_float(percentages.get("eth"))
    except Exception as exc:
        print(f"coingecko dominance fallback: {exc}")
        payload = http_json("https://api.coinlore.net/api/global/")
        row = payload[0] if isinstance(payload, list) and payload else {}
        btc = as_float(row.get("btc_d"))
        eth = as_float(row.get("eth_d"))
    alt = 100 - btc - eth if btc is not None and eth is not None else None
    return MarketDominance(bitcoin=btc, ethereum=eth, alt_ex_btc_eth=alt)


def fetch_crypto_caps() -> CryptoCaps:
    try:
        global_payload = http_json("https://api.coingecko.com/api/v3/global")
        data = global_payload.get("data", {})
        total1 = as_float((data.get("total_market_cap") or {}).get("usd"))
        percentages = data.get("market_cap_percentage", {})
        btc = as_float(percentages.get("btc"))
        eth = as_float(percentages.get("eth"))
        kaspa_payload = http_json(
            "https://api.coingecko.com/api/v3/coins/kaspa?"
            "localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
        )
        kaspa_cap = as_float(((kaspa_payload.get("market_data") or {}).get("market_cap") or {}).get("usd"))
    except Exception as exc:
        print(f"coingecko cap fallback: {exc}")
        global_payload = http_json("https://api.coinlore.net/api/global/")
        row = global_payload[0] if isinstance(global_payload, list) and global_payload else {}
        total1 = as_float(row.get("total_mcap"))
        btc = as_float(row.get("btc_d"))
        eth = as_float(row.get("eth_d"))
        kaspa_payload = http_json("https://api.coinlore.net/api/ticker/?id=70485")
        kaspa_row = kaspa_payload[0] if isinstance(kaspa_payload, list) and kaspa_payload else {}
        kaspa_cap = as_float(kaspa_row.get("market_cap_usd"))
    total2 = total1 * (100 - btc) / 100 if total1 is not None and btc is not None else None
    total3 = (
        total1 * (100 - btc - eth) / 100
        if total1 is not None and btc is not None and eth is not None
        else None
    )
    return CryptoCaps(total1=total1, total2=total2, total3=total3, kaspa=kaspa_cap)


def fetch_fear_greed_index_cached(state: dict[str, Any]) -> FearGreedIndex:
    def fetch() -> dict[str, Any]:
        index = fetch_fear_greed_index()
        return {"value": index.value, "classification": index.classification}

    value = cached_value(
        state,
        "fear_greed",
        SLOW_INDEX_CACHE_SECONDS,
        fetch,
    )
    return FearGreedIndex(value=int(value["value"]), classification=str(value["classification"]))


def fetch_global_ranks_cached(state: dict[str, Any]) -> GlobalRanks:
    def fetch() -> dict[str, Any]:
        ranks = fetch_global_ranks()
        return {"coinmarketcap": ranks.coinmarketcap, "coingecko": ranks.coingecko}

    value = cached_value(
        state,
        "global_ranks",
        GLOBAL_RANK_CACHE_SECONDS,
        fetch,
    )
    return GlobalRanks(
        coinmarketcap=value.get("coinmarketcap"),
        coingecko=value.get("coingecko"),
    )


def fetch_market_dominance_cached(state: dict[str, Any]) -> MarketDominance:
    def fetch() -> dict[str, Any]:
        dominance = fetch_market_dominance()
        return {
            "bitcoin": dominance.bitcoin,
            "ethereum": dominance.ethereum,
            "alt_ex_btc_eth": dominance.alt_ex_btc_eth,
        }

    value = cached_value(
        state,
        "market_dominance",
        SLOW_INDEX_CACHE_SECONDS,
        fetch,
    )
    return MarketDominance(
        bitcoin=value.get("bitcoin"),
        ethereum=value.get("ethereum"),
        alt_ex_btc_eth=value.get("alt_ex_btc_eth"),
        kaspa=value.get("kaspa"),
    )


def fetch_crypto_caps_cached(state: dict[str, Any]) -> CryptoCaps:
    def fetch() -> dict[str, Any]:
        caps = fetch_crypto_caps()
        return {
            "total1": caps.total1,
            "total2": caps.total2,
            "total3": caps.total3,
            "kaspa": caps.kaspa,
        }

    value = cached_value(
        state,
        "crypto_caps",
        SLOW_INDEX_CACHE_SECONDS,
        fetch,
    )
    return CryptoCaps(
        total1=value.get("total1"),
        total2=value.get("total2"),
        total3=value.get("total3"),
        kaspa=value.get("kaspa"),
    )


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
    open_interest: float | None = None,
    funding_rate: float | None = None,
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
        futures_open_interest=open_interest,
        futures_funding_rate=funding_rate,
        futures_only=True,
        market_rank=market_rank,
        price_change_pct=price_change_pct,
    )


def fetch_gate_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=KAS_USDT")
    ticker = payload[0]
    last = as_float(ticker.get("last"))
    total_size = as_float(ticker.get("total_size"))
    multiplier = as_float(ticker.get("quanto_multiplier"))
    open_interest = total_size * multiplier * last if total_size is not None and multiplier is not None and last is not None else None
    return (
        last,
        as_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle")),
        open_interest,
        as_float(ticker.get("funding_rate")),
    )


def fetch_mexc_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://contract.mexc.com/api/v1/contract/ticker?symbol=KAS_USDT")
    ticker = payload.get("data", {})
    return as_float(ticker.get("lastPrice")), as_float(ticker.get("amount24")), None, as_float(ticker.get("fundingRate"))


def fetch_kucoin_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://api-futures.kucoin.com/api/v1/contracts/active")
    for ticker in payload.get("data", []):
        if ticker.get("symbol") == "KASUSDTM":
            last = as_float(ticker.get("lastTradePrice"))
            open_interest = as_float(ticker.get("openInterest"))
            multiplier = as_float(ticker.get("multiplier"))
            open_interest_value = (
                open_interest * multiplier * last
                if open_interest is not None and multiplier is not None and last is not None
                else None
            )
            return last, as_float(ticker.get("turnoverOf24h")), open_interest_value, as_float(ticker.get("fundingFeeRate"))
    raise RuntimeError("KASUSDTM not found")


def fetch_bybit_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://api.bybit.com/v5/market/tickers?category=linear&symbol=KASUSDT")
    ticker = payload.get("result", {}).get("list", [{}])[0]
    return (
        as_float(ticker.get("lastPrice")),
        as_float(ticker.get("turnover24h")),
        as_float(ticker.get("openInterestValue")),
        as_float(ticker.get("fundingRate")),
    )


def fetch_bitget_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://api.bitget.com/api/v2/mix/market/ticker?symbol=KASUSDT&productType=USDT-FUTURES")
    ticker = payload.get("data", [{}])[0]
    mark_price = as_float(ticker.get("markPrice") or ticker.get("lastPr"))
    holding_amount = as_float(ticker.get("holdingAmount"))
    open_interest = holding_amount * mark_price if holding_amount is not None and mark_price is not None else None
    return (
        as_float(ticker.get("lastPr")),
        as_float(ticker.get("quoteVolume") or ticker.get("usdtVolume")),
        open_interest,
        as_float(ticker.get("fundingRate")),
    )


def fetch_htx_futures() -> tuple[float | None, float | None, float | None, float | None]:
    payload = http_json("https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code=KAS-USDT")
    ticker = payload.get("tick", {})
    funding_payload = http_json("https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code=KAS-USDT")
    funding = as_float((funding_payload.get("data") or {}).get("funding_rate"))
    return as_float(ticker.get("close")), as_float(ticker.get("trade_turnover")), None, funding


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
    open_interest_payload = http_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=KASUSDT")
    premium_payload = http_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=KASUSDT")
    last = as_float(ticker.get("lastPrice"))
    open_interest = as_float(open_interest_payload.get("openInterest"))
    return futures_ticker(
        "Binance",
        last,
        as_float(ticker.get("quoteVolume")),
        open_interest=open_interest * last if open_interest is not None and last is not None else None,
        funding_rate=as_float(premium_payload.get("lastFundingRate")),
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
    last = as_float(quote.get("best_ask_price") or quote.get("best_bid_price") or ticker.get("reference_price"))
    open_interest = as_float(ticker.get("open_interest"))
    return futures_ticker(
        "Coinbase",
        last,
        as_float(ticker.get("notional_24hr")),
        open_interest=open_interest * last if open_interest is not None and last is not None else None,
        funding_rate=None,
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
        funding_rate=None,
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
        open_interest=as_float(ticker.get("open_interest_value")),
        funding_rate=as_float(ticker.get("funding_rate") or ticker.get("expected_funding_rate")),
        market_rank=rank,
        price_change_pct=price_change * 100 if price_change is not None else None,
    )


FUTURES_FETCHERS: dict[str, Callable[[], tuple[float | None, float | None, float | None, float | None]]] = {
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


def apply_market_ranks(state: dict[str, Any], tickers: list[Ticker]) -> None:
    for ticker in tickers:
        if not ticker.ok:
            continue
        fetcher = MARKET_RANK_FETCHERS.get(ticker.exchange)
        if not fetcher:
            continue
        try:
            ticker.market_rank = cached_value(
                state,
                f"market_rank:{ticker.exchange}",
                MARKET_RANK_CACHE_SECONDS,
                fetcher,
            )
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
            futures_last, futures_volume, open_interest, funding_rate = fetcher()
            ticker.futures_last = futures_last
            ticker.futures_quote_volume = futures_volume
            ticker.futures_open_interest = open_interest
            ticker.futures_funding_rate = funding_rate
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


def interval_seconds(interval: str) -> int:
    match = re.fullmatch(r"(\d+)([mhd])", interval)
    if not match:
        raise ValueError(f"unsupported interval: {interval}")
    multipliers = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
    return int(match.group(1)) * multipliers[match.group(2)]


def fetch_gate_candles(currency_pair: str = "KAS_USDT", hours: int = 24, interval: str = "1m") -> list[Candle]:
    seconds = interval_seconds(interval)
    max_limit = 900
    end_ts = int(time.time())
    start_ts = end_ts - hours * 60 * 60
    candles: list[Candle] = []

    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(end_ts, cursor + max_limit * seconds)
        payload = http_json(
            "https://api.gateio.ws/api/v4/spot/candlesticks?"
            f"currency_pair={urllib.parse.quote(currency_pair)}"
            f"&interval={urllib.parse.quote(interval)}"
            f"&from={cursor}&to={chunk_end}&limit={max_limit}"
        )
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
        cursor = chunk_end

    deduped = {candle.ts: candle for candle in candles if start_ts <= candle.ts <= end_ts}
    return sorted(deduped.values(), key=lambda candle: candle.ts)


def fetch_hashrate() -> float | None:
    payload = http_json("https://api.kaspa.org/info/hashrate")
    return as_float(payload.get("hashrate"))


def fetch_hashrate_history(hours: int = 24) -> list[HashratePoint]:
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    points: list[HashratePoint] = []
    for day in days:
        try:
            payload = http_json(f"https://api.kaspa.org/info/hashrate/history/{day}?resolution=1m")
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                payload = http_json(f"https://api.kaspa.org/info/hashrate/history/{day}?resolution=15m")
            else:
                raise
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


def fetch_wallet_balance(address: str = WHALE_WALLET_ADDRESS) -> float:
    payload = http_json(f"https://api.kaspa.org/addresses/{urllib.parse.quote(address, safe='')}/balance")
    return float(payload["balance"]) / SOMPI_PER_KAS


def fetch_wallet_transactions(
    address: str = WHALE_WALLET_ADDRESS,
    limit: int = 100,
    max_pages: int = 5,
    days: int = 30,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cutoff_ms = (int(time.time()) - days * 24 * 60 * 60) * 1000
    encoded = urllib.parse.quote(address, safe="")
    for page in range(max_pages):
        payload = http_json(
            f"https://api.kaspa.org/addresses/{encoded}/full-transactions?"
            f"limit={limit}&offset={page * limit}"
        )
        if not isinstance(payload, list) or not payload:
            break
        rows.extend(payload)
        oldest = min(int(tx.get("block_time") or tx.get("accepting_block_time") or 0) for tx in payload)
        if oldest and oldest < cutoff_ms:
            break
    return rows


def wallet_tx_flow(tx: dict[str, Any], address: str = WHALE_WALLET_ADDRESS) -> tuple[float, float]:
    incoming = 0.0
    outgoing = 0.0
    for output in tx.get("outputs") or []:
        if output.get("script_public_key_address") == address:
            amount = as_float(output.get("amount")) or 0
            incoming += amount / SOMPI_PER_KAS
    for tx_input in tx.get("inputs") or []:
        if tx_input.get("previous_outpoint_address") == address:
            amount = as_float(tx_input.get("previous_outpoint_amount")) or 0
            outgoing += amount / SOMPI_PER_KAS
    return incoming, outgoing


def update_wallet_summary(state: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    balance = fetch_wallet_balance()
    transactions = fetch_wallet_transactions()
    history = [
        item
        for item in state.get("whale_wallet_history", [])
        if isinstance(item, dict) and now - int(item.get("ts", 0) or 0) <= 31 * 24 * 60 * 60
    ]
    history.append({"ts": now, "balance": balance})
    state["whale_wallet_history"] = history

    def baseline_for(seconds: int) -> float | None:
        target = now - seconds
        candidates = [item for item in history if int(item.get("ts", 0) or 0) <= target]
        if not candidates:
            return None
        return float(max(candidates, key=lambda item: int(item.get("ts", 0) or 0)).get("balance") or 0)

    def delta_for(seconds: int) -> tuple[float | None, float | None]:
        baseline = baseline_for(seconds)
        if baseline is None:
            return None, None
        return pct_change(balance, baseline), balance - baseline

    alert = None
    cutoff_ms = (now - 24 * 60 * 60) * 1000
    flow_periods = {
        "24h": {"seconds": 24 * 60 * 60, "in": 0.0, "out": 0.0},
        "7d": {"seconds": 7 * 24 * 60 * 60, "in": 0.0, "out": 0.0},
        "30d": {"seconds": 30 * 24 * 60 * 60, "in": 0.0, "out": 0.0},
    }
    for tx in transactions:
        if not tx.get("is_accepted", True):
            continue
        tx_time = int(tx.get("block_time") or tx.get("accepting_block_time") or 0)
        incoming, outgoing = wallet_tx_flow(tx)
        for item in flow_periods.values():
            if tx_time and tx_time >= (now - int(item["seconds"])) * 1000:
                item["in"] = float(item["in"]) + incoming
                item["out"] = float(item["out"]) + outgoing
        if tx_time and tx_time < cutoff_ms:
            continue
        if incoming >= WHALE_TRANSFER_ALERT_KAS or outgoing >= WHALE_TRANSFER_ALERT_KAS:
            if incoming >= outgoing:
                alert = {"direction": "IN", "amount": incoming, "tx": str(tx.get("transaction_id", ""))[:8]}
            else:
                alert = {"direction": "OUT", "amount": outgoing, "tx": str(tx.get("transaction_id", ""))[:8]}
            break

    period_deltas = {
        "24h": delta_for(24 * 60 * 60),
        "7d": delta_for(7 * 24 * 60 * 60),
        "30d": delta_for(30 * 24 * 60 * 60),
    }
    for period, item in flow_periods.items():
        delta_pct, amount_delta = period_deltas[period]
        if delta_pct is None:
            net = float(item["in"]) - float(item["out"])
            if net:
                estimated_baseline = balance - net
                period_deltas[period] = (pct_change(balance, estimated_baseline), net)
    return {
        "balance": balance,
        "delta_24h": period_deltas["24h"][0],
        "delta_7d": period_deltas["7d"][0],
        "delta_30d": period_deltas["30d"][0],
        "amount_24h": period_deltas["24h"][1],
        "amount_7d": period_deltas["7d"][1],
        "amount_30d": period_deltas["30d"][1],
        "alert": alert,
    }


def fetch_latest_blue_score() -> int:
    blocks = http_json("https://api.kaspa.org/blocks-from-bluescore?blueScoreLt=999999999999&includeTransactions=false")
    return max(int(block["header"]["blueScore"]) for block in blocks)


def fetch_virtual_chain(blue_score_gte: int) -> list[dict[str, Any]]:
    return http_json(
        "https://api.kaspa.org/virtual-chain?"
        f"blueScoreGte={blue_score_gte}&limit=100&includeCoinbase=false",
        timeout=20,
    )


def update_transaction_1m_history(state: dict[str, Any], hours: int = 24) -> list[TransactionPoint]:
    latest_blue_score = fetch_latest_blue_score()
    source = f"virtual_chain_{TRANSACTION_BUCKET_SECONDS}s"
    bucket_key = f"transaction_{TRANSACTION_BUCKET_SECONDS}s_buckets"
    last_blue_score = as_float(state.get("last_transaction_blue_score"))
    if state.get("transaction_source") != source:
        bucket_counts: dict[int, int] = {}
        last_blue_score = None
    else:
        bucket_counts = {
            int(point["ts"]): int(point["count"])
            for point in state.get(bucket_key, [])
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
    state["transaction_source"] = source
    state["last_transaction_blue_score"] = max_seen_blue_score
    state[bucket_key] = points
    return [TransactionPoint(ts=point["ts"], count=point["count"]) for point in points]


def fetch_tickers(state: dict[str, Any]) -> list[Ticker]:
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
    apply_market_ranks(state, tickers)
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


def candles_to_state(candles: list[Candle]) -> list[dict[str, Any]]:
    return [
        {
            "ts": candle.ts,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        for candle in candles
    ]


def candles_from_state(rows: Any) -> list[Candle]:
    candles: list[Candle] = []
    if not isinstance(rows, list):
        return candles
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candles.append(
                Candle(
                    ts=int(row["ts"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(candles, key=lambda candle: candle.ts)


def cache_candles(state: dict[str, Any], key: str, candles: list[Candle]) -> None:
    if candles:
        state.setdefault("chart_cache", {})[key] = {"ts": int(time.time()), "candles": candles_to_state(candles)}


def cached_candles(state: dict[str, Any], key: str) -> list[Candle]:
    cache = state.get("chart_cache", {})
    if not isinstance(cache, dict):
        return []
    item = cache.get(key, {})
    if not isinstance(item, dict):
        return []
    return candles_from_state(item.get("candles"))


def update_history(state: dict[str, Any], tickers: list[Ticker], max_points: int = 1440) -> list[dict[str, Any]]:
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


def compact_kas(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.3f}B KAS"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M KAS"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K KAS"
    return f"{value:.2f} KAS"


def quote_volume_millions(value: float | None) -> str:
    if value is None:
        return "-"
    if 0 < value < 1_000:
        return "<0.001M"
    if 0 < value < 10_000:
        return f"{value / 1_000_000:.3f}M"
    return f"{value / 1_000_000:.2f}M"


def delta_suffix(value: float | None, threshold: float = 0.0) -> str:
    if value is None or abs(value) < threshold:
        return ""
    if value == 0:
        return "0.0%"
    if abs(value) < 10:
        return f"{value:+.1f}%"
    return f"{value:+.0f}%"


def rank_delta_suffix(value: int | None) -> str:
    if value is None or value == 0:
        return ""
    return f"+{value}" if value > 0 else str(value)


def volume_with_delta(value: float | None, delta_pct: float | None) -> str:
    base = quote_volume_millions(value)
    suffix = delta_suffix(delta_pct)
    return f"{base}{suffix}" if suffix else base


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
    return f"{price:,.0f}"


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
    emojis = {
        "Extreme Fear": "🥶",
        "Fear": "😨",
        "Neutral": "😐",
        "Greed": "🤑",
        "Extreme Greed": "🔥",
    }
    emoji = emojis.get(index.classification, "•")
    return f"{index.value} {emoji}"


def caption_signed_number(value: int | None) -> str:
    if value is None:
        return ""
    if value > 0:
        return f" 🟢+{value}"
    if value < 0:
        return f" 🔴{value}"
    return " ⚪0"


def fmt_global_ranks(ranks: GlobalRanks | None, rank_deltas: dict[str, int] | None = None) -> str:
    if ranks is None:
        return "-"
    rank_deltas = rank_deltas or {}
    cmc = (
        f"#{ranks.coinmarketcap}{caption_signed_number(rank_deltas.get('cmc_rank'))}"
        if ranks.coinmarketcap is not None
        else "-"
    )
    cg = (
        f"#{ranks.coingecko}{caption_signed_number(rank_deltas.get('cg_rank'))}"
        if ranks.coingecko is not None
        else "-"
    )
    return f"CMC {cmc} / CG {cg}"


def rank_value(rank: int | None, delta: int | None) -> str:
    if rank is None:
        return "-"
    return f"#{rank}{caption_signed_number(delta)}"


def metric_with_delta(text: str, deltas: dict[str, float], key: str, precision: int = 1) -> str:
    delta = deltas.get(key)
    if delta is None:
        return text
    return f"{text} {caption_signed_percent(delta, precision=precision)}"


def dominance_value(value: float | None, deltas: dict[str, float], key: str) -> str:
    if value is None:
        return "-"
    return metric_with_delta(f"{value:.2f}%", deltas, key, precision=2)


def compact_cap(value: float | None) -> str:
    if value is None:
        return "-"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value:,.0f}"


def compact_cap_billions(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value / 1_000_000_000:.2f}B"


def cap_value(value: float | None, deltas: dict[str, float], key: str, force_billions: bool = False) -> str:
    if value is None:
        return "-"
    text = compact_cap_billions(value) if force_billions else compact_cap(value)
    return metric_with_delta(text, deltas, key, precision=1)


def quote_volume_value(ticker: Ticker) -> float | None:
    if ticker.quote_volume is not None:
        return ticker.quote_volume
    if ticker.base_volume is not None:
        return ticker.base_volume * ticker.last
    return None


def aggregate_delta_pct(items: list[tuple[float | None, float | None]]) -> float | None:
    current_total = 0.0
    previous_total = 0.0
    used = False
    for current, delta_pct in items:
        if current is None or delta_pct is None:
            continue
        denominator = 1 + delta_pct / 100
        if denominator == 0:
            continue
        current_total += current
        previous_total += current / denominator
        used = True
    if not used or previous_total == 0:
        return None
    return ((current_total - previous_total) / previous_total) * 100


def ticker_metric_key(ticker: Ticker) -> str:
    return f"futures:{ticker.exchange}" if ticker.futures_only else f"spot:{ticker.exchange}"


def pct_change(current: float | None, baseline: Any) -> float | None:
    baseline_value = as_float(baseline)
    if current is None or baseline_value in (None, 0):
        return None
    return ((current - baseline_value) / baseline_value) * 100


def utc_metric_date(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def metric_snapshot(ticker: Ticker) -> dict[str, Any]:
    return {
        "price": ticker.futures_last if ticker.futures_only and ticker.futures_last is not None else ticker.last,
        "spot_volume": quote_volume_value(ticker),
        "futures_volume": ticker.futures_quote_volume,
        "open_interest": ticker.futures_open_interest,
    }


def apply_utc_volume_deltas(state: dict[str, Any], tickers: list[Ticker]) -> None:
    today = utc_metric_date()
    baseline_state = state.get("utc_metric_baselines")
    if not isinstance(baseline_state, dict) or baseline_state.get("date") != today:
        baseline_state = {"date": today, "metrics": {}}
        state["utc_metric_baselines"] = baseline_state

    baselines = baseline_state.setdefault("metrics", {})
    if not isinstance(baselines, dict):
        baselines = {}
        baseline_state["metrics"] = baselines

    for ticker in tickers:
        if not ticker.ok or ticker.exchange == "Coinone":
            continue
        key = ticker_metric_key(ticker)
        snapshot = metric_snapshot(ticker)
        baseline = baselines.get(key)
        if not isinstance(baseline, dict):
            baselines[key] = snapshot
            ticker.spot_volume_delta_pct = None
            ticker.futures_volume_delta_pct = None
            if not ticker.futures_only:
                ticker.volume_change_pct = None
            continue

        spot_volume = snapshot.get("spot_volume")
        futures_volume = snapshot.get("futures_volume")
        price = snapshot.get("price")
        open_interest = snapshot.get("open_interest")
        if "price" not in baseline:
            baseline["price"] = price
            ticker.price_change_pct = None
        else:
            ticker.price_change_pct = pct_change(price, baseline.get("price"))
        if "open_interest" not in baseline:
            baseline["open_interest"] = open_interest
            ticker.open_interest_delta_pct = None
        else:
            ticker.open_interest_delta_pct = pct_change(open_interest, baseline.get("open_interest"))
        ticker.spot_volume_delta_pct = pct_change(spot_volume, baseline.get("spot_volume"))
        ticker.futures_volume_delta_pct = pct_change(futures_volume, baseline.get("futures_volume"))
        if not ticker.futures_only:
            ticker.volume_change_pct = ticker.spot_volume_delta_pct


def latest_transaction_count(transaction_points: list[TransactionPoint]) -> int | None:
    if not transaction_points:
        return None
    return transaction_points[-1].count


def summary_metric_values(
    tickers: list[Ticker],
    usdt_krw: float | None,
    fear_greed: FearGreedIndex | None,
    global_ranks: GlobalRanks | None,
    market_dominance: MarketDominance | None,
    crypto_caps: CryptoCaps | None,
    hashrate_ths: float | None,
    transaction_count: int | None,
) -> dict[str, float | int | None]:
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if not ticker.futures_only and ticker.unit in {"USDT", "USD"}]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)
    return {
        "avg_usdt": avg_usdt,
        "krw": coinone.last if coinone else None,
        "usdt_krw": usdt_krw,
        "fear_greed": fear_greed.value if fear_greed else None,
        "cmc_rank": global_ranks.coinmarketcap if global_ranks else None,
        "cg_rank": global_ranks.coingecko if global_ranks else None,
        "dominance_btc": market_dominance.bitcoin if market_dominance else None,
        "dominance_eth": market_dominance.ethereum if market_dominance else None,
        "dominance_alt": market_dominance.alt_ex_btc_eth if market_dominance else None,
        "dominance_kas": market_dominance.kaspa if market_dominance else None,
        "cap_total1": crypto_caps.total1 if crypto_caps else None,
        "cap_total2": crypto_caps.total2 if crypto_caps else None,
        "cap_total3": crypto_caps.total3 if crypto_caps else None,
        "cap_kaspa": crypto_caps.kaspa if crypto_caps else None,
        "hashrate_ths": hashrate_ths,
        "tx_1m": transaction_count,
    }


def apply_utc_summary_deltas(
    state: dict[str, Any],
    values: dict[str, float | int | None],
) -> tuple[dict[str, float], dict[str, int]]:
    today = utc_metric_date()
    baseline_state = state.get("utc_summary_baselines")
    if not isinstance(baseline_state, dict) or baseline_state.get("date") != today:
        baseline_state = {"date": today, "metrics": {}}
        state["utc_summary_baselines"] = baseline_state

    baselines = baseline_state.setdefault("metrics", {})
    if not isinstance(baselines, dict):
        baselines = {}
        baseline_state["metrics"] = baselines

    deltas: dict[str, float] = {}
    rank_deltas: dict[str, int] = {}
    rank_keys = {"cmc_rank", "cg_rank"}
    for key, value in values.items():
        if value is None:
            continue
        baseline = baselines.get(key)
        if baseline is None:
            baselines[key] = value
            continue
        if key in rank_keys:
            rank_deltas[key] = int(baseline) - int(value)
            continue
        delta = pct_change(float(value), baseline)
        if delta is not None:
            deltas[key] = delta
    return deltas, rank_deltas


def apply_metric_deltas(state: dict[str, Any], tickers: list[Ticker]) -> None:
    previous = state.get("previous_metrics", {})
    current: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        if not ticker.ok or ticker.exchange == "Coinone":
            continue
        key = ticker_metric_key(ticker)
        old = previous.get(key, {}) if isinstance(previous, dict) else {}
        spot_volume = quote_volume_value(ticker)
        futures_volume = ticker.futures_quote_volume
        open_interest = ticker.futures_open_interest
        if ticker.market_rank is not None and old.get("rank") is not None:
            ticker.market_rank_delta = int(old["rank"]) - int(ticker.market_rank)
        if open_interest is not None and old.get("open_interest") not in (None, 0):
            ticker.open_interest_delta_pct = ((open_interest - float(old["open_interest"])) / float(old["open_interest"])) * 100
        current[key] = {
            "rank": ticker.market_rank,
            "spot_volume": spot_volume,
            "futures_volume": futures_volume,
            "open_interest": open_interest,
        }
    state["previous_metrics"] = current


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
    return volume_with_delta(quote_volume_value(ticker), ticker.spot_volume_delta_pct)


def market_futures_volume(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return volume_with_delta(ticker.futures_quote_volume, ticker.futures_volume_delta_pct)


def market_open_interest(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return volume_with_delta(ticker.futures_open_interest, ticker.open_interest_delta_pct)


def market_funding(ticker: Ticker) -> str:
    if not ticker.ok or ticker.futures_funding_rate is None:
        return "-"
    return f"{ticker.futures_funding_rate * 100:+.3f}%"


def market_rank(ticker: Ticker) -> str:
    if not ticker.ok or ticker.market_rank is None:
        return "-"
    prefix = "F" if ticker.futures_only else "S"
    return f"{prefix}#{ticker.market_rank}{rank_delta_suffix(ticker.market_rank_delta)}"


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
    if value > 0:
        return "#37d67a"
    if value < 0:
        return "#ff6b6b"
    return "#9aa4b2"


def caption_volume(ticker: Ticker) -> str:
    if not ticker.ok:
        return "-"
    return quote_volume_millions(quote_volume_value(ticker))


def caption_total_volume(value: float) -> str:
    return quote_volume_millions(value)


def futures_summary(tickers: list[Ticker]) -> tuple[float, Ticker | None]:
    futures = [
        ticker
        for ticker in tickers
        if ticker.ok and ticker.futures_quote_volume is not None and ticker.futures_quote_volume > 0
    ]
    total = sum(float(ticker.futures_quote_volume or 0) for ticker in futures)
    top = max(futures, key=lambda ticker: ticker.futures_quote_volume or 0, default=None)
    return total, top


def open_interest_summary(tickers: list[Ticker]) -> tuple[float, Ticker | None]:
    futures = [
        ticker
        for ticker in tickers
        if ticker.ok and ticker.futures_open_interest is not None and ticker.futures_open_interest > 0
    ]
    total = sum(float(ticker.futures_open_interest or 0) for ticker in futures)
    top = max(futures, key=lambda ticker: ticker.futures_open_interest or 0, default=None)
    return total, top


def funding_summary(tickers: list[Ticker]) -> tuple[float | None, Ticker | None, Ticker | None]:
    futures = [ticker for ticker in tickers if ticker.ok and ticker.futures_funding_rate is not None]
    if not futures:
        return None, None, None
    average = sum(float(ticker.futures_funding_rate or 0) for ticker in futures) / len(futures)
    highest = max(futures, key=lambda ticker: ticker.futures_funding_rate or 0, default=None)
    lowest = min(futures, key=lambda ticker: ticker.futures_funding_rate or 0, default=None)
    return average, highest, lowest


def caption_futures_summary(tickers: list[Ticker]) -> str | None:
    total, top = futures_summary(tickers)
    if total <= 0 or top is None:
        return None
    return f"{quote_volume_millions(total)} top {caption_exchange(top.exchange)}"


def caption_open_interest_summary(tickers: list[Ticker]) -> str | None:
    total, top = open_interest_summary(tickers)
    if total <= 0 or top is None:
        return None
    return f"{quote_volume_millions(total)} top {caption_exchange(top.exchange)}"


def caption_funding_summary(tickers: list[Ticker]) -> str | None:
    average, highest, lowest = funding_summary(tickers)
    if average is None:
        return None
    return f"avg {caption_signed_percent(average * 100, precision=3)}"


def kimchi_premium(avg_usdt: float | None, coinone: Ticker | None, usdt_krw: float | None) -> float | None:
    if not avg_usdt or not coinone or not usdt_krw:
        return None
    fair_krw = avg_usdt * usdt_krw
    if fair_krw <= 0:
        return None
    return ((coinone.last - fair_krw) / fair_krw) * 100


def futures_basis(avg_usdt: float | None, tickers: list[Ticker]) -> float | None:
    basis_values = []
    for ticker in tickers:
        if not ticker.ok or ticker.futures_last is None or ticker.futures_last <= 0:
            continue
        spot_reference = None
        if not ticker.futures_only and ticker.unit in {"USDT", "USD"} and ticker.last > 0:
            spot_reference = ticker.last
        elif avg_usdt:
            spot_reference = avg_usdt
        if spot_reference:
            basis_values.append(((ticker.futures_last - spot_reference) / spot_reference) * 100)
    if not basis_values:
        return None
    return sum(basis_values) / len(basis_values)


def futures_interpretation(tickers: list[Ticker], basis: float | None) -> str:
    average_funding, _, _ = funding_summary(tickers)
    oi_delta_values = [
        ticker.open_interest_delta_pct
        for ticker in tickers
        if ticker.open_interest_delta_pct is not None
    ]
    futures_volume_delta_values = [
        ticker.futures_volume_delta_pct
        for ticker in tickers
        if ticker.futures_volume_delta_pct is not None
    ]
    avg_oi_delta = sum(oi_delta_values) / len(oi_delta_values) if oi_delta_values else None
    avg_futures_volume_delta = (
        sum(futures_volume_delta_values) / len(futures_volume_delta_values)
        if futures_volume_delta_values
        else None
    )

    heat = 0
    if average_funding is not None:
        if average_funding >= 0.0001:
            heat += 1
        elif average_funding <= -0.00005:
            heat -= 1
    if basis is not None:
        if basis >= 0.25:
            heat += 1
        elif basis <= -0.25:
            heat -= 1
    if avg_oi_delta is not None and avg_oi_delta >= 3:
        heat += 1
    if avg_futures_volume_delta is not None and avg_futures_volume_delta >= 8:
        heat += 1

    if heat >= 2:
        return "long-hot"
    if heat <= -2:
        return "short-heavy"
    if avg_oi_delta is not None and avg_oi_delta >= 3:
        return "OI rising"
    if average_funding is not None and abs(average_funding) < 0.00003 and (basis is None or abs(basis) < 0.15):
        return "neutral"
    return "mixed"


def interest_score(tickers: list[Ticker]) -> float:
    score = 0.0
    for ticker in tickers:
        if not ticker.ok or ticker.exchange == "Coinone":
            continue
        if ticker.market_rank_delta:
            score += max(-3.0, min(3.0, ticker.market_rank_delta / 2))
        if ticker.spot_volume_delta_pct:
            score += max(-2.0, min(2.0, ticker.spot_volume_delta_pct / 5))
        if ticker.futures_volume_delta_pct:
            score += max(-2.0, min(2.0, ticker.futures_volume_delta_pct / 5))
        if ticker.open_interest_delta_pct:
            score += max(-1.5, min(1.5, ticker.open_interest_delta_pct / 5))
        if ticker.futures_funding_rate:
            score += max(-1.0, min(1.0, ticker.futures_funding_rate * 4000))
    return score


def caption_alerts(tickers: list[Ticker], premium: float | None, basis: float | None) -> str | None:
    alerts = []
    for ticker in tickers:
        if ticker.market_rank_delta is not None and ticker.market_rank_delta >= 5:
            alerts.append(f"{caption_exchange(ticker.exchange)} rank +{ticker.market_rank_delta}")
            break
    top_futures_delta = max(
        (ticker.futures_volume_delta_pct for ticker in tickers if ticker.futures_volume_delta_pct is not None),
        default=None,
    )
    if top_futures_delta is not None and top_futures_delta >= 10:
        alerts.append(f"FutVol +{top_futures_delta:.0f}%")
    top_oi_delta = max(
        (ticker.open_interest_delta_pct for ticker in tickers if ticker.open_interest_delta_pct is not None),
        default=None,
    )
    if top_oi_delta is not None and top_oi_delta >= 8:
        alerts.append(f"OI +{top_oi_delta:.0f}%")
    average_funding, highest_funding, _ = funding_summary(tickers)
    if highest_funding and highest_funding.futures_funding_rate is not None and highest_funding.futures_funding_rate >= 0.00015:
        alerts.append(f"Fund {caption_exchange(highest_funding.exchange)} hot")
    elif average_funding is not None and average_funding <= -0.00005:
        alerts.append("Funding short")
    if premium is not None and abs(premium) >= 1.0:
        alerts.append(f"Kimchi {caption_signed_percent(premium, precision=1)}")
    if basis is not None and abs(basis) >= 0.25:
        alerts.append(f"Basis {caption_signed_percent(basis, precision=2)}")
    if not alerts:
        return None
    return " / ".join(alerts[:2])


def priority_alert_text(
    tickers: list[Ticker],
    premium: float | None,
    basis: float | None,
    wallet_summary: dict[str, Any] | None = None,
) -> str:
    watch = []
    for ticker in tickers:
        if not ticker.ok:
            continue
        if ticker.spot_volume_delta_pct is not None and abs(ticker.spot_volume_delta_pct) >= 20:
            watch.append(f"{caption_exchange(ticker.exchange)} spot {caption_signed_percent(ticker.spot_volume_delta_pct, 0)}")
        if ticker.futures_volume_delta_pct is not None and abs(ticker.futures_volume_delta_pct) >= 20:
            watch.append(f"{caption_exchange(ticker.exchange)} fut {caption_signed_percent(ticker.futures_volume_delta_pct, 0)}")
        if ticker.open_interest_delta_pct is not None and abs(ticker.open_interest_delta_pct) >= 10:
            watch.append(f"{caption_exchange(ticker.exchange)} OI {caption_signed_percent(ticker.open_interest_delta_pct, 0)}")
    if premium is not None and abs(premium) >= 1.0:
        watch.append(f"Kimchi {caption_signed_percent(premium, 1)}")
    if basis is not None and abs(basis) >= 0.25:
        watch.append(f"Basis {caption_signed_percent(basis, 2)}")
    if watch:
        return "🔶 Watch " + " / ".join(watch[:2])
    return "ℹ️ Info calm"


def chart_badges(
    tickers: list[Ticker],
    alert_text: str,
    wallet_summary: dict[str, Any] | None,
    transaction_count: int | None,
) -> list[str]:
    futures_total, _ = futures_summary(tickers)
    wallet_balance = as_float(wallet_summary.get("balance")) if wallet_summary else None
    return [
        alert_text.replace("Critical", "Crit").replace("Watch", "Watch").replace("Info", "Info"),
        f"Wallet {compact_kas(wallet_balance).replace(' KAS', '')}",
        f"Fut {quote_volume_millions(futures_total)}",
        f"TX {compact_count(transaction_count)}/m" if transaction_count is not None else "TX -",
    ]


def api_error_labels(tickers: list[Ticker], extra_errors: list[str] | None = None) -> list[str]:
    labels = []
    for ticker in tickers:
        if ticker.error:
            labels.append(ticker.exchange)
    labels.extend(extra_errors or [])
    deduped = []
    for label in labels:
        if label not in deduped:
            deduped.append(label)
    return deduped


def update_api_status(state: dict[str, Any], tickers: list[Ticker], extra_errors: list[str] | None = None) -> list[str]:
    now = int(time.time())
    status = state.setdefault("api_status", {})
    errors = set(api_error_labels(tickers, extra_errors))
    observed = {ticker.exchange for ticker in tickers}
    observed.update({"USDT/KRW", "F&G", "Ranks", "Dominance", "Crypto CAP", "Hashrate", "TX", "Wallet", "Candles", "BTC"})
    for label in sorted(observed):
        item = status.setdefault(label, {})
        if label in errors:
            item["last_error"] = now
            item["failures"] = int(item.get("failures", 0)) + 1
        else:
            item["last_ok"] = now
            item["failures"] = 0
    return sorted(errors)


def data_status_text(state: dict[str, Any], error_labels: list[str]) -> str:
    if error_labels:
        text = "miss " + ",".join(error_labels[:4])
        if len(error_labels) > 4:
            text += f"+{len(error_labels) - 4}"
        return text
    status = state.get("api_status", {})
    stale = []
    now = int(time.time())
    for label, item in status.items():
        if not isinstance(item, dict):
            continue
        last_ok = int(item.get("last_ok", 0) or 0)
        if last_ok and now - last_ok > 15 * 60:
            stale.append(label)
    if stale:
        return "stale " + ",".join(sorted(stale)[:3])
    return "fresh; cache <=60s"


def update_api_performance(state: dict[str, Any]) -> dict[str, Any] | None:
    if not HTTP_METRICS:
        return None
    ok_metrics = [metric for metric in HTTP_METRICS if metric.get("ok")]
    if not ok_metrics:
        return None
    average_ms = int(sum(int(metric["ms"]) for metric in ok_metrics) / len(ok_metrics))
    slowest = max(ok_metrics, key=lambda metric: int(metric.get("ms", 0)))
    host = str(slowest.get("host", "api")).replace("api.", "").replace("www.", "")
    summary = {
        "ts": int(time.time()),
        "count": len(ok_metrics),
        "avg_ms": average_ms,
        "slow_host": host.split(".")[0],
        "slow_ms": int(slowest.get("ms", 0)),
    }
    state["api_performance"] = summary
    return summary


def api_performance_text(summary: dict[str, Any] | None) -> str | None:
    if not summary:
        return None
    return f"{summary.get('avg_ms', 0)}ms slow {summary.get('slow_host', '-')}"


def caption_change(value: float | None) -> str:
    if value is None:
        return "-"
    return caption_signed_percent(value, precision=1 if abs(value) < 10 else 0)


def caption_signed_percent(value: float | None, precision: int = 1) -> str:
    if value is None:
        return "-"
    rounded = round(value, precision)
    text = f"{rounded:+.{precision}f}%" if rounded != 0 else f"{0:.{precision}f}%"
    if rounded == 0:
        return f"⚪{text}"
    if rounded < 0:
        return f"🔴{text}"
    return f"🟢{text}"


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


def fit_display_width(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    suffix = "..."
    suffix_width = display_width(suffix)
    if width <= suffix_width:
        return "." * width
    chars: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + char_width > width - suffix_width:
            break
        chars.append(char)
        used += char_width
    return "".join(chars) + suffix


def caption_summary_row(label: str, value: str, value_width: int) -> str:
    fitted_value = fit_display_width(value, value_width)
    return f"{pad_right(label, 7)} {fitted_value}"


def caption_separator(width: int = 6) -> str:
    return "-" * width


def format_caption_summary_rows(rows: list[tuple[str, str]]) -> list[str]:
    value_width = 42
    return [caption_summary_row(label, value, value_width) for label, value in rows]


def caption_html(body_lines: list[str]) -> str:
    return f"<pre>{html.escape(chr(10).join(body_lines))}</pre>"


def fit_caption_html(body_lines: list[str], max_bytes: int = 1010) -> str:
    fitted = list(body_lines)
    optional_prefixes = ("Alerts", "Ranks", "Kimchi", "Basis", "API", "Data", "Alert")
    for prefix in optional_prefixes:
        caption = caption_html(fitted)
        if len(caption.encode("utf-8")) <= max_bytes:
            return caption
        fitted = [line for line in fitted if not line.startswith(prefix)]
    return caption_html(fitted)


def render_caption(
    tickers: list[Ticker],
    hashrate_ths: float | None = None,
    display_dt: datetime | None = None,
    usdt_krw: float | None = None,
    fear_greed: FearGreedIndex | None = None,
    global_ranks: GlobalRanks | None = None,
    market_dominance: MarketDominance | None = None,
    crypto_caps: CryptoCaps | None = None,
    data_status: str | None = None,
    api_performance: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    summary_deltas: dict[str, float] | None = None,
    rank_deltas: dict[str, int] | None = None,
    transaction_count: int | None = None,
    wallet_summary: dict[str, Any] | None = None,
) -> str:
    settings = settings or DEFAULT_SETTINGS
    summary_deltas = summary_deltas or {}
    rank_deltas = rank_deltas or {}
    display_dt = display_dt or datetime.now(KST).replace(second=0, microsecond=0)
    now = display_dt.strftime("%m-%d %H:%M:%S KST")
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if not ticker.futures_only and ticker.unit in {"USDT", "USD"}]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)
    premium = kimchi_premium(avg_usdt, coinone, usdt_krw)
    basis = futures_basis(avg_usdt, tickers)

    market_rows: list[tuple[str, str]] = []
    ranking_rows: list[tuple[str, str]] = []
    dominance_rows: list[tuple[str, str]] = []
    crypto_cap_rows: list[tuple[str, str]] = []
    network_rows: list[tuple[str, str]] = []
    event_rows: list[tuple[str, str]] = []
    wallet_rows: list[tuple[str, str]] = []
    if avg_usdt:
        market_rows.append(("Avg", metric_with_delta(f"{avg_usdt:.6f} USDT", summary_deltas, "avg_usdt")))
    if coinone:
        market_rows.append(("KRW", metric_with_delta(f"{coinone.last:,.2f} KRW", summary_deltas, "krw")))
    if usdt_krw is None and avg_usdt and coinone:
        usdt_krw = coinone.last / avg_usdt
    if usdt_krw:
        market_rows.append(("USDT/KRW", metric_with_delta(f"{usdt_krw:,.2f} KRW", summary_deltas, "usdt_krw")))
    if premium is not None:
        market_rows.append(("Kimchi", caption_signed_percent(premium, precision=2)))
    if basis is not None:
        market_rows.append(("Basis", caption_signed_percent(basis, precision=2)))
    if fear_greed is not None:
        market_rows.append(("F&G", metric_with_delta(fmt_fear_greed(fear_greed), summary_deltas, "fear_greed")))
    if global_ranks is not None and (global_ranks.coinmarketcap is not None or global_ranks.coingecko is not None):
        ranking_rows.extend(
            [
                ("CMC", rank_value(global_ranks.coinmarketcap, rank_deltas.get("cmc_rank"))),
                ("CG", rank_value(global_ranks.coingecko, rank_deltas.get("cg_rank"))),
            ]
        )
    if market_dominance is not None:
        dominance_rows.extend(
            [
                ("BTC", dominance_value(market_dominance.bitcoin, summary_deltas, "dominance_btc")),
                ("ETH", dominance_value(market_dominance.ethereum, summary_deltas, "dominance_eth")),
                ("ALT", dominance_value(market_dominance.alt_ex_btc_eth, summary_deltas, "dominance_alt")),
                ("KAS", dominance_value(market_dominance.kaspa, summary_deltas, "dominance_kas")),
            ]
        )
    if crypto_caps is not None:
        crypto_cap_rows.extend(
            [
                ("TOTAL1", cap_value(crypto_caps.total1, summary_deltas, "cap_total1")),
                ("TOTAL2", cap_value(crypto_caps.total2, summary_deltas, "cap_total2")),
                ("TOTAL3", cap_value(crypto_caps.total3, summary_deltas, "cap_total3")),
                ("KAS", cap_value(crypto_caps.kaspa, summary_deltas, "cap_kaspa", force_billions=True)),
            ]
        )
    market_rows.append(("Alert", priority_alert_text(tickers, premium, basis, wallet_summary)))
    if data_status and not data_status.startswith("fresh"):
        network_rows.append(("Data", data_status))
    api_text = api_performance_text(api_performance)
    if api_text and settings.get("show_api_quality", True):
        network_rows.append(("API", api_text))
    event_rows.append(("Toccata", fmt_countdown(TOCATA_HARDFORK_AT, display_dt)))
    if hashrate_ths is not None:
        network_rows.append(("HR", metric_with_delta(fmt_hashrate(hashrate_ths), summary_deltas, "hashrate_ths")))
    if transaction_count is not None:
        network_rows.append(("TX", metric_with_delta(f"{compact_count(transaction_count)}/1m", summary_deltas, "tx_1m")))
    if wallet_summary:
        wallet_rows.append(("1st 🐋", compact_kas(as_float(wallet_summary.get("balance")))))
        ranges = []
        for label, key in (("24h", "delta_24h"), ("7d", "delta_7d"), ("30d", "delta_30d")):
            delta = wallet_summary.get(key)
            ranges.append(f"{label} {caption_signed_percent(delta, precision=1) if delta is not None else 'wait'}")
        wallet_rows.append(("WΔ", " ".join(ranges)))
        alert = wallet_summary.get("alert")
        if isinstance(alert, dict):
            wallet_rows.append(("⚠️", f"{alert.get('direction')} {compact_kas(as_float(alert.get('amount')))}"))

    separator = caption_separator()
    body_lines = [now, separator]
    if market_rows:
        body_lines.append("Market")
        for row in format_caption_summary_rows(market_rows):
            body_lines.append(row)
    if ranking_rows:
        body_lines.append(separator)
        body_lines.append("Ranking")
        for row in format_caption_summary_rows(ranking_rows):
            body_lines.append(row)
    if dominance_rows:
        body_lines.append(separator)
        body_lines.append("Dominance")
        for row in format_caption_summary_rows(dominance_rows):
            body_lines.append(row)
    if crypto_cap_rows:
        body_lines.append(separator)
        body_lines.append("Crypto CAP")
        for row in format_caption_summary_rows(crypto_cap_rows):
            body_lines.append(row)
    if network_rows:
        body_lines.append(separator)
        body_lines.append("Network")
        for row in format_caption_summary_rows(network_rows):
            body_lines.append(row)
    if event_rows:
        body_lines.append(separator)
        body_lines.append("Event")
        for row in format_caption_summary_rows(event_rows):
            body_lines.append(row)
    if wallet_rows:
        body_lines.append(separator)
        body_lines.append("Wallet")
        for row in format_caption_summary_rows(wallet_rows):
            body_lines.append(row)

    body_lines.append(separator)
    body_lines.append(caption_row("Exch", "Price", "PUTC", "Vol$", "VUTC"))
    total_volume = 0.0
    total_delta_items: list[tuple[float | None, float | None]] = []
    for ticker in [ticker for ticker in market_tickers(tickers) if not ticker.futures_only]:
        if not ticker.ok:
            body_lines.append(caption_row(caption_exchange(ticker.exchange), "error", "-", "-", "-"))
            continue
        quote_volume = quote_volume_value(ticker)
        if quote_volume is not None:
            total_volume += quote_volume
            total_delta_items.append((quote_volume, ticker.volume_change_pct))
        body_lines.append(
            caption_row(
                caption_exchange(ticker.exchange),
                market_price(ticker),
                caption_change(ticker.price_change_pct),
                caption_volume(ticker),
                caption_change(ticker.volume_change_pct),
            )
        )
    body_lines.append(
        caption_row(
            "Total",
            "",
            "",
            caption_total_volume(total_volume),
            caption_change(aggregate_delta_pct(total_delta_items)),
        )
    )

    return fit_caption_html(body_lines)


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
    api_errors: list[str] | None = None,
    chart_badges: list[str] | None = None,
) -> bytes:
    width, height = 1600, 1080
    image = Image.new("RGB", (width, height), "#10131a")
    draw = ImageDraw.Draw(image)
    title_font = load_font(50, bold=True)
    text_font = load_font(31)
    small_font = load_font(23)

    draw.rectangle((0, 0, width, 110), fill="#171b24")
    draw.text((48, 28), "KASPA LIVE TICKER", fill="#f3f6fb", font=title_font)
    draw.text((width - 500, 32), "24H / 1M CANDLES", fill="#9aa4b2", font=text_font)
    display_dt = display_dt or datetime.now(KST).replace(second=0, microsecond=0)
    draw.text((width - 235, 72), display_dt.strftime("%H:%M:%S KST"), fill="#9aa4b2", font=small_font)
    badge_x = 470
    for badge in (chart_badges or [])[:4]:
        badge_text = fit_display_width(badge, 20)
        bbox = draw.textbbox((0, 0), badge_text, font=small_font)
        badge_width = min(210, bbox[2] - bbox[0] + 20)
        draw.rounded_rectangle((badge_x, 70, badge_x + badge_width, 100), radius=5, fill="#101722", outline="#2b3342")
        draw.text((badge_x + 10, 75 - bbox[1]), badge_text, fill="#dfe7f3", font=small_font)
        badge_x += badge_width + 10

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
        bar_panel_height = (chart_box[3] - chart_box[1]) * 0.30
        bar_panel_top = chart_box[3] - bar_panel_height - 2
        bar_panel_mid = bar_panel_top + bar_panel_height / 2
        bar_panel_bottom = chart_box[3] - 2

        hashrate_points = hashrate_points or []
        visible_hashrates = [
            point
            for point in hashrate_points
            if candle_start_ts <= point.ts <= candle_end_ts and point.ths > 0
        ]
        if visible_hashrates or visible_transactions:
            draw.rounded_rectangle(
                (inner_left, bar_panel_top, inner_right, bar_panel_bottom),
                radius=5,
                outline="#223047",
                width=1,
            )
            draw.line((inner_left, bar_panel_mid, inner_right, bar_panel_mid), fill="#394861", width=2)
            draw.text((inner_left + 8, bar_panel_top + 4), "HR15", fill="#ff8fac", font=small_font)
            draw.text((inner_left + 8, bar_panel_mid + 4), "TX", fill="#9fb8ee", font=small_font)
        if visible_hashrates:
            hashrate_values = [point.ths for point in visible_hashrates]
            min_hashrate = min(hashrate_values)
            max_hashrate = max(hashrate_values)
            hashrate_range = max_hashrate - min_hashrate
            hashrate_diffs = [
                later.ts - earlier.ts
                for earlier, later in zip(visible_hashrates, visible_hashrates[1:])
                if later.ts > earlier.ts
            ]
            hashrate_bucket_seconds = min(hashrate_diffs) if hashrate_diffs else 15 * 60
            hashrate_slot = (hashrate_bucket_seconds / max(candle_end_ts - candle_start_ts, 1)) * (inner_right - inner_left)
            hashrate_bar_width = max(5, min(28, hashrate_slot * 0.80))
            max_hashrate_bar_height = (bar_panel_height / 2) - 8
            for point in visible_hashrates:
                x = x_for_ts(point.ts)
                if hashrate_range > 0:
                    normalized = (point.ths - min_hashrate) / hashrate_range
                else:
                    normalized = 0.65
                bar_height = 8 + normalized * max_hashrate_bar_height
                draw.rectangle(
                    (
                        x - hashrate_bar_width / 2,
                        bar_panel_mid - bar_height,
                        x + hashrate_bar_width / 2,
                        bar_panel_mid - 1,
                    ),
                    fill="#241827",
                    outline="#4a2438",
                )

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
            max_bar_height = (bar_panel_height / 2) - 8
            last_transaction = visible_transactions[-1]
            last_transaction_x = x_for_ts(last_transaction.ts + transaction_bucket_seconds // 2)
            last_transaction_height = 8 + (last_transaction.count / max_transactions) * max_bar_height
            for point in visible_transactions:
                x = x_for_ts(point.ts + transaction_bucket_seconds // 2)
                bar_height = 8 + (point.count / max_transactions) * max_bar_height
                draw.rectangle(
                    (x - bar_width / 2, bar_panel_mid + 1, x + bar_width / 2, min(bar_panel_bottom, bar_panel_mid + bar_height)),
                    fill="#1f2c44",
                )
            tx_text = f"TX {compact_count(last_transaction.count)}/1m  max {compact_count(max_transactions)}"
            tx_bbox = draw.textbbox((0, 0), tx_text, font=small_font)
            tx_pad_x, tx_pad_y = 8, 5
            tx_width = tx_bbox[2] - tx_bbox[0] + tx_pad_x * 2
            tx_height = tx_bbox[3] - tx_bbox[1] + tx_pad_y * 2
            tx_label_x = label_left
            tx_label_y = min(chart_box[3] - tx_height - 8, bar_panel_mid + 8)
            tx_anchor_y = min(bar_panel_bottom - 8, bar_panel_mid + last_transaction_height)
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

        if visible_hashrates:
            latest_hashrate = hashrate_ths or visible_hashrates[-1].ths
            hashrate_text = f"HR {fmt_hashrate(latest_hashrate)}"
            hashrate_bbox = draw.textbbox((0, 0), hashrate_text, font=small_font)
            hashrate_pad_x, hashrate_pad_y = 8, 5
            hashrate_width = hashrate_bbox[2] - hashrate_bbox[0] + hashrate_pad_x * 2
            hashrate_height = hashrate_bbox[3] - hashrate_bbox[1] + hashrate_pad_y * 2
            hashrate_label_x = label_left
            hashrate_label_y = max(bar_panel_top + 4, bar_panel_mid - hashrate_height - 8)
            draw.rounded_rectangle(
                (
                    hashrate_label_x,
                    hashrate_label_y,
                    hashrate_label_x + hashrate_width,
                    hashrate_label_y + hashrate_height,
                ),
                radius=5,
                fill="#0b0e13",
                outline="#8a3d5c",
                width=1,
            )
            draw.text(
                (hashrate_label_x + hashrate_pad_x, hashrate_label_y + hashrate_pad_y - hashrate_bbox[1]),
                hashrate_text,
                fill="#ff8fac",
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
    rank_right_x = 330
    price_right_x = 530
    price_change_right_x = 690
    volume_right_x = 980
    futures_volume_right_x = 1235
    open_interest_right_x = 1395
    funding_right_x = 1516
    draw.text((exchange_x, header_y), "Exchange", fill="#9aa4b2", font=small_font)
    draw_right(rank_right_x, header_y, "Mkt Rank", "#9aa4b2", small_font)
    draw_right(price_right_x, header_y, "Price", "#9aa4b2", small_font)
    draw_right(price_change_right_x, header_y, "PUTC", "#9aa4b2", small_font)
    draw_right(volume_right_x, header_y, "Spot $ UTC", "#9aa4b2", small_font)
    draw_right(futures_volume_right_x, header_y, "Fut $ UTC", "#9aa4b2", small_font)
    draw_right(open_interest_right_x, header_y, "OI $ UTC", "#9aa4b2", small_font)
    draw_right(funding_right_x, header_y, "Fund", "#9aa4b2", small_font)
    draw.line((84, header_y + 32, 1516, header_y + 32), fill="#2b3342", width=1)

    def draw_metric_with_delta(
        right_x: int,
        y_pos: int,
        value: float | None,
        delta_pct: float | None,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        base = quote_volume_millions(value)
        suffix = delta_suffix(delta_pct)
        if not suffix:
            draw_right(right_x, y_pos, base, "#f3f6fb", font)
            return
        suffix_bbox = draw.textbbox((0, 0), suffix, font=font)
        suffix_width = suffix_bbox[2] - suffix_bbox[0]
        draw_right(right_x, y_pos, suffix, change_color(delta_pct), font)
        draw_right(right_x - suffix_width - 6, y_pos, base, "#f3f6fb", font)

    y = 678
    total_volume = 0.0
    total_futures_volume = 0.0
    total_open_interest = 0.0
    total_volume_delta_items: list[tuple[float | None, float | None]] = []
    total_futures_delta_items: list[tuple[float | None, float | None]] = []
    total_open_interest_delta_items: list[tuple[float | None, float | None]] = []
    row_height = 28
    row_font = load_font(22)
    for ticker in market_tickers(tickers):
        color = "#37d67a" if ticker.ok else "#ff6b6b"
        quote_volume = quote_volume_value(ticker) if ticker.ok else None
        futures_quote_volume = ticker.futures_quote_volume if ticker.ok else None
        open_interest = ticker.futures_open_interest if ticker.ok else None
        if quote_volume is not None:
            total_volume += quote_volume
            total_volume_delta_items.append((quote_volume, ticker.spot_volume_delta_pct))
        if futures_quote_volume is not None:
            total_futures_volume += futures_quote_volume
            total_futures_delta_items.append((futures_quote_volume, ticker.futures_volume_delta_pct))
        if open_interest is not None:
            total_open_interest += open_interest
            total_open_interest_delta_items.append((open_interest, ticker.open_interest_delta_pct))
        if (y - 678) // row_height % 2 == 0:
            draw.rectangle((84, y - 1, 1516, y + row_height - 1), fill="#121720")
        draw.text((exchange_x, y + 3), ticker.exchange, fill=color, font=row_font)
        draw_right(rank_right_x, y + 3, market_rank(ticker), "#f3f6fb", row_font)
        draw_right(price_right_x, y + 3, market_price(ticker), "#f3f6fb", row_font)
        draw_right(price_change_right_x, y + 3, price_change_value(ticker), price_change_color(ticker), row_font)
        draw_metric_with_delta(volume_right_x, y + 3, quote_volume, ticker.spot_volume_delta_pct, row_font)
        draw_metric_with_delta(futures_volume_right_x, y + 3, futures_quote_volume, ticker.futures_volume_delta_pct, row_font)
        draw_metric_with_delta(open_interest_right_x, y + 3, open_interest, ticker.open_interest_delta_pct, row_font)
        draw_right(funding_right_x, y + 3, market_funding(ticker), change_color(ticker.futures_funding_rate), row_font)
        y += row_height

    draw.line((84, y + 2, 1516, y + 2), fill="#2b3342", width=1)
    draw.text((exchange_x, y + 8), "Total", fill="#9aa4b2", font=row_font)
    draw_metric_with_delta(volume_right_x, y + 8, total_volume, aggregate_delta_pct(total_volume_delta_items), row_font)
    draw_metric_with_delta(
        futures_volume_right_x,
        y + 8,
        total_futures_volume,
        aggregate_delta_pct(total_futures_delta_items),
        row_font,
    )
    draw_metric_with_delta(
        open_interest_right_x,
        y + 8,
        total_open_interest,
        aggregate_delta_pct(total_open_interest_delta_items),
        row_font,
    )

    if api_errors:
        error_text = "API miss: " + ", ".join(api_errors[:6])
        if len(api_errors) > 6:
            error_text += f" +{len(api_errors) - 6}"
        draw.text((84, height - 34), error_text, fill="#ff9f43", font=small_font)
    else:
        draw.text((84, height - 34), "Data: fresh; cache <=60s", fill="#6f7b8f", font=small_font)

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
    trim_runtime_logs()
    HTTP_METRICS.clear()
    display_dt = datetime.now(KST).replace(second=0, microsecond=0)
    settings = load_settings()
    state = load_state()
    run_errors: list[str] = []
    tickers = fetch_tickers(state)
    apply_metric_deltas(state, tickers)
    apply_utc_volume_deltas(state, tickers)
    history = update_history(state, tickers)
    hashrate_ths = None
    usdt_krw = None
    fear_greed = None
    global_ranks = None
    market_dominance = None
    crypto_caps = None
    wallet_summary = None
    hashrate_points: list[HashratePoint] = []
    transaction_points: list[TransactionPoint] = []
    try:
        usdt_krw = fetch_usdt_krw()
    except Exception as exc:
        print(f"usdt/krw fallback: {exc}")
        run_errors.append("USDT/KRW")
    try:
        fear_greed = fetch_fear_greed_index_cached(state)
    except Exception as exc:
        print(f"fear greed fallback: {exc}")
        run_errors.append("F&G")
    try:
        global_ranks = fetch_global_ranks_cached(state)
    except Exception as exc:
        print(f"global ranks fallback: {exc}")
        run_errors.append("Ranks")
    try:
        market_dominance = fetch_market_dominance_cached(state)
    except Exception as exc:
        print(f"market dominance fallback: {exc}")
        run_errors.append("Dominance")
    try:
        crypto_caps = fetch_crypto_caps_cached(state)
    except Exception as exc:
        print(f"crypto cap fallback: {exc}")
        run_errors.append("Crypto CAP")
    if market_dominance is not None and crypto_caps is not None and crypto_caps.total1 and crypto_caps.kaspa:
        market_dominance.kaspa = crypto_caps.kaspa / crypto_caps.total1 * 100
    try:
        hashrate_ths = fetch_hashrate()
        hashrate_points = fetch_hashrate_history()
    except Exception as exc:
        print(f"hashrate fallback: {exc}")
        run_errors.append("Hashrate")
    try:
        transaction_points = update_transaction_1m_history(state)
    except Exception as exc:
        print(f"transactions fallback: {exc}")
        run_errors.append("TX")
    try:
        wallet_summary = update_wallet_summary(state)
    except Exception as exc:
        print(f"wallet fallback: {exc}")
        run_errors.append("Wallet")
    try:
        candles = fetch_gate_candles()
        cache_candles(state, "KAS_USDT:1m:24h", candles)
    except Exception as exc:
        print(f"candle fallback: {exc}")
        run_errors.append("Candles")
        candles = cached_candles(state, "KAS_USDT:1m:24h") or history_as_candles(history)
    btc_candles: list[Candle] = []
    try:
        btc_candles = fetch_gate_candles("BTC_USDT")
        cache_candles(state, "BTC_USDT:1m:24h", btc_candles)
    except Exception as exc:
        print(f"btc fallback: {exc}")
        run_errors.append("BTC")
        btc_candles = cached_candles(state, "BTC_USDT:1m:24h")
    error_labels = update_api_status(state, tickers, run_errors)
    api_performance = update_api_performance(state)
    status_text = data_status_text(state, error_labels)
    transaction_count = latest_transaction_count(transaction_points)
    summary_values = summary_metric_values(
        tickers,
        usdt_krw,
        fear_greed,
        global_ranks,
        market_dominance,
        crypto_caps,
        hashrate_ths,
        transaction_count,
    )
    summary_deltas, rank_deltas = apply_utc_summary_deltas(state, summary_values)
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if not ticker.futures_only and ticker.unit in {"USDT", "USD"}]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)
    premium = kimchi_premium(avg_usdt, coinone, usdt_krw)
    basis = futures_basis(avg_usdt, tickers)
    alert_text = priority_alert_text(tickers, premium, basis, wallet_summary)
    caption = render_caption(
        tickers,
        hashrate_ths,
        display_dt,
        usdt_krw,
        fear_greed,
        global_ranks,
        market_dominance,
        crypto_caps,
        status_text,
        api_performance,
        settings,
        summary_deltas,
        rank_deltas,
        transaction_count,
        wallet_summary,
    )
    chart_hashrate_points = hashrate_points if settings.get("show_aux_panel", True) else []
    chart_transaction_points = transaction_points if settings.get("show_aux_panel", True) else []
    chart_png = render_chart(
        candles,
        tickers,
        chart_hashrate_points,
        hashrate_ths,
        btc_candles,
        chart_transaction_points,
        display_dt,
        error_labels,
        chart_badges(tickers, alert_text, wallet_summary, transaction_count),
    )
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
        loop(max(args.interval, 60), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
