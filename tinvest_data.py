"""
T-Investments (Т-Банк / Tinkoff Invest) — модуль данных и бумажной торговли
═════════════════════════════════════════════════════════════════════════════════
Фаза 1 (активна): реальные котировки и история через T-Invest API.
Фаза 2 (активна): бумажная торговля через T-Invest Sandbox.
Фаза 3 (заглушка): реальные ордера — только после ENABLE_REAL_TRADING=true.

Быстрый старт:
  1. Получи токен: https://www.tbank.ru/invest/settings/api/
     → Создать токен → тип "Полный доступ" (нужен для sandbox API)
  2. Пропиши в .env:  TINVEST_TOKEN=t.xxxxxxxxxxxxxxx
  3. Установи SDK (ВАЖНО: пакет переехал с PyPI на реестр T-Bank):
       pip install t-tech-investments \
           --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
     Если не работает — резервный вариант с PyPI:
       pip install tinkoff-investments
  4. Создай sandbox-счёт:
       python tinvest_data.py --create-sandbox
     Скопируй account_id в TINVEST_SANDBOX_ACCOUNT_ID в .env
  5. Пополни sandbox-счёт виртуальными деньгами:
       python tinvest_data.py --fund-sandbox

Преимущества T-Invest перед MOEX ISS:
  ✓ Реальное время (стримы, не REST-поллинг)
  ✓ Стакан заявок (Level 2) — важно для скальпинга
  ✓ Исполнение ордеров (Фаза 2+)
  ✓ Лента сделок в реальном времени
"""

import os
import sys
import logging
import logging.handlers
import importlib
import warnings
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ─── v0.9.38.3: Persistent sandbox blacklist для тикеров с 50002 ──────────────
# Кейс 16.04.2026: OZON получал 50002 от T-Invest sandbox 3 раза за день.
# v0.9.36: runtime-blacklist (in-memory, TTL 24ч).
# v0.9.38.3: persistent-blacklist (JSON-файл) — переживает рестарты бота.
#   При 50002 на ордер TTL = 168ч (7 дней): инструмент, скорее всего, просто
#   не доступен в sandbox T-Invest и завтра снова вернёт 50002.
#   При figi_missing TTL = 24ч: FIGI мог появиться.
import json as _json
import pathlib as _pathlib

_SANDBOX_UNAVAILABLE: dict[str, tuple[datetime, str]] = {}   # ticker -> (marked_at_utc, reason)
SANDBOX_BLACKLIST_TTL_HOURS         = 24     # для figi_missing и прочих временных причин
SANDBOX_BLACKLIST_TTL_HOURS_50002   = 168    # 7 дней для инструментов с 50002 (не в sandbox)
_BLACKLIST_FILE: _pathlib.Path = _pathlib.Path(__file__).parent / "sandbox_blacklist.json"


def _save_blacklist() -> None:
    """Сохраняем blacklist в файл (переживает рестарты)."""
    try:
        data = {
            ticker: [marked_at.isoformat(), reason]
            for ticker, (marked_at, reason) in _SANDBOX_UNAVAILABLE.items()
        }
        _BLACKLIST_FILE.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    except Exception as _e:
        logger.debug("sandbox blacklist save error: %s", _e)


def _load_blacklist() -> None:
    """Загружаем blacklist из файла при старте модуля."""
    if not _BLACKLIST_FILE.exists():
        return
    try:
        data = _json.loads(_BLACKLIST_FILE.read_text(encoding="utf-8"))
        for ticker, (ts_str, reason) in data.items():
            marked_at = datetime.fromisoformat(ts_str)
            _SANDBOX_UNAVAILABLE[ticker] = (marked_at, reason)
        if _SANDBOX_UNAVAILABLE:
            logger.debug(
                "sandbox blacklist loaded: %d тикеров из %s",
                len(_SANDBOX_UNAVAILABLE), _BLACKLIST_FILE.name,
            )
    except Exception as _e:
        logger.debug("sandbox blacklist load error: %s", _e)

_load_blacklist()  # загружаем сразу при импорте модуля


def mark_sandbox_unavailable(ticker: str, reason: str = "50002") -> None:
    """Отметить тикер как недоступный в sandbox.

    v0.9.36: TTL-blacklist при 50002 от T-Invest.
    v0.9.37: reason='figi_missing' — когда FIGI_MAP не содержит тикер.
    v0.9.38.3: persistent — сохраняется в файл, переживает рестарты.
    """
    ttl = SANDBOX_BLACKLIST_TTL_HOURS_50002 if reason == "50002" else SANDBOX_BLACKLIST_TTL_HOURS
    if ticker not in _SANDBOX_UNAVAILABLE:
        logger.warning(
            "[SANDBOX_BLACKLIST] %s → blacklist на %dч (reason=%s) [persistent]",
            ticker, ttl, reason,
        )
    _SANDBOX_UNAVAILABLE[ticker] = (datetime.now(timezone.utc), reason)
    _save_blacklist()


def is_sandbox_available(ticker: str) -> bool:
    """True — если тикер не в blacklist или TTL истёк."""
    marked = _SANDBOX_UNAVAILABLE.get(ticker)
    if not marked:
        return True
    marked_at, reason = marked
    ttl = SANDBOX_BLACKLIST_TTL_HOURS_50002 if reason == "50002" else SANDBOX_BLACKLIST_TTL_HOURS
    age_h = (datetime.now(timezone.utc) - marked_at).total_seconds() / 3600
    if age_h >= ttl:
        _SANDBOX_UNAVAILABLE.pop(ticker, None)
        _save_blacklist()
        logger.info("[SANDBOX_BLACKLIST] %s → TTL истёк (%dч), снимаем блок", ticker, age_h)
        return True
    return False


