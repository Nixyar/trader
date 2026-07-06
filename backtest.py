#!/usr/bin/env python3
"""
backtest.py — честный исторический бэктест ЯДРОВОЙ стратегии бота
(объёмная аномалия D1 + уровни + ATR-стоп/тейк).

Зачем это нужно
───────────────
Боевой бот (moex_bot.py) накопил всего ~21 сделку в песочнице за 2 недели —
этого статистически НЕДОСТАТОЧНО, чтобы судить, есть ли у стратегии edge.
Любая «оптимизация» параметров вслепую = подгонка под шум.

Этот модуль НЕ дублирует торговую логику. Он импортирует ровно те же функции,
что использует боевой бот в run_once():
    calc_levels, detect_volume_anomaly, determine_direction,
    build_market_signal, evaluate_setup_quality
и прогоняет их по 2–3 годам дневных свечей MOEX (ISS API, бесплатно).

Что он измеряет — и чего НЕ измеряет
────────────────────────────────────
МОЖЕТ: edge ядровой D1-стратегии на дневных данных — win rate, матожидание,
       profit factor, кривую эквити при 1%-риске, max drawdown.
НЕ МОЖЕТ (честно признаём ограничения):
  • H1-подтверждение входа (check_h1_confirmation) — нет истории H1 за годы,
    поэтому бэктест берёт ВСЕ D1-сигналы. Это ВЕРХНЯЯ оценка: боевой бот
    торгует подмножество, прошедшее H1-фильтр.
  • intraday-уточнение entry/VWAP — вход берётся по дневному close (как fallback
    в build_market_signal, когда intraday недоступен).
  • новостную и index_rebound стратегии — они не бэктестятся (нет истории).
  • частичную фиксацию take1 + перенос стопа в безубыток — упрощённо: позиция
    держится целиком до stop / take2 / таймаута.
  • проскальзывание — моделируется только комиссия. Реальные исполнения на
    тонком рынке MOEX будут ХУЖЕ.

Вывод: если даже здесь edge отрицательный или нулевой — на реальных деньгах
будет хуже. Если положительный — это лишь повод для forward-теста, не для
немедленного перехода на реальные деньги.

Запуск:
    python3 backtest.py                      # дефолт: ~2 года, все тикеры
    python3 backtest.py --years 3 --quality  # 3 года, только сетапы качества >= B
    python3 backtest.py --tickers SBER,GAZP  # подмножество
    python3 backtest.py --commission 0.05    # комиссия %/сторону (Т-Банк ~0.04-0.05)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from statistics import mean, pstdev

import requests

# ── Импортируем боевые функции БЕЗ побочных эффектов ────────────────────────
# moex_bot.py весь под `if __name__ == "__main__"`, импорт безопасен.
logging.disable(logging.CRITICAL)  # глушим логи бота на время прогона
import moex_bot as mb  # noqa: E402

BASE_URL = "https://iss.moex.com/iss"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache")
WINDOW = 55           # окно свечей — как в боевом get_candles(days=55)
DEFAULT_HORIZON = 10  # макс. дней удержания позиции до таймаут-выхода
GRADE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}  # качество сетапа: A — лучшее


# ════════════════════════════════════════════════════════════════════════════
#  Загрузка истории (с кэшем на диск, чтобы не дёргать ISS лишний раз)
# ════════════════════════════════════════════════════════════════════════════
def fetch_daily_history(ticker: str, years: float, engine: str = "stock", market: str = "shares",
                        board: str = "TQBR") -> list[dict]:
    """Тянет дневные свечи за `years` лет с пагинацией ISS. Кэширует на диск."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{ticker}_{years}y.json")
    if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path)) < 86400:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    date_from = (datetime.now() - timedelta(days=int(years * 365) + 60)).strftime("%Y-%m-%d")
    out: list[dict] = []
    start = 0
    while True:
        url = (
            f"{BASE_URL}/engines/{engine}/markets/{market}/boards/{board}"
            f"/securities/{ticker}/candles.json?from={date_from}&interval=24&start={start}"
        )
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"    [!] {ticker}: ошибка загрузки: {e}", file=sys.stderr)
            break
        cols = data["candles"]["columns"]
        rows = [dict(zip(cols, row)) for row in data["candles"]["data"]]
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:  # ISS отдаёт страницами; короткая страница = последняя
            break
        start += len(rows)
        time.sleep(0.15)  # вежливость к ISS

    # Нормализуем под формат, который ждут функции бота
    norm = [
        {
            "open": c.get("open"), "close": c.get("close"),
            "high": c.get("high"), "low": c.get("low"),
            "value": c.get("value") or 0.0, "begin": c.get("begin") or "",
        }
        for c in out
        if c.get("close") and c.get("high") and c.get("low")
    ]
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(norm, f)
    return norm


