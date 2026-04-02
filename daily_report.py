#!/usr/bin/env python3
"""
MOEX Bot — Ежедневный отчёт (19:00 МСК, будни)
════════════════════════════════════════════════
Собирает за сегодняшний день:
  • Открытые/закрытые сделки из trade_log.json
  • Текущие позиции из signals_state.json
  • Ошибки и предупреждения из bot.log
  • Итоговую статистику: P&L, winrate

Запуск вручную:   python daily_report.py
Запуск через крон: добавь в crontab (crontab -e):
  0 16 * * 1-5 cd /path/to/trader && python daily_report.py
  (16:00 UTC = 19:00 МСК)

Переменные окружения (те же что у бота):
  TELEGRAM_TOKEN   — токен бота
  TELEGRAM_CHAT_ID — ID чата
"""

import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

import requests

# ─── Конфиг ──────────────────────────────────────────────────────────────────
_DIR = Path(__file__).parent

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TRADE_LOG_FILE   = _DIR / "trade_log.json"
STATE_FILE       = _DIR / "signals_state.json"
LOG_FILE         = _DIR / "bot.log"
LOG_DIR          = _DIR / "logs"

TODAY_STR = date.today().strftime("%Y-%m-%d")
TODAY_LABEL = date.today().strftime("%d.%m.%Y")


# ─── Telegram ─────────────────────────────────────────────────────────────────
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы — вывод в консоль:\n")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    text,
            "parse_mode": "HTML",
        }, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ─── Данные ───────────────────────────────────────────────────────────────────