def list_sandbox_unavailable() -> list[tuple[str, str]]:
    """Для диагностики / EOD-отчёта. Возвращает [(ticker, reason), ...]."""
    return sorted((t, r) for t, (_dt, r) in _SANDBOX_UNAVAILABLE.items())


# ══════════════════════════════════════════════════════════════════════════════
#  v0.9.38 — верификация LOT_SIZE против MOEX ISS
# ══════════════════════════════════════════════════════════════════════════════
_LOT_VERIFY_DONE: bool = False

def verify_lot_sizes(timeout: float = 5.0, force: bool = False) -> list[tuple[str, int, int]]:
    """Фетчит LOTSIZE тикеров из MOEX ISS и сверяет с LOT_SIZE.

    Возвращает список (ticker, code_val, moex_val) для mismatched тикеров.
    Вызывается при старте bot'а. WARN логируется для каждого расхождения.
    Идемпотентна — при повторном вызове без force=True ничего не делает.
    """
    global _LOT_VERIFY_DONE
    if _LOT_VERIFY_DONE and not force:
        return []

    import urllib.request as _urlreq
    import json as _json

    mismatches: list[tuple[str, int, int]] = []
    checked = 0
    for ticker, code_val in LOT_SIZE.items():
        url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
               f"TQBR/securities/{ticker}.json?iss.meta=off")
        try:
            raw = _urlreq.urlopen(url, timeout=timeout).read()
            data = _json.loads(raw)
            cols = data["securities"]["columns"]
            rows = data["securities"]["data"]
            if not rows:
                continue
            idx = cols.index("LOTSIZE")
            moex_val = int(rows[0][idx])
        except Exception as _e:
            logger.debug("verify_lot_sizes %s: %s", ticker, _e)
            continue
        checked += 1
        if moex_val != code_val:
            mismatches.append((ticker, code_val, moex_val))
            logger.warning(
                "[LOT_SIZE MISMATCH] %s: код=%d, MOEX ISS=%d — "
                "обнови tinvest_data.LOT_SIZE (иначе позиция откроется "
                "неправильного размера!)",
                ticker, code_val, moex_val,
            )

    if mismatches:
        logger.warning(
            "[LOT_SIZE] проверено %d тикеров, mismatches: %d — требуется правка LOT_SIZE",
            checked, len(mismatches),
        )
    else:
        logger.info("[LOT_SIZE] проверено %d тикеров, всё синхронно с MOEX ISS ✅", checked)

    _LOT_VERIFY_DONE = True
    return mismatches

# ── Fallback logging (при запуске tinvest_data.py напрямую) ───────────────────
# Когда модуль запускается как скрипт (python tinvest_data.py --portfolio),
# moex_bot.py не инициализирует logging, поэтому настраиваем его здесь.
# Если basicConfig уже вызван из moex_bot, handlers добавлены — блок пропускается.
if not logging.getLogger().handlers:
    _log_dir = os.path.dirname(os.path.abspath(__file__))
    _log_file = os.path.join(_log_dir, "bot.log")
    _fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _fh = logging.handlers.RotatingFileHandler(
        _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _sh.setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG, handlers=[_fh])
    logging.getLogger().addHandler(_sh)
# ──────────────────────────────────────────────────────────────────────────────

# ── SDK module detection ───────────────────────────────────────────────────────
# t-tech-investments v0.3.4 может устанавливать модуль под разными именами.
# Пробуем все возможные варианты и кэшируем первый рабочий.

_SDK_MODULE_NAME: Optional[str] = None   # имя модуля (после первого _detect_sdk())
_SDK_DETECT_DONE: bool = False           # флаг: детекция уже проводилась

_SDK_CANDIDATES = [
    "t_tech.invest",       # t-tech-investments ≥0.3 (новый namespace T-Bank)
    "tinkoff.invest",      # tinkoff-investments PyPI / t-tech-investments 0.2.x
    "tinkoff_invest",      # возможное альтернативное имя
    "t_invest",            # другой вариант T-Bank
    "tinvest",             # устаревший пакет tinvest (PyPI)
]


def _detect_sdk() -> Optional[str]:
    """
    Пробует импортировать SDK по каждому кандидату из _SDK_CANDIDATES.
    Возвращает имя первого рабочего модуля или None.
    Результат кэшируется; повторный вызов не тратит время.
    """
    global _SDK_MODULE_NAME, _SDK_DETECT_DONE
    if _SDK_DETECT_DONE:
        return _SDK_MODULE_NAME

    _SDK_DETECT_DONE = True
    errors: list[str] = []

    for name in _SDK_CANDIDATES:
        try:
            importlib.import_module(name)
            _SDK_MODULE_NAME = name
            logger.debug(f"T-Invest SDK найден: {name}")
            return name
        except ImportError as e:
            errors.append(f"  {name}: {e}")
        except Exception as e:
            errors.append(f"  {name}: {type(e).__name__}: {e}")

    # Ни один вариант не сработал — логируем подробности
    logger.warning(
        "T-Invest SDK не найден. Пробовал:\n"
        + "\n".join(errors)
        + "\n\nУстанови SDK:\n"
        "  pip install t-tech-investments "
        "--index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple\n"
        "  Или резервный: pip install tinkoff-investments\n"
        "Затем запусти диагностику: python check_sdk.py"
    )
    return None


def _proto_to_float(obj) -> float:
    """
    Конвертирует Quotation или MoneyValue из protobuf в float.
    Работает с любой версией SDK — не зависит от утилит.

    Оба типа имеют поля units (int) и nano (int, 1/1e9 единицы):
      Quotation(units=150, nano=500_000_000) → 150.5
      MoneyValue(currency="rub", units=150, nano=500_000_000) → 150.5
    """
    if obj is None:
        return 0.0
    units = getattr(obj, "units", 0) or 0
    nano  = getattr(obj, "nano",  0) or 0
    return units + nano / 1_000_000_000


