# Telegram Kaspa Ticker

`@kaspa_ticker`처럼 Telegram 채널의 메시지 하나를 고정해두고, 차트/거래소별 시세/볼륨을 주기적으로 갱신하는 봇입니다.

## 동작 방식

- public 거래소 ticker API에서 KAS 시세를 가져옵니다.
- 최근 KAS/USDT 평균 가격을 `state.json`에 저장해 간단한 차트 PNG를 만듭니다.
- 첫 실행 때는 `sendPhoto`로 메시지를 하나 보냅니다.
- 이후에는 저장된 `message_id`를 사용해 `editMessageMedia`로 같은 메시지를 수정합니다.
- 동일한 렌더링 결과면 Telegram API 호출을 건너뜁니다.

현재 수집 거래소:

- Coinone `KAS/KRW`
- Gate.io `KAS/USDT`
- MEXC `KAS/USDT`
- KuCoin `KAS/USDT`

## 준비

```bash
cd /Users/psdjc/.openclaw/workspace/telegram-kaspa-ticker
python3 -m pip install -r requirements.txt
```

환경변수:

```bash
export TELEGRAM_BOT_TOKEN="123456:..."
export TELEGRAM_CHAT_ID="@your_channel_or_chat_id"
export TICKER_INTERVAL_SECONDS=5
```

채널에 올리려면 봇을 채널 admin으로 추가하고 메시지 게시 권한을 줘야 합니다.
BotFather나 다른 도구에서 `3610763757`처럼 양수 채널 ID만 받은 경우에는 그대로 넣어도 됩니다.
봇은 Telegram Bot API용 `-1003610763757` 형태로 자동 보정합니다.

## 테스트

Telegram에 보내지 않고 로컬 차트만 생성:

```bash
python3 ticker_bot.py --once --dry-run
```

봇 토큰과 채널 접근권한 확인:

```bash
python3 ticker_bot.py --check-telegram
```

실제 Telegram 메시지 1회 생성/수정:

```bash
python3 ticker_bot.py --once
```

5초 주기 반복:

```bash
python3 ticker_bot.py
```

백그라운드 실행:

```bash
./botctl.sh start
./botctl.sh status
./botctl.sh tail
./botctl.sh stop
```

macOS launchd 상시 실행:

```bash
./install_launchd.sh
launchctl print gui/$(id -u)/com.psdjcraw.kaspa-info-telegram
./uninstall_launchd.sh
```

## 운영 메모

- `state.json`을 지우면 다음 실행 때 새 메시지를 보냅니다.
- 기존 메시지를 계속 쓰려면 `state.json`의 `message_id`를 보존하세요.
- Telegram이 rate limit을 주면 코드가 15초 이상 backoff합니다.
- 5초 이미지 갱신은 공격적으로 느껴질 수 있습니다. 안정 운영은 10~30초 차트 갱신 또는 텍스트-only 갱신을 섞는 쪽이 낫습니다.