def build_regime_map(years: float, ma: int = 50) -> dict[str, str]:
    """date(YYYY-MM-DD) → 'up'/'down' по тренду IMOEX: close vs его MA(ma)."""
    idx = fetch_daily_history("IMOEX", years, engine="stock", market="index", board="SNDX")
    closes = [(c["begin"][:10], c["close"]) for c in idx if c.get("close")]
    out: dict[str, str] = {}
    for i in range(len(closes)):
        if i < ma:
            continue
        window = [c for _, c in closes[i - ma:i]]
        out[closes[i][0]] = "up" if closes[i][1] > (sum(window) / len(window)) else "down"
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Симуляция исхода одной сделки против будущих дневных свечей
# ════════════════════════════════════════════════════════════════════════════
def simulate_trade(sig: dict, future: list[dict], horizon: int) -> tuple[str, float]:
    """
    Возвращает (result, exit_price).
    Консервативно: если в одной свече задеты и стоп, и тейк — считаем стоп первым.
    """
    entry = sig["entry"]
    stop = sig["stop"]
    take2 = sig["take2"]
    direction = sig["direction"]
    for c in future[:horizon]:
        hi, lo = c["high"], c["low"]
        if direction == "LONG":
            if lo <= stop:
                return "loss", stop
            if hi >= take2:
                return "win_t2", take2
        else:  # SHORT
            if hi >= stop:
                return "loss", stop
            if lo <= take2:
                return "win_t2", take2
    # Таймаут — выход по close последней доступной свечи горизонта
    last = future[min(horizon, len(future)) - 1] if future else None
    exit_price = last["close"] if last else entry
    return "timeout", exit_price


def trade_r_multiple(sig: dict, exit_price: float, commission_pct: float) -> float:
    """R-кратность сделки за вычетом комиссии (в единицах риска на акцию)."""
    entry, stop, direction = sig["entry"], sig["stop"], sig["direction"]
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0.0
    pnl_per_share = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
    comm_per_share = commission_pct / 100.0 * (entry + exit_price)  # round-trip
    return (pnl_per_share - comm_per_share) / risk_per_share


# ════════════════════════════════════════════════════════════════════════════
#  Прогон одного тикера
# ════════════════════════════════════════════════════════════════════════════
def backtest_ticker(ticker: str, candles: list[dict], args) -> list[dict]:
    trades: list[dict] = []
    ctx: dict = {}  # пустой макро-контекст: brent/gold/imoex → нейтрально (влияют лишь на score)
    n = len(candles)
    for t in range(WINDOW, n - 1):
        window = candles[t - WINDOW:t + 1]
        anomaly = mb.detect_volume_anomaly(window, ticker=ticker)
        if not anomaly.get("anomaly"):
            continue
        levels = mb.calc_levels(window)
        if not levels:
            continue

        price = levels.get("last_close")
        _dir, _type = mb.determine_direction(price, levels)
        ma50 = levels.get("ma50")
        # Тот же MA50_HARD_FILTER, что в боевом run_once (блокирует mean-rev против тренда)
        if mb.MA50_HARD_FILTER and ma50 and _type == "mean_reversion":
            if (_dir == "LONG" and price < ma50) or (_dir == "SHORT" and price > ma50):
                continue

        if args.type and _type != args.type:
            continue
        if args.direction and _dir != args.direction:
            continue
        if getattr(args, "regime_map", None) is not None:
            regime = args.regime_map.get(window[-1].get("begin", "")[:10])
            if regime is None:
                continue
            aligned = (regime == "up" and _dir == "LONG") or (regime == "down" and _dir == "SHORT")
            if not aligned:
                continue

        sig = mb.build_market_signal(ticker, levels, anomaly, ctx,
                                     intraday=None, h1_levels=None, h1_confirm=None)
        if not sig:
            continue

        if args.quality:
            grade, _score, _flags = mb.evaluate_setup_quality(sig)
            if GRADE_ORDER.get(grade, 0) < GRADE_ORDER.get(mb.AUTO_ORDER_MIN_SETUP_QUALITY, 99):
                continue

        future = candles[t + 1:]
        result, exit_price = simulate_trade(sig, future, args.horizon)
        r = trade_r_multiple(sig, exit_price, args.commission)
        trades.append({
            "ticker": ticker,
            "date": window[-1].get("begin", "")[:10],
            "direction": sig["direction"],
            "type": _type,
            "entry": sig["entry"], "stop": sig["stop"], "take2": sig["take2"],
            "rr": sig.get("rr"),
            "result": result, "exit": exit_price, "R": r,
        })
    return trades