def _sdk_import(*names: str):
    """
    Импортирует имена из SDK-модуля независимо от его реального пути.

    Пример:
        Client, CandleInterval = _sdk_import("Client", "CandleInterval")
    """
    mod_name = _detect_sdk()
    if mod_name is None:
        raise ImportError("T-Invest SDK не установлен (попробуй: python check_sdk.py)")

    # Пробуем: from <mod_name> import <name>
    # А если не нашли — пробуем из utils / enums sub-модулей
    result = []
    mod = importlib.import_module(mod_name)

    for name in names:
        obj = None

        # 1. Прямо в корневом модуле
        obj = getattr(mod, name, None)

        # 2. В utils
        if obj is None:
            try:
                utils = importlib.import_module(f"{mod_name}.utils")
                obj = getattr(utils, name, None)
            except ImportError:
                pass

        if obj is None:
            raise ImportError(
                f"Не найдено '{name}' в модуле '{mod_name}'. "
                "Возможно, версия SDK несовместима."
            )
        result.append(obj)

    return result[0] if len(result) == 1 else result

# ── Конфигурация ──────────────────────────────────────────────────────────────

TINVEST_TOKEN              = os.environ.get("TINVEST_TOKEN", "")
TINVEST_SANDBOX_ACCOUNT_ID = os.environ.get("TINVEST_SANDBOX_ACCOUNT_ID", "")

# Стартовый баланс sandbox-счёта в рублях (можно менять)
SANDBOX_INITIAL_BALANCE_RUB = float(os.environ.get("SANDBOX_BALANCE", "1000000"))

# FIGI-коды для MOEX тикеров (Tinkoff использует FIGI вместо тикеров)
# Обновляй при добавлении новых инструментов.
# Найти FIGI: https://www.tbank.ru/invest/
FIGI_MAP: dict[str, str] = {
    "GAZP":  "BBG004730RP0",
    "SBER":  "BBG004730N88",
    "LKOH":  "BBG004731032",
    "ROSN":  "BBG004731354",
    "NVTK":  "BBG00475KKY8",
    "GMKN":  "BBG004731489",
    "YDEX":  "BBG006L8G4H1",
    "TATN":  "BBG004RVFCY3",
    "MGNT":  "BBG004RVFFC0",
    "PLZL":  "BBG000QJW156",
    "SNGS":  "BBG004730ZJ9",
    "MTSS":  "BBG004S681B4",
    "ALRS":  "BBG004S68B31",
    "VTBR":  "BBG004730REP",
    "CHMF":  "BBG00475MY39",
    # TCSG делистован в 2024 при реструктуризации TCS Group → T-Технологии.
    # Новый тикер на МОСБИРЖЕ: "T" (T-Банк / T-Технологии).
    # FIGI нового листинга нужно уточнить через: tinkoff.ru/invest/ → поиск "T"
    "T":     "BBG00QPYJ5H0",   # TODO: уточнить FIGI для нового тикера "T"
    "TCSG":  "BBG00QPYJ5H0",   # оставлен для обратной совместимости (может не работать)
    "PHOR":  "BBG004S689R0",
    "AFKS":  "BBG004S68614",
    "NLMK":  "BBG004S681M2",
    "SIBN":  "BBG004FWGS36",   # Газпром нефть
    # FLOT: FIGI BBG000NF0ZQ4 даёт NOT_FOUND (50002) в T-Invest API.
    # Совкомфлот торгуется на MOEX, но инструмент может быть недоступен в T-Invest.
    # H1-свечи будут fallback на MOEX ISS. Для исправления уточни FIGI на tbank.ru/invest/
    # "FLOT":  "BBG000NF0ZQ4",  # ОТКЛЮЧЕНО — NOT_FOUND в T-Invest (v0.9.6)
    "RUAL":  "BBG008F2T3T2",   # Русал
    "OZON":  "BBG00Y91R9T3",   # Ozon
    "MOEX":  "BBG004730JJ5",   # Мосбиржа
    "SMLT":  "BBG006HBB564",   # Самолет
    "TRNFP": "BBG00475K6C3",   # Транснефть-п
    # v0.9.6 — новые тикеры
    "ENPG":  "BBG000FH5YM2",   # Эн+ Груп (алюминий + гидроэнергетика)
    "MAGN":  "BBG004S68507",   # ММК (Магнитогорский металлургический комбинат)
    "AFLT":  "BBG004S683W7",   # Аэрофлот
    "PIKK":  "BBG004730ZL5",   # ПИК Груп (девелопер)
    # v0.9.9 — новые тикеры
    # AKRN: BBG004S68BH6 возвращал данные для инструмента ~467 руб (не АКРОН ~20000 руб).
    # Отключён — H1-свечи и цены берутся из MOEX ISS. Уточнить FIGI на tbank.ru/invest/
    # "AKRN":  "BBG004S68BH6",   # ОТКЛЮЧЕНО — неверный инструмент (v0.9.10)
    "IRAO":  "BBG004S68473",   # ИнтерРАО (электроэнергетика, экспорт электроэнергии)
    # v0.9.37 — CBOM (МКБ) добавлен в watchlist в v0.9.34 без FIGI.
    # 17.04.2026: 163 WARNING/день. FIGI ориентир по OpenFIGI — BBG000TY1CX1.
    # ВАЖНО: если T-Invest вернёт 50002 NOT_FOUND на этот FIGI —
    # закомментируй строку, runtime-blacklist (v0.9.37) сам заблокирует тикер
    # и EOD-отчёт покажет его отдельной строкой «⚠️ FIGI требует проверки».
    "CBOM":  "BBG000TY1CX1",   # МКБ — Московский Кредитный Банк (ПРОВЕРИТЬ!)
}

