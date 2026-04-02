"""
MOEX Signal Bot — Модуль 1 (автономный)
─────────────────────────────────────────
Получает дневные свечи по топ тикерам MOEX.
Детектирует аномалии объёма (>2x от среднего + мин. абсолютный объём).
Считает уровни поддержки/сопротивления и ATR.
Выдаёт торговые сигналы: вход / стоп / тейки / R:R.
"""

import os
import requests
from datetime import datetime, timedelta
from statistics import mean


# ─── Топ ликвидных тикеров MOEX ───────────────────────────────────────────────

TICKERS = [
    "GAZP",   # Газпром
    "SBER",   # Сбербанк
    "LKOH",   # Лукойл
    "ROSN",   # Роснефть
    "NVTK",   # Новатэк
    "GMKN",   # Норникель
    "YDEX",   # Яндекс
    "TATN",   # Татнефть
    "MGNT",   # Магнит
    "PLZL",   # Полюс (золото)
    "SNGS",   # Сургутнефтегаз
    "MTSS",   # МТС
    "ALRS",   # Алроса
    "VTBR",   # ВТБ
    "CHMF",   # Северсталь
    "TCSG",   # Т-Банк
    "PHOR",   # ФосАгро
    "AFKS",   # АФК Система
    "NLMK",   # НЛМК
    "SIBN",   # Газпром нефть
    "FLOT",   # Совкомфлот
    "RUAL",   # Русал
    "OZON",   # Ozon
    "MOEX",   # Московская биржа
    "SMLT",   # Самолет
    "TRNFP",  # Транснефть-п
]

# Минимальный объём торгов за день (руб.) — фильтр низколиквидных дней
# После 2022 г. иностранцы ушли, объёмы упали — порог важен
MIN_VOLUME_RUB = int(os.environ.get("MIN_VOLUME_RUB", "50_000_000".replace("_", "")))

# ─── MOEX API ─────────────────────────────────────────────────────────────────

BASE_URL = "https://iss.moex.com/iss"


