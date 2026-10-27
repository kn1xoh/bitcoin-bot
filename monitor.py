"""Мониторинг BTC: уведомления в Telegram.

1. Резкое падение или рост цены за короткое окно.
2. Переход индекса страха и жадности (alternative.me) в зону «Страх» или «Сильный страх».

Запускается раз в несколько минут (GitHub Actions по расписанию).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

THRESHOLD_PCT = float(os.getenv("THRESHOLD_PCT", "3"))   # порог изменения, %
WINDOW_MIN = int(os.getenv("WINDOW_MIN", "15"))          # окно сравнения, минут
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))      # пауза между одинаковыми уведомлениями
FNG_ALERTS = os.getenv("FNG_ALERTS", "true").lower() == "true"  # уведомления по индексу страха и жадности
STATE_FILE = os.getenv("STATE_FILE", "state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TEST_MESSAGE = os.getenv("TEST_MESSAGE", "").lower() == "true"

HEADERS = {"User-Agent": "btc-monitor/1.0"}

# Зоны индекса от самой «страшной» к самой «жадной»
FNG_ZONES = ["Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"]
FNG_NAMES_RU = {
    "Extreme Fear": "Сильный страх",
    "Fear": "Страх",
    "Neutral": "Нейтрально",
    "Greed": "Жадность",
    "Extreme Greed": "Сильная жадность",
}
FNG_ALERT_ZONES = {"Extreme Fear", "Fear"}


def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


# ---------- Цена ----------
# Каждая свеча приводится к виду (время, low, high, close)

def candles_coinbase():
    data = http_get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"
    )
    # формат Coinbase: [time, low, high, open, close, volume]
    return [(int(c[0]), float(c[1]), float(c[2]), float(c[4])) for c in data]


def candles_kraken():
    data = http_get_json("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1")
    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")
    rows = next(v for k, v in data["result"].items() if k != "last")
    # формат Kraken: [time, open, high, low, close, vwap, volume, count]
    return [(int(c[0]), float(c[3]), float(c[2]), float(c[4])) for c in rows]


def get_candles():
    errors = []
    for source in (candles_coinbase, candles_kraken):
        try:
            candles = source()
            if candles:
                return sorted(candles), source.__name__
        except Exception as e:
            errors.append(f"{source.__name__}: {e}")
    raise RuntimeError("Не удалось получить цену: " + "; ".join(errors))


def analyze(candles, now):
    """Возвращает (текущая цена, изменение от максимума %, изменение от минимума %, high, low)."""
    window = [c for c in candles if c[0] >= now - WINDOW_MIN * 60]
    if not window:
        raise RuntimeError("Нет свежих данных за окно")
    current = window[-1][3]
    high = max(c[2] for c in window)
    low = min(c[1] for c in window)
    drop_pct = (current - high) / high * 100
    rise_pct = (current - low) / low * 100
    return current, drop_pct, rise_pct, high, low


# ---------- Индекс страха и жадности ----------

def get_fear_greed():
    """Возвращает (значение 0–100, зона на английском, как в API)."""
    data = http_get_json("https://api.alternative.me/fng/?limit=1")
    item = data["data"][0]
    zone = item["value_classification"]
    if zone not in FNG_ZONES:
        raise RuntimeError(f"Неизвестная зона индекса: {zone}")
    return int(item["value"]), zone


def fng_text(value, zone):
    return f"{value} — {FNG_NAMES_RU[zone]}"


# ---------- Общее ----------

def fmt_usd(x):
    return "$" + f"{x:,.0f}".replace(",", " ")


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=body, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram: {result}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def check_price(state, now):
    """Проверка резкого движения цены. Возвращает (текущая цена или None, изменилось ли состояние)."""
    candles, source = get_candles()

    if now - candles[-1][0] > 10 * 60:
        print(f"Данные о цене устарели (источник {source}), пропускаю проверку цены")
        return None, False

    current, drop_pct, rise_pct, high, low = analyze(candles, now)
    print(f"[{source}] BTC {fmt_usd(current)} | от максимума {drop_pct:+.2f}% "
          f"| от минимума {rise_pct:+.2f}% (окно {WINDOW_MIN} мин)")

    if TEST_MESSAGE:
        return current, False

    cooldown = COOLDOWN_MIN * 60
    changed = False

    if drop_pct <= -THRESHOLD_PCT and now - state.get("last_drop", 0) > cooldown:
        send_telegram(
            f"🔻 BTC упал на {abs(drop_pct):.1f}% за {WINDOW_MIN} мин\n"
            f"Было: {fmt_usd(high)} → сейчас: {fmt_usd(current)}"
        )
        state["last_drop"] = int(now)
        changed = True
        print("Отправлено уведомление о падении")

    if rise_pct >= THRESHOLD_PCT and now - state.get("last_rise", 0) > cooldown:
        send_telegram(
            f"🚀 BTC вырос на {rise_pct:.1f}% за {WINDOW_MIN} мин\n"
            f"Было: {fmt_usd(low)} → сейчас: {fmt_usd(current)}"
        )
        state["last_rise"] = int(now)
        changed = True
        print("Отправлено уведомление о росте")

    return current, changed


def check_fear_greed(state, price):
    """Уведомление, когда индекс переходит в более страшную зону: в «Страх» или в «Сильный страх».

    Возвращает (значение, зона, изменилось ли состояние).
    """
    value, zone = get_fear_greed()
    print(f"Индекс страха и жадности: {value} ({zone})")

    if TEST_MESSAGE:
        return value, zone, False

    prev = state.get("fng_zone")
    if prev == zone:
        return value, zone, False

    # Уведомляем при входе в зону страха и при углублении: Страх → Сильный страх.
    # При первом запуске (prev нет) тоже сообщаем, если уже страх.
    got_scarier = prev not in FNG_ZONES or FNG_ZONES.index(zone) < FNG_ZONES.index(prev)
    if zone in FNG_ALERT_ZONES and got_scarier:
        icon = "😱" if zone == "Extreme Fear" else "😨"
        lines = [f"{icon} Индекс страха и жадности перешёл в зону «{FNG_NAMES_RU[zone]}»",
                 f"Сейчас: {fng_text(value, zone)}"]
        if prev in FNG_ZONES:
            lines.append(f"Было: {FNG_NAMES_RU[prev]}")
        if price is not None:
            lines.append(f"Цена BTC: {fmt_usd(price)}")
        send_telegram("\n".join(lines))
        print("Отправлено уведомление по индексу страха и жадности")

    state["fng_zone"] = zone
    state["fng_value"] = value
    return value, zone, True


def main():
    now = time.time()
    state = load_state()
    errors = []
    changed = False

    price = None
    try:
        price, price_changed = check_price(state, now)
        changed |= price_changed
    except Exception as e:
        errors.append(f"цена: {e}")

    fng = None
    if FNG_ALERTS:
        try:
            value, zone, fng_changed = check_fear_greed(state, price)
            fng = (value, zone)
            changed |= fng_changed
        except Exception as e:
            errors.append(f"индекс страха и жадности: {e}")

    if TEST_MESSAGE:
        lines = ["✅ Мониторинг BTC работает"]
        lines.append(f"Текущая цена: {fmt_usd(price)}" if price is not None else "Цена: нет данных")
        if FNG_ALERTS:
            lines.append(f"Индекс страха и жадности: {fng_text(*fng)}" if fng else
                         "Индекс страха и жадности: нет данных")
        send_telegram("\n".join(lines))
        print("Тестовое сообщение отправлено")

    if changed:
        save_state(state)

    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