# Маппинг обратно: FIGI → тикер
TICKER_BY_FIGI: dict[str, str] = {v: k for k, v in FIGI_MAP.items()}

# Лотность (число акций в 1 лоте). T-Invest работает в лотах, не акциях.
# Если лот = 1 акция — можно ставить quantity=1.
# Обновляй при изменении лотности на бирже.
#
# v0.9.38 — РЕВИЗИЯ через MOEX ISS (17.04.2026). Исправлено 7 несоответствий,
# которые вели к тому, что реальные позиции отличались от задуманного sizing
# в 10, 100 и даже 10 000 раз (кейс SBER 17.04: 31 лот открылось на 10 004₽
# вместо планируемых ~100 000₽ из-за LOT_SIZE[SBER]=10 при биржевой 1).
#
# При изменении — прогнать `python3 -c "from tinvest_data import verify_lot_sizes;
# verify_lot_sizes()"` для сверки с MOEX ISS (см. функцию ниже).
LOT_SIZE: dict[str, int] = {
    "GAZP": 10, "SBER": 1,  "LKOH": 1,  "ROSN": 1,  "NVTK": 1,       # v0.9.38: SBER 10→1
    "GMKN": 10, "YDEX": 1,  "TATN": 1,  "MGNT": 1,  "PLZL": 1,       # v0.9.38: GMKN 1→10
    "SNGS": 100,"MTSS": 10, "ALRS": 10, "VTBR": 1,  "CHMF": 1,       # v0.9.38: SNGS 1→100, VTBR 10000→1
    "T":    1,  "TCSG": 1,  "PHOR": 1,  "AFKS": 100,"NLMK": 10, "SIBN": 1,  # v0.9.38: SIBN 10→1
    "FLOT": 10, "RUAL": 10, "OZON": 1,  "MOEX": 10, "SMLT": 1,
    "TRNFP":1,
    # v0.9.6
    "ENPG": 1,  "MAGN": 10, "AFLT": 10, "PIKK": 1,
    # v0.9.9
    "AKRN": 1,  "IRAO": 100,
    # v0.9.38 — добавлены отсутствующие (ранее падали в default=1 при sizing)
    "X5":   1,  "HEAD": 1,  "POSI": 1,  "LSRG": 1,
    "CBOM": 100,   # MOEX подтвердил LOTSIZE=100 (не 10 как было в moex_bot.LOT_SIZES)
}


def _get_token() -> str:
    """
    Читает токен из окружения каждый раз заново.
    Это важно: moex_bot.py загружает .env ПОСЛЕ импорта tinvest_data,
    поэтому модульная переменная TINVEST_TOKEN может быть пустой.
    """
    return (
        os.environ.get("TINVEST_TOKEN", "")
        or TINVEST_TOKEN  # fallback на значение при импорте
    )


def _get_account_id() -> str:
    """Аналогично — читает account_id из окружения динамически."""
    return (
        os.environ.get("TINVEST_SANDBOX_ACCOUNT_ID", "")
        or TINVEST_SANDBOX_ACCOUNT_ID
    )


def is_available() -> bool:
    """Проверяем, задан ли токен и установлен ли SDK.

    Поддерживаем несколько пакетов (автоопределение):
      - t-tech-investments  (новый, реестр T-Bank — рекомендуется)
      - tinkoff-investments (старый PyPI — резервный)
      - tinkoff_invest / t_invest / tinvest — альтернативные имена
    """
    tok = _get_token()
    if not tok or tok == "вставь_токен_сюда":
        return False
    return _detect_sdk() is not None


_FIGI_MISSING_LOGGED: set[str] = set()

def get_figi(ticker: str) -> Optional[str]:
    """Возвращает FIGI для тикера или None если не найден.

    v0.9.37: WARNING логируется один раз на тикер (до рестарта процесса),
    плюс автоматический blacklist с reason='figi_missing', чтобы sandbox
    не пытался размещать ордер и не генерил фантомы в trade_log.
    """
    figi = FIGI_MAP.get(ticker)
    if not figi:
        if ticker not in _FIGI_MISSING_LOGGED:
            logger.warning(
                "FIGI не найден для тикера %s — добавь в FIGI_MAP (tinvest_data.py). "
                "Тикер помечен как sandbox-unavailable до рестарта.",
                ticker,
            )
            _FIGI_MISSING_LOGGED.add(ticker)
            mark_sandbox_unavailable(ticker, reason="figi_missing")
    return figi


# ══════════════════════════════════════════════════════════════════════════════
#  ФАЗА 1: Данные (котировки, история)
# ══════════════════════════════════════════════════════════════════════════════

def get_last_price(ticker: str) -> Optional[dict]:
    """
    Последняя цена через T-Invest API.
    Замена get_intraday_price() из moex_bot.py.

    Returns: {"last": float, "open": float, "change_pct": float} или None
    """
    if not is_available():
        return None

    figi = get_figi(ticker)
    if not figi:
        return None

    try:
        Client = _sdk_import("Client")

        with Client(_get_token()) as client:
            response = client.market_data.get_last_prices(figi=[figi])
            if not response.last_prices:
                return None
            lp = response.last_prices[0]
            last_price = _proto_to_float(lp.price)

            # Дневная свеча для open-цены
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            candles = client.market_data.get_candles(
                figi=figi,
                from_=today_start,
                to=datetime.now(timezone.utc),
                interval=5,  # 5-минутные свечи
            )
            open_price = last_price
            if candles.candles:
                open_price = _proto_to_float(candles.candles[0].open)

            change_pct = (last_price - open_price) / open_price * 100 if open_price else 0

            return {
                "open":       round(open_price, 4),
                "last":       round(last_price, 4),
                "change_pct": round(change_pct, 2),
            }

    except Exception as e:
        logger.error(f"T-Invest get_last_price({ticker}): {e}")
        return None


