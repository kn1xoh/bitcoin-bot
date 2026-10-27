"""Мониторинг BTC: уведомления в Telegram.

Что отслеживается:
1. Резкое падение или рост цены за короткое окно (по умолчанию ±3% за 30 минут).
2. Медленные движения: изменение за 24 часа и за 7 дней.
3. Пробой ценовых уровней, заданных вручную (командами в Telegram или переменной LEVELS).
4. Просадка от максимума за 90 дней (ступени 15/20/30%).
5. Технические сигналы на дневках: цена относительно 200-дневной средней, RSI(14).
6. Индекс страха и жадности: вход в «Страх»/«Сильный страх», выход из страха, длительность страха.
7. Ежедневная сводка.
8. Команды в Telegram: /price, /levels, /add, /del, /help.

Запускается раз в несколько минут (GitHub Actions по расписанию), состояние хранится в state.json.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def env_bool(name, default):
    return os.getenv(name, default).strip().lower() == "true"


def env_numbers(name, default):
    raw = os.getenv(name, default)
    return [float(x) for x in raw.replace(" ", "").split(",") if x]


# Резкие движения
THRESHOLD_PCT = float(os.getenv("THRESHOLD_PCT", "3"))
WINDOW_MIN = int(os.getenv("WINDOW_MIN", "30"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))

# Медленные движения: "часы:процент" через запятую
SLOW_RULES = [
    (int(h), float(p))
    for h, p in (r.split(":") for r in os.getenv("SLOW_RULES", "24:5,168:10").replace(" ", "").split(",") if r)
]

# Ценовые уровни (начальный список; дальше управляются командами /add и /del)
LEVELS = env_numbers("LEVELS", "")
LEVEL_BUFFER_PCT = float(os.getenv("LEVEL_BUFFER_PCT", "0.3"))

# Просадка от максимума
DRAWDOWN_DAYS = int(os.getenv("DRAWDOWN_DAYS", "90"))
DRAWDOWN_STEPS = sorted(env_numbers("DRAWDOWN_STEPS", "15,20,30"))
DRAWDOWN_HYST = 3  # на сколько п.п. просадка должна сократиться, чтобы ступень «сбросилась»

# Технические сигналы
TECH_ALERTS = env_bool("TECH_ALERTS", "true")
MA_DAYS = 200
MA_BUFFER_PCT = 1.0
RSI_PERIOD = 14
RSI_LOW = float(os.getenv("RSI_LOW", "30"))
RSI_EXIT = float(os.getenv("RSI_EXIT", "35"))

# Индекс страха и жадности
FNG_ALERTS = env_bool("FNG_ALERTS", "true")
FEAR_STREAK_DAYS = [int(x) for x in env_numbers("FEAR_STREAK_DAYS", "3,7,14,30")]

# Сводка и команды
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "9"))
TZ_OFFSET = float(os.getenv("TZ_OFFSET", "3"))
COMMANDS = env_bool("COMMANDS", "true")

STATE_FILE = os.getenv("STATE_FILE", "state.json")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TEST_MESSAGE = env_bool("TEST_MESSAGE", "false")

HEADERS = {"User-Agent": "btc-monitor/2.0"}
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

FNG_ZONES = ["Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"]
FNG_NAMES_RU = {
    "Extreme Fear": "Сильный страх",
    "Fear": "Страх",
    "Neutral": "Нейтрально",
    "Greed": "Жадность",
    "Extreme Greed": "Сильная жадность",
}
FNG_ALERT_ZONES = {"Extreme Fear", "Fear"}


# ---------- Сеть ----------

def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def tg_api(method, params):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")
    return result["result"]


def send_telegram(text):
    params = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    try:
        tg_api("sendMessage", params)
    except Exception as e:
        # Если Telegram не принял разметку — отправляем то же самое простым текстом
        print(f"HTML не отправился ({e}), шлю без разметки", file=sys.stderr)
        params.pop("parse_mode")
        params["text"] = html.unescape(re.sub(r"</?(b|i|pre|code)>", "", text))
        tg_api("sendMessage", params)


OUTBOX = []


def alert(text):
    """Уведомления одного запуска копятся и уходят одним сообщением."""
    OUTBOX.append(text)


# ---------- Свечи ----------
# Каждая свеча приводится к виду (время начала, low, high, close)

COINBASE_GRAN = {"1m": 60, "1h": 3600, "1d": 86400}
KRAKEN_INT = {"1m": 1, "1h": 60, "1d": 1440}


def candles_coinbase(tf):
    data = http_get_json(
        f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={COINBASE_GRAN[tf]}"
    )
    # формат Coinbase: [time, low, high, open, close, volume]
    return [(int(c[0]), float(c[1]), float(c[2]), float(c[4])) for c in data]


def candles_kraken(tf):
    data = http_get_json(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={KRAKEN_INT[tf]}")
    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")
    rows = next(v for k, v in data["result"].items() if k != "last")
    # формат Kraken: [time, open, high, low, close, vwap, volume, count]
    return [(int(c[0]), float(c[3]), float(c[2]), float(c[4])) for c in rows]


def get_candles(tf):
    errors = []
    for source in (candles_coinbase, candles_kraken):
        try:
            candles = source(tf)
            if candles:
                return sorted(candles), source.__name__.replace("candles_", "")
        except Exception as e:
            errors.append(f"{source.__name__}: {e}")
    raise RuntimeError(f"Не удалось получить свечи {tf}: " + "; ".join(errors))


def get_fng_history():
    """Список (значение, зона, время) от сегодняшнего дня к прошлому."""
    data = http_get_json("https://api.alternative.me/fng/?limit=60")
    result = []
    for item in data["data"]:
        zone = item["value_classification"]
        if zone not in FNG_ZONES:
            raise RuntimeError(f"Неизвестная зона индекса: {zone}")
        result.append((int(item["value"]), zone, int(item["timestamp"])))
    return result


# ---------- Расчёты ----------

def change_over(hourly, now, hours, price):
    """Изменение цены за hours часов. Возвращает (процент, цена тогда) или None."""
    target = now - hours * 3600
    past = [c for c in hourly if c[0] + 3600 <= target]
    if not past:
        return None
    ref = past[-1][3]
    return (price - ref) / ref * 100, ref


def drawdown(daily, now, price):
    """Просадка от максимума за DRAWDOWN_DAYS дней. Возвращает (глубина в %, максимум)."""
    since = now - DRAWDOWN_DAYS * 86400
    peak = max([c[2] for c in daily if c[0] >= since] + [price])
    return (peak - price) / peak * 100, peak


def moving_average(daily, days):
    closes = [c[3] for c in daily]
    if len(closes) < days:
        return None
    return sum(closes[-days:]) / days


def rsi(daily, period=RSI_PERIOD):
    closes = [c[3] for c in daily]
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def fear_streak(history):
    """Сколько дней подряд (включая сегодня) индекс в зоне страха, из них в сильном страхе, и дата начала."""
    streak = extreme = 0
    start = None
    for value, zone, ts in history:
        if zone not in FNG_ALERT_ZONES:
            break
        streak += 1
        extreme += zone == "Extreme Fear"
        start = ts
    return streak, extreme, start


# ---------- Форматирование ----------

def fmt_usd(x):
    return "$" + f"{x:,.0f}".replace(",", " ")


def fmt_pct(x):
    return f"{x:+.1f}%".replace("-", "−")


def period_name(hours):
    if hours % 24 == 0 and hours >= 48:
        return f"{hours // 24} дн"
    return f"{hours} ч"


def esc(x):
    return html.escape(str(x), quote=False)


def table(rows):
    """Строки (подпись, значение) в моноширинном блоке, значения выровнены в колонку."""
    rows = [(str(k), str(v)) for k, v in rows if v is not None]
    width = max(len(k) for k, _ in rows)
    return "<pre>" + "\n".join(esc(f"{k:<{width}}  {v}".rstrip()) for k, v in rows) + "</pre>"


def card(title, rows=None):
    text = f"<b>{esc(title)}</b>"
    if rows:
        text += "\n" + table(rows)
    return text


def level_key(level):
    return str(int(level)) if float(level).is_integer() else str(level)


def days_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


# ---------- Сбор данных ----------

def collect(now, errors):
    m = {}
    try:
        minute, source = get_candles("1m")
        if now - minute[-1][0] <= 10 * 60:
            m["minute"] = minute
            m["price"] = minute[-1][3]
            m["source"] = source
        else:
            errors.append(f"минутные свечи устарели (источник {source})")
    except Exception as e:
        errors.append(f"минутные свечи: {e}")

    try:
        hourly, _ = get_candles("1h")
        m["hourly"] = hourly
        if "price" not in m and now - hourly[-1][0] <= 2 * 3600:
            m["price"] = hourly[-1][3]
    except Exception as e:
        errors.append(f"часовые свечи: {e}")

    try:
        daily, _ = get_candles("1d")
        m["daily"] = daily
    except Exception as e:
        errors.append(f"дневные свечи: {e}")

    if FNG_ALERTS:
        try:
            m["fng"] = get_fng_history()
        except Exception as e:
            errors.append(f"индекс страха и жадности: {e}")

    if "price" in m:
        print(f"[{m.get('source', 'hourly')}] BTC {fmt_usd(m['price'])}")
    return m


# ---------- Проверки ----------
# Каждая проверка меняет state и добавляет уведомления через alert().

def check_fast(state, m, now):
    if "minute" not in m:
        return
    window = [c for c in m["minute"] if c[0] >= now - WINDOW_MIN * 60]
    if not window:
        return
    current = window[-1][3]
    high = max(c[2] for c in window)
    low = min(c[1] for c in window)
    drop_pct = (current - high) / high * 100
    rise_pct = (current - low) / low * 100
    print(f"Окно {WINDOW_MIN} мин: от максимума {drop_pct:+.2f}%, от минимума {rise_pct:+.2f}%")

    cooldown = COOLDOWN_MIN * 60
    if drop_pct <= -THRESHOLD_PCT and now - state.get("last_drop", 0) > cooldown:
        alert(card(f"▼ Резкое падение {fmt_pct(drop_pct)} за {WINDOW_MIN} мин",
                   [("Было", fmt_usd(high)), ("Сейчас", fmt_usd(current))]))
        state["last_drop"] = int(now)
    if rise_pct >= THRESHOLD_PCT and now - state.get("last_rise", 0) > cooldown:
        alert(card(f"▲ Резкий рост {fmt_pct(rise_pct)} за {WINDOW_MIN} мин",
                   [("Было", fmt_usd(low)), ("Сейчас", fmt_usd(current))]))
        state["last_rise"] = int(now)


def check_slow(state, m, now):
    if "hourly" not in m or "price" not in m:
        return
    price = m["price"]
    slow = state.setdefault("slow", {})
    for hours, pct in SLOW_RULES:
        ch = change_over(m["hourly"], now, hours, price)
        if ch is None:
            continue
        change, ref = ch
        print(f"За {period_name(hours)}: {change:+.2f}%")
        cooldown = hours * 3600 / 2
        if change <= -pct and now - slow.get(f"drop_{hours}", 0) > cooldown:
            alert(card(f"▼ Падение {fmt_pct(change)} за {period_name(hours)}",
                       [("Было", fmt_usd(ref)), ("Сейчас", fmt_usd(price))]))
            slow[f"drop_{hours}"] = int(now)
        if change >= pct and now - slow.get(f"rise_{hours}", 0) > cooldown:
            alert(card(f"▲ Рост {fmt_pct(change)} за {period_name(hours)}",
                       [("Было", fmt_usd(ref)), ("Сейчас", fmt_usd(price))]))
            slow[f"rise_{hours}"] = int(now)


def get_levels(state):
    if "levels" not in state:
        state["levels"] = sorted(LEVELS, reverse=True)
    return state["levels"]


def check_levels(state, m, now):
    if "price" not in m:
        return
    price = m["price"]
    sides = state.setdefault("level_side", {})
    buf = LEVEL_BUFFER_PCT / 100
    down, up = [], []
    for level in get_levels(state):
        key = level_key(level)
        side = sides.get(key)
        if side is None:
            sides[key] = "above" if price >= level else "below"
        elif side == "above" and price < level * (1 - buf):
            sides[key] = "below"
            down.append(level)
        elif side == "below" and price > level * (1 + buf):
            sides[key] = "above"
            up.append(level)
    for crossed, arrow, word in ((sorted(down, reverse=True), "▼", "вниз"), (sorted(up), "▲", "вверх")):
        if not crossed:
            continue
        title = f"{arrow} Пробой {'уровня' if len(crossed) == 1 else 'уровней'} {word}"
        label = "Уровень" if len(crossed) == 1 else "Уровни"
        rows = [(label if i == 0 else "", fmt_usd(x)) for i, x in enumerate(crossed)]
        alert(card(title, rows + [("Сейчас", fmt_usd(price))]))


def check_drawdown(state, m, now):
    if "daily" not in m or "price" not in m or not DRAWDOWN_STEPS:
        return
    depth, peak = drawdown(m["daily"], now, m["price"])
    reached = max([s for s in DRAWDOWN_STEPS if depth >= s], default=0)
    stored = state.get("dd_step")
    print(f"Просадка от максимума за {DRAWDOWN_DAYS} дн: {depth:.1f}% (максимум {fmt_usd(peak)})")
    if stored is None:
        state["dd_step"] = reached
    elif reached > stored:
        alert(card(f"▼ Просадка {fmt_pct(-depth)} от максимума за {DRAWDOWN_DAYS} дн",
                   [("Максимум", fmt_usd(peak)), ("Сейчас", fmt_usd(m["price"]))]))
        state["dd_step"] = reached
    elif reached < stored and depth < stored - DRAWDOWN_HYST:
        state["dd_step"] = reached


def check_tech(state, m, now):
    if not TECH_ALERTS or "daily" not in m or "price" not in m:
        return
    price = m["price"]
    daily = m["daily"]

    ma = moving_average(daily, MA_DAYS)
    if ma is not None:
        buf = MA_BUFFER_PCT / 100
        side = state.get("ma_side")
        new_side = side
        if price < ma * (1 - buf):
            new_side = "below"
        elif price > ma * (1 + buf):
            new_side = "above"
        if side is None:
            state["ma_side"] = new_side or ("above" if price >= ma else "below")
        elif new_side != side:
            state["ma_side"] = new_side
            title = ("▼ Цена ушла ниже 200-дневной средней" if new_side == "below"
                     else "▲ Цена вернулась выше 200-дневной средней")
            alert(card(title, [("MA200", fmt_usd(ma)), ("Цена", fmt_usd(price)),
                               ("Разница", fmt_pct((price - ma) / ma * 100))]))

    value = rsi(daily)
    if value is not None:
        print(f"RSI({RSI_PERIOD}) дн: {value:.1f}, MA{MA_DAYS}: {fmt_usd(ma) if ma else 'нет данных'}")
        zone = state.get("rsi_zone")
        if zone is None:
            state["rsi_zone"] = "oversold" if value < RSI_LOW else "normal"
        elif zone == "normal" and value < RSI_LOW:
            state["rsi_zone"] = "oversold"
            alert(card(f"▼ Дневной RSI({RSI_PERIOD}): перепроданность",
                       [("RSI", f"{value:.0f} (порог {RSI_LOW:g})"), ("Цена", fmt_usd(price))]))
        elif zone == "oversold" and value > RSI_EXIT:
            state["rsi_zone"] = "normal"
            alert(card(f"▲ Дневной RSI({RSI_PERIOD}) вышел из перепроданности",
                       [("RSI", f"{value:.0f} (выход выше {RSI_EXIT:g})"), ("Цена", fmt_usd(price))]))


def check_fear_greed(state, m, now):
    if "fng" not in m:
        return
    history = m["fng"]
    value, zone, _ = history[0]
    price = m.get("price")
    print(f"Индекс страха и жадности: {value} ({zone})")

    prev = state.get("fng_zone")
    if prev != zone:
        title = None
        # Вход в страх или углубление: Страх → Сильный страх. При первом запуске тоже сообщаем.
        got_scarier = prev not in FNG_ZONES or FNG_ZONES.index(zone) < FNG_ZONES.index(prev)
        if zone in FNG_ALERT_ZONES and got_scarier:
            title = f"Страх и жадность: зона «{FNG_NAMES_RU[zone]}»"
        elif prev in FNG_ALERT_ZONES and zone not in FNG_ALERT_ZONES:
            title = "Страх и жадность: выход из зоны страха"
        if title:
            alert(card(title, [
                ("Индекс", value),
                ("Зона", FNG_NAMES_RU[zone]),
                ("Было", FNG_NAMES_RU[prev] if prev in FNG_ZONES else None),
                ("Цена BTC", fmt_usd(price) if price is not None else None),
            ]))
        state["fng_zone"] = zone
    state["fng_value"] = value

    streak, extreme, start = fear_streak(history)
    if streak == 0:
        state.pop("fng_streak", None)
        return
    reached = max([d for d in FEAR_STREAK_DAYS if streak >= d], default=0)
    info = state.get("fng_streak")
    if info is None or info.get("start") != start:
        # Новая серия. Если бот запущен посреди длинной серии — не спамим старыми отметками.
        state["fng_streak"] = {"start": start, "reported": reached if info is None else 0}
        info = state["fng_streak"]
    if reached > info["reported"]:
        alert(card(f"Страх держится {streak} {days_word(streak)} подряд", [
            ("Индекс", value),
            ("Зона", FNG_NAMES_RU[zone]),
            ("Сильный страх", f"{extreme} {days_word(extreme)}" if extreme else None),
            ("Цена BTC", fmt_usd(price) if price is not None else None),
        ]))
        info["reported"] = reached


def summary_text(state, m, now, title):
    if "price" not in m:
        return card(title, [("Цена", "нет данных")])
    price = m["price"]

    main_rows = [("Цена", fmt_usd(price))]
    if "hourly" in m:
        for hours in (24, 168):
            ch = change_over(m["hourly"], now, hours, price)
            if ch:
                main_rows.append((f"За {period_name(hours)}", fmt_pct(ch[0])))
    blocks = [card(title, main_rows)]

    ind = []
    if "daily" in m:
        depth, peak = drawdown(m["daily"], now, price)
        ind.append((f"Максимум {DRAWDOWN_DAYS} дн", fmt_usd(peak)))
        ind.append(("От максимума", fmt_pct(-depth)))
        ma = moving_average(m["daily"], MA_DAYS)
        if ma:
            ind.append(("MA200", fmt_usd(ma)))
            ind.append(("Цена к MA200", fmt_pct((price - ma) / ma * 100)))
        value = rsi(m["daily"])
        if value is not None:
            ind.append((f"RSI({RSI_PERIOD}) дн", f"{value:.0f}"))
    if "fng" in m:
        value, zone, _ = m["fng"][0]
        ind.append(("Страх/жадность", f"{value}"))
        ind.append(("Зона", FNG_NAMES_RU[zone]))
        streak, _, _ = fear_streak(m["fng"])
        if streak > 1:
            ind.append(("В страхе", f"{streak} {days_word(streak)} подряд"))
    if ind:
        blocks.append("<b>Индикаторы</b>\n" + table(ind))

    levels = get_levels(state)
    above = [x for x in levels if x > price]
    below = [x for x in levels if x <= price]
    lv_rows = []
    if above:
        lv = min(above)
        lv_rows.append(("Выше", f"{fmt_usd(lv)}  {fmt_pct((lv - price) / price * 100)}"))
    if below:
        lv = max(below)
        lv_rows.append(("Ниже", f"{fmt_usd(lv)}  {fmt_pct((lv - price) / price * 100)}"))
    if lv_rows:
        blocks.append("<b>Ближайшие уровни</b>\n" + table(lv_rows))

    return "\n\n".join(blocks)


def check_summary(state, m, now):
    local = datetime.fromtimestamp(now, LOCAL_TZ)
    today = local.date().isoformat()
    if local.hour < SUMMARY_HOUR or state.get("summary_date") == today or "price" not in m:
        return
    send_telegram(summary_text(state, m, now, f"Сводка BTC на {local:%d.%m}"))
    state["summary_date"] = today


# ---------- Команды Telegram ----------

HELP = (
    "<b>Команды</b>\n"
    "/price — цена и показатели сейчас\n"
    "/levels — список уровней\n"
    "/add 60000 65k — добавить уровни\n"
    "/del 60000 — удалить уровень\n"
    "/del all — удалить все уровни\n"
    "/help — эта справка\n\n"
    "<i>Бот проверяет сообщения раз в несколько минут, поэтому ответ приходит не сразу.</i>"
)


def parse_price(s):
    s = s.lower().replace("$", "").replace("_", "").strip(",")
    mult = 1000 if s.endswith("k") or s.endswith("к") else 1
    value = float(s.rstrip("kк")) * mult
    if value <= 0:
        raise ValueError
    return value


def levels_text(state, m):
    levels = get_levels(state)
    if not levels:
        return "Уровней нет. Добавить: /add 60000"
    price = m.get("price")
    rows = []
    price_shown = price is None
    for lv in sorted(levels, reverse=True):
        if not price_shown and lv <= price:
            rows.append((fmt_usd(price), "← цена сейчас"))
            price_shown = True
        rows.append((fmt_usd(lv), fmt_pct((lv - price) / price * 100) if price else ""))
    if not price_shown:
        rows.append((fmt_usd(price), "← цена сейчас"))
    return card("Уровни", rows)


def handle_command(state, m, now, cmd, args):
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/price":
        return summary_text(state, m, now, "BTC сейчас")
    if cmd == "/levels":
        return levels_text(state, m)
    if cmd in ("/add", "/del"):
        if not args:
            return f"Укажи цену, например: {cmd} 60000"
        levels = get_levels(state)
        sides = state.setdefault("level_side", {})
        if cmd == "/del" and args[0].lower() in ("all", "все"):
            state["levels"] = []
            state["level_side"] = {}
            return "Все уровни удалены"
        try:
            values = [parse_price(a) for a in args]
        except ValueError:
            return "Не понял цену. Пример: /add 60000 65k"
        for v in values:
            if cmd == "/add" and v not in levels:
                levels.append(v)
            elif cmd == "/del" and v in levels:
                levels.remove(v)
                sides.pop(level_key(v), None)
        levels.sort(reverse=True)
        return levels_text(state, m)
    return "Неизвестная команда.\n\n" + HELP


def process_commands(state, m, now):
    updates = tg_api("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0})
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or str(msg["chat"]["id"]) != str(CHAT_ID):
            continue
        if now - msg.get("date", 0) > 3600:  # старые сообщения не обрабатываем
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        # "60 000" → "60000": склеиваем пробелы внутри чисел
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = []
        for p in parts[1:]:
            if args and p.isdigit() and len(p) == 3 and args[-1].isdigit() and len(args[-1]) <= 3:
                args[-1] += p
            else:
                args.extend(x for x in p.split(",") if x)
        print(f"Команда: {text}")
        send_telegram(handle_command(state, m, now, cmd, args))


# ---------- Запуск ----------

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


def main():
    now = time.time()
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    errors = []

    m = collect(now, errors)

    if TEST_MESSAGE:
        send_telegram(summary_text(state, m, now, "Мониторинг BTC работает"))
        print("Тестовое сообщение отправлено")
    else:
        steps = [check_fast, check_slow, check_levels, check_drawdown, check_tech,
                 check_fear_greed, check_summary]
        if COMMANDS:
            steps.insert(0, process_commands)
        for step in steps:
            try:
                step(state, m, now)
            except Exception as e:
                errors.append(f"{step.__name__}: {e}")
        if OUTBOX:
            try:
                send_telegram("\n\n".join(OUTBOX))
            except Exception as e:
                errors.append(f"отправка уведомлений: {e}")
        if json.dumps(state, sort_keys=True) != before:
            save_state(state)

    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