def load_trade_log() -> list[dict]:
    if not TRADE_LOG_FILE.exists():
        return []
    try:
        with open(TRADE_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_today_log_lines() -> list[str]:
    """Читаем сегодняшние строки из bot.log (или logs/bot_YYYY-MM-DD.log)."""
    lines = []

    # Сначала пробуем именованный лог в logs/
    named = LOG_DIR / f"bot_{TODAY_STR}.log"
    if named.exists():
        try:
            lines = named.read_text(errors="replace").splitlines()
        except Exception:
            pass

    # Если нет — читаем основной bot.log и фильтруем по дате
    if not lines and LOG_FILE.exists():
        try:
            all_lines = LOG_FILE.read_text(errors="replace").splitlines()
            prefix = TODAY_STR  # логи обычно начинаются с "2026-04-01 ..."
            lines = [l for l in all_lines if l.startswith(prefix)]
        except Exception:
            pass

    return lines


# ─── Анализ ───────────────────────────────────────────────────────────────────
def trades_today(all_trades: list[dict]) -> tuple[list, list, list]:
    """Возвращает (открытые_сегодня, закрытые_сегодня, всё_ещё_открытые)."""
    opened_today  = [t for t in all_trades if t.get("date") == TODAY_STR]
    closed_today  = [t for t in all_trades if (t.get("exit_time") or "").startswith(TODAY_STR)]
    still_open    = [t for t in all_trades if t.get("result") is None and t.get("exit_price") is None]
    return opened_today, closed_today, still_open


def parse_errors_warnings(lines: list[str]) -> tuple[list[str], list[str]]:
    errors   = [l for l in lines if " ERROR " in l or " CRITICAL " in l]
    warnings = [l for l in lines if " WARNING " in l]
    return errors[-10:], warnings[-10:]   # последние 10


def day_pnl_stats(closed: list[dict]) -> dict:
    pnls = [t["pnl_pct"] for t in closed if t.get("pnl_pct") is not None]
    if not pnls:
        return {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "winrate": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "count":     len(pnls),
        "wins":      len(wins),
        "losses":    len(losses),
        "total_pnl": round(sum(pnls), 2),
        "winrate":   round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "avg_win":   round(sum(wins)   / len(wins),   2) if wins   else 0.0,
        "avg_loss":  round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


def open_positions_summary(state: dict) -> tuple[str, int]:
    """
    Читаем signals_state.json — только активные сандбокс-позиции (ключи sb_* с order_id).
    Возвращает (строка-сводка, кол-во позиций).
    """
    positions = []
    for key, val in state.items():
        # Только сандбокс-записи с реальным order_id
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        if not val.get("order_id"):
            continue
        ticker    = val.get("ticker", key.replace("sb_", "").split("_")[0])
        direction = val.get("direction", "?")
        price     = val.get("price") or val.get("entry", 0)
        lots      = val.get("lots", 1)
        stop      = val.get("stop", 0)
        take2     = val.get("take2", 0)
        arrow     = "🟢" if direction == "LONG" else "🔴"
        stop_str  = f"  стоп {stop:.1f}" if stop else ""
        take_str  = f"  цель {take2:.1f}" if take2 else ""
        # Дата открытия из ключа: sb_PHOR_LONG_2026-03-12 → 2026-03-12
        parts = key.split("_")
        open_date = parts[-1] if len(parts) >= 4 and "-" in parts[-1] else ""
        date_str  = f"  [{open_date}]" if open_date else ""
        positions.append(f"  {arrow}{ticker} {direction} × {lots} лот @ {price:.1f}{stop_str}{take_str}{date_str}")

    text = "\n".join(positions) if positions else "  нет открытых позиций"
    return text, len(positions)


# ─── Форматирование ───────────────────────────────────────────────────────────
def format_trade(t: dict, prefix: str = "") -> str:
    ticker = t.get("ticker", "?")
    dirn   = t.get("direction", "?")
    entry  = t.get("entry", 0)
    result = t.get("result", "")
    pnl    = t.get("pnl_pct")
    arrow  = "🟢" if dirn == "LONG" else "🔴"
    pnl_str = f"  {'+' if pnl and pnl>0 else ''}{pnl:.2f}%" if pnl is not None else ""
    result_icon = {"win": "✅", "loss": "❌", "breakeven": "➖"}.get(str(result).lower(), "⏳")
    return f"{prefix}{result_icon}{arrow}{ticker} {dirn} @ {entry:.1f}{pnl_str}"


def build_report(
    opened:     list[dict],
    closed:     list[dict],
    still_open: list[dict],
    stats:      dict,
    state:      dict,
    errors:     list[str],
    warnings:   list[str],
    log_lines:  int,
) -> str:
    lines = [f"📊 <b>Отчёт бота за {TODAY_LABEL}</b>"]

    # ── Статистика дня ────────────────────────────────────────────────────────
    total_pnl = stats["total_pnl"]
    pnl_emoji = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "⚪")
    lines.append(
        f"\n{pnl_emoji} <b>P&amp;L дня: {'+' if total_pnl > 0 else ''}{total_pnl:.2f}%</b>"
        f"  |  Закрыто: {stats['count']} сд."
        + (f"  |  WR: {stats['winrate']:.0f}%" if stats["count"] else "")
    )
    if stats["count"]:
        lines.append(
            f"   ✅ побед {stats['wins']} (ср. +{stats['avg_win']:.2f}%)  "
            f"❌ убытков {stats['losses']} (ср. {stats['avg_loss']:.2f}%)"
        )

    # ── Открытые сегодня ─────────────────────────────────────────────────────
    if opened:
        lines.append(f"\n📥 <b>Открыто сегодня ({len(opened)}):</b>")
        for t in opened:
            lines.append(format_trade(t, prefix="  "))

    # ── Закрытые сегодня ─────────────────────────────────────────────────────
    if closed:
        lines.append(f"\n📤 <b>Закрыто сегодня ({len(closed)}):</b>")
        for t in closed:
            lines.append(format_trade(t, prefix="  "))

    # ── Текущие позиции ───────────────────────────────────────────────────────
    pos_text, pos_count = open_positions_summary(state)
    lines.append(f"\n💼 <b>Текущий портфель ({pos_count} поз.):</b>")
    lines.append(pos_text)

    # ── Активность за день ────────────────────────────────────────────────────
    lines.append(f"\n🔄 <b>Активность:</b> {log_lines} строк в логе за сегодня")

    # ── Ошибки ────────────────────────────────────────────────────────────────
    if errors:
        lines.append(f"\n🚨 <b>Ошибки ({len(errors)}):</b>")
        for e in errors[:5]:
            # Обрезаем длинные строки
            short = e[20:100] if len(e) > 20 else e
            lines.append(f"  <code>{short}</code>")

    if warnings:
        lines.append(f"\n⚠️ <b>Предупреждения: {len(warnings)}</b> (последнее: {warnings[-1][20:80] if warnings else ''})")

    lines.append(f"\n⏰ Сгенерирован в {datetime.now().strftime('%H:%M:%S')} МСК")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"📋 Формирую отчёт за {TODAY_LABEL}...")

    all_trades           = load_trade_log()
    state                = load_state()
    log_lines_raw        = load_today_log_lines()
    errors, warnings     = parse_errors_warnings(log_lines_raw)
    opened, closed, still_open = trades_today(all_trades)
    stats                = day_pnl_stats(closed)

    report = build_report(
        opened     = opened,
        closed     = closed,
        still_open = still_open,
        stats      = stats,
        state      = state,
        errors     = errors,
        warnings   = warnings,
        log_lines  = len(log_lines_raw),
    )

    ok = tg_send(report)
    print("✅ Отчёт отправлен в Telegram" if ok else "📄 Отчёт выведен в консоль")
    return 0


if __name__ == "__main__":
    sys.exit(main())