def get_orderbook(ticker: str, depth: int = 10) -> Optional[dict]:
    """
    Стакан заявок (Level 2) через T-Invest API.

    Returns:
        {
          "bid_volume":  int,   — суммарный объём заявок на покупку (в лотах)
          "ask_volume":  int,   — суммарный объём заявок на продажу (в лотах)
          "imbalance":   float, — (bid-ask)/(bid+ask) ∈ [-1, 1]
                                  > 0 → давление покупателей, < 0 → продавцов
          "spread":      float, — спред (best_ask - best_bid)
          "best_bid":    float, — лучшая цена покупки
          "best_ask":    float, — лучшая цена продажи
        }
        или None если SDK недоступен / FIGI не найден / ошибка API.

    Использование в build_market_signal():
        imbalance > 0.2  → покупательское давление (поддерживает LONG)
        imbalance < -0.2 → давление продавцов (поддерживает SHORT)
    """
    if not is_available():
        return None

    figi = get_figi(ticker)
    if not figi:
        logger.debug(f"get_orderbook({ticker}): FIGI не найден")
        return None

    try:
        Client = _sdk_import("Client")

        with Client(_get_token()) as client:
            ob = client.market_data.get_order_book(figi=figi, depth=depth)

        bids = ob.bids  # list[Order(price: Quotation, quantity: int)]
        asks = ob.asks

        if not bids and not asks:
            return None

        # quantity в стакане — в лотах (целое число, не Quotation)
        bid_vol = sum(int(b.quantity) for b in bids)
        ask_vol = sum(int(a.quantity) for a in asks)
        total   = bid_vol + ask_vol
        imbalance = round((bid_vol - ask_vol) / total, 3) if total > 0 else 0.0

        best_bid = _proto_to_float(bids[0].price) if bids else 0.0
        best_ask = _proto_to_float(asks[0].price) if asks else 0.0
        spread   = round(best_ask - best_bid, 4) if (best_bid and best_ask) else 0.0

        return {
            "bid_volume": bid_vol,
            "ask_volume": ask_vol,
            "imbalance":  imbalance,
            "spread":     spread,
            "best_bid":   round(best_bid, 4),
            "best_ask":   round(best_ask, 4),
        }

    except Exception as e:
        logger.debug(f"get_orderbook({ticker}): {e}")
        return None


def get_candles_tinvest(ticker: str, days: int = 21) -> list[dict]:
    """
    История дневных свечей через T-Invest API.
    Замена get_candles() из moex_bot.py (совместимый формат).

    Returns: список {"open", "close", "high", "low", "value", "begin"}
    """
    if not is_available():
        return []

    figi = get_figi(ticker)
    if not figi:
        return []

    try:
        Client, CandleInterval = _sdk_import("Client", "CandleInterval")

        date_from = datetime.now(timezone.utc) - timedelta(days=days + 5)

        with Client(_get_token()) as client:
            response = client.market_data.get_candles(
                figi=figi,
                from_=date_from,
                to=datetime.now(timezone.utc),
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            )

        result = []
        lot = LOT_SIZE.get(ticker, 1)
        for c in response.candles:
            vol_lots = c.volume  # T-Invest отдаёт объём в лотах
            # Конвертируем в рубли: объём_лотов × лотность × цена_закрытия
            close_price = _proto_to_float(c.close)
            vol_rub = vol_lots * lot * close_price
            result.append({
                "open":  _proto_to_float(c.open),
                "close": close_price,
                "high":  _proto_to_float(c.high),
                "low":   _proto_to_float(c.low),
                "value": vol_rub,
                "begin": c.time.isoformat(),
            })

        return result[-days:] if len(result) > days else result

    except Exception as e:
        logger.error(f"T-Invest get_candles({ticker}): {e}")
        return []


# Тикеры, у которых H1-свечи в T-Invest возвращают NOT_FOUND (50002).
# Для них сразу используем MOEX ISS — T-Invest даже не пробуем,
# чтобы не засорять лог [ERROR] от SDK-логгера t_tech.invest.logging.
# v0.9.35: добавлен SMLT (BBG006HBB564 даёт NOT_FOUND в sandbox).
_TINVEST_CANDLES_SKIP: set[str] = {
    "FLOT",   # BBG000NF0ZQ4 — NOT_FOUND v0.9.6
    "SMLT",   # BBG006HBB564 — NOT_FOUND v0.9.35
    "SIBN",   # BBG004FWGS36 — NOT_FOUND (упоминался в комментариях)
    "PIKK",   # BBG004730ZL5 — NOT_FOUND (упоминался в комментариях)
    # v0.9.38.3: подтверждённые 50002 на ORDER PLACEMENT в sandbox (не только свечи)
    # Эти инструменты физически отсутствуют в T-Invest sandbox — попытка ордера
    # даёт 50002 NOT_FOUND. Сигналы генерируются, но sandbox-ордер не размещается.
    # H1 свечи приходят через MOEX ISS fallback (поэтому CANDLES_SKIP нужен для тишины).
    "CHMF",   # подтверждён 50002 на sandbox_place_order (22.04.2026)
    "VTBR",   # подтверждён 50002 на sandbox_place_order (22.04.2026)
    "OZON",   # подтверждён 50002 на sandbox_place_order (апрель 2026)
}


