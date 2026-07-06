#!/usr/bin/env python3
"""
pulse_tracker.py — объективный трекер публичных прогнозов трейдеров из Пульса
(или любого тг-канала). Превращает «он крутой, у него +132%» в проверяемую
статистику: сколько прогнозов реально сбылось и каким был бы P&L после издержек.

Зачем: витринные проценты в Пульсе не проверяемы (искажаются вводами/выводами),
а отдельные сделки публично закрыты. НО публичные ПРОГНОЗЫ (посты вида
«минимум будет на 2480», «беру SBER в лонг») — falsifiable. Этот модуль их
фиксирует и беспристрастно оценивает по реальным котировкам MOEX.

Доступ к банковской сессии НЕ требуется — только публичные данные MOEX ISS.

Рабочий цикл:
  1. Увидел его прогноз → залогировал (бот зафиксирует цену на этот момент):
       python3 pulse_tracker.py add --ticker IMOEX --target 2480 \
           --note "минимум будет на 2480" --source <url>
       python3 pulse_tracker.py add --ticker SBER --dir long --horizon 5 \
           --note "беру сбер"
  2. Периодически считаешь итоги (бот тянет котировки и закрывает прогнозы):
       python3 pulse_tracker.py score
  3. Смотришь беспристрастный счёт:
       python3 pulse_tracker.py report

Типы прогнозов:
  --target X   : предсказан уровень X (поддержка/цель). Успех = цена дошла до X
                 в течение горизонта. Оценивается близость и удержание уровня.
  --dir long/short : направленный прогноз. Считается P&L входа по цене на момент
                 прогноза, удержание `horizon` дней, выход по close, минус издержки,
                 в сравнении с «купи-держи» того же тикера за тот же срок.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

MSK = timezone(timedelta(hours=3))
ISS = "https://iss.moex.com/iss"
CALLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pulse_calls.json")
ROUNDTRIP_COST_PCT = float(os.environ.get("ROUNDTRIP_COST_PCT", "0.10"))

# Индексы тянутся из другого движка ISS, чем акции.
INDEX_TICKERS = {"IMOEX", "RTSI", "MOEXBC", "MCFTR", "IMOEX2", "RTS"}


def now_msk_iso() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M")


# ── Котировки MOEX (акции TQBR и индексы SNDX) ──────────────────────────────
def fetch_daily(ticker: str, days: int = 60) -> list[dict]:
    t = ticker.upper()
    if t in INDEX_TICKERS:
        path = f"engines/stock/markets/index/boards/SNDX/securities/{t}"
    else:
        path = f"engines/stock/markets/shares/boards/TQBR/securities/{t}"
    date_from = (datetime.now(MSK) - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    url = f"{ISS}/{path}/candles.json?from={date_from}&interval=24"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
            cols = data["candles"]["columns"]
            rows = [dict(zip(cols, row)) for row in data["candles"]["data"]]
            return [
                {"date": c["begin"][:10], "open": c["open"], "close": c["close"],
                 "high": c["high"], "low": c["low"]}
                for c in rows if c.get("close")
            ]
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"  [!] не удалось получить котировки {t}: {e}")
                return []
            time.sleep(0.8)
    return []


def last_price(ticker: str) -> float | None:
    c = fetch_daily(ticker, days=10)
    return c[-1]["close"] if c else None


# ── Хранилище ───────────────────────────────────────────────────────────────
def load_calls() -> list[dict]:
    if not os.path.exists(CALLS_FILE):
        return []
    try:
        with open(CALLS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_calls(calls: list[dict]) -> None:
    with open(CALLS_FILE, "w", encoding="utf-8") as f:
        json.dump(calls, f, ensure_ascii=False, indent=2)


# ── add ─────────────────────────────────────────────────────────────────────
def cmd_add(args) -> None:
    ticker = args.ticker.upper()
    ref = last_price(ticker)
    if ref is None:
        print(f"❌ Не удалось получить текущую цену {ticker} — прогноз не записан.")
        return
    if not args.target and not args.dir:
        print("❌ Укажи либо --target X (уровень), либо --dir long/short (направление).")
        return
    call = {
        "id": uuid.uuid4().hex[:8],
        "author": args.author,
        "logged_at": now_msk_iso(),
        "ticker": ticker,
        "kind": "target" if args.target else "direction",
        "direction": args.dir,
        "target": args.target,
        "horizon_days": args.horizon,
        "ref_price": ref,
        "note": args.note,
        "source": args.source,
        "status": "open",
        "outcome": None,
    }
    calls = load_calls()
    calls.append(call)
    save_calls(calls)
    kind = f"target={args.target}" if args.target else f"dir={args.dir} {args.horizon}д"
    print(f"✅ Записан прогноз [{call['id']}] {args.author}: {ticker} {kind}")
    print(f"   Цена на момент прогноза: {ref}  ({call['logged_at']} МСК)")


# ── score ───────────────────────────────────────────────────────────────────
def _score_target(call: dict, candles: list[dict]) -> dict | None:
    """Прогноз уровня. Берём свечи С ДАТЫ прогноза. Дошла ли цена до target."""
    d0 = call["logged_at"][:10]
    fut = [c for c in candles if c["date"] >= d0]
    if len(fut) < 1:
        return None
    target = call["target"]
    ref = call["ref_price"]
    hi = max(c["high"] for c in fut)
    lo = min(c["low"] for c in fut)
    reached = lo <= target <= hi
    # ближайшее приближение к цели
    nearest = min(fut, key=lambda c: min(abs(c["high"] - target), abs(c["low"] - target)))
    nearest_gap = min(abs(nearest["high"] - target), abs(nearest["low"] - target))
    last = fut[-1]["close"]
    horizon_over = len(fut) >= call["horizon_days"]
    if not reached and not horizon_over:
        return None  # ещё рано закрывать
    return {
        "target_reached": reached,
        "nearest_gap_pct": round(nearest_gap / target * 100, 2),
        "min_since": lo, "max_since": hi, "last": last,
        "days_observed": len(fut),
        "verdict": "сбылся (цена дошла до уровня)" if reached
                   else f"не сбылся за {call['horizon_days']}д (ближе всего {round(nearest_gap/target*100,2)}%)",
    }


def _score_direction(call: dict, candles: list[dict]) -> dict | None:
    """Направленный прогноз. P&L входа по ref_price, удержание horizon дней."""
    d0 = call["logged_at"][:10]
    fut = [c for c in candles if c["date"] >= d0]
    if len(fut) < call["horizon_days"]:
        return None  # горизонт ещё не истёк
    entry = call["ref_price"]
    exit_px = fut[call["horizon_days"] - 1]["close"]
    raw = (exit_px - entry) / entry * 100
    if call["direction"] == "short":
        raw = -raw
    net = raw - ROUNDTRIP_COST_PCT
    bh = (exit_px - entry) / entry * 100  # купи-держи за тот же срок
    return {
        "entry": entry, "exit": exit_px,
        "gross_pct": round(raw, 2),
        "net_pct": round(net, 2),
        "buyhold_pct": round(bh, 2),
        "profitable": net > 0,
        "verdict": ("прибыльный" if net > 0 else "убыточный") + f" ({round(net,2)}% после издержек)",
    }


def cmd_score(args) -> None:
    calls = load_calls()
    open_calls = [c for c in calls if c["status"] == "open"]
    if not open_calls:
        print("Нет открытых прогнозов для оценки.")
        return
    by_ticker: dict[str, list[dict]] = {}
    closed = 0
    for c in open_calls:
        if c["ticker"] not in by_ticker:
            by_ticker[c["ticker"]] = fetch_daily(c["ticker"], days=90)
        candles = by_ticker[c["ticker"]]
        if not candles:
            continue
        outcome = (_score_target if c["kind"] == "target" else _score_direction)(c, candles)
        if outcome:
            c["status"] = "closed"
            c["outcome"] = outcome
            c["closed_at"] = now_msk_iso()
            closed += 1
            print(f"  [{c['id']}] {c['author']} {c['ticker']}: {outcome['verdict']}")
    save_calls(calls)
    print(f"\nЗакрыто прогнозов: {closed}. Открытых осталось: "
          f"{sum(1 for c in calls if c['status']=='open')}")


# ── report ──────────────────────────────────────────────────────────────────
def cmd_report(args) -> None:
    calls = load_calls()
    if args.author:
        calls = [c for c in calls if c["author"] == args.author]
    closed = [c for c in calls if c["status"] == "closed" and c.get("outcome")]
    sep = "═" * 60
    print(f"\n╔{sep}╗")
    title = f"СЧЁТ ПРОГНОЗОВ" + (f" — {args.author}" if args.author else "")
    print(f"  📊 {title}")
    print(f"╠{sep}╣")
    print(f"  Всего прогнозов : {len(calls)}  (закрыто: {len(closed)}, "
          f"открыто: {sum(1 for c in calls if c['status']=='open')})")
    if not closed:
        print(f"  Пока нет закрытых прогнозов. Добавляй через add, потом score.")
        print(f"╚{sep}╝\n")
        return

    targets = [c for c in closed if c["kind"] == "target"]
    dirs = [c for c in closed if c["kind"] == "direction"]

    if targets:
        hit = sum(1 for c in targets if c["outcome"]["target_reached"])
        print(f"╠{sep}╣")
        print(f"  Прогнозы УРОВНЕЙ : {len(targets)}")
        print(f"    Сбылось        : {hit}/{len(targets)}  ({100*hit/len(targets):.0f}%)")

    if dirs:
        prof = sum(1 for c in dirs if c["outcome"]["profitable"])
        net_sum = sum(c["outcome"]["net_pct"] for c in dirs)
        bh_sum = sum(c["outcome"]["buyhold_pct"] for c in dirs)
        print(f"╠{sep}╣")
        print(f"  НАПРАВЛЕННЫЕ     : {len(dirs)}")
        print(f"    Прибыльных     : {prof}/{len(dirs)}  ({100*prof/len(dirs):.0f}%)")
        print(f"    Сумма P&L (нетто, если копировать): {net_sum:+.2f}%")
        print(f"    Для сравнения, купи-держи за те же сроки: {bh_sum:+.2f}%")
        edge = net_sum - bh_sum
        print(f"    Преимущество над купи-держи: {edge:+.2f}%  "
              f"{'✅' if edge > 0 else '❌ (проще держать)'}")
    print(f"╚{sep}╝")
    print("  Беспристрастно: прогноз сбылся/не сбылся по реальным котировкам MOEX.")
    print("  Витринные % из Пульса тут ни при чём — только проверяемый результат.\n")


def cmd_list(args) -> None:
    for c in load_calls():
        o = c.get("outcome")
        tail = f" → {o['verdict']}" if o else "  (открыт)"
        kind = f"target={c['target']}" if c["kind"] == "target" else f"{c['direction']} {c['horizon_days']}д"
        print(f"  [{c['id']}] {c['logged_at']} {c['author']} {c['ticker']} {kind} "
              f"@ {c['ref_price']}{tail}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Объективный трекер прогнозов трейдеров")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="записать прогноз")
    a.add_argument("--ticker", required=True, help="тикер MOEX или индекс (IMOEX)")
    a.add_argument("--target", type=float, help="предсказанный уровень цены")
    a.add_argument("--dir", choices=["long", "short"], help="направленный прогноз")
    a.add_argument("--horizon", type=int, default=5, help="горизонт оценки, торговых дней")
    a.add_argument("--note", default="", help="текст прогноза")
    a.add_argument("--source", default="", help="ссылка на пост")
    a.add_argument("--author", default="extreme911", help="автор прогноза")
    a.set_defaults(func=cmd_add)

    s = sub.add_parser("score", help="оценить открытые прогнозы по котировкам")
    s.set_defaults(func=cmd_score)

    r = sub.add_parser("report", help="итоговый счёт")
    r.add_argument("--author", default="", help="фильтр по автору")
    r.set_defaults(func=cmd_report)

    l = sub.add_parser("list", help="список прогнозов")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
