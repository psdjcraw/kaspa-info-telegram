from __future__ import annotations

import argparse
import hashlib
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
        exchange="Gate.io",
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


FETCHERS: list[Callable[[], Ticker]] = [fetch_coinone, fetch_gate, fetch_mexc, fetch_kucoin]


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


def update_history(state: dict[str, Any], tickers: list[Ticker], max_points: int = 288) -> list[dict[str, Any]]:
    usd_prices = [ticker.last for ticker in tickers if ticker.ok and ticker.unit == "USDT" and ticker.last > 0]
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


def fmt_volume(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {unit}"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K {unit}"
    return f"{value:.2f} {unit}"


def render_caption(tickers: list[Ticker]) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    ok_tickers = [ticker for ticker in tickers if ticker.ok]
    usd_prices = [ticker.last for ticker in ok_tickers if ticker.unit == "USDT"]
    avg_usdt = sum(usd_prices) / len(usd_prices) if usd_prices else None
    coinone = next((ticker for ticker in ok_tickers if ticker.exchange == "Coinone"), None)

    lines = ["<b>KASPA 실시간 티커</b>", f"<code>{now}</code>"]
    if avg_usdt:
        lines.append(f"평균: <b>{avg_usdt:.6f} USDT</b>")
    if coinone:
        lines.append(f"국내: <b>{coinone.last:,.2f} KRW</b>")
    lines.append("")

    for ticker in tickers:
        if not ticker.ok:
            lines.append(f"• {ticker.exchange}: <code>error</code>")
            continue
        spread = ""
        if ticker.bid and ticker.ask and ticker.bid > 0:
            spread = f" / spread {(ticker.ask - ticker.bid) / ticker.bid * 100:.2f}%"
        lines.append(
            "• "
            f"<b>{ticker.exchange}</b> {fmt_price(ticker)}"
            f" / vol {fmt_volume(ticker.base_volume, 'KAS')}"
            f"{spread}"
        )

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


def render_chart(history: list[dict[str, Any]], tickers: list[Ticker]) -> bytes:
    width, height = 960, 540
    image = Image.new("RGB", (width, height), "#10131a")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34, bold=True)
    text_font = load_font(22)
    small_font = load_font(18)

    draw.rectangle((0, 0, width, 84), fill="#171b24")
    draw.text((32, 24), "KASPA LIVE TICKER", fill="#f3f6fb", font=title_font)
    draw.text((width - 270, 30), datetime.now(KST).strftime("%H:%M:%S KST"), fill="#9aa4b2", font=text_font)

    chart_box = (54, 116, 912, 378)
    draw.rounded_rectangle(chart_box, radius=12, fill="#0b0e13", outline="#2b3342", width=2)
    draw.line((chart_box[0], chart_box[3], chart_box[2], chart_box[3]), fill="#3d4658", width=1)

    prices = [float(point["price"]) for point in history if point.get("price")]
    if len(prices) >= 2:
        min_price, max_price = min(prices), max(prices)
        if min_price == max_price:
            min_price -= 0.0001
            max_price += 0.0001
        points = []
        for index, price in enumerate(prices):
            x = chart_box[0] + 18 + index * ((chart_box[2] - chart_box[0] - 36) / max(len(prices) - 1, 1))
            y = chart_box[3] - 20 - ((price - min_price) / (max_price - min_price)) * (chart_box[3] - chart_box[1] - 40)
            points.append((x, y))
        draw.line(points, fill="#37d67a", width=4)
        for point in points[-4:]:
            draw.ellipse((point[0] - 4, point[1] - 4, point[0] + 4, point[1] + 4), fill="#f4d35e")
        draw.text((chart_box[0] + 20, chart_box[1] + 16), f"High {max_price:.6f}", fill="#9aa4b2", font=small_font)
        draw.text((chart_box[0] + 20, chart_box[3] - 38), f"Low {min_price:.6f}", fill="#9aa4b2", font=small_font)
    else:
        draw.text((chart_box[0] + 250, chart_box[1] + 110), "collecting chart history...", fill="#9aa4b2", font=text_font)

    y = 408
    for ticker in tickers[:4]:
        color = "#37d67a" if ticker.ok else "#ff6b6b"
        draw.text((54, y), ticker.exchange, fill=color, font=text_font)
        draw.text((190, y), fmt_price(ticker), fill="#f3f6fb", font=text_font)
        draw.text((430, y), f"vol {fmt_volume(ticker.base_volume, 'KAS')}", fill="#9aa4b2", font=text_font)
        y += 34

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
    chart_png = render_chart(history, tickers)
    CHART_PATH.write_bytes(chart_png)

    fingerprint = hashlib.sha256(chart_png + caption.encode("utf-8")).hexdigest()
    if state.get("last_fingerprint") == fingerprint:
        print("skip: no visible change")
        return
    state["last_fingerprint"] = fingerprint
    save_state(state)

    if dry_run:
        print(caption)
        print(f"chart: {CHART_PATH}")
        return

    send_or_edit_message(state, chart_png, caption)
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
    parser.add_argument("--interval", type=int, default=int(env("TICKER_INTERVAL_SECONDS", "5")))
    args = parser.parse_args()

    if args.check_telegram:
        try:
            check_telegram_config()
        except RuntimeError as exc:
            print(f"error: {exc}")
            sys.exit(1)
        return

    if args.once:
        run_once(dry_run=args.dry_run)
    else:
        loop(max(args.interval, 5), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
