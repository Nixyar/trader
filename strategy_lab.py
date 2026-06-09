#!/usr/bin/env python3
"""
strategy_lab.py — честное сравнение РАЗНЫХ семейств стратегий.

Вопрос пользователя: «найди стратегию, которая торгует в плюс не хуже профи».
Честный бенчмарк профессионала = «купить и держать» (buy & hold). Большинство
активных управляющих его НЕ обыгрывают после издержек. Этот модуль проверяет,
способна ли хоть одна классическая систематическая стратегия обойти buy & hold
на акциях MOEX — вне выборки и с учётом издержек.

Стратегии (long-only, по каждому тикеру независимо, equal-weight портфель):
  • bh       — buy & hold (бенчмарк профи)
  • tsmom    — time-series momentum: в позиции, пока цена > MA(200) (тренд вверх),
               иначе кэш. Единственный эффект с десятилетиями подтверждений.
  • ma_cross — пересечение MA(20)/MA(100): в позиции, пока быстрая > медленной.
  • rsi2     — краткосрочный возврат к среднему (Connors): вход при RSI(2)<10 в
               восходящем тренде (цена>MA200), выход при цене>MA(5).

Метрики: годовая доходность (CAGR), макс. просадка, Sharpe (годовой), и
out-of-sample (1-я половина периода vs 2-я). Издержки — на каждую смену позиции.

ВАЖНО: даже если что-то обгонит buy&hold на этой истории — это НЕ гарантия
будущего и НЕ «плюс каждый месяц». Это лишь повод для forward-теста.

Запуск:
    python3 strategy_lab.py --years 4 --commission 0.10
"""
from __future__ import annotations

import argparse
import logging
import math
from statistics import mean, pstdev

logging.disable(logging.CRITICAL)
import backtest as bt  # переиспользуем загрузчик истории с кэшем
import moex_bot as mb

TRADING_DAYS = 252


# ── Индикаторы на массиве close ─────────────────────────────────────────────
def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_series(xs: list[float], n: int = 2) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    for i in range(n, len(xs)):
        ag = mean(gains[i - n + 1:i + 1])
        al = mean(losses[i - n + 1:i + 1])
        if al == 0:
            out[i] = 100.0
        else:
            rs = ag / al
            out[i] = 100 - 100 / (1 + rs)
    return out


# ── Стратегии: (closes, highs, lows) → список целевых позиций (0/1) по дням ──
def strat_bh(closes, highs, lows):
    return [1] * len(closes)


def strat_tsmom(closes, highs, lows, ma=200):
    m = sma(closes, ma)
    return [1 if (m[i] is not None and closes[i] > m[i]) else 0 for i in range(len(closes))]


def strat_ma_cross(closes, highs, lows, fast=20, slow=100):
    f, s = sma(closes, fast), sma(closes, slow)
    return [1 if (f[i] is not None and s[i] is not None and f[i] > s[i]) else 0
            for i in range(len(closes))]


def strat_rsi2(closes, highs, lows, low=10.0, ma_trend=200, ma_exit=5):
    r = rsi_series(closes, 2)
    mt = sma(closes, ma_trend)
    me = sma(closes, ma_exit)
    pos = [0] * len(closes)
    holding = 0
    for i in range(len(closes)):
        if holding:
            if me[i] is not None and closes[i] > me[i]:   # выход: цена выше короткой MA
                holding = 0
        else:
            if (r[i] is not None and mt[i] is not None      # вход: перепроданность в тренде
                    and r[i] < low and closes[i] > mt[i]):
                holding = 1
        pos[i] = holding
    return pos


def strat_fib(closes, highs, lows, lookback=60, ma_trend=200):
    """
    Фибоначчи-ретрейсмент (long-only). В восходящем тренде (цена>MA200) ждём
    откат в зону 0.382–0.618 свинга последних `lookback` дней и разворот вверх →
    вход. Цель — вершина свинга, стоп — низ свинга.
    """
    mt = sma(closes, ma_trend)
    pos = [0] * len(closes)
    holding = 0
    target = stop = None
    for i in range(len(closes)):
        if i < lookback or mt[i] is None:
            continue
        if holding:
            if closes[i] >= target or closes[i] <= stop:
                holding = 0
        else:
            sh = max(highs[i - lookback:i])
            sl = min(lows[i - lookback:i])
            rng = sh - sl
            if rng <= 0:
                continue
            fib_382 = sh - 0.382 * rng
            fib_618 = sh - 0.618 * rng
            uptrend = closes[i] > mt[i]
            in_zone = fib_618 <= closes[i] <= fib_382
            turning_up = closes[i] > closes[i - 1]
            if uptrend and in_zone and turning_up:
                holding, target, stop = 1, sh, sl
        pos[i] = holding
    return pos


