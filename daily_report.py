#!/usr/bin/env python3
"""
MOEX Bot — Ежедневный отчёт  v0.9.38.3
══════════════════════════════════════
Что нового (v0.9.38.2):
  + Отправка архива *_auto.tar.gz в Telegram прямо из daily_report.py
    (раньше это делал отдельный скрипт на VPS, не в git — затирался git pull)
  + Cleanup старых архивов: keep=7, остальные удаляются
  + Футер подвала теперь берёт версию из moex_bot.BOT_VERSION
    (раньше был хардкод v0.9.35 → показывал неверную версию после рестартов)

Что нового (v0.9.35):
  + Δ депозита в ₽ (реальное изменение капитала, не % от входа)
  + P&L позиций переименован — без путаницы с P&L портфеля
  + Недельная статистика (скользящие 7 дн. из trade_log)
  + Открытые позиции: дней в позиции + % до стопа + оценка риска в ₽
  + Закрытые позиции: цена выхода + проскальзывание стопа (если стоп пробит)
  + Активность: ошибки / предупреждения / INFO-строк раздельно
  + Правильный путь к логу: logs/bot.log (RotatingFileHandler)
  + МСК-дата (не UTC) — работает корректно на иностранном VPS
  + load_dotenv при ручном запуске

Запуск вручную:   python daily_report.py
Cron (UTC VPS):   55 15 * * 1-5 cd /path/to/trader && python daily_report.py
                  (15:55 UTC = 18:55 МСК — за 5 мин до отправки)

Env:  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  (из .env или EnvironmentFile)
"""

import html
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import pytz
import requests

# ─── Конфиг ──────────────────────────────────────────────────────────────────
_DIR = Path(__file__).parent

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TRADE_LOG_FILE = _DIR / "trade_log.json"
STATE_FILE     = _DIR / "signals_state.json"
EOD_FILE       = _DIR / "eod_state.json"
LOG_DIR        = _DIR / "logs"
LOG_FILE       = _DIR / "bot.log"   # fallback если logs/ нет
DECISION_LOG_FILE = _DIR / "signals_decision_log.jsonl"
OPPORTUNITY_LOG_FILE = _DIR / "opportunity_log.jsonl"

# МСК — фиксируем один раз на весь запуск (без скачков при переходе суток)
_MSK_TZ     = pytz.timezone("Europe/Moscow")
_NOW_MSK    = datetime.now(_MSK_TZ)
TODAY_STR   = _NOW_MSK.strftime("%Y-%m-%d")
TODAY_LABEL = _NOW_MSK.strftime("%d.%m.%Y")
DAILY_DIAGNOSTICS_FILE = _DIR / f"daily_diagnostics_{TODAY_STR}.json"
_INSTRUMENT_ALIASES = {
    "BBG00F6NKQX3": "SMLT",
}


def _normalize_ticker(ticker: str | None) -> str:
    value = str(ticker or "")
    return _INSTRUMENT_ALIASES.get(value, value)

# Лотность — синхронизирована с moex_bot.py (нужна для расчёта ₽ P&L)
# v0.9.38 — импортируем унифицированный LOT_SIZE из tinvest_data (единственный
# источник правды, синхронизирован с MOEX ISS). До v0.9.38 этот словарь
# расходился с sandbox-sizing на 7 тикерах.
try:
    from tinvest_data import LOT_SIZE as _TD_LOT
    LOT_SIZES: dict[str, int] = dict(_TD_LOT)
    from tinvest_data import list_degraded_instruments as _list_degraded_instruments
except Exception:
    LOT_SIZES = {
        "GAZP": 10, "SBER": 1,  "LKOH": 1,  "ROSN": 1,  "NVTK": 1,
        "GMKN": 10, "YDEX": 1,  "TATN": 1,  "MGNT": 1,  "PLZL": 1,
        "SNGS": 100,"MTSS": 10, "ALRS": 10, "VTBR": 1,  "CHMF": 1,
        "T":    1,  "PHOR": 1,  "AFKS": 100,"NLMK": 10, "SIBN": 1,
        "FLOT": 10, "RUAL": 10, "OZON": 1,  "MOEX": 10, "SMLT": 1,
        "TRNFP":1,  "ENPG": 1,  "MAGN": 10, "AFLT": 10, "PIKK": 1,
        "AKRN": 1,  "IRAO": 100,
        "X5":   1,  "HEAD": 1,  "POSI": 1,  "LSRG": 1,  "CBOM": 100,
    }
    def _list_degraded_instruments() -> list[dict]:
        return []


# ─── Telegram ─────────────────────────────────────────────────────────────────
def _strip_html_tags(text: str) -> str:
    """v0.9.38.3: стриппер для HTML-fallback при 400-ошибке parse_mode."""
    import re
    return re.sub(r"</?[a-zA-Z][^>]*>", "", text)


def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы — вывод в консоль:\n")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram HTML error: {e}")
        # v0.9.38.3: fallback — повторяем без parse_mode, чисто plain text.
        # Причина: если в логах ошибок попался `<` / `>` или битый тег, TG 400.
        # Мы всё равно хотим доставить отчёт, пусть без форматирования.
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text":    _strip_html_tags(text),
            }, timeout=15)
            r.raise_for_status()
            print("✅ Отчёт доставлен plain-text fallback (без HTML)")
            return True
        except Exception as e2:
            print(f"Telegram plain fallback также упал: {e2}")
            return False


