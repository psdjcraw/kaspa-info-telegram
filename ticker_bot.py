from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import sys
import time
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


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


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
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
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
    return Ticker(
        exchange="Kraken",
        pair="KAS/USD",
        last=float(ticker["c"][0]),
        bid=as_float(ticker["b"][0]),
        ask=as_float(ticker["a"][0]),
        base_volume=as_float(ticker["v"][1]),
        quote_volume=None,
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


def fetch_gate_candles(hours: int = 24, interval: str = "5m") -> list[Candle]:
    limit = min(max(hours * 12, 1), 1000)
    payload = http_json(
        "https://api.gateio.ws/api/v4/spot/candlesticks?"
        f"currency_pair=KAS_USDT&interval={interval}&limit={limit}"
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
    usd_prices = [ticker.last for ticker in tickers if ticker.ok and ticker.unit in {"USDT", "USD"} and ticker.last > 0]
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


def market_tickers(tickers: list[Ticker]) -> list[Ticker]:
    return [ticker for ticker in tickers if ticker.exchange != "Coinone"]


def market_price(ticker: Ticker) -> str:
    if not ticker.ok:
        return "error"
    return f"{ticker.last:.4f}"


def market_volume(ticker: Ticker) -> str:
    if not ticker.ok or ticker.base_volume is None:
        return "-"
    return f"{ticker.base_volume:,.0f}"


def render_caption(tickers: list[Ticker]) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if ticker.unit in {"USDT", "USD"}]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)

    lines = ["<b>KASPA 실시간 티커</b>", f"<code>{now}</code>"]
    if avg_usdt:
        lines.append(f"평균: <b>{avg_usdt:.6f} USDT</b>")
    if coinone:
        lines.append(f"국내: <b>{coinone.last:,.2f} KRW</b>")

    table = [f"{'Exchange':<8} {'Price':>8} {'Volume':>14}"]
    total_volume = 0.0
    for ticker in market_tickers(tickers):
        if not ticker.ok:
            table.append(f"{ticker.exchange[:8]:<8} {'error':>8} {'-':>14}")
            continue
        if ticker.base_volume is not None:
            total_volume += ticker.base_volume
        table.append(f"{ticker.exchange[:8]:<8} {market_price(ticker):>8} {market_volume(ticker):>14}")
    table.append(f"{'Total':<8} {'':>8} {total_volume:>14,.0f}")

    lines.append(f"<pre>{html.escape(chr(10).join(table))}</pre>")

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


def render_chart(candles: list[Candle], tickers: list[Ticker]) -> bytes:
    width, height = 1600, 960
    image = Image.new("RGB", (width, height), "#10131a")
    draw = ImageDraw.Draw(image)
    title_font = load_font(50, bold=True)
    text_font = load_font(31)
    small_font = load_font(23)

    draw.rectangle((0, 0, width, 110), fill="#171b24")
    draw.text((48, 28), "KASPA LIVE TICKER", fill="#f3f6fb", font=title_font)
    draw.text((width - 500, 32), "24H / 5M CANDLES", fill="#9aa4b2", font=text_font)
    draw.text((width - 235, 72), datetime.now(KST).strftime("%H:%M:%S KST"), fill="#9aa4b2", font=small_font)

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
        inner_right = chart_box[2] - 96
        step = (inner_right - inner_left) / max(len(candles), 1)
        candle_width = max(2, min(9, int(step * 0.68)))

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
        label_indexes = [0, len(candles) // 4, len(candles) // 2, (len(candles) * 3) // 4, len(candles) - 1]
        for label_index in label_indexes:
            candle = candles[label_index]
            x = inner_left + label_index * step + step / 2
            draw.line((x, chart_box[1] + 1, x, chart_box[3] - 1), fill="#182030", width=1)
            label = datetime.fromtimestamp(candle.ts, tz=KST).strftime("%H:%M")
            draw.text((x - 34, chart_box[3] + 16), label, fill="#9aa4b2", font=small_font)

        draw.text((chart_box[0] + 28, chart_box[1] + 18), f"High {max(highs):.6f}", fill="#9aa4b2", font=small_font)
        draw.text((chart_box[0] + 28, chart_box[3] - 48), f"Low {min(lows):.6f}", fill="#9aa4b2", font=small_font)
        draw.text((chart_box[2] - 330, chart_box[1] + 18), f"Last {last.close:.6f} USDT", fill="#f3f6fb", font=text_font)
    else:
        draw.text((chart_box[0] + 380, chart_box[1] + 160), "collecting candle history...", fill="#9aa4b2", font=text_font)

    header_y = 640
    draw.text((84, header_y), "Exchange", fill="#9aa4b2", font=small_font)
    draw.text((460, header_y), "Price", fill="#9aa4b2", font=small_font)
    draw.text((780, header_y), "Volume", fill="#9aa4b2", font=small_font)

    y = 678
    total_volume = 0.0
    row_font = load_font(27)
    for ticker in market_tickers(tickers):
        color = "#37d67a" if ticker.ok else "#ff6b6b"
        if ticker.ok and ticker.base_volume is not None:
            total_volume += ticker.base_volume
        draw.text((84, y + 3), ticker.exchange, fill=color, font=row_font)
        draw.text((460, y + 3), market_price(ticker), fill="#f3f6fb", font=row_font)
        draw.text((780, y + 3), market_volume(ticker), fill="#f3f6fb", font=row_font)
        y += 31

    draw.line((84, y + 2, 1190, y + 2), fill="#2b3342", width=1)
    draw.text((84, y + 8), "Total", fill="#9aa4b2", font=row_font)
    draw.text((780, y + 8), f"{total_volume:,.0f}", fill="#f3f6fb", font=row_font)

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
    state = load_state()
    tickers = fetch_tickers()
    history = update_history(state, tickers)
    caption = render_caption(tickers)
    try:
        candles = fetch_gate_candles()
    except Exception as exc:
        print(f"candle fallback: {exc}")
        candles = history_as_candles(history)
    chart_png = render_chart(candles, tickers)
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


def loop(interval: int, dry_run: bool = False) -> None:
    while True:
        started = time.monotonic()
        try:
            run_once(dry_run=dry_run)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"telegram/http error {exc.code}: {body[:300]}")
            time.sleep(max(interval * 2, 15))
        except Exception as exc:
            print(f"error: {exc}")
            time.sleep(max(interval, 10))
        elapsed = time.monotonic() - started
        time.sleep(max(1, interval - elapsed))


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