# ════════════════════════════════════════════════════════════════════════════
#  Метрики
# ════════════════════════════════════════════════════════════════════════════
def report(trades: list[dict], args) -> None:
    if not trades:
        print("\n❌ Ни одной сделки не сгенерировано. Проверь тикеры/период.")
        return

    trades.sort(key=lambda x: x["date"])
    Rs = [t["R"] for t in trades]
    wins = [t for t in trades if t["R"] > 0]
    losses = [t for t in trades if t["R"] <= 0]
    gross_win = sum(t["R"] for t in wins)
    gross_loss = -sum(t["R"] for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Кривая эквити: фиксированный риск RISK_PCT% на сделку, компаундинг
    risk_frac = mb.RISK_PCT / 100.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    curve = []
    for t in trades:
        equity *= (1 + risk_frac * t["R"])
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        curve.append(equity)

    exp_R = mean(Rs)
    sharpe = (exp_R / pstdev(Rs)) if len(Rs) > 1 and pstdev(Rs) > 0 else 0.0

    span = f"{trades[0]['date']} … {trades[-1]['date']}"
    print("\n" + "═" * 64)
    print("  БЭКТЕСТ ЯДРОВОЙ СТРАТЕГИИ (объёмная аномалия D1)")
    print("═" * 64)
    print(f"  Период сделок:     {span}")
    print(f"  Тикеров:           {len(set(t['ticker'] for t in trades))}")
    print(f"  Комиссия:          {args.commission}%/сторону   Горизонт: {args.horizon} дн")
    print(f"  Фильтр качества:   {'ВКЛ (>= ' + mb.AUTO_ORDER_MIN_SETUP_QUALITY + ')' if args.quality else 'выкл (все D1-сигналы)'}")
    print("─" * 64)
    print(f"  Сделок:            {len(trades)}")
    print(f"  Win rate:          {100*len(wins)/len(trades):.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Матожидание:       {exp_R:+.3f} R на сделку")
    print(f"  Profit factor:     {pf:.2f}")
    print(f"  Avg win / loss:    {(gross_win/len(wins) if wins else 0):+.2f}R / "
          f"{(-gross_loss/len(losses) if losses else 0):+.2f}R")
    print(f"  Sharpe (per-trade):{sharpe:+.2f}")
    print("─" * 64)
    print(f"  Итоговая эквити:   ×{equity:.3f}  ({(equity-1)*100:+.1f}% при {mb.RISK_PCT:.0f}% риске/сделку)")
    print(f"  Max drawdown:      {max_dd*100:.1f}%")
    print("═" * 64)

    # Разбивка по результату
    from collections import Counter
    rc = Counter(t["result"] for t in trades)
    print("  Исходы:           ", dict(rc))

    # Разбивка по тикерам (топ по числу сделок)
    by_t: dict[str, list[float]] = {}
    for t in trades:
        by_t.setdefault(t["ticker"], []).append(t["R"])
    print("─" * 64)
    print("  По тикерам (сделок / сумма R / win%):")
    for tk, rs in sorted(by_t.items(), key=lambda x: -sum(x[1])):
        w = sum(1 for r in rs if r > 0)
        print(f"    {tk:6s}  {len(rs):3d}   {sum(rs):+6.2f}R   {100*w/len(rs):3.0f}%")

    print("\n  ⚠️  Это ВЕРХНЯЯ оценка (без H1-фильтра, без проскальзывания).")
    print("      Реальная торговля будет хуже. См. шапку backtest.py.\n")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"  💾 Сделки сохранены: {args.csv}")