def get_h1_candles(ticker: str, days: int = 5) -> list[dict]:
    """
    История часовых (H1) свечей через T-Invest API.
    Используется для анализа H1-уровней поддержки/сопротивления (v0.9.2).

    Тикеры из _TINVEST_CANDLES_SKIP сразу возвращают [] (fallback на MOEX ISS),
    без вызова T-Invest и без шумного [ERROR] от t_tech.invest.logging.

    Returns: список {"open", "close", "high", "low", "value", "begin"}
    Количество свечей: ~days × 7 часов торгов MOEX ≈ 35 свечей за 5 дней.
    """
    if not is_available():
        return []

    # v0.9.35: быстрый выход для тикеров с известным NOT_FOUND в T-Invest
    if ticker in _TINVEST_CANDLES_SKIP:
        logger.debug("get_h1_candles(%s): в списке CANDLES_SKIP → MOEX ISS fallback", ticker)
        return []

    figi = get_figi(ticker)
    if not figi:
        return []

    try:
        Client, CandleInterval = _sdk_import("Client", "CandleInterval")

        # Берём чуть больше дней для запаса (выходные)
        date_from = datetime.now(timezone.utc) - timedelta(days=days + 2)

        with Client(_get_token()) as client:
            response = client.market_data.get_candles(
                figi=figi,
                from_=date_from,
                to=datetime.now(timezone.utc),
                interval=CandleInterval.CANDLE_INTERVAL_HOUR,
            )

        lot = LOT_SIZE.get(ticker, 1)
        result = []
        for c in response.candles:
            close_price = _proto_to_float(c.close)
            vol_rub = c.volume * lot * close_price
            result.append({
                "open":  _proto_to_float(c.open),
                "close": close_price,
                "high":  _proto_to_float(c.high),
                "low":   _proto_to_float(c.low),
                "value": vol_rub,
                "begin": c.time.isoformat(),
            })

        return result

    except Exception as e:
        err_str = str(e)
        # 50002 = "Instrument not found" — нет в T-Invest (неизвестный тикер)
        # Добавь тикер в _TINVEST_CANDLES_SKIP чтобы избежать этой ошибки в будущем
        if "50002" in err_str:
            logger.warning(
                "T-Invest get_h1_candles(%s): NOT_FOUND 50002 → MOEX ISS fallback "
                "(добавь '%s' в _TINVEST_CANDLES_SKIP для тишины)", ticker, ticker
            )
        else:
            logger.error("T-Invest get_h1_candles(%s): %s", ticker, e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  ФАЗА 2: Бумажная торговля (Sandbox) — АКТИВНА
# ══════════════════════════════════════════════════════════════════════════════

def create_sandbox_account() -> Optional[str]:
    """
    Создаёт новый sandbox-счёт и возвращает его ID.
    Вызови один раз, сохрани ID в .env как TINVEST_SANDBOX_ACCOUNT_ID.

    Использование:
        python tinvest_data.py --create-sandbox
    """
    if not is_available():
        print("❌ Токен не задан или SDK не установлен")
        return None
    try:
        Client = _sdk_import("Client")
        with Client(_get_token()) as client:
            response = client.sandbox.open_sandbox_account()
            account_id = response.account_id
            print(f"✅ Sandbox-счёт создан: {account_id}")
            print(f"\nДобавь в .env:")
            print(f"  TINVEST_SANDBOX_ACCOUNT_ID={account_id}")
            return account_id
    except Exception as e:
        print(f"❌ Ошибка создания sandbox-счёта: {e}")
        logger.error(f"create_sandbox_account: {e}")
        return None


def fund_sandbox_account(
    amount_rub: float = SANDBOX_INITIAL_BALANCE_RUB,
    account_id: str = "",
) -> bool:
    """
    Пополняет sandbox-счёт виртуальными рублями.

    Args:
        amount_rub: сумма в рублях (по умолчанию SANDBOX_INITIAL_BALANCE_RUB)
        account_id: ID счёта (если пусто — берётся из .env)

    Использование:
        python tinvest_data.py --fund-sandbox
    """
    acc_id = account_id or TINVEST_SANDBOX_ACCOUNT_ID
    if not acc_id:
        print("❌ TINVEST_SANDBOX_ACCOUNT_ID не задан в .env")
        return False
    if not is_available():
        print("❌ Токен не задан или SDK не установлен")
        return False

    try:
        # sandbox_pay_in требует MoneyValue (с полем currency), а не Quotation.
        # Пробуем импортировать MoneyValue напрямую из SDK.
        Client, MoneyValue = _sdk_import("Client", "MoneyValue")

        units = int(amount_rub)
        nano  = int(round((amount_rub - units) * 1_000_000_000))
        amount = MoneyValue(currency="rub", units=units, nano=nano)

        with Client(_get_token()) as client:
            client.sandbox.sandbox_pay_in(
                account_id=acc_id,
                amount=amount,
            )
        print(f"✅ Sandbox-счёт {acc_id} пополнен на {amount_rub:,.0f} ₽")
        return True
    except Exception as e:
        err_str = str(e)
        # Ошибка 90001 = T-Bank требует SMS-подтверждение.
        # Это баг/ограничение t-tech-investments 0.3.x: sandbox_pay_in
        # иногда проходит через боевой pipeline проверок.
        # Альтернатива: пополнить через веб-кабинет T-Invest Sandbox.
        if "90001" in err_str or "sms" in err_str.lower():
            print(
                "⚠️  T-Bank требует SMS-подтверждение для sandbox_pay_in.\n"
                "   Это ограничение API версии 0.3.x.\n\n"
                "   Пополни счёт вручную через кабинет:\n"
                "   https://www.tbank.ru/invest/ → Песочница → Пополнить\n\n"
                f"   Account ID: {acc_id}"
            )
        else:
            print(f"❌ Ошибка пополнения sandbox: {e}")
        logger.error(f"fund_sandbox_account: {e}")
        return False


def get_sandbox_portfolio(account_id: str = "") -> Optional[dict]:
    """
    Возвращает портфель sandbox-счёта: баланс + открытые позиции.
    Полезно для оценки результатов бумажной торговли.
    """
    acc_id = account_id or _get_account_id()
    if not acc_id or not is_available():
        return None

    try:
        Client = _sdk_import("Client")

        with Client(_get_token()) as client:
            # get_sandbox_portfolio deprecated, но operations.get_portfolio не работает
            # для sandbox-аккаунтов (UNAUTHENTICATED 40003). Заглушаем предупреждение.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                portfolio = client.sandbox.get_sandbox_portfolio(account_id=acc_id)

        positions = []
        for pos in portfolio.positions:
            ticker = TICKER_BY_FIGI.get(pos.figi, pos.figi)
            qty    = _proto_to_float(pos.quantity)
            avg    = _proto_to_float(pos.average_position_price)
            curr   = _proto_to_float(pos.current_price)
            # qty < 0 = SHORT: прибыль когда цена падает → инвертируем знак PnL
            raw_pnl = round((curr - avg) / avg * 100, 2) if avg else 0
            pnl     = raw_pnl if qty >= 0 else -raw_pnl
            if qty != 0:
                positions.append({
                    "ticker":     ticker,
                    "quantity":   qty,
                    "avg_price":  round(avg, 2),
                    "curr_price": round(curr, 2),
                    "pnl_pct":    pnl,
                })

        return {
            "total_amount_rub": round(
                _proto_to_float(portfolio.total_amount_portfolio), 2
            ),
            "positions": positions,
        }

    except Exception as e:
        logger.error(f"get_sandbox_portfolio: {e}")
        return None


def sandbox_place_order(
    ticker: str,
    direction: str,     # "LONG" (BUY) или "SHORT" (SELL)
    quantity: int = 1,  # количество лотов
    account_id: str = "",
) -> Optional[dict]:
    """
    Выставляет рыночный ордер в T-Invest Sandbox (бумажная торговля).

    Args:
        ticker:     тикер из FIGI_MAP (напр. "SBER")
        direction:  "LONG" = покупка, "SHORT" = продажа
        quantity:   количество лотов (1 лот = LOT_SIZE[ticker] акций)
        account_id: ID sandbox-счёта (если пусто — из .env)

    Returns:
        {"order_id": str, "status": str, "direction": str, "ticker": str}
        или None при ошибке

    Предварительно:
        1. Создай счёт:  python tinvest_data.py --create-sandbox
        2. Пополни счёт: python tinvest_data.py --fund-sandbox
        3. Пропиши TINVEST_SANDBOX_ACCOUNT_ID в .env
    """
    acc_id = account_id or _get_account_id()
    if not acc_id:
        logger.error(
            "TINVEST_SANDBOX_ACCOUNT_ID не задан. "
            "Запусти: python tinvest_data.py --create-sandbox"
        )
        return None

    if not is_available():
        logger.warning("T-Invest недоступен (нет токена или SDK)")
        return None

    figi = get_figi(ticker)
    if not figi:
        return None

    try:
        Client, OrderDirection, OrderType = _sdk_import(
            "Client", "OrderDirection", "OrderType"
        )
        import uuid

        order_dir = (
            OrderDirection.ORDER_DIRECTION_BUY
            if direction == "LONG"
            else OrderDirection.ORDER_DIRECTION_SELL
        )

        with Client(_get_token()) as client:
            resp = client.sandbox.post_sandbox_order(
                figi=figi,
                quantity=quantity,
                direction=order_dir,
                account_id=acc_id,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4()),
            )
            exec_price = (
                _proto_to_float(resp.executed_order_price)
                if resp.executed_order_price
                else None
            )

        result = {
            "order_id":  resp.order_id,
            "status":    str(resp.execution_report_status),
            "direction": direction,
            "ticker":    ticker,
            "quantity":  quantity,
            "price":     exec_price,
            "lots":      quantity,
            "shares":    quantity * LOT_SIZE.get(ticker, 1),
        }
        logger.info(
            f"[SANDBOX] {direction} {ticker} ×{quantity}л "
            f"→ {resp.order_id}  статус={resp.execution_report_status}"
        )
        return result

    except Exception as e:
        err_str = str(e)
        # Код 30079 = "Instrument is not available for trading" (временно/квалинвестор).
        # Это штатная ситуация для sandbox — логируем как WARNING, не ERROR.
        if "30079" in err_str:
            logger.warning(
                f"[SANDBOX] {ticker} недоступен для торговли (30079 — "
                f"инструмент может быть временно закрыт или требует квалинвестора)"
            )
        elif "50002" in err_str:
            logger.warning(
                f"[SANDBOX] {ticker} не найден в T-Invest (50002) — "
                f"инструмент недоступен в sandbox, ордер пропущен"
            )
            # v0.9.36: помечаем в runtime-blacklist до рестарта / TTL
            mark_sandbox_unavailable(ticker)
        else:
            logger.error(f"sandbox_place_order({ticker}): {e}")
        return None