def get_candles(ticker: str, days: int = 25) -> list[dict]:
    """Получаем дневные свечи за последние N дней."""
    date_from = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = (
        f"{BASE_URL}/engines/stock/markets/shares/boards/TQBR"
        f"/securities/{ticker}/candles.json"
        f"?from={date_from}&interval=24"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        columns = data["candles"]["columns"]
        rows = data["candles"]["data"]
        candles = [dict(zip(columns, row)) for row in rows]
        return candles[-days:] if len(candles) >= days else candles
    except Exception as e:
        print(f"  [!] Ошибка при получении {ticker}: {e}")
        return []


# ─── Анализ ──────────────────────────────────────────────────────────────────

def detect_volume_anomaly(candles: list[dict], threshold: float = 2.0) -> dict:
    """
    Сравниваем последний объём со средним за предыдущие свечи.
    threshold=2.0 → объём в 2 раза выше среднего = аномалия.
    Дополнительно: проверяем абсолютный объём >= MIN_VOLUME_RUB.
    """
    if len(candles) < 5:
        return {"anomaly": False, "reason": "недостаточно данных"}

    volumes = [c["value"] for c in candles if c.get("value")]
    if len(volumes) < 2:
        return {"anomaly": False, "reason": "нет данных по объёму"}

    last_vol = volumes[-1]

    # Абсолютный фильтр: отсекаем дни с мизерным объёмом
    if last_vol < MIN_VOLUME_RUB:
        return {
            "anomaly": False,
            "reason": f"объём ниже порога ({last_vol / 1e6:.1f}M < {MIN_VOLUME_RUB / 1e6:.0f}M руб.)",
            "ratio": 0,
        }

    baseline_vols = volumes[:-1]
    avg_vol = mean(baseline_vols)

    ratio = last_vol / avg_vol if avg_vol > 0 else 0

    return {
        "anomaly": ratio >= threshold,
        "ratio": round(ratio, 2),
        "last_volume": last_vol,
        "avg_volume": round(avg_vol, 0),
    }


def calc_levels(candles: list[dict]) -> dict:
    """
    Простые уровни поддержки и сопротивления:
    - сопротивление = максимум за N дней
    - поддержка     = минимум за N дней
    - ATR           = средний дневной диапазон (для стопа и тейка)
    """
    if not candles:
        return {}

    highs  = [c["high"]  for c in candles if c.get("high")]
    lows   = [c["low"]   for c in candles if c.get("low")]
    closes = [c["close"] for c in candles if c.get("close")]

    if not highs or not lows:
        return {}

    resistance = max(highs)
    support    = min(lows)

    ranges = [h - l for h, l in zip(highs, lows)]
    atr    = mean(ranges) if ranges else 0

    last_close = closes[-1] if closes else None

    return {
        "resistance": round(resistance, 2),
        "support":    round(support, 2),
        "atr":        round(atr, 2),
        "last_close": round(last_close, 2) if last_close else None,
    }


def build_signal(ticker: str, levels: dict, anomaly: dict) -> dict | None:
    """
    Формируем торговый сигнал если есть аномалия объёма.
    Логика:
    - Цена ближе к поддержке   → сигнал LONG
    - Цена ближе к сопротивлению → сигнал SHORT
    - Стоп  = 1.5x ATR от входа
    - Тейк1 = 1x ATR, Тейк2 = 2x ATR, Тейк3 = до уровня
    """
    if not anomaly.get("anomaly"):
        return None

    price      = levels.get("last_close")
    support    = levels.get("support")
    resistance = levels.get("resistance")
    atr        = levels.get("atr", 0)

    if not all([price, support, resistance, atr]):
        return None

    mid       = (support + resistance) / 2
    direction = "LONG" if price < mid else "SHORT"

    if direction == "LONG":
        stop  = round(price - 1.5 * atr, 2)
        take1 = round(price + 1.0 * atr, 2)
        take2 = round(price + 2.0 * atr, 2)
        take3 = resistance
    else:
        stop  = round(price + 1.5 * atr, 2)
        take1 = round(price - 1.0 * atr, 2)
        take2 = round(price - 2.0 * atr, 2)
        take3 = support

    rr = abs(take2 - price) / abs(price - stop) if abs(price - stop) > 0 else 0

    return {
        "ticker":       ticker,
        "direction":    direction,
        "entry":        price,
        "stop":         stop,
        "take1":        take1,
        "take2":        take2,
        "take3":        round(take3, 2),
        "rr_ratio":     round(rr, 2),
        "volume_ratio": anomaly["ratio"],
    }


# ─── Форматирование вывода ────────────────────────────────────────────────────

def format_signal(signal: dict) -> str:
    direction_emoji = "🟢 LONG" if signal["direction"] == "LONG" else "🔴 SHORT"
    return (
        f"\n{'='*45}\n"
        f"  {direction_emoji}  —  {signal['ticker']}\n"
        f"{'='*45}\n"
        f"  Вход:    {signal['entry']}\n"
        f"  Стоп:    {signal['stop']}\n"
        f"  Тейк 1:  {signal['take1']}\n"
        f"  Тейк 2:  {signal['take2']}\n"
        f"  Тейк 3:  {signal['take3']}\n"
        f"  R/R:     1:{signal['rr_ratio']}\n"
        f"  Объём:   x{signal['volume_ratio']} от среднего  ⚡ АНОМАЛИЯ\n"
    )


# ─── Главный запуск ───────────────────────────────────────────────────────────

def run_scan():
    print(f"\n🔍 Сканирование MOEX — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"   Тикеров: {len(TICKERS)}\n")

    signals    = []
    no_anomaly = []

    for ticker in TICKERS:
        print(f"  → {ticker}", end=" ", flush=True)
        candles = get_candles(ticker, days=21)

        if not candles:
            print("— нет данных")
            continue

        anomaly = detect_volume_anomaly(candles)
        levels  = calc_levels(candles)

        if anomaly.get("anomaly"):
            print(f"⚡ объём x{anomaly['ratio']}")
            signal = build_signal(ticker, levels, anomaly)
            if signal:
                signals.append(signal)
        else:
            reason = anomaly.get("reason", "")
            ratio  = anomaly.get("ratio", 0)
            suffix = f" ({reason})" if reason else f"(x{ratio})"
            print(f"— норма {suffix}")
            no_anomaly.append(ticker)

    # Вывод сигналов
    if signals:
        print(f"\n\n🎯 НАЙДЕНО СИГНАЛОВ: {len(signals)}")
        for s in sorted(signals, key=lambda x: x["volume_ratio"], reverse=True):
            print(format_signal(s))
    else:
        print("\n\n😴 Аномалий не найдено. Рынок спокойный.")

    print(f"\n✅ Сканирование завершено. Без аномалий: {len(no_anomaly)}")
    return signals


if __name__ == "__main__":
    run_scan()