def strat_high52(closes, highs, lows, lookback=252, prox=0.05, ma=100):
    """
    Эффект 52-недельного максимума (George & Hwang): акции у годового максимума
    продолжают расти (anchoring bias). Long, пока цена в пределах `prox` от
    252-дн максимума И выше MA100; выход — при уходе ниже MA100.
    """
    m = sma(closes, ma)
    pos = [0] * len(closes)
    holding = 0
    for i in range(len(closes)):
        if i < lookback or m[i] is None:
            continue
        hi252 = max(highs[i - lookback:i + 1])
        near_high = closes[i] >= (1 - prox) * hi252
        above_ma = closes[i] > m[i]
        if holding:
            if not above_ma:
                holding = 0
        else:
            if near_high and above_ma:
                holding = 1
        pos[i] = holding
    return pos


def strat_donchian(closes, highs, lows, enter=20, exit_=10):
    """Пробой уровней (Donchian / «черепахи»): вход при пробое N-дн максимума,
    выход при пробое M-дн минимума. Классическое трендследование по уровням."""
    pos = [0] * len(closes)
    holding = 0
    for i in range(len(closes)):
        if i < enter:
            continue
        if holding:
            if closes[i] <= min(lows[i - exit_:i]):
                holding = 0
        else:
            if closes[i] >= max(highs[i - enter:i]):
                holding = 1
        pos[i] = holding
    return pos


STRATEGIES = {
    "bh": strat_bh,
    "tsmom": strat_tsmom,
    "ma_cross": strat_ma_cross,
    "rsi2": strat_rsi2,
    "fib": strat_fib,
    "donchian": strat_donchian,
}


# ── Доходности одной стратегии по одному тикеру ─────────────────────────────
def daily_returns(closes: list[float], positions: list[int], cost_pct: float) -> list[float]:
    """ret_t = pos_{t-1}·pct_change_t − издержки на смену позиции."""
    rets: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            rets.append(0.0)
            continue
        chg = closes[i] / closes[i - 1] - 1.0
        r = positions[i - 1] * chg
        if positions[i] != positions[i - 1]:           # вход или выход → издержка
            r -= cost_pct / 100.0
        rets.append(r)
    return rets


def metrics(rets: list[float]) -> dict:
    if not rets:
        return {"cagr": 0, "mdd": 0, "sharpe": 0, "n": 0}
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    years = len(rets) / TRADING_DAYS
    cagr = eq ** (1 / years) - 1 if years > 0 and eq > 0 else -1
    mu = mean(rets)
    sd = pstdev(rets)
    sharpe = (mu / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "final": eq, "n": len(rets)}


# ── Прогон стратегии по всей вселенной (equal-weight портфель) ──────────────
def run_strategy(strat, hist: dict[str, list[dict]], cost_pct: float,
                 date_lo: str = "", date_hi: str = "~") -> dict:
    """
    Портфель equal-weight, ВЫРОВНЕННЫЙ ПО ДАТАМ (а не по индексу — иначе у разных
    тикеров «день i» это разные даты, и метрики ломаются).

    `strat` — имя из STRATEGIES либо произвольная функция (closes, highs, lows)->pos
    (для брутфорса параметров). Индикаторы считаются по ПОЛНОЙ истории тикера
    (без заглядывания вперёд), доходности учитываются только в окне [date_lo, date_hi].
    """
    fn = STRATEGIES[strat] if isinstance(strat, str) else strat
    by_date: dict[str, list[float]] = {}
    for ticker, candles in hist.items():
        rows = [c for c in candles if c.get("close") and c.get("begin")]
        if len(rows) < 220:
            continue
        closes = [c["close"] for c in rows]
        highs = [c.get("high") or c["close"] for c in rows]
        lows = [c.get("low") or c["close"] for c in rows]
        dates = [c["begin"][:10] for c in rows]
        pos = fn(closes, highs, lows)          # позиции по всей истории
        rets = daily_returns(closes, pos, cost_pct)  # длина len-1, соответствует dates[1:]
        for i, r in enumerate(rets):
            d = dates[i + 1]
            if date_lo <= d <= date_hi:
                by_date.setdefault(d, []).append(r)
    if not by_date:
        return metrics([])
    port = [mean(by_date[d]) for d in sorted(by_date)]
    return metrics(port)


