#!/usr/bin/env python3
"""
brute_force.py — перебор параметров стратегий с ЗАЩИТОЙ от переобучения.

Пользователь просил «брутфорс: перебери стратегии и найди золотую».
Опасность: если перебрать сотни конфигов и взять лучший на истории — почти
гарантированно найдётся «золотой» график, который развалится на реальных
деньгах (data snooping). Чтобы этого избежать, конфиг считается кандидатом,
только если он проходит ВСЕ фильтры одновременно:

  1. положительная доходность в ОБЕИХ независимых половинах окна (out-of-sample);
  2. полная доходность ВЫШЕ безрисковой ставки (иначе проще держать ОФЗ);
  3. ранжирование по ХУДШЕЙ из половин (min OOS), а не по лучшему результату —
     награждаем устойчивость, а не подгонку.

Запуск:
    python3 brute_force.py --from 2016-01-01 --to 2021-12-31
    python3 brute_force.py --from 2023-01-01
"""
from __future__ import annotations

import argparse
import logging
from functools import partial
from itertools import product

logging.disable(logging.CRITICAL)
import strategy_lab as sl
import backtest as bt
import moex_bot as mb

# ── Сетки параметров (экономически осмысленные диапазоны) ───────────────────
GRID = {
    "tsmom":    [{"ma": m} for m in (50, 100, 150, 200)],
    "ma_cross": [{"fast": f, "slow": s} for f, s in product((10, 20, 50), (50, 100, 200)) if f < s],
    "donchian": [{"enter": e, "exit_": x} for e, x in product((10, 20, 40, 55), (5, 10, 20)) if x < e],
    "rsi2":     [{"low": lo, "ma_exit": me} for lo, me in product((5, 10, 15), (5, 10))],
    "fib":      [{"lookback": lb, "ma_trend": mt} for lb, mt in product((40, 60, 90), (100, 200))],
    "high52":   [{"prox": p, "ma": m} for p, m in product((0.02, 0.05, 0.10), (50, 100, 200))],
}
BASE = {
    "tsmom": sl.strat_tsmom, "ma_cross": sl.strat_ma_cross, "donchian": sl.strat_donchian,
    "rsi2": sl.strat_rsi2, "fib": sl.strat_fib, "high52": sl.strat_high52,
}


def main():
    ap = argparse.ArgumentParser(description="Брутфорс параметров с OOS-защитой")
    ap.add_argument("--from", dest="d_from", type=str, default="2016-01-01")
    ap.add_argument("--to", dest="d_to", type=str, default="~")
    ap.add_argument("--commission", type=float, default=0.10)
    args = ap.parse_args()

    try:
        rf = float(mb.fetch_cbr_rate())
    except Exception:
        rf = 16.0

    from datetime import datetime as _dt
    years = (_dt.now() - _dt.strptime(args.d_from, "%Y-%m-%d")).days / 365 + 1.2
    lo, hi = args.d_from, args.d_to

    print(f"Загрузка истории (~{years:.1f} лет)…")
    hist = {}
    for tk in mb.TICKERS:
        c = bt.fetch_daily_history(tk, years)
        if len(c) >= 220:
            hist[tk] = c
    mid = sl.median_date(hist, lo, hi)
    win = f"{lo}…{hi if hi != '~' else 'сейчас'}"
    print(f"Загружено {len(hist)} тикеров. Окно {win}. Безрисковая ставка {rf:.1f}%.\n")

    total = sum(len(v) for v in GRID.values())
    print(f"Перебираю {total} конфигов. Фильтр: плюс в ОБЕИХ половинах И выше {rf:.1f}%.\n")

    survivors = []
    for fam, configs in GRID.items():
        for cfg in configs:
            fn = partial(BASE[fam], **cfg)
            full = sl.run_strategy(fn, hist, args.commission, lo, hi)
            h1 = sl.run_strategy(fn, hist, args.commission, lo, mid)
            h2 = sl.run_strategy(fn, hist, args.commission, mid, hi)
            worst = min(h1["cagr"], h2["cagr"])
            beats_rf = full["cagr"] * 100 > rf
            robust = h1["cagr"] > 0 and h2["cagr"] > 0
            if robust and beats_rf:
                survivors.append((fam, cfg, full, h1, h2, worst))

    print("═" * 72)
    if not survivors:
        print("  РЕЗУЛЬТАТ БРУТФОРСА: ни один из конфигов не прошёл фильтр.")
        print(f"  Ни одна стратегия не дала плюс в обоих периодах И выше {rf:.1f}% годовых.")
        print("  Это и есть честный ответ: золотой гусыни в этих данных нет.")
    else:
        print(f"  КАНДИДАТЫ, прошедшие OOS-фильтр (отсортированы по худшей половине):")
        print(f"  {'стратегия':<22} {'CAGR':>7} {'DD':>6} {'OOS1':>7} {'OOS2':>7}")
        print("  " + "─" * 60)
        for fam, cfg, full, h1, h2, worst in sorted(survivors, key=lambda x: -x[5]):
            label = f"{fam} {cfg}"
            print(f"  {label:<22} {full['cagr']*100:>6.1f}% {full['mdd']*100:>5.1f}% "
                  f"{h1['cagr']*100:>6.1f}% {h2['cagr']*100:>6.1f}%")
        print("\n  ⚠️  Прошедший фильтр ≠ золотая гусыня. Это лишь кандидат на")
        print("      forward-тест. На новых данных может развалиться.")
    print("═" * 72)


if __name__ == "__main__":
    main()
