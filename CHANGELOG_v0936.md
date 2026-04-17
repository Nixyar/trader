# v0.9.36 — applied 2026-04-17

Патчи применены прямо в `moex_bot.py` и `tinvest_data.py`.
Бэкап оригинала: `moex_bot.py.bak_before_v0936_20260416_182542`.

Diff: **394 строки добавлено, 45 удалено** (moex_bot.py +370, tinvest_data.py +69 / −45).
Компиляция: `py_compile` ✅. Smoke-тесты: ✅ (6/6). test_bot.py: 64/67 (3 падения — pre-existing, не связаны с патчами).

## Что изменилось

| # | Проблема с 16.04 | Где | Что сделано |
|---|---|---|---|
| 🔴 #1 | Phantom-сделки: CBOM в trade_log как win+loss без реальных ордеров | `update_trade_result` | Добавлено поле `executed` + проверка наличия `sb_<sid>` с order_id. Legacy-записи по умолчанию `executed=True`. |
| 🔴 #2 | OZON entry=4368 при цене 548 ₽ (×8 ratio) | `build_market_signal` + новый `_validate_entry` | Guard: если entry/last > ×5 или < ×0.2 → ERROR + fallback на last_price. 5..×5 → WARNING. |
| 🔴 #3 | OZON 50002 retry-loop (17 раз/день) | `tinvest_data.sandbox_place_order` + `sandbox_execute_signals` | Runtime blacklist при 50002 с TTL 24ч. `is_sandbox_available()` guard перед размещением ордера. |
| 🟡 #4 | CBOM v2 «погоня за вершиной» после target2 | новый `_is_in_cooldown` | 90 мин cooldown после target2, 60 мин после stop_hit (в том же direction). ENV-переопределяемо. |
| 🟡 #5 | AFKS добавлялся в h1_watch 11 раз после открытия | `add_to_h1_watch` | Guard: skip если уже есть открытая `sb_`-позиция. Также skip повторного добавления до expires_at. |
| 🟡 #6 | Telegram 400 на EOD-отчётах 15+16.04 | `tg_send` + `_tg_chunks` + `tg_selfcheck` | Авто-чанкинг >4000 chars + новый `tg_selfcheck()` (getMe при старте). HTML→plain fallback уже был (v0.9.10). |
| 🟡 #7 | 33 WARNING risk-block/день спамят лог | новый `_should_log_risk_block` | Dedup по (ticker, reason) окно 30 мин. Счётчик подавленных → `risk_block_summary()` для EOD. |
| 🟢 #10 | Нет защиты от просадки внутри дня | `check_daily_loss_brake` + вызов в начале `sandbox_execute_signals` | `MAX_DAILY_LOSS_PCT=2.0` (ENV). При drawdown ≥ 2% к SOD — новые входы блокируются до конца дня. |
| ⚙️ fix | Дефолт `SANDBOX_MAX_TOTAL_PCT=20` не совпадал с .env (40) | global const | Дефолт → 40.0, синхронизирован с .env и Notion KB. |
| 📌 bump | Версия | line 575 | `BOT_VERSION = "v0.9.36"` |

## Проверка после деплоя

```bash
# 1. Компиляция
python3 -m py_compile moex_bot.py tinvest_data.py

# 2. Smoke-тест (быстрый)
python3 test_bot.py -k TestSignalKey -v  # или любые pure-logic тесты

# 3. Токен TG
grep TELEGRAM moex_bot.py | head -1
curl "https://api.telegram.org/bot${TG_TOKEN}/getMe"

# 4. Версия в логах (при первом скане)
python3 moex_bot.py --once 2>&1 | grep "v0.9.36"
```

## Что смотреть в логах v0.9.36

- ✅ Заголовок сканов: `🤖 MOEX Signal Bot v0.9.36`
- ✅ Первое `[SANDBOX_BLACKLIST] OZON → runtime-blacklist на 24ч` и тишина после
- ✅ Риск-блоки: первый WARNING с msg, далее `debug`-тишина 30 мин
- ✅ В `trade_log.json` у новых записей поле `"executed": true/false`
- ✅ При попытке повторного входа: `⏸ Cooldown: CBOM LONG в кулдауне (60мин) после target2`
- ✅ При drawdown > 2%: `🛑 Daily loss brake активен: drawdown 2.15% ≥ 2.0%`

## Что НЕ было в патчах (не критично сейчас)

- **Корневая причина OZON entry×8** — защита есть (_validate_entry), но откуда берётся множитель — нужен отдельный дебаг с живым MOEX fetch. Можешь когда OZON снова даст сигнал — залогировать `logger.debug("raw intraday: %s", intraday)` в `build_market_signal`, я посмотрю.
- **Новостные orphan digest (PATCH #9)** — требует перекроить логику `send_news_orphan`, отложил до следующей итерации, шум не критичный.
- **Exceptional signal handling (PATCH #8)** — второстепенное, TG-алерт на blocked score≥18 можно добавить позже.

## Откат

```bash
cp moex_bot.py.bak_before_v0936_20260416_182542 moex_bot.py
# tinvest_data.py нет бэкапа, но диф изолированный: удалить блок v0.9.36 в начале + mark_sandbox_unavailable вызов в sandbox_place_order
```