# ─── v0.9.38.2: отправка архива логов в Telegram ─────────────────────────────
# Раньше это делал отдельный скрипт на VPS, который не был в git
# и был затёрт при деплое v0.9.38. Теперь — часть daily_report.py.
def _build_daily_archive() -> Path | None:
    """
    Запускает export_logs.sh (если есть) либо собирает архив сам.
    Возвращает путь к *_auto.tar.gz или None при ошибке.
    """
    import subprocess, shutil, tarfile

    date_str = _NOW_MSK.strftime("%Y-%m-%d")
    archive  = _DIR / f"trader_export_{date_str}_auto.tar.gz"

    # Файлы-кандидаты — копируем в архив если существуют
    candidates = [
        _DIR / "trade_log.json",
        _DIR / "signals_score_log.jsonl",
        _DIR / "signals_decision_log.jsonl",
        _DIR / "opportunity_log.jsonl",
        _DIR / f"daily_diagnostics_{date_str}.json",
        _DIR / "news_memory.json",
        _DIR / "signals_state.json",
        _DIR / "cbr_rate_cache.json",
        _DIR / "instrument_capabilities.json",
        _DIR / "instrument_uid_cache.json",
        _DIR / "sandbox_blacklist.json",
        _DIR / "event_calendar.json",
        _DIR / "bot.log",
        _DIR / "h1_watch.json",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        print("❌ _build_daily_archive: нет файлов для упаковки")
        return None

    try:
        with tarfile.open(archive, "w:gz") as tar:
            for p in existing:
                tar.add(p, arcname=p.name)
            if (_DIR / "logs").is_dir():
                tar.add(_DIR / "logs", arcname="logs")
        return archive
    except Exception as e:
        print(f"❌ _build_daily_archive: {e}")
        return None


def tg_send_document(path: Path, caption: str = "") -> bool:
    """Отправка файла в Telegram через sendDocument."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  нет TG токена — архив не отправлен")
        return False
    if not path.exists():
        print(f"❌ tg_send_document: файл не найден: {path}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption[:1000],  # TG лимит caption = 1024
                },
                files={"document": (path.name, f, "application/gzip")},
                timeout=120,
            )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram sendDocument error: {e}")
        return False


def _cleanup_old_archives(keep: int = 7) -> None:
    """Удаляет старые trader_export_*_auto.tar.gz, оставляя последние keep."""
    import glob
    files = sorted(
        glob.glob(str(_DIR / "trader_export_*_auto.tar.gz")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            os.remove(old)
        except Exception:
            pass


# ─── Загрузка данных ──────────────────────────────────────────────────────────
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


def load_eod_state() -> dict:
    """Читаем equity на начало дня из eod_state.json (пишется moex_bot.py)."""
    if not EOD_FILE.exists():
        return {}
    try:
        with open(EOD_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_today_log_lines() -> list[str]:
    """
    Сегодняшние строки из лога.
    Порядок: logs/bot_YYYY-MM-DD.log → logs/bot.log → bot.log
    Фильтрует по МСК-дате (TODAY_STR = '2026-04-14').
    """
    candidates = [
        LOG_DIR / f"bot_{TODAY_STR}.log",
        LOG_DIR / "bot.log",
        LOG_FILE,
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            all_lines = path.read_text(errors="replace").splitlines()
            matched = [l for l in all_lines if l.startswith(TODAY_STR)]
            if matched:
                return matched
        except Exception:
            pass
    return []


def load_today_decision_entries() -> list[dict]:
    if not DECISION_LOG_FILE.exists():
        return []
    entries: list[dict] = []
    try:
        with open(DECISION_LOG_FILE, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                ts = str(item.get("ts") or "")
                if ts.startswith(TODAY_STR):
                    entries.append(item)
    except Exception:
        return []
    return entries


def load_today_opportunity_entries() -> list[dict]:
    if not OPPORTUNITY_LOG_FILE.exists():
        return []
    entries: list[dict] = []
    try:
        with open(OPPORTUNITY_LOG_FILE, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                ts = str(item.get("ts") or "")
                if ts.startswith(TODAY_STR):
                    entries.append(item)
    except Exception:
        return []
    return entries


def summarize_decisions(entries: list[dict]) -> dict:
    summary = {
        "total": len(entries),
        "actions": {},
        "reasons": {},
    }
    for item in entries:
        action = str(item.get("action") or "unknown")
        reason = str(item.get("reason") or "unknown")
        summary["actions"][action] = summary["actions"].get(action, 0) + 1
        summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
    return summary


def summarize_opportunities(entries: list[dict], decision_entries: list[dict] | None = None) -> dict:
    actions: dict[str, int] = {}
    reasons: dict[str, int] = {}
    tiers: dict[str, int] = {}
    for item in entries:
        action = str(item.get("action") or "unknown")
        reason = str(item.get("reason") or "unknown")
        tier = str(item.get("ticker_tier") or "unknown")
        actions[action] = actions.get(action, 0) + 1
        reasons[reason] = reasons.get(reason, 0) + 1
        tiers[tier] = tiers.get(tier, 0) + 1
    legacy_cleaned = 0
    for item in decision_entries or []:
        if str(item.get("reason")) == "legacy_state_cleanup":
            legacy_cleaned += 1
    return {
        "opportunity": len(entries),
        "executed": actions.get("executed", 0),
        "rejected": actions.get("rejected", 0),
        "watch_only": actions.get("watch_only", 0),
        "legacy_cleaned": legacy_cleaned,
        "actions": actions,
        "reasons": reasons,
        "tiers": tiers,
    }


def summarize_capabilities() -> dict:
    degraded = _list_degraded_instruments()
    capability_counts: dict[str, int] = {}
    grouped: dict[str, set[str]] = {}
    rows: list[str] = []
    for item in degraded:
        ticker = _normalize_ticker(item.get("ticker", "?"))
        caps = item.get("capabilities", {}) or {}
        for capability, meta in caps.items():
            capability_counts[capability] = capability_counts.get(capability, 0) + 1
            reason = str((meta or {}).get("reason") or "")
            label = f"{capability}:{reason}" if reason else capability
            grouped.setdefault(ticker, set()).add(label)
    for ticker, labels in grouped.items():
        if labels:
            rows.append(f"{ticker} ({', '.join(sorted(labels)[:3])})")
    return {
        "count": len(grouped),
        "rows": rows[:6],
        "capability_counts": capability_counts,
    }


def summarize_strategy_results(all_trades: list[dict]) -> dict:
    strategies: dict[str, dict[str, float | int]] = {}
    for t in all_trades:
        pattern = str(t.get("pattern") or "UNSPECIFIED")
        rec = strategies.setdefault(pattern, {
            "signals": 0,
            "executed": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "winrate": 0.0,
        })
        rec["signals"] += 1
        executed = _is_executed(t)
        if executed:
            rec["executed"] += 1
        else:
            rec["virtual"] = int(rec.get("virtual", 0)) + 1
        pnl = t.get("pnl_pct")
        if executed and pnl is not None:
            rec["closed"] += 1
            rec["total_pnl"] += float(pnl)
            if float(pnl) > 0:
                rec["wins"] += 1
            elif float(pnl) < 0:
                rec["losses"] += 1

    for rec in strategies.values():
        closed = int(rec["closed"])
        wins = int(rec["wins"])
        total_pnl = float(rec["total_pnl"])
        rec["avg_pnl"] = round(total_pnl / closed, 2) if closed else 0.0
        rec["total_pnl"] = round(total_pnl, 2)
        rec["winrate"] = round(wins / closed * 100, 1) if closed else 0.0

    top = sorted(
        (
            {"pattern": pattern, **stats}
            for pattern, stats in strategies.items()
        ),
        key=lambda item: (-int(item["signals"]), item["pattern"]),
    )
    return {"patterns": top}


def summarize_uid_fallback(lines: list[str]) -> dict:
    stats = {
        "uid_cache_success": 0,
        "uid_find_failed": 0,
        "uid_retry_success": 0,
        "uid_retry_failed": 0,
        "tickers": {},
    }
    for line in lines:
        ticker = None
        if "[UID]" in line and "→ uid=" in line:
            stats["uid_cache_success"] += 1
        if "[UID]" in line and "инструмент не найден через FindInstrument" in line:
            stats["uid_find_failed"] += 1
        if "[via UID retry" in line:
            stats["uid_retry_success"] += 1
        if "retry с UID тоже не прошёл" in line:
            stats["uid_retry_failed"] += 1
        for known in ("SBER", "GAZP", "OZON", "CBOM", "SMLT", "VTBR", "CHMF", "AFLT", "AFKS", "MOEX"):
            if known in line:
                ticker = known
                break
        if ticker and ("UID" in line or "via UID" in line):
            stats["tickers"][ticker] = stats["tickers"].get(ticker, 0) + 1
    return stats


def summarize_network_resilience(lines: list[str]) -> dict:
    stats = {
        "moex_request_retry": 0,
        "moex_request_failed": 0,
        "endpoints": {},
    }
    for line in lines:
        if "transient network error, retry" in line:
            stats["moex_request_retry"] += 1
            endpoint = "get_candles"
            stats["endpoints"][endpoint] = stats["endpoints"].get(endpoint, 0) + 1
        if "Max retries exceeded" in line or "Read timed out" in line or "ConnectTimeoutError" in line:
            stats["moex_request_failed"] += 1
            endpoint = "get_candles" if "get_candles(" in line else "unknown"
            stats["endpoints"][endpoint] = stats["endpoints"].get(endpoint, 0) + 1
    return stats


def summarize_reconcile_health(state: dict, decision_entries: list[dict]) -> dict:
    stats = {
        "ghost_closed": 0,
        "linked_orphan": 0,
        "orphan": 0,
        "signal_without_sb": 0,
    }
    mismatch_lines, mismatch_count = portfolio_mismatch_summary(state)
    gap_lines, gap_count = signal_state_gap_summary(state)
    stats["signal_without_sb"] = gap_count
    for key, val in state.items():
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        if val.get("close_reason") == "reconcile_ghost":
            stats["ghost_closed"] += 1
        if val.get("closed_at"):
            continue
        status = str(val.get("reconcile_status") or val.get("execution_status") or "")
        if status == "linked_orphan":
            stats["linked_orphan"] += 1
        elif status == "orphan" or ("orphan" in key.lower() and not val.get("closed_at")):
            stats["orphan"] += 1
    stats["open_mismatch_rows"] = mismatch_lines[:8]
    stats["gap_rows"] = gap_lines[:8]
    stats["reconcile_events_today"] = sum(1 for e in decision_entries if str(e.get("reason")) == "reconcile_orphan")
    return stats


def summarize_stop_execution_quality(all_trades: list[dict], *, date_str: str | None = None) -> dict:
    stats = {
        "stop_loss_trades": 0,
        "slipped_stop_exits": 0,
        "avg_slippage_pct": 0.0,
        "max_slippage_pct": 0.0,
        "rows": [],
    }
    slippage_values: list[float] = []

    for trade in all_trades:
        if date_str and not str(trade.get("exit_time") or "").startswith(date_str):
            continue
        if not _is_executed(trade):
            continue
        if str(trade.get("result") or "").lower() not in {"loss", "stop"}:
            continue

        direction = str(trade.get("direction") or "").upper()
        stop = float(trade.get("stop") or 0)
        exit_price = float(trade.get("exit_price") or 0)
        if direction not in {"LONG", "SHORT"} or stop <= 0 or exit_price <= 0:
            continue

        adverse_slip = (exit_price - stop) if direction == "SHORT" else (stop - exit_price)
        if adverse_slip <= 0:
            continue

        slip_pct = adverse_slip / stop * 100
        stats["stop_loss_trades"] += 1
        stats["slipped_stop_exits"] += 1
        slippage_values.append(slip_pct)
        stats["rows"].append({
            "signal_id": trade.get("signal_id"),
            "ticker": trade.get("ticker"),
            "direction": direction,
            "planned_stop": round(stop, 4),
            "actual_exit": round(exit_price, 4),
            "slippage_abs": round(adverse_slip, 4),
            "slippage_pct": round(slip_pct, 3),
            "exit_time": trade.get("exit_time"),
        })

    if slippage_values:
        stats["avg_slippage_pct"] = round(sum(slippage_values) / len(slippage_values), 3)
        stats["max_slippage_pct"] = round(max(slippage_values), 3)
        stats["rows"] = sorted(
            stats["rows"],
            key=lambda row: (-float(row["slippage_pct"]), str(row.get("signal_id") or "")),
        )[:8]

    return stats


def build_daily_diagnostics(
    all_trades: list[dict],
    state: dict,
    log_lines: list[str],
    decision_entries: list[dict],
    opportunity_entries: list[dict] | None = None,
) -> dict:
    return {
        "date": TODAY_STR,
        "generated_at": _NOW_MSK.isoformat(),
        "strategy_results": summarize_strategy_results(all_trades),
        "reason_codes": summarize_decisions(decision_entries),
        "release_counters": summarize_opportunities(opportunity_entries or [], decision_entries),
        "uid_fallback": summarize_uid_fallback(log_lines),
        "network_resilience": summarize_network_resilience(log_lines),
        "reconcile_health": summarize_reconcile_health(state, decision_entries),
        "stop_execution_quality": summarize_stop_execution_quality(all_trades, date_str=TODAY_STR),
    }


def save_daily_diagnostics(payload: dict) -> None:
    try:
        DAILY_DIAGNOSTICS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  save_daily_diagnostics: {e}")


# ─── Анализ сделок ────────────────────────────────────────────────────────────
def _is_executed(t: dict) -> bool:
    """v0.9.37: real sandbox order placed?
    Legacy trades без поля executed считаются реальными (True).
    """
    status = str(t.get("execution_status") or "")
    if status in {"virtual", "ghost_closed", "rejected"}:
        return False
    executed = t.get("executed", True)
    if executed is False:
        return False
    return True


def trades_today(all_trades: list[dict]) -> tuple[list, list]:
    """Возвращает (открытые_сегодня, закрытые_сегодня).

    v0.9.37: Обе выборки включают и executed, и phantom — фильтрация по
    `_is_executed` выполняется выше (в day_pnl_stats / estimate_deposit_delta /
    build_report), чтобы мы одновременно видели реальные P&L и отдельно
    считали количество phantom-сигналов.
    """
    opened = [t for t in all_trades if t.get("date") == TODAY_STR]
    closed = [t for t in all_trades if (t.get("exit_time") or "").startswith(TODAY_STR)]
    return opened, closed


def day_pnl_stats(closed: list[dict]) -> dict:
    """v0.9.37: считает P&L только по реально размещённым сделкам (executed=True).
    phantom-сигналы (FIGI missing / 50002 / risk-block) возвращаются отдельно
    в поле `phantom` — это предотвращает фиктивные wins в отчёте (кейс 17.04.2026).
    """
    real    = [t for t in closed if _is_executed(t)]
    phantom = [t for t in closed if not _is_executed(t)]

    pnls = [t["pnl_pct"] for t in real if t.get("pnl_pct") is not None]
    if not pnls:
        base = {
            "count": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "total_pnl": 0.0, "winrate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        }
    else:
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        bes    = [p for p in pnls if p == 0]
        base = {
            "count":      len(pnls),
            "wins":       len(wins),
            "losses":     len(losses),
            "breakevens": len(bes),
            "total_pnl":  round(sum(pnls), 2),
            "winrate":    round(len(wins) / len(pnls) * 100, 1),
            "avg_win":    round(sum(wins)   / len(wins),   2) if wins   else 0.0,
            "avg_loss":   round(sum(losses) / len(losses), 2) if losses else 0.0,
        }
    base["phantom_count"] = len(phantom)
    base["phantom_tickers"] = sorted({t.get("ticker", "?") for t in phantom})
    return base


def weekly_stats(all_trades: list[dict]) -> dict:
    """Скользящие 7 дней: все закрытые сделки за последние 7 календарных дней."""
    cutoff = (datetime.now(_MSK_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = [
        t for t in all_trades
        if t.get("exit_time") and t["exit_time"] >= cutoff
        and t.get("pnl_pct") is not None
        and _is_executed(t)
    ]
    if not recent:
        return {}
    pnls   = [t["pnl_pct"] for t in recent]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "count":   len(pnls),
        "wins":    len(wins),
        "losses":  len(losses),
        "total":   round(sum(pnls), 2),
        "winrate": round(len(wins) / len(pnls) * 100, 1),
        "from":    cutoff,
    }


def parse_log_stats(lines: list[str]) -> dict:
    """Разбиваем лог по уровням: ERROR/CRITICAL, WARNING, INFO/DEBUG.
    Формат строки: '2026-04-14 09:18:37 [INFO] module: message'
    """
    errors   = [l for l in lines if "[ERROR]"    in l or "[CRITICAL]" in l]
    warnings = [l for l in lines if "[WARNING]"  in l]
    infos    = [l for l in lines if "[INFO]"      in l or "[DEBUG]"    in l]
    return {
        "total":    len(lines),
        "errors":   errors[-5:],    # последние 5 для вывода
        "n_errors": len(errors),
        "n_warn":   len(warnings),
        "n_info":   len(infos),
    }


# ─── Открытые позиции ─────────────────────────────────────────────────────────
def open_positions_summary(state: dict) -> tuple[list[str], int, float]:
    """
    Активные sandbox-позиции (sb_* с order_id и без closed_at).
    Возвращает (список строк, кол-во, суммарный deployed_rub).
    """
    rows: list[tuple[str, dict]] = []   # (key, val)
    for key, val in state.items():
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        if not val.get("order_id"):
            continue
        if val.get("closed_at"):          # v0.9.35: фикс — пропускаем закрытые
            continue
        rows.append((key, val))

    # Сортировка: сначала свежие (по opened_at)
    rows.sort(key=lambda kv: kv[1].get("opened_at", ""), reverse=True)

    lines: list[str] = []
    total_deployed: float = 0.0

    for key, val in rows:
        ticker    = _normalize_ticker(val.get("ticker", key.replace("sb_", "").split("_")[0]))
        direction = val.get("direction", "?")
        price     = float(val.get("price") or val.get("entry") or 0)
        lots      = int(val.get("lots", 1))
        stop      = float(val.get("stop", 0))
        take2     = float(val.get("take2", 0))
        opened_at = val.get("opened_at", "")

        arrow = "🟢" if direction == "LONG" else "🔴"

        # Deployed ₽
        lot_size    = LOT_SIZES.get(ticker, 1)
        deployed    = price * lots * lot_size
        total_deployed += deployed

        # Дней в позиции
        days_open = ""
        if opened_at:
            try:
                import pytz as _ptz
                from datetime import timezone as _tz
                dt_open = datetime.fromisoformat(opened_at)
                if dt_open.tzinfo is None:
                    dt_open = dt_open.replace(tzinfo=_tz.utc)
                delta_d = (_NOW_MSK - dt_open.astimezone(_MSK_TZ)).days
                days_open = f" {delta_d}д" if delta_d > 0 else " <1д"
            except Exception:
                pass

        # % до стопа
        stop_dist = ""
        if stop and price:
            dist_pct = abs(stop - price) / price * 100
            stop_dist = f"  стоп {stop:.1f} ({dist_pct:.1f}%)"

        take_str = f"  цель {take2:.1f}" if take2 else ""

        # Дата из ключа: sb_TICKER_DIR_2026-04-14
        parts = key.split("_")
        open_date = parts[-1] if len(parts) >= 4 and "-" in parts[-1] else ""
        date_str  = f"  [{open_date}]" if open_date else ""

        deployed_str = f"  (~{deployed/1000:.0f}k₽)" if deployed >= 1000 else ""

        lines.append(
            f"  {arrow}{ticker} {direction} × {lots}л @ {price:.1f}"
            f"{stop_dist}{take_str}{deployed_str}{days_open}{date_str}"
        )

    if not lines:
        lines = ["  нет открытых позиций"]

    return lines, len(rows), total_deployed


def portfolio_mismatch_summary(state: dict) -> tuple[list[str], int]:
    """
    Показывает открытые sb_-записи, которые не выглядят как нормальные исполняемые позиции.
    Сейчас это в первую очередь orphan-записи reconcile, из-за которых внешняя сводка
    и локальный отчёт могут показывать разное число открытых позиций.
    """
    rows: list[str] = []
    for key, val in state.items():
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        if val.get("closed_at"):
            continue
        if val.get("order_id"):
            continue
        ticker = _normalize_ticker(val.get("ticker", key.replace("sb_", "").split("_")[0]))
        direction = val.get("direction", "?")
        lots = int(abs(val.get("lots", 0) or 0))
        note = str(val.get("note") or "")
        reason = "orphan" if "orphan" in key.lower() or "orphan" in note.lower() else "untracked"
        price = float(val.get("price") or val.get("entry") or 0)
        qty_str = f" × {lots}л" if lots else ""
        px_str = f" @ {price:.1f}" if price else ""
        rows.append(f"  ⚠️{ticker} {direction}{qty_str}{px_str}  [{reason}]")

    if not rows:
        return [], 0
    rows.sort()
    return rows, len(rows)


def signal_state_gap_summary(state: dict) -> tuple[list[str], int]:
    """
    Базовые сигналы, которые выглядят открытыми, но не имеют живой sb_-позиции.
    Это отдельный класс проблемы от orphan: идея сигнала в state есть, а подтверждения
    реального исполнения нет.
    """
    open_sb_keys: set[str] = set()
    open_sb_base_keys: set[str] = set()
    open_sb_ticker_dirs: set[tuple[str, str]] = set()
    for key, val in state.items():
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        if val.get("closed_at"):
            continue
        open_sb_keys.add(key[3:])
        open_sb_ticker_dirs.add((
            _normalize_ticker(val.get("ticker", key.replace("sb_", "").split("_")[0])),
            str(val.get("direction", "")),
        ))
        base_signal_key = val.get("base_signal_key")
        if base_signal_key:
            open_sb_base_keys.add(str(base_signal_key))

    rows: list[str] = []
    for key, val in state.items():
        if not isinstance(val, dict):
            continue
        if key.startswith(("sb_", "news_", "ntg_")):
            continue
        if val.get("execution_status") in {"virtual", "ghost_closed", "rejected"}:
            continue
        if val.get("hit") in ("target2", "stop_hit"):
            continue
        if key in open_sb_keys or key in open_sb_base_keys:
            continue
        ticker = _normalize_ticker(val.get("ticker", key.split("_")[0]))
        direction = str(val.get("direction", "?"))
        if (ticker, direction) in open_sb_ticker_dirs:
            continue
        entry = float(val.get("entry") or 0)
        entry_str = f" @ {entry:.1f}" if entry else ""
        rows.append(f"  ⚠️{ticker} {direction}{entry_str}  [signal_without_sb]")

    if not rows:
        return [], 0
    rows.sort()
    return rows, len(rows)


def execution_truth_summary(state: dict, trades: list[dict]) -> dict:
    sb_open = 0
    sb_orphan = 0
    base_open = 0
    base_with_sb = 0
    trade_status: dict[str, int] = {}

    open_sb_base_keys: set[str] = set()
    open_sb_ticker_dirs: set[tuple[str, str]] = set()
    for key, val in state.items():
        if not isinstance(val, dict):
            continue
        if key.startswith("sb_") and not val.get("closed_at"):
            sb_open += 1
            if not val.get("order_id"):
                sb_orphan += 1
            open_sb_ticker_dirs.add((
                _normalize_ticker(val.get("ticker", key.replace("sb_", "").split("_")[0])),
                str(val.get("direction", "")),
            ))
            base_signal_key = val.get("base_signal_key")
            if base_signal_key:
                open_sb_base_keys.add(str(base_signal_key))
            else:
                open_sb_base_keys.add(key[3:])

    for key, val in state.items():
        if not isinstance(val, dict):
            continue
        if key.startswith(("sb_", "news_", "ntg_")):
            continue
        if val.get("hit") in ("target2", "stop_hit"):
            continue
        base_open += 1
        ticker_dir = (
            _normalize_ticker(val.get("ticker", key.split("_")[0])),
            str(val.get("direction", "")),
        )
        if key in open_sb_base_keys or ticker_dir in open_sb_ticker_dirs:
            base_with_sb += 1

    for t in trades:
        status = str(t.get("execution_status") or ("filled" if t.get("executed") else "signaled"))
        trade_status[status] = trade_status.get(status, 0) + 1

    return {
        "sb_open": sb_open,
        "sb_orphan": sb_orphan,
        "base_open": base_open,
        "base_with_sb": base_with_sb,
        "base_without_sb": max(0, base_open - base_with_sb),
        "trade_status": trade_status,
    }


# ─── Форматирование сделок ────────────────────────────────────────────────────
def format_trade_closed(t: dict) -> str:
    """Закрытая сделка: вход → выход, P&L, проскальзывание стопа если было."""
    ticker    = _normalize_ticker(t.get("ticker", "?"))
    dirn      = t.get("direction", "?")
    entry     = float(t.get("entry", 0))
    stop      = float(t.get("stop", 0))
    exit_p    = t.get("exit_price")
    pnl       = t.get("pnl_pct")
    conf      = t.get("confidence", "")
    result    = str(t.get("result", "")).lower()

    arrow     = "🟢" if dirn == "LONG" else "🔴"
    res_icon  = {"win": "✅", "win_t1": "✅", "win_t2": "✅",
                 "loss": "❌", "stop": "❌",
                 "breakeven": "➖"}.get(result, "❌" if (pnl or 0) < 0 else "✅")

    pnl_str = f"  {'+' if (pnl or 0) > 0 else ''}{pnl:.2f}%" if pnl is not None else ""

    # Проскальзывание стопа: выход хуже стопа на ≥ 0.1%
    slip_str = ""
    if stop and exit_p and pnl is not None and pnl < 0:
        if dirn == "SHORT":
            slip = exit_p - stop   # SHORT: выход выше стопа = плохо
        else:
            slip = stop - exit_p   # LONG:  выход ниже стопа = плохо
        if slip > 0.05 * stop / 100 * 100:  # > 0.05₽ проскальзывание
            slip_str = f"  ⚠️slip+{slip:.1f}₽"

    # Краткий тег уверенности
    conf_tag = ""
    if "ОТЛИЧНАЯ" in conf:
        conf_tag = " [🔥🔥]"
    elif "ВЫСОКАЯ" in conf:
        conf_tag = " [🔥]"
    elif "СРЕДНЯЯ" in conf:
        conf_tag = " [🟡]"

    exit_str = f" → {exit_p:.2f}" if exit_p else ""
    exec_tag = "" if _is_executed(t) else " [phantom]"
    return f"  {res_icon}{arrow} {ticker} {dirn}  @ {entry:.1f}{exit_str}{pnl_str}{slip_str}{conf_tag}{exec_tag}"


def format_trade_opened(t: dict) -> str:
    """Открытая сегодня сделка (может быть ещё открыта или уже закрыта)."""
    ticker      = _normalize_ticker(t.get("ticker", "?"))
    dirn        = t.get("direction", "?")
    entry       = float(t.get("entry", 0))
    pnl         = t.get("pnl_pct")
    raw_result  = t.get("result")
    result      = str(raw_result).lower() if raw_result is not None else ""
    conf        = t.get("confidence", "")
    rsi         = t.get("rsi")
    vwap        = t.get("vwap_confirm")

    arrow    = "🟢" if dirn == "LONG" else "🔴"
    if result in ("win", "win_t1", "win_t2"):
        res_icon = "✅"
    elif result in ("loss", "stop") or (pnl is not None and pnl < 0):
        res_icon = "❌"
    elif raw_result is None and pnl is None:   # ещё открыта
        res_icon = "⏳"
    else:
        res_icon = "➖"

    pnl_str = f"  {'+' if (pnl or 0) > 0 else ''}{pnl:.2f}%" if pnl is not None else "  открыта"
    exec_tag = "" if _is_executed(t) else "  [phantom]"

    # Предупреждения
    flags = []
    if rsi and rsi > 73 and dirn == "LONG":
        flags.append(f"RSI⚠️{rsi:.0f}")
    if vwap is False:
        flags.append("VWAP✗")

    flag_str = "  [" + " | ".join(flags) + "]" if flags else ""
    return f"  {res_icon}{arrow} {ticker} {dirn}  @ {entry:.1f}{pnl_str}{flag_str}{exec_tag}"


# ─── Δ депозита ───────────────────────────────────────────────────────────────
def estimate_deposit_delta(state: dict, closed_today: list[dict], equity_sod: float) -> str:
    """
    Оценка реального изменения депозита в ₽ за день.
    Два источника (комбинируем оба):
      1. trade_log closed_today → pnl_pct × примерный размер позиции (5% equity)
      2. sb_*-записи из state, закрытые сегодня и НЕ входящие в trade_log
         (сюда попадают orphan-сделки вроде TRNFP — с реальными lots/close_price)
    Пропускаем reconcile_ghost (без реальной сделки).
    """
    if equity_sod <= 0:
        return ""

    total_rub: float = 0.0
    has_data  = False

    # ── Источник 1: trade_log ─────────────────────────────────────────────────
    # v0.9.37: phantom-сделки (executed=False) игнорируем в Δ депозита — они
    # не размещались в sandbox. Без этого фильтра кейс 17.04 дал +3566 ₽
    # против реальных +58 ₽.
    tickers_in_tradelog: set[str] = set()
    for t in closed_today:
        if not _is_executed(t):
            continue
        p      = t.get("pnl_pct")
        ticker = t.get("ticker", "")
        if p is not None:
            # Позиция ≈ 5% equity (SANDBOX_MAX_POS_PCT по умолчанию)
            approx_pos = equity_sod * 0.05
            total_rub += approx_pos * p / 100
            has_data = True
        if ticker:
            tickers_in_tradelog.add(ticker)

    # ── Источник 2: sb_* из state, закрытые сегодня (orphan/manual) ──────────
    for key, val in state.items():
        if not key.startswith("sb_") or not isinstance(val, dict):
            continue
        closed_at    = val.get("closed_at", "")
        close_reason = val.get("close_reason", "")
        # Только закрытые сегодня
        if not (closed_at and closed_at[:10] == TODAY_STR):
            continue
        # Пропускаем ghost — реальной рыночной сделки не было
        if close_reason == "reconcile_ghost":
            continue
        ticker    = val.get("ticker", "")
        # Пропускаем, если тикер уже посчитан через trade_log
        if ticker in tickers_in_tradelog:
            continue
        entry_p  = float(val.get("price") or val.get("entry") or 0)
        close_p  = float(val.get("close_price") or 0)
        lots     = int(val.get("lots", 0))
        if not (entry_p and close_p and lots):
            continue
        direction = val.get("direction", "LONG")
        lot_size  = LOT_SIZES.get(ticker, 1)
        mult      = -1 if direction == "SHORT" else 1
        total_rub += mult * (close_p - entry_p) * lots * lot_size
        has_data   = True

    if not has_data:
        return ""

    pct    = total_rub / equity_sod * 100
    sign   = "+" if total_rub >= 0 else ""
    emoji  = "🟢" if total_rub >= 0 else "🔴"
    # "≈" только если оценивали через trade_log (не точные данные sb_)
    approx = "≈ " if closed_today else ""
    return (
        f"{emoji} Δ депозита: {approx}{sign}{total_rub:,.0f} ₽  ({sign}{pct:.2f}%)\n"
        f"   SOD: {equity_sod:,.0f} ₽  →  {approx}{equity_sod + total_rub:,.0f} ₽"
    )


# ─── Сборка отчёта ────────────────────────────────────────────────────────────
def build_report(
    opened:     list[dict],
    closed:     list[dict],
    all_trades: list[dict],
    stats:      dict,
    state:      dict,
    log_stats:  dict,
    decision_stats: dict,
    capability_stats: dict,
    eod_state:  dict,
    week:       dict,
) -> str:
    parts: list[str] = []
    truth = execution_truth_summary(state, all_trades)

    # ── Заголовок ─────────────────────────────────────────────────────────────
    parts.append(f"📊 <b>Отчёт бота за {TODAY_LABEL}</b>")

    # ── P&L позиций + Δ депозита ──────────────────────────────────────────────
    # v0.9.37: в stats["count"] теперь только реально размещённые сделки
    # (executed=True). Phantom считаются отдельно в stats["phantom_count"].
    if stats["count"]:
        total_pnl = stats["total_pnl"]
        pnl_emoji = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "⚪")
        wr_str    = f"  |  WR: {stats['winrate']:.0f}%" if stats["count"] else ""

        parts.append(
            f"\n{pnl_emoji} <b>P&amp;L позиций: {'+' if total_pnl > 0 else ''}{total_pnl:.2f}%</b>"
            f"  |  Закрыто: {stats['count']} сд.{wr_str}"
        )
        detail = []
        if stats["wins"]:
            detail.append(f"✅ {stats['wins']} побед (ср. +{stats['avg_win']:.2f}%)")
        if stats["losses"]:
            detail.append(f"❌ {stats['losses']} убытков (ср. {stats['avg_loss']:.2f}%)")
        if detail:
            parts.append("   " + "   ".join(detail))

        # Реальное изменение депозита
        equity_sod = float(eod_state.get("equity_day_start", 0))
        delta_line = estimate_deposit_delta(state, closed, equity_sod)
        if delta_line:
            parts.append(delta_line)

    elif eod_state.get("equity_day_start"):
        equity_sod = float(eod_state["equity_day_start"])
        parts.append(f"\n💼 Депозит SOD: {equity_sod:,.0f} ₽  |  сделок за день: 0")

    # ── Phantom-сигналы (executed=False) ──────────────────────────────────────
    # Не размещены в sandbox: FIGI missing / 50002 / risk-block / no-order.
    # В P&L/WR не попадают — но показываем, чтобы было видно на что бот среагировал.
    phantom_count = stats.get("phantom_count", 0)
    if phantom_count:
        phantom_tickers = stats.get("phantom_tickers") or []
        tickers_str = ", ".join(phantom_tickers) if phantom_tickers else "?"
        parts.append(
            f"\n⚠️ <b>Фантомы (не размещены): {phantom_count}</b>  "
            f"[{tickers_str}]  — проверь FIGI_MAP / sandbox blacklist"
        )

    # ── Открытые сегодня ──────────────────────────────────────────────────────
    if opened:
        opened_real = [t for t in opened if _is_executed(t)]
        opened_phantom = [t for t in opened if not _is_executed(t)]
        title = f"\n📥 <b>Открыто сегодня ({len(opened)}):</b>"
        if opened_phantom:
            title += f"  real={len(opened_real)} phantom={len(opened_phantom)}"
        parts.append(title)
        for t in opened:
            parts.append(format_trade_opened(t))

    # ── Закрытые сегодня ──────────────────────────────────────────────────────
    if closed:
        parts.append(f"\n📤 <b>Закрыто сегодня ({len(closed)}):</b>")
        for t in closed:
            parts.append(format_trade_closed(t))

    # ── Текущий портфель ──────────────────────────────────────────────────────
    pos_lines, pos_count, deployed_rub = open_positions_summary(state)
    header = f"💼 <b>Текущий портфель ({pos_count} поз.)</b>"
    if deployed_rub > 0:
        header += f"  ~{deployed_rub/1000:.0f}k₽ задействовано"
    parts.append(f"\n{header}:")
    parts.extend(pos_lines)

    mismatch_lines, mismatch_count = portfolio_mismatch_summary(state)
    if mismatch_count:
        parts.append(f"\n⚠️ <b>Portfolio mismatch ({mismatch_count})</b>:")
        parts.extend(mismatch_lines)
        parts.append("  Проверь reconcile/orphan и сверку sandbox-портфеля с local state")

    signal_gap_lines, signal_gap_count = signal_state_gap_summary(state)
    if signal_gap_count:
        parts.append(f"\n🧷 <b>Signal/state gaps ({signal_gap_count})</b>:")
        parts.extend(signal_gap_lines[:8])
        parts.append("  Есть открытый сигнал в state, но нет живой sb_-позиции")

    parts.append(
        f"\n🧭 <b>Execution truth:</b> sb_open={truth['sb_open']}  "
        f"orphan={truth['sb_orphan']}  base_open={truth['base_open']}  "
        f"linked={truth['base_with_sb']}  gaps={truth['base_without_sb']}"
    )
    trade_status = truth.get("trade_status") or {}
    if trade_status:
        top_status = sorted(trade_status.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        parts.append("  " + "  ".join(f"{name}={count}" for name, count in top_status))

    # ── Неделя ────────────────────────────────────────────────────────────────
    if week and week.get("count"):
        w_emoji = "🟢" if week["total"] > 0 else "🔴"
        parts.append(
            f"\n📈 <b>Неделя (скользящие 7 дн.):</b>  {week['count']} сд.  "
            f"|  ✅{week['wins']} ❌{week['losses']}  "
            f"|  WR {week['winrate']:.0f}%  "
            f"|  {w_emoji} Σ {'+' if week['total'] > 0 else ''}{week['total']:.2f}%"
        )

    # ── Активность ────────────────────────────────────────────────────────────
    n_tot  = log_stats["total"]
    n_err  = log_stats["n_errors"]
    n_warn = log_stats["n_warn"]
    n_info = log_stats["n_info"]
    if n_tot:
        act_line = f"🔄 <b>Активность:</b> {n_tot} строк"
        if n_err:
            act_line += f"  🚨 {n_err} ошибок"
        if n_warn:
            act_line += f"  ⚠️ {n_warn} предупр."
        if n_info:
            act_line += f"  ℹ️ {n_info} info"
    else:
        act_line = "🔄 <b>Активность:</b> 0 строк в логе — бот, возможно, не запущен"
    parts.append(f"\n{act_line}")

    # ── Ошибки (последние 3) ──────────────────────────────────────────────────
    # v0.9.38.3: html.escape(short) — иначе SDK-сообщения вида
    # "T-Invest GetCandles NOT_FOUND 50002 → <ticker>" ломают HTML parse_mode
    # (это и было причиной 21.04 19:00 sendMessage 400).
    if log_stats["errors"]:
        parts.append(f"\n🚨 <b>Последние ошибки:</b>")
        for e in log_stats["errors"][-3:]:
            short = e[20:120] if len(e) > 20 else e
            parts.append(f"  <code>{html.escape(short)}</code>")

    if capability_stats.get("count"):
        rows = capability_stats.get("rows") or []
        parts.append(f"\n🧩 <b>Universe health:</b> degraded инструментов {capability_stats['count']}")
        for row in rows:
            parts.append(f"  • {html.escape(row)}")
        cap_counts = capability_stats.get("capability_counts") or {}
        if cap_counts:
            top_caps = sorted(cap_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            parts.append("  " + "  ".join(f"{name}={count}" for name, count in top_caps))

    if decision_stats.get("total"):
        top_reasons = sorted(
            (decision_stats.get("reasons") or {}).items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:5]
        if top_reasons:
            parts.append(f"\n🧠 <b>Причины решений:</b>")
            parts.append("  " + "  ".join(f"{reason}={count}" for reason, count in top_reasons))

    # ── Подвал ────────────────────────────────────────────────────────────────
    # v0.9.38.3: читаем BOT_VERSION grep'ом из файла — импорт moex_bot падает
    # из-за тяжёлых module-level side-effects (logging, asyncio, os.environ).
    _ver = "v?.?.?"
    try:
        import re as _re
        _bot_file = Path(__file__).parent / "moex_bot.py"
        if _bot_file.exists():
            for _line in _bot_file.read_text(encoding="utf-8").splitlines():
                _m = _re.match(r'^BOT_VERSION\s*=\s*"([^"]+)"', _line)
                if _m:
                    _ver = _m.group(1)
                    break
    except Exception:
        pass
    parts.append(
        f"\n⏰ {_NOW_MSK.strftime('%H:%M:%S')} МСК  |  MOEX Bot {_ver}"
    )

    return "\n".join(parts)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    # Грузим .env при ручном запуске (systemd берёт env из EnvironmentFile)
    try:
        import dotenv as _dotenv
        _dotenv.load_dotenv(Path(__file__).parent / ".env", override=False)
        global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
        TELEGRAM_TOKEN   = TELEGRAM_TOKEN   or os.environ.get("TELEGRAM_TOKEN",   "")
        TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
    except ImportError:
        pass

    print(f"📋 Формирую отчёт за {TODAY_LABEL}...")

    all_trades         = load_trade_log()
    state              = load_state()
    eod_state          = load_eod_state()
    log_lines_raw      = load_today_log_lines()
    log_stats          = parse_log_stats(log_lines_raw)
    decision_entries   = load_today_decision_entries()
    decision_stats     = summarize_decisions(decision_entries)
    capability_stats   = summarize_capabilities()
    opportunity_entries = load_today_opportunity_entries()
    diagnostics        = build_daily_diagnostics(all_trades, state, log_lines_raw, decision_entries, opportunity_entries)
    save_daily_diagnostics(diagnostics)
    opened, closed     = trades_today(all_trades)
    stats              = day_pnl_stats(closed)
    week               = weekly_stats(all_trades)

    report = build_report(
        opened    = opened,
        closed    = closed,
        all_trades = all_trades,
        stats     = stats,
        state     = state,
        log_stats = log_stats,
        decision_stats = decision_stats,
        capability_stats = capability_stats,
        eod_state = eod_state,
        week      = week,
    )

    ok = tg_send(report)
    if ok:
        print("✅ Отчёт отправлен в Telegram")
    else:
        print("📄 Отчёт выведен в консоль (Telegram недоступен или токен не задан)")

    # ── v0.9.38.2: отправка архива логов ──────────────────────────────────────
    try:
        archive = _build_daily_archive()
        if archive and archive.exists():
            size_kb = archive.stat().st_size / 1024
            caption = f"📦 Архив логов трейдера за {TODAY_LABEL}"
            if tg_send_document(archive, caption=caption):
                print(f"✅ Архив отправлен: {archive.name} ({size_kb:.1f} КБ)")
            else:
                print(f"⚠️  Архив создан, но не отправлен: {archive}")
            _cleanup_old_archives(keep=7)
        else:
            print("⚠️  Архив логов не создан (см. предыдущие warnings)")
    except Exception as _e:
        print(f"⚠️  Ошибка при создании/отправке архива: {_e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