# ════════════════════════════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser(description="Бэктест ядровой стратегии MOEX-бота")
    ap.add_argument("--years", type=float, default=2.0, help="глубина истории, лет")
    ap.add_argument("--tickers", type=str, default="", help="CSV-список (по умолчанию все из бота)")
    ap.add_argument("--quality", action="store_true", help="применять фильтр качества сетапа (>= B)")
    ap.add_argument("--commission", type=float, default=0.05, help="комиссия %%/сторону")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="макс дней удержания")
    ap.add_argument("--csv", type=str, default="", help="путь для выгрузки сделок в CSV")
    ap.add_argument("--split", action="store_true",
                    help="out-of-sample: разбить период пополам и сравнить + проверить устойчивость по тикерам")
    ap.add_argument("--min-vol-ratio", type=float, default=None,
                    help="мин. множитель аномалии объёма (переопределяет VOLUME_THRESHOLD)")
    ap.add_argument("--type", choices=["momentum", "mean_reversion"], default=None,
                    help="торговать только сигналы этого типа")
    ap.add_argument("--direction", choices=["LONG", "SHORT"], default=None,
                    help="торговать только в эту сторону")
    ap.add_argument("--tier", choices=["1", "2"], default=None,
                    help="ограничить вселенную: 1 — самые ликвидные, 2 — второй эшелон")
    ap.add_argument("--regime", action="store_true",
                    help="trend-following: брать сделку только по направлению тренда IMOEX (LONG в up, SHORT в down)")
    args = ap.parse_args()

    if args.min_vol_ratio is not None:
        mb.VOLUME_THRESHOLD = args.min_vol_ratio  # детектор аномалии возьмёт новый порог

    args.regime_map = build_regime_map(args.years) if args.regime else None
    if args.regime:
        print(f"Карта режима IMOEX построена: {len(args.regime_map)} торговых дней")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.tier == "1":
        tickers = sorted(mb.TIER_1_TICKERS)
    elif args.tier == "2":
        tickers = sorted(mb.TIER_2_TICKERS)
    else:
        tickers = list(mb.TICKERS)
    print(f"Загрузка истории ({args.years} лет) для {len(tickers)} тикеров…")

    all_trades: list[dict] = []
    for tk in tickers:
        candles = fetch_daily_history(tk, args.years)
        if len(candles) < WINDOW + 5:
            print(f"  {tk}: мало данных ({len(candles)}), пропуск")
            continue
        tr = backtest_ticker(tk, candles, args)
        all_trades.extend(tr)
        print(f"  {tk}: {len(candles)} свечей → {len(tr)} сделок")

    if args.split:
        split_report(all_trades)
    else:
        report(all_trades, args)


def split_report(trades: list[dict]) -> None:
    """Out-of-sample: первая половина периода vs вторая. Edge должен ПЕРЕЖИВАТЬ смену режима."""
    if len(trades) < 20:
        print("\n❌ Мало сделок для split-анализа.")
        return
    trades.sort(key=lambda x: x["date"])
    mid_date = trades[len(trades) // 2]["date"]
    first = [t for t in trades if t["date"] < mid_date]
    second = [t for t in trades if t["date"] >= mid_date]

    def stats(ts: list[dict]) -> tuple[float, float, int]:
        Rs = [t["R"] for t in ts]
        gw = sum(r for r in Rs if r > 0)
        gl = -sum(r for r in Rs if r <= 0)
        pf = gw / gl if gl > 0 else float("inf")
        return mean(Rs), pf, len(ts)

    e1, pf1, n1 = stats(first)
    e2, pf2, n2 = stats(second)
    print("\n" + "═" * 64)
    print("  OUT-OF-SAMPLE: устойчив ли edge между периодами рынка?")
    print("═" * 64)
    print(f"  Период 1 ({first[0]['date']}…{first[-1]['date']}): "
          f"{n1} сделок, матожидание {e1:+.3f}R, PF {pf1:.2f}")
    print(f"  Период 2 ({second[0]['date']}…{second[-1]['date']}): "
          f"{n2} сделок, матожидание {e2:+.3f}R, PF {pf2:.2f}")
    print("─" * 64)

    # Устойчивость по тикерам: корреляция «суммы R в п.1» и «в п.2».
    # Высокая → edge привязан к тикеру (реален). Около нуля → шум.
    by1: dict[str, float] = {}
    by2: dict[str, float] = {}
    for t in first:
        by1[t["ticker"]] = by1.get(t["ticker"], 0) + t["R"]
    for t in second:
        by2[t["ticker"]] = by2.get(t["ticker"], 0) + t["R"]
    common = sorted(set(by1) & set(by2))
    if len(common) >= 3:
        x = [by1[t] for t in common]
        y = [by2[t] for t in common]
        mx, my = mean(x), mean(y)
        cov = mean([(a - mx) * (b - my) for a, b in zip(x, y)])
        sx, sy = pstdev(x), pstdev(y)
        corr = cov / (sx * sy) if sx > 0 and sy > 0 else 0.0
        # Сколько тикеров-«победителей» п.1 остались в плюсе в п.2
        winners1 = [t for t in common if by1[t] > 0]
        persisted = sum(1 for t in winners1 if by2[t] > 0)
        print(f"  Корреляция R по тикерам (п.1 ↔ п.2): {corr:+.2f}")
        print(f"    (>+0.5 — edge реален; ~0 — шум; <0 — анти-устойчивость)")
        print(f"  Победители п.1, оставшиеся в плюсе в п.2: {persisted}/{len(winners1)}")
    print("═" * 64)
    verdict = "ШУМ — edge не переносится" if (e1 * e2 <= 0 or abs(e1) < 0.03 or abs(e2) < 0.03) \
        else "есть устойчивость — стоит копать"
    print(f"  Вердикт: {verdict}\n")


if __name__ == "__main__":
    main()