def median_date(hist: dict[str, list[dict]], lo: str = "", hi: str = "~") -> str:
    """Медианная торговая дата в окне [lo,hi] — граница для OOS-сплита."""
    all_dates = sorted({c["begin"][:10] for candles in hist.values()
                        for c in candles if c.get("begin") and lo <= c["begin"][:10] <= hi})
    return all_dates[len(all_dates) // 2] if all_dates else ""


def main():
    ap = argparse.ArgumentParser(description="Сравнение семейств стратегий vs buy&hold")
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--commission", type=float, default=0.10, help="издержка %/смену позиции")
    ap.add_argument("--tickers", type=str, default="")
    ap.add_argument("--risk-free", type=float, default=None,
                    help="безрисковая ставка %% годовых (по умолчанию — ключевая ставка ЦБ)")
    ap.add_argument("--from", dest="d_from", type=str, default="",
                    help="начало окна YYYY-MM-DD (по умолчанию — вся загруженная история)")
    ap.add_argument("--to", dest="d_to", type=str, default="~",
                    help="конец окна YYYY-MM-DD")
    args = ap.parse_args()

    rf = args.risk_free
    if rf is None:
        try:
            rf = float(mb.fetch_cbr_rate())
        except Exception:
            rf = 16.0

    # Если задан --from в прошлом — грузим достаточно лет, чтобы покрыть окно.
    years = args.years
    if args.d_from:
        from datetime import datetime as _dt
        years = max(years, (_dt.now() - _dt.strptime(args.d_from, "%Y-%m-%d")).days / 365 + 1.2)
    lo, hi = args.d_from, args.d_to

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or list(mb.TICKERS)
    print(f"Загрузка истории (~{years:.1f} лет) для {len(tickers)} тикеров…")
    hist: dict[str, list[dict]] = {}
    for tk in tickers:
        c = bt.fetch_daily_history(tk, years)
        if len(c) >= 220:
            hist[tk] = c
    print(f"Загружено: {len(hist)} тикеров с достаточной историей\n")

    win = f"{lo or 'старт'}…{hi if hi != '~' else 'сейчас'}"
    print("═" * 74)
    print(f"  СРАВНЕНИЕ СТРАТЕГИЙ  (long-only, издержки {args.commission}%/смену, окно {win})")
    print("═" * 74)
    print(f"  {'Стратегия':<10} {'CAGR':>8} {'Просадка':>9} {'Sharpe':>7}   "
          f"{'OOS п.1':>8} {'OOS п.2':>8}  вердикт")
    print("─" * 74)
    print(f"  {'risk-free':<10} {rf:>7.1f}% {0.0:>8.1f}% {'∞':>7}   "
          f"{rf:>7.1f}% {rf:>7.1f}%  ОФЗ/фонд денежного рынка (почти без риска)")

    mid = median_date(hist, lo, hi)
    bh_full = run_strategy("bh", hist, args.commission, lo, hi)
    rows = []
    for name in STRATEGIES:
        full = run_strategy(name, hist, args.commission, lo, hi)
        h1 = run_strategy(name, hist, args.commission, lo, mid)
        h2 = run_strategy(name, hist, args.commission, mid, hi)
        beats_bh = full["cagr"] > bh_full["cagr"]
        robust = h1["cagr"] > 0 and h2["cagr"] > 0
        if name == "bh":
            verdict = "бенчмарк"
        elif beats_bh and robust:
            verdict = "✅ лучше B&H и устойчиво"
        elif beats_bh:
            verdict = "⚠️ лучше B&H, но не в обоих периодах"
        else:
            verdict = "❌ не лучше B&H"
        print(f"  {name:<10} {full['cagr']*100:>7.1f}% {full['mdd']*100:>8.1f}% "
              f"{full['sharpe']:>7.2f}   {h1['cagr']*100:>7.1f}% {h2['cagr']*100:>7.1f}%  {verdict}")
        rows.append((name, full, h1, h2))

    print("═" * 74)
    print("  Профессионал считается успешным, если стабильно обгоняет buy&hold (bh)")
    print("  ПОСЛЕ издержек. Колонки OOS — две независимые половины периода:")
    print("  устойчивая стратегия должна быть в плюсе в ОБЕИХ.\n")


if __name__ == "__main__":
    main()