def sandbox_close_position(
    ticker: str,
    account_id: str = "",
) -> Optional[dict]:
    """
    Закрывает открытую позицию по тикеру в sandbox.
    Определяет направление автоматически из портфеля.

    ВАЖНО: T-Invest portfolio API возвращает pos.quantity в АКЦИЯХ (не лотах)
    для инструментов с lot_size > 1. sandbox_place_order ожидает ЛОТЫ.
    Поэтому делаем конвертацию: qty_lots = round(shares / lot_size).
    """
    acc_id = account_id or _get_account_id()
    portfolio = get_sandbox_portfolio(acc_id)
    if not portfolio:
        return None

    lot_size = LOT_SIZE.get(ticker, 1)

    for pos in portfolio["positions"]:
        if pos["ticker"] == ticker and pos["quantity"] != 0:
            # Конвертируем акции → лоты (T-Invest portfolio возвращает акции)
            shares = abs(pos["quantity"])
            qty_lots = max(1, round(shares / lot_size))
            # Если длинная — продаём, если короткая — покупаем
            close_dir = "SHORT" if pos["quantity"] > 0 else "LONG"
            logger.info(
                f"[SANDBOX CLOSE] {ticker}: qty={pos['quantity']} акций "
                f"/ lot_size={lot_size} = {qty_lots} лот → {close_dir}"
            )
            return sandbox_place_order(ticker, close_dir, qty_lots, acc_id)

    logger.info(f"Нет открытой позиции по {ticker} в sandbox")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ФАЗА 3: Реальные ордера (ОСТОРОЖНО)
# ══════════════════════════════════════════════════════════════════════════════

def place_real_order(
    ticker: str,
    direction: str,
    quantity: int = 1,
    stop_price: Optional[float] = None,
) -> Optional[dict]:
    """
    РЕАЛЬНЫЙ ОРДЕР — НЕ АКТИВЕН.

    Включать только после:
      ✓ Минимум 2 недели бумажной торговли с положительной статистикой
      ✓ Настроенного риск-менеджмента (максимальный размер позиции, дневной лимит убытка)
      ✓ Явного флага ENABLE_REAL_TRADING=true в .env

    ВНИМАНИЕ: Реальная торговля несёт финансовые риски.
    """
    enable_real = os.environ.get("ENABLE_REAL_TRADING", "").lower() == "true"
    if not enable_real:
        logger.warning("Реальная торговля выключена. Установи ENABLE_REAL_TRADING=true в .env")
        return None

    # TODO (Фаза 3): реализовать после Фазы 2
    logger.info(f"[REAL ORDER] {direction} {ticker} ×{quantity} — НЕ РЕАЛИЗОВАНО")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  CLI — быстрые команды
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import dotenv  # type: ignore[import]

    # Загружаем .env из папки рядом со скриптом
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        dotenv.load_dotenv(env_path)
        # Перечитываем переменные после загрузки .env
        TINVEST_TOKEN              = os.environ.get("TINVEST_TOKEN", "")
        TINVEST_SANDBOX_ACCOUNT_ID = os.environ.get("TINVEST_SANDBOX_ACCOUNT_ID", "")
    except ImportError:
        pass  # python-dotenv не установлен — ладно, берём из env

    args = sys.argv[1:]

    if "--create-sandbox" in args:
        print("\n🏦 Создаём sandbox-счёт...")
        create_sandbox_account()

    elif "--fund-sandbox" in args:
        print(f"\n💰 Пополняем sandbox-счёт на {SANDBOX_INITIAL_BALANCE_RUB:,.0f} ₽...")
        fund_sandbox_account()

    elif "--portfolio" in args:
        print("\n📊 Портфель sandbox-счёта:")
        p = get_sandbox_portfolio()
        if p:
            print(f"  Итого: {p['total_amount_rub']:,.0f} ₽")
            if p["positions"]:
                for pos in p["positions"]:
                    pnl_sign = "+" if pos["pnl_pct"] >= 0 else ""
                    print(
                        f"  {pos['ticker']:6}  ×{pos['quantity']}  "
                        f"avg={pos['avg_price']}  curr={pos['curr_price']}  "
                        f"PnL={pnl_sign}{pos['pnl_pct']}%"
                    )
            else:
                print("  Открытых позиций нет")
        else:
            print("  Нет данных (задай TINVEST_SANDBOX_ACCOUNT_ID в .env)")

    elif "--test-order" in args:
        # Тестовый ордер на 1 лот SBER
        print("\n🧪 Тестовый sandbox-ордер: LONG SBER ×1 лот...")
        result = sandbox_place_order("SBER", "LONG", 1)
        if result:
            print(f"  ✅ Исполнен: {result}")
        else:
            print("  ❌ Ошибка — проверь токен и account_id")

    else:
        # Статус модуля
        tok = _get_token()
        acc = _get_account_id()
        token_ok   = bool(tok and tok != "вставь_токен_сюда")
        sandbox_ok = bool(acc)

        print("\nT-Investments Integration Status")
        print("=" * 45)
        print(f"Токен задан:       {'✅ да' if token_ok else '❌ нет  → задай TINVEST_TOKEN в .env'}")
        sdk_mod = _detect_sdk()
        print(f"SDK установлен:    ", end="")
        if sdk_mod:
            print(f"✅ да  (модуль: {sdk_mod})")
        else:
            print(
                "❌ нет  → установи:\n"
                "         pip install t-tech-investments "
                "--index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple\n"
                "         или запусти диагностику: python check_sdk.py"
            )
        print(f"Sandbox account:   {'✅ ' + acc if sandbox_ok else '⏳ не задан'}")
        print(f"Модуль готов:      {'✅ да' if is_available() else '⏳ нет'}")
        print(f"\nТикеров в FIGI_MAP: {len(FIGI_MAP)}")
        print()
        print("Команды:")
        print("  python tinvest_data.py --create-sandbox   # создать sandbox-счёт")
        print("  python tinvest_data.py --fund-sandbox     # пополнить счёт")
        print("  python tinvest_data.py --portfolio        # показать портфель")
        print("  python tinvest_data.py --test-order       # тестовый ордер")
