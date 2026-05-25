"""
test_bot.py — юнит-тесты для moex_signal_bot.py и moex_bot.py
Запуск: python test_bot.py
Зависимости: только стандартная библиотека + unittest.mock
feedparser мокируется через sys.modules (не нужен для unit-тестов).
"""

import sys
import os
import glob
import json
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from statistics import mean


class _FixedTradingDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


# ─── Путь к ботам ─────────────────────────────────────────────────────────────

BOT_DIR = os.path.join(os.path.dirname(__file__), "mnt", "Desktop", "trader")
sys.path.insert(0, BOT_DIR)

# ─── Мокируем feedparser (не установлен в тест-окружении) ────────────────────

_fake_feedparser = MagicMock()
_fake_feedparser.parse.return_value = MagicMock(entries=[])
sys.modules.setdefault("feedparser", _fake_feedparser)

# ═══════════════════════════════════════════════════════════════════════════════
#  ТЕСТЫ moex_signal_bot.py
# ═══════════════════════════════════════════════════════════════════════════════

import moex_signal_bot as msb


class TestDetectVolumeAnomaly(unittest.TestCase):

    def _make_candles(self, volumes: list[float]) -> list[dict]:
        return [{"value": v, "high": 100.0, "low": 90.0, "close": 95.0, "open": 92.0}
                for v in volumes]

    def test_no_anomaly_normal_volume(self):
        """Объём в 1.5x от среднего — не аномалия (порог 2.0)."""
        candles = self._make_candles([100_000_000.0] * 20 + [150_000_000.0])
        result = msb.detect_volume_anomaly(candles)
        self.assertFalse(result["anomaly"])
        self.assertAlmostEqual(result["ratio"], 1.5, places=1)

    def test_anomaly_detected(self):
        """Объём в 2.5x — аномалия."""
        candles = self._make_candles([100_000_000.0] * 20 + [250_000_000.0])
        result = msb.detect_volume_anomaly(candles)
        self.assertTrue(result["anomaly"])
        self.assertAlmostEqual(result["ratio"], 2.5, places=1)

    def test_anomaly_exact_threshold(self):
        """Ровно 2.0x — на границе, считаем аномалией (>=)."""
        candles = self._make_candles([100_000_000.0] * 20 + [200_000_000.0])
        result = msb.detect_volume_anomaly(candles)
        self.assertTrue(result["anomaly"])

    def test_below_min_volume_rub(self):
        """Объём сильно выше среднего, но в абсолюте < MIN_VOLUME_RUB → не аномалия."""
        candles = self._make_candles([1_000.0] * 20 + [10_000.0])
        result = msb.detect_volume_anomaly(candles)
        self.assertFalse(result["anomaly"])
        self.assertIn("reason", result)

    def test_insufficient_data(self):
        """Меньше 5 свечей — нет данных для анализа."""
        candles = self._make_candles([100_000_000.0] * 3)
        result = msb.detect_volume_anomaly(candles)
        self.assertFalse(result["anomaly"])
        self.assertEqual(result["reason"], "недостаточно данных")

    def test_no_value_field(self):
        """Свечи без поля value → нет данных по объёму."""
        candles = [{"high": 100.0, "low": 90.0} for _ in range(10)]
        result = msb.detect_volume_anomaly(candles)
        self.assertFalse(result["anomaly"])

    def test_custom_threshold(self):
        """Кастомный порог threshold=3.0."""
        candles = self._make_candles([100_000_000.0] * 20 + [250_000_000.0])
        # 2.5x < 3.0x → не аномалия при threshold=3
        result = msb.detect_volume_anomaly(candles, threshold=3.0)
        self.assertFalse(result["anomaly"])


class TestCalcLevels(unittest.TestCase):

    def _make_candles(self, highs, lows, closes):
        return [
            {"high": h, "low": l, "close": c, "open": c - 1.0, "value": 100_000_000.0}
            for h, l, c in zip(highs, lows, closes)
        ]

    def test_basic_levels(self):
        highs  = [110.0, 115.0, 108.0, 112.0, 120.0]
        lows   = [100.0, 102.0,  98.0, 105.0, 110.0]
        closes = [105.0, 110.0, 100.0, 108.0, 115.0]
        levels = msb.calc_levels(self._make_candles(highs, lows, closes))
        self.assertEqual(levels["resistance"], 120.0)
        self.assertEqual(levels["support"],     98.0)
        self.assertEqual(levels["last_close"], 115.0)

    def test_atr_uniform(self):
        """ATR = средний (high−low) = 10 для равномерных свечей."""
        highs  = [110.0, 110.0, 110.0]
        lows   = [100.0, 100.0, 100.0]
        closes = [105.0, 105.0, 105.0]
        levels = msb.calc_levels(self._make_candles(highs, lows, closes))
        self.assertAlmostEqual(levels["atr"], 10.0)

    def test_empty_candles(self):
        self.assertEqual(msb.calc_levels([]), {})


class TestBuildSignal(unittest.TestCase):

    def _anomaly(self, ratio=3.0):
        return {"anomaly": True, "ratio": ratio}

    def test_long_signal_price_near_support(self):
        """Цена 102 при поддержке 100, сопротивлении 200 → LONG."""
        levels = {"support": 100.0, "resistance": 200.0, "atr": 5.0, "last_close": 102.0}
        sig = msb.build_signal("SBER", levels, self._anomaly())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["direction"], "LONG")
        self.assertLess(sig["stop"],  sig["entry"])
        self.assertGreater(sig["take1"], sig["entry"])

    def test_short_signal_price_near_resistance(self):
        """Цена 198 при сопротивлении 200 → SHORT."""
        levels = {"support": 100.0, "resistance": 200.0, "atr": 5.0, "last_close": 198.0}
        sig = msb.build_signal("SBER", levels, self._anomaly())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["direction"], "SHORT")
        self.assertGreater(sig["stop"],  sig["entry"])
        self.assertLess(sig["take1"], sig["entry"])

    def test_no_signal_without_anomaly(self):
        levels = {"support": 100.0, "resistance": 200.0, "atr": 5.0, "last_close": 150.0}
        sig = msb.build_signal("SBER", levels, {"anomaly": False})
        self.assertIsNone(sig)

    def test_rr_ratio(self):
        """LONG: entry=100, stop=92.5 (1.5*5), take2=110 (2*5) → RR≈1.33."""
        levels = {"support": 90.0, "resistance": 200.0, "atr": 5.0, "last_close": 100.0}
        sig = msb.build_signal("X", levels, self._anomaly())
        self.assertEqual(sig["direction"], "LONG")
        self.assertAlmostEqual(sig["rr_ratio"], round(10 / 7.5, 2))

    def test_no_signal_zero_atr(self):
        """ATR=0 → стоп невозможен, сигнал не создаётся."""
        levels = {"support": 100.0, "resistance": 200.0, "atr": 0.0, "last_close": 102.0}
        sig = msb.build_signal("X", levels, self._anomaly())
        self.assertIsNone(sig)


# ═══════════════════════════════════════════════════════════════════════════════
#  ТЕСТЫ moex_bot.py
# ═══════════════════════════════════════════════════════════════════════════════

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")

import moex_bot as mb
import tinvest_data as td
import daily_report as dr


class TestCalcRSI(unittest.TestCase):

    def test_all_gains_returns_high_rsi(self):
        closes = list(range(1, 25))
        rsi = mb.calc_rsi(closes)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 90.0)

    def test_all_losses_returns_low_rsi(self):
        closes = list(range(25, 0, -1))
        rsi = mb.calc_rsi(closes)
        self.assertIsNotNone(rsi)
        self.assertLess(rsi, 10.0)

    def test_flat_returns_100(self):
        """Flat prices: все дельты = 0 → avg_loss=0 → RSI=100."""
        closes = [100.0] * 20
        rsi = mb.calc_rsi(closes)
        self.assertIsNotNone(rsi)
        self.assertEqual(rsi, 100.0)

    def test_insufficient_data_returns_none(self):
        rsi = mb.calc_rsi([100.0] * 5, period=14)
        self.assertIsNone(rsi)

    def test_wilder_ema_not_sma(self):
        """Проверяем что calc_rsi использует EMA Уайлдера, а не SMA."""
        import random
        random.seed(42)
        closes = [100.0]
        for _ in range(50):
            closes.append(closes[-1] + random.uniform(-3, 3))

        rsi_wilders = mb.calc_rsi(closes, period=14)
        self.assertIsNotNone(rsi_wilders)

        # Ручной SMA-RSI для сравнения
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0.0) for d in deltas]
        losses = [abs(min(d, 0.0)) for d in deltas]
        sma_g  = mean(gains[-14:])
        sma_l  = mean(losses[-14:])
        rsi_sma = round(100 - 100 / (1 + sma_g / sma_l), 1) if sma_l else 100.0

        self.assertNotAlmostEqual(
            rsi_wilders, rsi_sma, places=0,
            msg="calc_rsi, похоже, использует SMA вместо EMA Уайлдера"
        )

    def test_result_in_0_100_range(self):
        """RSI всегда в диапазоне 0–100."""
        import random
        random.seed(7)
        for _ in range(10):
            closes = [100.0 + random.uniform(-50, 50) * i * 0.1 for i in range(30)]
            rsi = mb.calc_rsi(closes)
            if rsi is not None:
                self.assertGreaterEqual(rsi, 0.0)
                self.assertLessEqual(rsi, 100.0)


class TestParseNewsAge(unittest.TestCase):

    def test_utc_1h_ago(self):
        from email.utils import format_datetime
        dt  = datetime.now(timezone.utc) - timedelta(hours=1)
        age = mb.parse_news_age(format_datetime(dt))
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 1.0, delta=0.05)

    def test_msk_offset(self):
        """Новость с явным +0300 должна корректно считаться."""
        from email.utils import format_datetime
        msk = timezone(timedelta(hours=3))
        dt  = datetime.now(msk) - timedelta(hours=2)
        age = mb.parse_news_age(format_datetime(dt))
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 2.0, delta=0.1)

    def test_invalid_returns_none(self):
        self.assertIsNone(mb.parse_news_age("not-a-date"))

    def test_future_clamp_to_zero(self):
        """Новость из «будущего» → 0, не отрицательное."""
        from email.utils import format_datetime
        dt  = datetime.now(timezone.utc) + timedelta(minutes=10)
        age = mb.parse_news_age(format_datetime(dt))
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)

    def test_24h_old(self):
        from email.utils import format_datetime
        dt  = datetime.now(timezone.utc) - timedelta(hours=24)
        age = mb.parse_news_age(format_datetime(dt))
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 24.0, delta=0.1)


class TestAssessNewsStatus(unittest.TestCase):
    """
    assess_news_status(item: NewsItem, intraday_data: dict) -> str
    intraday_data = {"TICKER": {"change_pct": float}}
    item.direction: "LONG" / "SHORT" / "NEUTRAL"
    item.tickers:   list[str]
    """

    def _item(self, direction: str, tickers: list[str]) -> mb.NewsItem:
        from email.utils import format_datetime
        published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=3))
        return mb.NewsItem(
            title     = "Тестовый заголовок",
            source    = "test",
            published = published,
            url       = "http://example.com",
            tickers   = tickers,
            direction = direction,
        )

    def test_positive_news_price_rose(self):
        """LONG-новость + цена выросла = Priced In или Active."""
        item   = self._item("LONG", ["SBER"])
        status = mb.assess_news_status(item, {"SBER": {"change_pct": 2.5}})
        self.assertIn(status, (mb.NEWS_STATUS_PRICED, mb.NEWS_STATUS_ACTIVE))

    def test_positive_news_price_fell_rejected(self):
        """LONG-новость, цена упала — рынок отверг."""
        item   = self._item("LONG", ["SBER"])
        status = mb.assess_news_status(item, {"SBER": {"change_pct": -2.5}})
        self.assertEqual(status, mb.NEWS_STATUS_REJECTED)

    def test_negative_news_price_fell(self):
        """SHORT-новость + цена упала = Priced In или Active."""
        item   = self._item("SHORT", ["SBER"])
        status = mb.assess_news_status(item, {"SBER": {"change_pct": -2.5}})
        self.assertIn(status, (mb.NEWS_STATUS_PRICED, mb.NEWS_STATUS_ACTIVE))

    def test_negative_news_price_rose_rejected(self):
        """SHORT-новость, цена выросла — рынок игнорирует."""
        item   = self._item("SHORT", ["SBER"])
        status = mb.assess_news_status(item, {"SBER": {"change_pct": 2.5}})
        self.assertEqual(status, mb.NEWS_STATUS_REJECTED)

    def test_neutral_direction_returns_unknown(self):
        """NEUTRAL-направление → статус неизвестен."""
        item   = self._item("NEUTRAL", ["SBER"])
        status = mb.assess_news_status(item, {"SBER": {"change_pct": 1.0}})
        self.assertEqual(status, mb.NEWS_STATUS_UNKNOWN)

    def test_no_intraday_data_returns_fresh_or_pending(self):
        """Нет внутридневных данных → Fresh или Pending."""
        item   = self._item("LONG", ["SBER"])
        status = mb.assess_news_status(item, {})
        self.assertIn(status, (mb.NEWS_STATUS_FRESH, mb.NEWS_STATUS_PENDING,
                               mb.NEWS_STATUS_UNKNOWN))


class TestIsMoexOpen(unittest.TestCase):
    """
    is_moex_open():
      now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    Мокируем datetime.now() чтобы вернуть UTC-время.
    MSK = UTC + 3h, поэтому UTC = MSK - 3h.
    """

    def _check_msk(self, weekday_offset: int, hour_msk: int, minute_msk: int) -> bool:
        """
        weekday_offset: 0=пн, 1=вт ... 5=сб, 6=вс (от 2026-03-02 = пн)
        Конвертируем MSK → UTC и патчим datetime.now.
        """
        # 2026-03-02 гарантированно понедельник (проверено)
        hour_utc = hour_msk - 3
        day = 2 + weekday_offset
        dt_utc = datetime(2026, 3, day, hour_utc, minute_msk, tzinfo=timezone.utc)

        with patch("moex_bot.datetime") as mock_dt:
            # datetime.now(timezone.utc) → возвращаем нашу UTC-заглушку
            mock_dt.now.return_value = dt_utc
            # Сохраняем конструктор datetime для replace() внутри функции
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return mb.is_moex_open()

    def test_open_midday_monday(self):
        self.assertTrue(self._check_msk(0, 12, 0))

    def test_open_right_at_opening(self):
        self.assertTrue(self._check_msk(0, 9, 50))

    def test_closed_1_minute_before_open(self):
        self.assertFalse(self._check_msk(0, 9, 49))

    def test_closed_morning_9_30(self):
        self.assertFalse(self._check_msk(0, 9, 30))

    def test_closed_evening_19_00(self):
        self.assertFalse(self._check_msk(0, 19, 0))

    def test_closed_right_after_close(self):
        self.assertFalse(self._check_msk(0, 18, 51))

    def test_open_right_at_close_boundary(self):
        self.assertTrue(self._check_msk(0, 18, 50))

    def test_closed_saturday(self):
        self.assertFalse(self._check_msk(5, 12, 0))

    def test_closed_sunday(self):
        self.assertFalse(self._check_msk(6, 12, 0))

    def test_open_friday_afternoon(self):
        self.assertTrue(self._check_msk(4, 15, 0))


class TestIsRelevant(unittest.TestCase):
    """
    is_relevant(item: NewsItem) -> bool
    Проверяет item.title + item.summary на ключевые слова из TICKER_MAP.
    """

    def _item(self, title: str, summary: str = "") -> mb.NewsItem:
        from email.utils import format_datetime
        published = format_datetime(datetime.now(timezone.utc))
        return mb.NewsItem(
            title     = title,
            source    = "test",
            published = published,
            url       = "http://example.com",
            summary   = summary,
        )

    def test_sber_keyword_in_title(self):
        item = self._item("Сбербанк открыл новый офис в Европе")
        self.assertTrue(mb.is_relevant(item))

    def test_gazprom_keyword(self):
        item = self._item("Газпром подписал соглашение о поставках")
        self.assertTrue(mb.is_relevant(item))

    def test_unrelated_headline_false(self):
        """Совсем не относится ни к одному тикеру и ни к одному экономическому слову."""
        item = self._item("Погода в Москве на выходные будет дождливой")
        self.assertFalse(mb.is_relevant(item))

    def test_oil_keyword_relevant(self):
        """Нефть — ключевое слово, должно быть релевантно."""
        item = self._item("Нефть Brent обвалилась до $60 за баррель")
        self.assertTrue(mb.is_relevant(item))

    def test_keyword_in_summary_not_title(self):
        """Ключевое слово в summary, не в title — тоже релевантно."""
        item = self._item("Рынок акций вырос", summary="Сбербанк обновил рекорд котировок")
        self.assertTrue(mb.is_relevant(item))


class TestSynthesizeSignals(unittest.TestCase):
    """
    synthesize_signals(market_signals: list[dict], news_signals: list[NewsItem]) -> list[dict]
    Возвращает список синтезированных сигналов.
    """

    from email.utils import format_datetime as _fmt_dt

    def _market_signal(self, ticker="SBER", direction="LONG",
                       rsi=45.0, vol_ratio=3.0, usdrub_confirm=True):
        return {
            "ticker": ticker, "direction": direction,
            "entry": 300.0, "stop": 290.0,
            "take1": 310.0, "take2": 320.0, "take3": 350.0,
            "rr_ratio": 2.0, "volume_ratio": vol_ratio,
            "rsi": rsi, "ma20": 295.0,
            "usdrub_confirm": usdrub_confirm,
            "usdrub_note": "", "imoex_note": "",
        }

    def _news_item(self, direction="LONG", tickers=None, status=""):
        from email.utils import format_datetime
        if tickers is None:
            tickers = ["SBER"]
        published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
        return mb.NewsItem(
            title     = "Сбербанк отчитался о рекордной прибыли",
            source    = "test",
            published = published,
            url       = "http://example.com",
            tickers   = tickers,
            direction = direction,
            strength  = 3,
            event_type= "EARNINGS",
            status    = status,
        )

    def test_aligned_signals_returns_results(self):
        """Объём + совпадающая новость → список сигналов не пустой."""
        mkt  = [self._market_signal("SBER", "LONG")]
        news = [self._news_item("LONG")]
        result = mb.synthesize_signals(mkt, news)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["ticker"], "SBER")

    def test_conflicting_news_lower_or_different_confidence(self):
        """Противоречивая новость → сигнал выходит с меньшей/другой уверенностью."""
        mkt   = [self._market_signal("SBER", "LONG")]
        news_ok  = [self._news_item("LONG")]
        news_bad = [self._news_item("SHORT")]

        r_ok  = mb.synthesize_signals(mkt, news_ok)
        r_bad = mb.synthesize_signals(mkt, news_bad)

        self.assertIsInstance(r_ok, list)
        self.assertIsInstance(r_bad, list)
        # Оба возвращают результаты, но уверенность разная
        if r_ok and r_bad:
            self.assertNotEqual(r_ok[0].get("confidence"), r_bad[0].get("confidence"))

    def test_sell_the_news_pattern(self):
        """
        SELL THE NEWS: рынок SHORT (объём вниз), но новость LONG (позитивная).
        Классика: «покупай слухи — продавай факты».
        v0.9.5: score-based — без H1 confirm паттерн получает ~7 очков = СРЕДНЯЯ.
        С H1 confirm score вырастет до 9+ = ВЫСОКАЯ.
        Тест: паттерн определён корректно, score ≥ 6 (не СЛАБАЯ).
        """
        mkt  = [self._market_signal("SBER", "SHORT")]   # рынок уже разворачивается
        news = [self._news_item("LONG")]                 # вышла хорошая новость
        result = mb.synthesize_signals(mkt, news)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        r = result[0]
        self.assertEqual(r.get("pattern"), "⚡ SELL THE NEWS",
                         f"Ожидали SELL THE NEWS, получили: {r.get('pattern')}")
        # v0.9.5: score-based confidence — минимальный сигнал даёт СРЕДНЮЮ (≥6 очков)
        self.assertGreaterEqual(r.get("confidence_score", 0), 6,
                                f"Score слишком низкий: {r.get('confidence_score')}")
        self.assertIn(r.get("confidence"), ["🔥🔥 ОТЛИЧНАЯ", "🔥 ВЫСОКАЯ", "🟡 СРЕДНЯЯ"])

    def test_weekly_hard_block_skips_only_one_ticker(self):
        market = [
            self._market_signal("NVTK", "LONG"),
            self._market_signal("SBER", "LONG"),
        ]
        with patch.object(mb, "get_weekly_trend") as mock_weekly, \
             patch.object(mb, "load_news_memory", return_value={}), \
             patch.object(mb, "get_upcoming_event", return_value=None), \
             patch.object(mb, "get_session_quality", return_value=(0, "test session")):
            mock_weekly.side_effect = [
                {"trend": "bear", "weekly_change": -4.2, "ma5_weekly": 100.0, "last_close": 95.8, "candles_count": 5},
                {"trend": "flat", "weekly_change": 0.0, "ma5_weekly": 100.0, "last_close": 100.0, "candles_count": 5},
            ]
            result = mb.synthesize_signals(market, [])

        self.assertEqual(len(result), 1, f"Ожидали, что батч не обнулится: {result}")
        self.assertEqual(result[0]["ticker"], "SBER")

    def test_news_event_gate_allows_strong_ai_event(self):
        news = self._news_item("LONG")
        news.analyzed_by = "ai"
        news.strength = 2
        news.event_type = "EARNINGS"
        news.status = mb.NEWS_STATUS_ACTIVE

        allowed, reason = mb.is_news_event_tradable(news)

        self.assertTrue(allowed)
        self.assertEqual(reason, "news_event_candidate")

    def test_news_event_gate_rejects_resolved_news(self):
        news = self._news_item("LONG")
        news.analyzed_by = "ai"
        news.strength = 3
        news.status = mb.NEWS_STATUS_PRICED

        allowed, reason = mb.is_news_event_tradable(news)

        self.assertFalse(allowed)
        self.assertEqual(reason, "news_event_already_resolved")

    def test_build_news_event_signals_creates_sandbox_signal(self):
        news = self._news_item("LONG")
        news.analyzed_by = "ai"
        news.strength = 3
        news.status = mb.NEWS_STATUS_ACTIVE
        candles = [
            {"open": 295.0, "high": 301.0, "low": 294.0, "close": 296.0, "value": 100_000_000},
            {"open": 296.0, "high": 303.0, "low": 295.0, "close": 301.0, "value": 120_000_000},
        ] * 30
        intraday = {
            "last": 302.0,
            "vwap": 300.0,
            "last_begin": "2026-05-05 12:00:00",
            "change_pct": 1.2,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            with patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "get_candles", return_value=candles), \
                 patch.object(mb, "get_intraday_price", return_value=intraday), \
                 patch.object(mb, "_tinvest_available", return_value=False), \
                 patch.object(mb, "is_moex_open", return_value=False):
                signals = mb.build_news_event_signals([news], {}, {}, set())
                synthesized = mb.synthesize_signals(signals, [news])

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["type"], "NEWS_EVENT")
        self.assertEqual(signals[0]["strategy"], "news_event")
        self.assertGreaterEqual(synthesized[0]["confidence_score"], 9)
        self.assertIn("news_event_signal", synthesized[0]["decision_reasons"])


class TestInstrumentCapabilities(unittest.TestCase):

    def test_mark_sandbox_unavailable_creates_capability_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            blacklist_file = os.path.join(tmpdir, "sandbox_blacklist.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.object(td, "_BLACKLIST_FILE", td._pathlib.Path(blacklist_file)):
                td._INSTRUMENT_CAPABILITIES.clear()
                td._SANDBOX_UNAVAILABLE.clear()

                td.mark_sandbox_unavailable("OZON", reason="50002")

                caps = td.get_instrument_capabilities("OZON")
                self.assertFalse(caps["sandbox_order"])
                degraded = td.list_degraded_instruments()
                self.assertEqual(degraded[0]["ticker"], "OZON")
                self.assertIn("sandbox_order", degraded[0]["capabilities"])
                self.assertTrue(os.path.exists(caps_file))

    def test_capability_ttl_expiry_restores_availability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)):
                td._INSTRUMENT_CAPABILITIES.clear()
                td.mark_instrument_issue("CBOM", "has_figi", "figi_missing", ttl_hours=1)
                self.assertFalse(td.instrument_capability_available("CBOM", "has_figi"))

                expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                td._INSTRUMENT_CAPABILITIES["CBOM"]["has_figi"]["expires_at"] = expired_at

                self.assertTrue(td.instrument_capability_available("CBOM", "has_figi"))

    def test_api_forbidden_sandbox_order_marks_long_capability_block(self):
        class FakeClient:
            def __init__(self, token):
                self.sandbox = self

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post_sandbox_order(self, **kwargs):
                raise Exception("INVALID_ARGUMENT 30052: Instrument forbidden for trading by API")

        class FakeOrderDirection:
            ORDER_DIRECTION_BUY = "BUY"
            ORDER_DIRECTION_SELL = "SELL"

        class FakeOrderType:
            ORDER_TYPE_MARKET = "MARKET"

        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            blacklist_file = os.path.join(tmpdir, "sandbox_blacklist.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.object(td, "_BLACKLIST_FILE", td._pathlib.Path(blacklist_file)), \
                 patch.object(td, "_get_account_id", return_value="sandbox-account"), \
                 patch.object(td, "is_available", return_value=True), \
                 patch.object(td, "get_figi", return_value="BBG000000000"), \
                 patch.object(td, "_get_token", return_value="token"), \
                 patch.object(td, "_sdk_import", return_value=(FakeClient, FakeOrderDirection, FakeOrderType)), \
                 patch.object(td, "find_and_cache_uid") as mock_find_uid:
                td._INSTRUMENT_CAPABILITIES.clear()
                td._SANDBOX_UNAVAILABLE.clear()
                td._UID_CACHE.clear()

                result = td.sandbox_place_order("YDEX", "SHORT", quantity=1)

                self.assertIsNone(result)
                self.assertFalse(td.is_sandbox_available("YDEX"))
                caps = td._INSTRUMENT_CAPABILITIES["YDEX"]["sandbox_order"]
                self.assertEqual(caps["reason"], "api_forbidden_30052")
                self.assertEqual(caps["ttl_hours"], td.SANDBOX_BLACKLIST_TTL_HOURS_50002)
                mock_find_uid.assert_not_called()

    def test_resolve_ticker_from_uid_cache(self):
        with patch.dict(td._UID_CACHE, {"SMLT": "BBG00F6NKQX3"}, clear=True):
            ticker = td.resolve_ticker_from_code("BBG00F6NKQX3")
        self.assertEqual(ticker, "SMLT")

    def test_resolve_ticker_from_static_alias(self):
        self.assertEqual(td.resolve_ticker_from_code("BBG00F6NKQX3"), "SMLT")

    def test_sandbox_order_uses_uid_when_figi_missing(self):
        class FakeResponse:
            order_id = "ord-uid"
            execution_report_status = "done"
            executed_order_price = None

        class FakeClient:
            last_kwargs = None

            def __init__(self, token):
                self.sandbox = self

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post_sandbox_order(self, **kwargs):
                FakeClient.last_kwargs = kwargs
                return FakeResponse()

        class FakeOrderDirection:
            ORDER_DIRECTION_BUY = "BUY"
            ORDER_DIRECTION_SELL = "SELL"

        class FakeOrderType:
            ORDER_TYPE_MARKET = "MARKET"

        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            blacklist_file = os.path.join(tmpdir, "sandbox_blacklist.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.object(td, "_BLACKLIST_FILE", td._pathlib.Path(blacklist_file)), \
                 patch.object(td, "_get_account_id", return_value="sandbox-account"), \
                 patch.object(td, "is_available", return_value=True), \
                 patch.object(td, "_get_token", return_value="token"), \
                 patch.object(td, "_sdk_import", return_value=(FakeClient, FakeOrderDirection, FakeOrderType)), \
                 patch.dict(td.FIGI_MAP, {}, clear=True), \
                 patch.dict(td._UID_CACHE, {"HEAD": "uid-head"}, clear=True):
                td._INSTRUMENT_CAPABILITIES.clear()
                td._SANDBOX_UNAVAILABLE.clear()

                result = td.sandbox_place_order("HEAD", "LONG", quantity=2)

        self.assertEqual(result["order_id"], "ord-uid")
        self.assertEqual(FakeClient.last_kwargs["instrument_id"], "uid-head")
        self.assertNotIn("figi", FakeClient.last_kwargs)

    def test_normalize_executed_order_price_handles_total_order_value(self):
        with patch.object(td, "get_lot_size", return_value=10):
            price = td._normalize_executed_order_price(
                "ROSN",
                raw_price=97_728.0,
                quantity=240,
                expected_price=407.0,
            )

        self.assertAlmostEqual(price, 407.2)

    def test_h1_candles_uses_resolved_uid_without_name_error(self):
        class FakeQuotation:
            units = 100
            nano = 0

        class FakeCandle:
            open = FakeQuotation()
            close = FakeQuotation()
            high = FakeQuotation()
            low = FakeQuotation()
            volume = 10
            time = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)

        class FakeResponse:
            candles = [FakeCandle()]

        class FakeClient:
            last_kwargs = None
            market_data = None

            def __init__(self, token):
                self.market_data = self

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_candles(self, **kwargs):
                FakeClient.last_kwargs = kwargs
                return FakeResponse()

        class FakeInterval:
            CANDLE_INTERVAL_HOUR = "hour"

        with patch.object(td, "is_available", return_value=True), \
             patch.object(td, "instrument_capability_available", return_value=True), \
             patch.object(td, "resolve_instrument_ids", return_value=(None, "uid-head")), \
             patch.object(td, "_sdk_import", return_value=(FakeClient, FakeInterval)), \
             patch.object(td, "_get_token", return_value="token"), \
             patch.object(td, "get_lot_size", return_value=1):
            result = td.get_h1_candles("HEAD")

        self.assertEqual(len(result), 1)
        self.assertEqual(FakeClient.last_kwargs["instrument_id"], "uid-head")
        self.assertNotIn("figi", FakeClient.last_kwargs)

    def test_resolve_instrument_ids_looks_up_uid_when_figi_missing(self):
        with patch.dict(td.FIGI_MAP, {}, clear=True), \
             patch.dict(td._UID_CACHE, {}, clear=True), \
             patch.object(td, "find_and_cache_uid", return_value="uid-posi") as mock_find, \
             patch.object(td, "get_figi") as mock_get_figi:
            figi, uid = td.resolve_instrument_ids("POSI")

        self.assertIsNone(figi)
        self.assertEqual(uid, "uid-posi")
        mock_find.assert_called_once_with("POSI")
        mock_get_figi.assert_not_called()

    def test_resolve_instrument_ids_clears_figi_degradation_when_uid_cached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.dict(td.FIGI_MAP, {}, clear=True), \
                 patch.dict(td._UID_CACHE, {"HEAD": "uid-head"}, clear=True):
                td._INSTRUMENT_CAPABILITIES.clear()
                td.mark_instrument_issue("HEAD", "has_figi", "figi_missing")
                figi, uid = td.resolve_instrument_ids("HEAD")

        self.assertIsNone(figi)
        self.assertEqual(uid, "uid-head")
        self.assertTrue(td.instrument_capability_available("HEAD", "has_figi"))

    def test_get_lot_size_learns_unknown_ticker_from_moex(self):
        with patch.dict(td.LOT_SIZE, {}, clear=True), \
             patch.object(td, "fetch_moex_lot_size", return_value=10):
            self.assertEqual(td.get_lot_size("VKCO"), 10)
            self.assertEqual(td.LOT_SIZE["VKCO"], 10)

    def test_get_lot_size_marks_unknown_when_moex_lookup_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.dict(td.LOT_SIZE, {}, clear=True), \
                 patch.object(td, "fetch_moex_lot_size", return_value=None):
                td._INSTRUMENT_CAPABILITIES.clear()

                self.assertEqual(td.get_lot_size("UNKNOWN"), 1)

                caps = td._INSTRUMENT_CAPABILITIES["UNKNOWN"]["lot_size"]
                self.assertEqual(caps["reason"], "unknown")

    def test_data_layer_missing_figi_does_not_blacklist_sandbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            caps_file = os.path.join(tmpdir, "instrument_capabilities.json")
            blacklist_file = os.path.join(tmpdir, "sandbox_blacklist.json")
            with patch.object(td, "_CAPABILITIES_FILE", td._pathlib.Path(caps_file)), \
                 patch.object(td, "_BLACKLIST_FILE", td._pathlib.Path(blacklist_file)), \
                 patch.dict(td.FIGI_MAP, {}, clear=True), \
                 patch.object(td, "is_available", return_value=True):
                td._INSTRUMENT_CAPABILITIES.clear()
                td._SANDBOX_UNAVAILABLE.clear()
                td._FIGI_MISSING_LOGGED.clear()

                self.assertEqual(td.get_h1_candles("HEAD"), [])

                self.assertTrue(td.is_sandbox_available("HEAD"))
                self.assertNotIn("HEAD", td._SANDBOX_UNAVAILABLE)
                self.assertFalse(td.instrument_capability_available("HEAD", "has_figi"))


class TestDailyReportDiagnostics(unittest.TestCase):

    def _market_signal(self, ticker="SBER", direction="LONG",
                       rsi=45.0, vol_ratio=3.0, usdrub_confirm=True):
        return {
            "ticker": ticker, "direction": direction,
            "entry": 300.0, "stop": 290.0,
            "take1": 310.0, "take2": 320.0, "take3": 350.0,
            "rr_ratio": 2.0, "volume_ratio": vol_ratio,
            "rsi": rsi, "ma20": 295.0,
            "usdrub_confirm": usdrub_confirm,
            "usdrub_note": "", "imoex_note": "",
        }

    def _news_item(self, direction="LONG", tickers=None, status=""):
        from email.utils import format_datetime
        if tickers is None:
            tickers = ["SBER"]
        published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
        return mb.NewsItem(
            title     = "Сбербанк отчитался о рекордной прибыли",
            source    = "test",
            published = published,
            url       = "http://example.com",
            tickers   = tickers,
            direction = direction,
            strength  = 3,
            event_type= "EARNINGS",
            status    = status,
        )

    def test_summarize_decisions_counts_reasons(self):
        summary = dr.summarize_decisions([
            {"action": "skipped", "reason": "sandbox_unavailable"},
            {"action": "skipped", "reason": "sandbox_unavailable"},
            {"action": "blocked", "reason": "weekly_hard_block"},
        ])
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["actions"]["skipped"], 2)
        self.assertEqual(summary["reasons"]["sandbox_unavailable"], 2)

    def test_summarize_opportunities_release_counters(self):
        summary = dr.summarize_opportunities([
            {"action": "executed", "reason": "sandbox_order_filled", "ticker_tier": "tier_1"},
            {"action": "rejected", "reason": "sandbox_order_rejected", "ticker_tier": "tier_2"},
            {"action": "watch_only", "reason": "tier3_watch_only", "ticker_tier": "tier_3"},
        ], [
            {"action": "migrated", "reason": "legacy_state_cleanup"},
        ])
        self.assertEqual(summary["opportunity"], 3)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["watch_only"], 1)
        self.assertEqual(summary["legacy_cleaned"], 1)
        self.assertEqual(summary["tiers"]["tier_3"], 1)

    def test_reconcile_health_ignores_closed_orphan_status(self):
        summary = dr.summarize_reconcile_health({
            "sb_HEAD_orphan": {
                "ticker": "HEAD",
                "execution_status": "orphan_closed",
                "reconcile_status": "orphan",
                "closed_at": "2026-05-04T06:50:23+00:00",
                "close_reason": "reconcile_orphan_auto_close",
            },
            "sb_BBG_orphan": {
                "ticker": "BBG",
                "execution_status": "ghost_closed",
                "reconcile_status": "orphan",
                "closed_at": "2026-05-04T06:50:23+00:00",
                "close_reason": "reconcile_ghost",
            },
        }, [])

        self.assertEqual(summary["orphan"], 0)
        self.assertEqual(summary["ghost_closed"], 1)

    def test_summarize_capabilities_groups_instrument_aliases(self):
        with patch.object(dr, "_list_degraded_instruments", return_value=[
            {
                "ticker": "BBG00F6NKQX3",
                "capabilities": {
                    "sandbox_order": {"reason": "figi_missing"},
                    "has_figi": {"reason": "figi_missing"},
                },
            },
            {
                "ticker": "SMLT",
                "capabilities": {
                    "h1_tinvest": {"reason": "known_fallback"},
                },
            },
        ]):
            summary = dr.summarize_capabilities()

        self.assertEqual(summary["count"], 1)
        self.assertEqual(len(summary["rows"]), 1)
        self.assertIn("SMLT", summary["rows"][0])

    def test_portfolio_mismatch_summary_detects_orphan(self):
        lines, count = dr.portfolio_mismatch_summary({
            "sb_SBER_orphan": {
                "ticker": "SBER",
                "direction": "LONG",
                "lots": 28,
                "price": 324.53,
                "note": "reconcile_orphan — позиция без записи в state",
            },
            "sb_AFLT_SHORT_2026-04-20": {
                "ticker": "AFLT",
                "direction": "SHORT",
                "order_id": "abc",
                "lots": 100,
                "price": 49.96,
            },
        })
        self.assertEqual(count, 1)
        self.assertIn("SBER LONG", lines[0])
        self.assertIn("[orphan]", lines[0])

    def test_signal_state_gap_summary_detects_open_signal_without_sb(self):
        lines, count = dr.signal_state_gap_summary({
            "CBOM_LONG_2026-04-21": {
                "ticker": "CBOM",
                "direction": "LONG",
                "entry": 6.56,
                "hit": None,
            },
            "sb_AFLT_SHORT_2026-04-20": {
                "ticker": "AFLT",
                "direction": "SHORT",
                "order_id": "abc",
                "base_signal_key": "AFLT_SHORT_2026-04-20",
            },
            "AFLT_SHORT_2026-04-20": {
                "ticker": "AFLT",
                "direction": "SHORT",
                "entry": 49.96,
                "hit": None,
            },
        })
        self.assertEqual(count, 1)
        self.assertIn("CBOM LONG", lines[0])
        self.assertIn("[signal_without_sb]", lines[0])

    def test_execution_truth_summary_counts_links_and_gaps(self):
        truth = dr.execution_truth_summary(
            {
                "SBER_LONG_2026-04-23": {"ticker": "SBER", "direction": "LONG", "hit": None},
                "CBOM_LONG_2026-04-21": {"ticker": "CBOM", "direction": "LONG", "hit": None},
                "sb_SBER_LONG_2026-04-23": {
                    "ticker": "SBER",
                    "order_id": "ord1",
                    "base_signal_key": "SBER_LONG_2026-04-23",
                },
                "sb_VTBR_orphan": {
                    "ticker": "VTBR",
                    "direction": "LONG",
                    "order_id": None,
                },
            },
            [
                {"signal_id": "SBER_LONG_2026-04-23", "execution_status": "filled"},
                {"signal_id": "CBOM_LONG_2026-04-21", "execution_status": "signaled"},
            ],
        )
        self.assertEqual(truth["sb_open"], 2)
        self.assertEqual(truth["sb_orphan"], 1)
        self.assertEqual(truth["base_open"], 2)
        self.assertEqual(truth["base_with_sb"], 1)
        self.assertEqual(truth["base_without_sb"], 1)

    def test_strategy_results_aggregate_by_pattern(self):
        summary = dr.summarize_strategy_results([
            {"pattern": "⚡ SELL THE NEWS", "executed": True, "pnl_pct": 1.2},
            {"pattern": "⚡ SELL THE NEWS", "executed": True, "pnl_pct": -0.7},
            {"pattern": "BREAKOUT", "executed": False, "pnl_pct": None},
            {"pattern": "BREAKOUT", "execution_status": "virtual", "pnl_pct": 5.0},
        ])
        rows = {row["pattern"]: row for row in summary["patterns"]}
        self.assertEqual(rows["⚡ SELL THE NEWS"]["signals"], 2)
        self.assertEqual(rows["⚡ SELL THE NEWS"]["closed"], 2)
        self.assertEqual(rows["⚡ SELL THE NEWS"]["winrate"], 50.0)
        self.assertEqual(rows["BREAKOUT"]["executed"], 0)
        self.assertEqual(rows["BREAKOUT"]["closed"], 0)
        self.assertEqual(rows["BREAKOUT"]["total_pnl"], 0.0)

    def test_strategy_results_exclude_anomalous_pnl(self):
        summary = dr.summarize_strategy_results([
            {"pattern": "H1", "executed": True, "pnl_pct": 2.0},
            {"pattern": "H1", "executed": True, "pnl_pct": -63.99},
        ])
        row = {row["pattern"]: row for row in summary["patterns"]}["H1"]

        self.assertEqual(row["closed"], 1)
        self.assertEqual(row["total_pnl"], 2.0)
        self.assertEqual(row["pnl_anomaly"], 1)

    def test_network_and_uid_summaries_parse_log_lines(self):
        lines = [
            "2026-04-23 12:00:00 [WARNING] moex_bot: get_candles(SBER): transient network error, retry 1/2: timeout",
            "2026-04-23 12:01:00 [ERROR] moex_bot: get_candles(SBER): Max retries exceeded",
            "2026-04-23 12:02:00 [INFO] tinvest_data: [UID] CBOM → uid=abc123 (FIGI=old) — кэшировано",
            "2026-04-23 12:03:00 [INFO] tinvest_data: [SANDBOX] LONG CBOM ×1л → xyz [via UID retry — FIGI устарел, UID закэширован]",
        ]
        net = dr.summarize_network_resilience(lines)
        uid = dr.summarize_uid_fallback(lines)
        self.assertEqual(net["moex_request_retry"], 1)
        self.assertEqual(net["moex_request_failed"], 1)
        self.assertEqual(uid["uid_cache_success"], 1)
        self.assertEqual(uid["uid_retry_success"], 1)

    def test_stop_execution_quality_tracks_adverse_slippage(self):
        summary = dr.summarize_stop_execution_quality([
            {
                "signal_id": "AFLT_SHORT_2026-05-08",
                "ticker": "AFLT",
                "direction": "SHORT",
                "result": "loss",
                "stop": 48.14,
                "exit_price": 48.56,
                "exit_time": "2026-05-12 09:50",
                "executed": True,
            },
            {
                "signal_id": "SBER_LONG_2026-05-08",
                "ticker": "SBER",
                "direction": "LONG",
                "result": "loss",
                "stop": 320.0,
                "exit_price": 319.2,
                "exit_time": "2026-05-12 10:10",
                "executed": True,
            },
            {
                "signal_id": "MOEX_LONG_2026-05-12",
                "ticker": "MOEX",
                "direction": "LONG",
                "result": None,
                "stop": 169.2,
                "exit_price": None,
                "executed": True,
            },
        ], date_str="2026-05-12")

        self.assertEqual(summary["stop_loss_trades"], 2)
        self.assertEqual(summary["slipped_stop_exits"], 2)
        self.assertEqual(summary["rows"][0]["ticker"], "AFLT")
        self.assertEqual(summary["rows"][0]["slippage_abs"], 0.42)
        self.assertEqual(summary["rows"][1]["ticker"], "SBER")
        self.assertEqual(summary["max_slippage_pct"], 0.872)

    def test_format_trade_opened_marks_phantom(self):
        line = dr.format_trade_opened({
            "ticker": "SMLT",
            "direction": "SHORT",
            "entry": 632.6,
            "result": None,
            "pnl_pct": None,
            "executed": False,
        })
        self.assertIn("[phantom]", line)

    def test_format_trade_closed_marks_phantom(self):
        line = dr.format_trade_closed({
            "ticker": "CHMF",
            "direction": "SHORT",
            "entry": 801.4,
            "exit_price": 795.0,
            "pnl_pct": 0.8,
            "result": "win_t2",
            "execution_status": "virtual",
        })
        self.assertIn("[phantom]", line)

    def test_is_executed_treats_none_as_legacy_real(self):
        self.assertTrue(dr._is_executed({"executed": None}))
        self.assertFalse(dr._is_executed({"execution_status": "virtual", "executed": True}))

    def test_signal_gap_uses_instrument_alias(self):
        lines, count = dr.signal_state_gap_summary({
            "SMLT_SHORT_2026-04-24_v2": {
                "ticker": "SMLT",
                "direction": "SHORT",
                "entry": 632.6,
                "hit": None,
            },
            "sb_BBG00F6NKQX3_orphan": {
                "ticker": "BBG00F6NKQX3",
                "direction": "SHORT",
                "lots": 156,
                "price": 616.6,
            },
        })
        self.assertEqual(count, 0, lines)

    def test_signal_gap_ignores_rejected_order_signal(self):
        lines, count = dr.signal_state_gap_summary({
            "VTBR_SHORT_2026-04-29": {
                "ticker": "VTBR",
                "direction": "SHORT",
                "entry": 87.8,
                "hit": None,
                "execution_status": "rejected",
                "order_reject_reason": "sandbox_place_order_failed",
            },
        })
        self.assertEqual(count, 0, lines)

    def test_no_market_signals_returns_empty(self):
        news = [self._news_item("LONG")]
        result = mb.synthesize_signals([], news)
        self.assertEqual(result, [])

    def test_no_news_signals_still_returns_results(self):
        """Без новостей — сигнал всё равно формируется (только объём)."""
        mkt = [self._market_signal("SBER", "LONG")]
        result = mb.synthesize_signals(mkt, [])
        self.assertIsInstance(result, list)
        # Должен быть сигнал с пометкой "только объём"
        self.assertGreater(len(result), 0)

    def test_unrelated_ticker_news_not_matched(self):
        """Новость по LKOH не влияет на сигнал по SBER."""
        mkt  = [self._market_signal("SBER", "LONG")]
        news = [self._news_item("SHORT", tickers=["LKOH"])]
        result_no_news  = mb.synthesize_signals(mkt, [])
        result_with_lkoh = mb.synthesize_signals(mkt, news)
        # Уверенность не должна упасть из-за чужой новости
        if result_no_news and result_with_lkoh:
            self.assertEqual(
                result_no_news[0].get("confidence"),
                result_with_lkoh[0].get("confidence")
            )

    def test_csv_env_list_normalizes_tickers(self):
        with patch.dict(os.environ, {"EXTRA_TICKERS": " vkco, belu ,, "}, clear=False):
            self.assertEqual(mb._csv_env_list("EXTRA_TICKERS"), ["VKCO", "BELU"])


class TestStateResilience(unittest.TestCase):

    def test_load_signals_state_recovers_corrupt_json_with_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = os.path.join(tmpdir, "signals_state.json")
            with open(bad_path, "w", encoding="utf-8") as f:
                f.write("{broken json")

            with patch.object(mb, "SIGNALS_STATE_FILE", bad_path):
                state = mb.load_signals_state()

            self.assertEqual(state, {})
            backups = glob.glob(bad_path + ".*.corrupt")
            self.assertTrue(backups, "Ожидали backup повреждённого state-файла")

    def test_save_and_load_trade_log_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "trade_log.json")
            payload = [{"signal_id": "SBER_LONG_2026-04-22", "ticker": "SBER"}]
            with patch.object(mb, "TRADE_LOG_FILE", log_path):
                mb.save_trade_log(payload)
                loaded = mb.load_trade_log()
            self.assertEqual(loaded, payload)

    def test_state_migration_removes_legacy_unexecuted_records_with_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "signals_state.json")
            trade_path = os.path.join(tmpdir, "trade_log.json")
            migrations_path = os.path.join(tmpdir, "state_migrations.json")
            decision_path = os.path.join(tmpdir, "decisions.jsonl")
            stale_sent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            state = {
                "OLD_LONG_2026-04-01": {
                    "ticker": "OLD",
                    "direction": "LONG",
                    "entry": 10.0,
                    "hit": None,
                },
                "SBER_LONG_2026-04-01": {
                    "ticker": "SBER",
                    "direction": "LONG",
                    "entry": 300.0,
                    "hit": None,
                },
                "sb_SBER_LONG_2026-04-01": {
                    "ticker": "SBER",
                    "direction": "LONG",
                    "order_id": "ord-1",
                },
                "ntg_old": {"sent": stale_sent},
            }
            trade_log = [
                {"signal_id": "OLD_LONG_2026-04-01", "ticker": "OLD"},
                {"signal_id": "SBER_LONG_2026-04-01", "ticker": "SBER", "execution_status": "filled"},
            ]
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            with open(trade_path, "w", encoding="utf-8") as f:
                json.dump(trade_log, f)

            with patch.object(mb, "SIGNALS_STATE_FILE", state_path), \
                 patch.object(mb, "TRADE_LOG_FILE", trade_path), \
                 patch.object(mb, "STATE_MIGRATIONS_FILE", migrations_path), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_path):
                summary = mb.run_state_migrations(force=True)

            migrated_state = json.loads(open(state_path, encoding="utf-8").read())
            migrated_trades = json.loads(open(trade_path, encoding="utf-8").read())
            migrations = json.loads(open(migrations_path, encoding="utf-8").read())

            self.assertEqual(summary["state_removed"], 1)
            self.assertEqual(summary["trade_removed"], 1)
            self.assertEqual(summary["ntg_removed"], 1)
            self.assertNotIn("OLD_LONG_2026-04-01", migrated_state)
            self.assertNotIn("ntg_old", migrated_state)
            self.assertEqual(migrated_state["SBER_LONG_2026-04-01"]["execution_status"], "filled")
            self.assertEqual(migrated_state["sb_SBER_LONG_2026-04-01"]["base_signal_key"], "SBER_LONG_2026-04-01")
            self.assertEqual(len(migrated_trades), 1)
            self.assertEqual(migrated_trades[0]["signal_id"], "SBER_LONG_2026-04-01")
            self.assertIn(mb.STATE_MIGRATION_VERSION, migrations)
            backups = os.listdir(os.path.join(tmpdir, "state_backups"))
            self.assertTrue(any(name.startswith("signals_state.json.") for name in backups))
            self.assertTrue(any(name.startswith("trade_log.json.") for name in backups))

    def test_state_migration_repairs_aggregate_close_price(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "signals_state.json")
            trade_path = os.path.join(tmpdir, "trade_log.json")
            migrations_path = os.path.join(tmpdir, "state_migrations.json")
            decision_path = os.path.join(tmpdir, "decisions.jsonl")
            state = {
                "sb_ROSN_SHORT_2026-05-06": {
                    "ticker": "ROSN",
                    "direction": "SHORT",
                    "order_id": "ord-rosn",
                    "lots": 240,
                    "close_price": 97_728.0,
                    "execution_status": "closed",
                    "base_signal_key": "ROSN_SHORT_2026-05-06",
                },
            }
            trade_log = [{
                "signal_id": "ROSN_SHORT_2026-05-06",
                "ticker": "ROSN",
                "direction": "SHORT",
                "entry": 421.0,
                "exit_price": 97_728.0,
                "pnl_pct": -23113.3,
                "execution_status": "closed",
            }]
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            with open(trade_path, "w", encoding="utf-8") as f:
                json.dump(trade_log, f)

            with patch.object(mb, "SIGNALS_STATE_FILE", state_path), \
                 patch.object(mb, "TRADE_LOG_FILE", trade_path), \
                 patch.object(mb, "STATE_MIGRATIONS_FILE", migrations_path), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_path):
                summary = mb.run_state_migrations(force=True)

            migrated_state = json.loads(open(state_path, encoding="utf-8").read())
            migrated_trades = json.loads(open(trade_path, encoding="utf-8").read())
            self.assertEqual(summary["price_repaired"], 1)
            self.assertEqual(migrated_trades[0]["exit_price"], 407.2)
            self.assertEqual(migrated_trades[0]["pnl_pct"], 3.28)
            self.assertEqual(migrated_state["sb_ROSN_SHORT_2026-05-06"]["close_price"], 407.2)
            self.assertEqual(migrated_state["sb_ROSN_SHORT_2026-05-06"]["close_price_raw"], 97_728.0)

    def test_state_migration_repairs_lot_size_aggregate_close_price(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "signals_state.json")
            trade_path = os.path.join(tmpdir, "trade_log.json")
            migrations_path = os.path.join(tmpdir, "state_migrations.json")
            decision_path = os.path.join(tmpdir, "decisions.jsonl")
            state = {
                "sb_ALRS_LONG_2026-05-07": {
                    "ticker": "ALRS",
                    "direction": "LONG",
                    "order_id": "ord-alrs",
                    "lots": 353,
                    "close_price": 99_051.8,
                    "execution_status": "closed",
                    "base_signal_key": "ALRS_LONG_2026-05-07",
                },
            }
            trade_log = [{
                "signal_id": "ALRS_LONG_2026-05-07",
                "ticker": "ALRS",
                "direction": "LONG",
                "entry": 28.84,
                "exit_price": 99_051.8,
                "pnl_pct": 343352.84,
                "execution_status": "closed",
            }]
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            with open(trade_path, "w", encoding="utf-8") as f:
                json.dump(trade_log, f)

            with patch.object(mb, "SIGNALS_STATE_FILE", state_path), \
                 patch.object(mb, "TRADE_LOG_FILE", trade_path), \
                 patch.object(mb, "STATE_MIGRATIONS_FILE", migrations_path), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_path):
                summary = mb.run_state_migrations(force=True)

            migrated_state = json.loads(open(state_path, encoding="utf-8").read())
            migrated_trades = json.loads(open(trade_path, encoding="utf-8").read())
            self.assertEqual(summary["price_repaired"], 1)
            self.assertEqual(migrated_trades[0]["exit_price"], 28.06)
            self.assertEqual(migrated_trades[0]["pnl_pct"], -2.7)
            self.assertEqual(migrated_state["sb_ALRS_LONG_2026-05-07"]["close_price"], 28.06)
            self.assertEqual(migrated_state["sb_ALRS_LONG_2026-05-07"]["close_price_raw"], 99_051.8)


class TestMoexNetworkRetry(unittest.TestCase):

    def test_get_candles_recovers_after_transient_error(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "candles": {
                "columns": ["open", "close", "high", "low", "value", "begin"],
                "data": [[100.0, 101.0, 102.0, 99.0, 1_000_000.0, "2026-04-23T10:00:00+03:00"]],
            }
        }
        with patch("moex_bot.requests.get", side_effect=[
            mb.requests.exceptions.ConnectTimeout("timeout"),
            response,
        ]), patch("moex_bot.time.sleep"):
            candles = mb.get_candles("SBER", days=1)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["close"], 101.0)


class TestReconcileLinking(unittest.TestCase):

    def test_reconcile_links_orphan_to_single_open_signal(self):
        state = {
            "SBER_LONG_2026-04-23": {
                "ticker": "SBER",
                "direction": "LONG",
                "entry": 320.0,
                "hit": None,
            }
        }
        portfolio = {
            "positions": [
                {"ticker": "SBER", "quantity": 28, "avg_price": 322.72, "curr_price": 324.53}
            ]
        }
        fake_tinvest = MagicMock()
        fake_tinvest.get_sandbox_portfolio.return_value = portfolio

        with patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
             patch.object(mb, "_tinvest_available", return_value=True), \
             patch.object(mb, "_tinvest", fake_tinvest), \
             patch.object(mb, "save_signals_state"), \
             patch.object(mb, "append_decision_log"):
            mb.reconcile_sandbox_state(state)

        self.assertIn("sb_SBER_LONG_2026-04-23", state)
        self.assertEqual(state["sb_SBER_LONG_2026-04-23"]["base_signal_key"], "SBER_LONG_2026-04-23")
        self.assertEqual(state["sb_SBER_LONG_2026-04-23"]["reconcile_status"], "linked_orphan")

    def test_reconcile_auto_closes_unlinked_orphan(self):
        state = {
            "sb_SBER_orphan": {
                "ticker": "SBER",
                "direction": "LONG",
                "entry": 322.72,
                "price": 320.0,
                "lots": 28,
                "execution_status": "orphan",
            }
        }
        portfolio = {
            "positions": [
                {"ticker": "SBER", "quantity": 28, "avg_price": 322.72, "curr_price": 320.0}
            ]
        }
        fake_tinvest = MagicMock()
        fake_tinvest.get_sandbox_portfolio.return_value = portfolio
        fake_tinvest.sandbox_close_position.return_value = {
            "order_id": "close-1",
            "price": 320.0,
        }

        with patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
             patch.object(mb, "SANDBOX_ORPHAN_POLICY", "close"), \
             patch.object(mb, "_tinvest_available", return_value=True), \
             patch.object(mb, "_tinvest", fake_tinvest), \
             patch.object(mb, "save_signals_state") as mock_save, \
             patch.object(mb, "append_decision_log") as mock_decision:
            mb.reconcile_sandbox_state(state)

        fake_tinvest.sandbox_close_position.assert_called_once_with("SBER")
        self.assertEqual(state["sb_SBER_orphan"]["execution_status"], "orphan_closed")
        self.assertEqual(state["sb_SBER_orphan"]["close_reason"], "reconcile_orphan_auto_close")
        self.assertEqual(state["sb_SBER_orphan"]["close_order_id"], "close-1")
        mock_save.assert_called_once()
        self.assertTrue(any(
            call.args[0].get("reason") == "reconcile_orphan_auto_close"
            for call in mock_decision.call_args_list
        ))


class TestExecutionRecording(unittest.TestCase):

    def _signal(self):
        return {
            "ticker": "SBER",
            "direction": "LONG",
            "entry": 300.0,
            "stop": 297.0,
            "take1": 303.0,
            "take2": 306.0,
            "confidence": "🔥🔥 ОТЛИЧНАЯ",
            "confidence_score": 12,
            "volume_ratio": 2.5,
            "rr": 2.0,
            "signal_version": "test",
            "decision_reasons": [],
            "data_quality_flags": [],
        }

    def test_sandbox_fill_creates_trade_log_and_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            fake_tinvest = MagicMock()
            fake_tinvest.get_sandbox_portfolio.return_value = {"total_amount_rub": 1_000_000, "positions": []}
            fake_tinvest.sandbox_place_order.return_value = {
                "order_id": "ord-1",
                "lots": 10,
                "price": 300.0,
            }
            state = {}

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "SANDBOX_MAX_TICKER_PCT", 100.0), \
                 patch.object(mb, "SANDBOX_MAX_TOTAL_PCT", 100.0), \
                 patch.object(mb, "datetime", _FixedTradingDateTime), \
                 patch.object(mb, "_tinvest_available", return_value=True), \
                 patch.object(mb, "_tinvest", fake_tinvest):
                placed = mb.sandbox_execute_signals([self._signal()], state)

            key = "SBER_LONG_2026-04-29"
            self.assertEqual(placed, 1)
            self.assertIn(key, state)
            self.assertIn(f"sb_{key}", state)
            self.assertEqual(state[key]["execution_status"], "filled")
            self.assertEqual(state[f"sb_{key}"]["order_id"], "ord-1")
            log = json.loads(open(trade_file, encoding="utf-8").read())
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]["signal_id"], key)
            self.assertEqual(log[0]["execution_status"], "filled")
            self.assertEqual(log[0]["sb_order_id"], "ord-1")

    def test_sandbox_order_failure_does_not_create_trade_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            fake_tinvest = MagicMock()
            fake_tinvest.get_sandbox_portfolio.return_value = {"total_amount_rub": 1_000_000, "positions": []}
            fake_tinvest.sandbox_place_order.return_value = None
            state = {}

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "SANDBOX_MAX_TICKER_PCT", 100.0), \
                 patch.object(mb, "SANDBOX_MAX_TOTAL_PCT", 100.0), \
                 patch.object(mb, "datetime", _FixedTradingDateTime), \
                 patch.object(mb, "_tinvest_available", return_value=True), \
                 patch.object(mb, "_tinvest", fake_tinvest):
                placed = mb.sandbox_execute_signals([self._signal()], state)

            self.assertEqual(placed, 0)
            self.assertEqual(state, {})
            self.assertFalse(os.path.exists(trade_file))
            score_lines = open(score_file, encoding="utf-8").read()
            self.assertIn("sandbox_order_rejected", score_lines)

    def test_tier3_signal_is_watch_only_without_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            fake_tinvest = MagicMock()
            fake_tinvest.get_sandbox_portfolio.return_value = {"total_amount_rub": 1_000_000, "positions": []}
            state = {}
            signal = self._signal()
            signal["ticker"] = "TIER3"

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "datetime", _FixedTradingDateTime), \
                 patch.object(mb, "_tinvest_available", return_value=True), \
                 patch.object(mb, "_tinvest", fake_tinvest), \
                 patch.object(mb, "ticker_tier", return_value="tier_3"):
                placed = mb.sandbox_execute_signals([signal], state)

            self.assertEqual(placed, 0)
            fake_tinvest.sandbox_place_order.assert_not_called()
            self.assertEqual(state, {})
            self.assertIn("tier3_watch_only", open(score_file, encoding="utf-8").read())
            self.assertIn("tier3_watch_only", open(opportunity_file, encoding="utf-8").read())

    def test_daily_stop_loss_brake_counts_real_losses_today(self):
        active, losses = mb.check_daily_stop_loss_brake([
            {
                "signal_id": "A",
                "result": "loss",
                "pnl_pct": -1.0,
                "exit_time": "2026-05-15 11:00",
                "executed": True,
            },
            {
                "signal_id": "B",
                "result": "win_t2",
                "pnl_pct": 1.5,
                "exit_time": "2026-05-15 12:00",
                "executed": True,
            },
            {
                "signal_id": "C",
                "result": "loss",
                "pnl_pct": -0.4,
                "exit_time": "2026-05-15 13:00",
                "execution_status": "virtual",
                "executed": False,
            },
            {
                "signal_id": "D",
                "result": "loss",
                "pnl_pct": -0.8,
                "exit_time": "2026-05-15 14:00",
                "executed": True,
            },
            {
                "signal_id": "OLD",
                "result": "loss",
                "pnl_pct": -1.2,
                "exit_time": "2026-05-14 14:00",
                "executed": True,
            },
        ], "2026-05-15", max_losses=2)

        self.assertTrue(active)
        self.assertEqual(losses, 2)

    def test_daily_stop_loss_brake_can_be_disabled(self):
        active, losses = mb.check_daily_stop_loss_brake([
            {
                "signal_id": "A",
                "result": "loss",
                "pnl_pct": -1.0,
                "exit_time": "2026-05-15 11:00",
                "executed": True,
            },
        ], "2026-05-15", max_losses=0)

        self.assertFalse(active)
        self.assertEqual(losses, 0)

    def test_daily_new_order_count_ignores_virtual_trades(self):
        count = mb.count_daily_new_orders([
            {"signal_id": "A", "date": "2026-05-18", "executed": True},
            {"signal_id": "B", "date": "2026-05-18", "execution_status": "virtual", "executed": False},
            {"signal_id": "C", "date": "2026-05-17", "executed": True},
        ], "2026-05-18")

        self.assertEqual(count, 1)

    def test_daily_ticker_loss_cooldown_detects_same_ticker_loss(self):
        self.assertTrue(mb.has_daily_ticker_loss([
            {
                "ticker": "GAZP",
                "result": "loss",
                "pnl_pct": -2.94,
                "exit_time": "2026-05-18 18:36",
                "executed": True,
            },
        ], "2026-05-18", "GAZP"))
        self.assertFalse(mb.has_daily_ticker_loss([
            {
                "ticker": "GAZP",
                "result": "loss",
                "pnl_pct": -2.94,
                "exit_time": "2026-05-17 18:36",
                "executed": True,
            },
        ], "2026-05-18", "GAZP"))

    def test_strategy_performance_guard_blocks_negative_edge(self):
        reason, meta = mb.strategy_performance_guard_reason(
            {"strategy": "news_event", "confidence_score": 21},
            [
                {"strategy": "news_event", "pnl_pct": -1.0, "exit_time": "2026-05-10", "executed": True},
                {"strategy": "news_event", "pnl_pct": -0.8, "exit_time": "2026-05-11", "executed": True},
                {"strategy": "news_event", "pnl_pct": 0.4, "exit_time": "2026-05-12", "executed": True},
                {"strategy": "news_event", "pnl_pct": -1.2, "exit_time": "2026-05-13", "executed": True},
                {"strategy": "news_event", "pnl_pct": -0.3, "exit_time": "2026-05-14", "executed": True},
                {"strategy": "index_rebound", "pnl_pct": -5.0, "exit_time": "2026-05-14", "executed": True},
            ],
            min_trades=5,
            min_pnl=0.0,
            min_winrate=45.0,
        )

        self.assertEqual(reason, "strategy_perf_guard")
        self.assertEqual(meta["strategy_label"], "news_event")
        self.assertEqual(meta["closed"], 5)

    def test_strategy_performance_guard_allows_small_sample(self):
        reason, meta = mb.strategy_performance_guard_reason(
            {"strategy": "news_event", "confidence_score": 21},
            [
                {"strategy": "news_event", "pnl_pct": -1.0, "exit_time": "2026-05-10", "executed": True},
                {"strategy": "news_event", "pnl_pct": -0.8, "exit_time": "2026-05-11", "executed": True},
            ],
            min_trades=5,
        )

        self.assertIsNone(reason)
        self.assertEqual(meta["closed"], 2)

    def test_recent_performance_guard_excludes_anomalous_pnl(self):
        trades = [{"ticker": "MTSS", "pnl_pct": -63.99, "exit_time": "2026-05-22", "executed": True}]
        trades.extend(
            {"ticker": f"T{i}", "pnl_pct": pnl, "exit_time": f"2026-05-{10+i:02d}", "executed": True}
            for i, pnl in enumerate([1.0, -0.5, 0.7, -0.4, 0.6])
        )
        active, meta = mb.recent_performance_guard(trades, min_trades=5, min_pnl=0.0, min_winrate=45.0)

        self.assertFalse(active)
        self.assertEqual(meta["closed"], 5)

    def test_weekly_target_status_uses_rub_and_excludes_anomalies(self):
        status = mb.weekly_target_status([
            {
                "ticker": "ROSN",
                "pnl_pct": 2.91,
                "position_rub": 100000,
                "exit_time": "2026-05-25 14:17",
                "executed": True,
            },
            {
                "ticker": "MTSS",
                "pnl_pct": -63.99,
                "position_rub": 100000,
                "exit_time": "2026-05-25 11:46",
                "executed": True,
            },
        ], now=datetime(2026, 5, 25, 19, 0, tzinfo=timezone.utc))

        self.assertEqual(status["closed"], 1)
        self.assertEqual(status["realized_rub"], 2910.0)
        self.assertEqual(status["anomalies_excluded"], 1)

    def test_close_failure_does_not_mark_trade_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            fake_tinvest = MagicMock()
            fake_tinvest.sandbox_close_position.return_value = None
            key = "SBER_LONG_2026-04-29"
            state = {
                key: {
                    **self._signal(),
                    "hit": None,
                    "opened_at": "2026-04-29T09:00:00+00:00",
                },
                f"sb_{key}": {
                    "ticker": "SBER",
                    "direction": "LONG",
                    "order_id": "ord-1",
                    "lots": 1,
                    "opened_at": "2026-04-29T09:00:00+00:00",
                    "execution_status": "filled",
                },
            }
            with open(trade_file, "w", encoding="utf-8") as f:
                json.dump([{
                    "signal_id": key,
                    "ticker": "SBER",
                    "direction": "LONG",
                    "entry": 300.0,
                    "result": "open",
                    "execution_status": "filled",
                    "executed": True,
                }], f)

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "TELEGRAM_TOKEN", "token"), \
                 patch.object(mb, "TELEGRAM_CHAT_ID", "chat"), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "load_signals_state", return_value=state), \
                 patch.object(mb, "save_signals_state"), \
                 patch.object(mb, "_tinvest_available", return_value=True), \
                 patch.object(mb, "_tinvest", fake_tinvest), \
                 patch.object(mb, "tg_send", return_value=True):
                mb.tg_notify_run([], [], {"SBER": {"last": 296.0}}, {})

            log = json.loads(open(trade_file, encoding="utf-8").read())
            self.assertEqual(log[0]["result"], "open")
            self.assertIsNone(state[key]["hit"])
            self.assertEqual(state[key]["close_pending_reason"], "stop_hit")
            self.assertIn("sandbox_close_rejected", open(score_file, encoding="utf-8").read())

    def test_close_success_marks_trade_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            fake_tinvest = MagicMock()
            fake_tinvest.sandbox_close_position.return_value = {"price": 296.0}
            key = "SBER_LONG_2026-04-29"
            state = {
                key: {
                    **self._signal(),
                    "hit": None,
                    "opened_at": "2026-04-29T09:00:00+00:00",
                },
                f"sb_{key}": {
                    "ticker": "SBER",
                    "direction": "LONG",
                    "order_id": "ord-1",
                    "lots": 1,
                    "opened_at": "2026-04-29T09:00:00+00:00",
                    "execution_status": "filled",
                },
            }
            with open(trade_file, "w", encoding="utf-8") as f:
                json.dump([{
                    "signal_id": key,
                    "ticker": "SBER",
                    "direction": "LONG",
                    "entry": 300.0,
                    "result": "open",
                    "execution_status": "filled",
                    "executed": True,
                }], f)

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "TELEGRAM_TOKEN", "token"), \
                 patch.object(mb, "TELEGRAM_CHAT_ID", "chat"), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "load_signals_state", return_value=state), \
                 patch.object(mb, "save_signals_state"), \
                 patch.object(mb, "_tinvest_available", return_value=True), \
                 patch.object(mb, "_tinvest", fake_tinvest), \
                 patch.object(mb, "tg_send", return_value=True):
                mb.tg_notify_run([], [], {"SBER": {"last": 296.0}}, {})

            log = json.loads(open(trade_file, encoding="utf-8").read())
            self.assertEqual(log[0]["result"], "loss")
            self.assertEqual(log[0]["execution_status"], "closed")
            self.assertEqual(state[f"sb_{key}"]["execution_status"], "closed")

    def test_auto_order_mode_does_not_record_telegram_signal_as_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_file = os.path.join(tmpdir, "trade_log.json")
            score_file = os.path.join(tmpdir, "score.jsonl")
            decision_file = os.path.join(tmpdir, "decision.jsonl")
            state: dict = {}

            with patch.object(mb, "TRADE_LOG_FILE", trade_file), \
                 patch.object(mb, "SCORE_LOG_FILE", score_file), \
                 patch.object(mb, "DECISION_LOG_FILE", decision_file), \
                 patch.object(mb, "TELEGRAM_TOKEN", "token"), \
                 patch.object(mb, "TELEGRAM_CHAT_ID", "chat"), \
                 patch.object(mb, "SANDBOX_AUTO_ORDER", True), \
                 patch.object(mb, "datetime", _FixedTradingDateTime), \
                 patch.object(mb, "load_signals_state", return_value=state), \
                 patch.object(mb, "save_signals_state") as mock_save, \
                 patch.object(mb, "tg_send") as mock_tg_send:
                mb.tg_notify_run([self._signal()], [], {}, {})

            self.assertFalse(mock_tg_send.called)
            self.assertFalse(os.path.exists(trade_file))
            self.assertEqual(state, {})
            mock_save.assert_called_once()
            score_lines = open(score_file, encoding="utf-8").read()
            self.assertIn("auto_order_controls_entry", score_lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  ТЕСТЫ v0.9.3 — check_h1_confirmation, H1 watch helpers, MA50 hard filter
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckH1Confirmation(unittest.TestCase):
    """check_h1_confirmation() — логика подтверждения H1-свечой."""

    D1_LEVELS = {"support": 100.0, "resistance": 110.0}

    def _candle(self, open_, close, high, low):
        return {"open": open_, "close": close, "high": high, "low": low}

    # ── LONG ──────────────────────────────────────────────────────────────────

    # Вспомогательная: список из 2 свечей (функция требует len >= 2, анализирует последнюю)
    def _h1(self, open_, close, high, low):
        dummy = self._candle(105, 105, 106, 104)   # нейтральная предыдущая свеча
        return [dummy, self._candle(open_, close, high, low)]

    def test_long_breakout_above_resistance(self):
        """H1 закрылась выше дневного сопротивления → breakout LONG."""
        result = mb.check_h1_confirmation("LONG", self.D1_LEVELS, self._h1(108, 112, 113, 107))
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "breakout")

    def test_long_momentum_bullish_candle(self):
        """Бычья H1-свеча: тело 60% диапазона, закрытие выше середины → momentum LONG."""
        # high=110, low=100, range=10; open=101, close=107 → body=6 (60%), mid=105
        result = mb.check_h1_confirmation("LONG", self.D1_LEVELS, self._h1(101, 107, 110, 100))
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "momentum")

    def test_long_no_confirmation_bearish_candle(self):
        """Медвежья H1-свеча при ожидании LONG → нет подтверждения."""
        result = mb.check_h1_confirmation("LONG", self.D1_LEVELS, self._h1(107, 102, 108, 101))
        self.assertIsNone(result)

    def test_long_no_confirmation_small_body(self):
        """H1-свеча бычья, но тело < 50% диапазона (доджи) → нет подтверждения."""
        # range=10, body=3 (30%)
        result = mb.check_h1_confirmation("LONG", self.D1_LEVELS, self._h1(102, 105, 110, 100))
        self.assertIsNone(result)

    # ── SHORT ─────────────────────────────────────────────────────────────────

    def test_short_breakout_below_support(self):
        """H1 закрылась ниже дневной поддержки → breakout SHORT."""
        result = mb.check_h1_confirmation("SHORT", self.D1_LEVELS, self._h1(102, 98, 103, 97))
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "breakout")

    def test_short_momentum_bearish_candle(self):
        """Медвежья H1-свеча: тело 60% диапазона, закрытие ниже середины → momentum SHORT."""
        # high=110, low=100, range=10; open=109, close=103 → body=6 (60%), mid=105
        result = mb.check_h1_confirmation("SHORT", self.D1_LEVELS, self._h1(109, 103, 110, 100))
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "momentum")

    def test_short_no_confirmation_bullish_candle(self):
        """Бычья H1-свеча при ожидании SHORT → нет подтверждения."""
        result = mb.check_h1_confirmation("SHORT", self.D1_LEVELS, self._h1(103, 108, 109, 102))
        self.assertIsNone(result)

    # ── Граничные случаи ──────────────────────────────────────────────────────

    def test_empty_candles_returns_none(self):
        """Пустой список свечей → None."""
        self.assertIsNone(mb.check_h1_confirmation("LONG", self.D1_LEVELS, []))

    def test_single_candle_returns_none(self):
        """Одна свеча (функция требует len >= 2) → None."""
        self.assertIsNone(mb.check_h1_confirmation("LONG", self.D1_LEVELS,
                                                    [self._candle(100, 105, 106, 99)]))

    def test_missing_ohlc_returns_none(self):
        """Последняя свеча без обязательных полей → None."""
        h1 = [self._candle(104, 105, 106, 103), {"close": 105}]
        self.assertIsNone(mb.check_h1_confirmation("LONG", self.D1_LEVELS, h1))


class TestH1WatchHelpers(unittest.TestCase):
    """expire_h1_watch() и add_to_h1_watch() — управление watch-листом."""

    def _make_entry(self, ticker, direction, hours_from_now):
        exp = (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()
        return {
            "ticker":    ticker,
            "direction": direction,
            "added_at":  datetime.now(timezone.utc).isoformat(),
            "expires_at": exp,
            "volume_ratio": 2.5,
            "levels_snapshot": {},
        }

    def test_expire_removes_expired_entries(self):
        """Запись с expires_at в прошлом → удаляется."""
        watch = {
            "SBER_LONG":  self._make_entry("SBER",  "LONG",  -1),   # истёк час назад
            "GAZP_SHORT": self._make_entry("GAZP", "SHORT",  +2),   # ещё не истёк
        }
        expired = mb.expire_h1_watch(watch)
        self.assertIn("SBER_LONG", expired)
        self.assertNotIn("SBER_LONG", watch)
        self.assertIn("GAZP_SHORT", watch)

    def test_expire_keeps_fresh_entries(self):
        """Записи с expires_at в будущем → остаются."""
        watch = {"LKOH_SHORT": self._make_entry("LKOH", "SHORT", +3)}
        expired = mb.expire_h1_watch(watch)
        self.assertEqual(expired, [])
        self.assertIn("LKOH_SHORT", watch)

    def test_add_to_h1_watch_creates_key(self):
        """add_to_h1_watch() создаёт запись с правильным ключом."""
        watch  = {}
        levels = {"last_close": 150.0, "support": 140.0, "resistance": 160.0,
                  "atr": 3.0, "ma50": 155.0}
        anomaly = {"ratio": 3.1}
        mb.add_to_h1_watch(watch, "SBER", "LONG", levels, anomaly)
        self.assertIn("SBER_LONG", watch)
        entry = watch["SBER_LONG"]
        self.assertEqual(entry["ticker"],    "SBER")
        self.assertEqual(entry["direction"], "LONG")
        self.assertAlmostEqual(entry["volume_ratio"], 3.1)
        self.assertIn("expires_at", entry)

    def test_add_to_h1_watch_expires_in_future(self):
        """expires_at всегда в будущем при добавлении."""
        watch = {}
        mb.add_to_h1_watch(watch, "NVTK", "SHORT",
                            {"last_close": 1000.0, "atr": 10.0}, {"ratio": 2.2})
        exp = datetime.fromisoformat(watch["NVTK_SHORT"]["expires_at"])
        self.assertGreater(exp, datetime.now(timezone.utc))

    def test_add_to_h1_watch_skips_open_sandbox_position(self):
        watch = {"HEAD_SHORT": self._make_entry("HEAD", "SHORT", +3)}
        added = mb.add_to_h1_watch(
            watch,
            "HEAD",
            "SHORT",
            {"last_close": 2762.0, "atr": 12.0},
            {"ratio": 5.0},
            {
                "sb_HEAD_SHORT_2026-05-15": {
                    "ticker": "HEAD",
                    "direction": "SHORT",
                    "order_id": "ord-1",
                },
            },
        )

        self.assertFalse(added)
        self.assertNotIn("HEAD_SHORT", watch)

    def test_daily_change_pct_uses_previous_close(self):
        candles = [{"close": 100.0}, {"close": 97.5}]

        self.assertEqual(mb.daily_change_pct(candles), -2.5)

    def test_price_momentum_radar_logs_watch_only_once(self):
        candles = [{"close": 100.0}, {"close": 97.5}]
        levels = {"last_close": 97.5, "ma50": 101.0}
        anomaly = {
            "anomaly": False,
            "ratio": 1.4,
            "last_volume": 120_000_000,
            "avg_volume": 85_000_000,
            "absolute_ok": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            with patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file):
                mb._MARKET_RADAR_SEEN.clear()
                self.assertTrue(mb.maybe_log_price_momentum_radar("GMKN", candles, levels, anomaly))
                self.assertFalse(mb.maybe_log_price_momentum_radar("GMKN", candles, levels, anomaly))

            rows = [json.loads(line) for line in open(opportunity_file, encoding="utf-8")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "watch_only")
            self.assertEqual(rows[0]["reason"], "price_momentum_radar")
            self.assertEqual(rows[0]["direction"], "SHORT")

    def test_h1_watch_radar_entry_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            with patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file):
                mb._MARKET_RADAR_SEEN.clear()
                first = mb.append_market_radar_opportunity(
                    "HEAD", "h1_watch_pending",
                    direction="SHORT",
                    levels={"last_close": 2911},
                    anomaly={"ratio": 2.03},
                    change_pct=-1.88,
                )
                second = mb.append_market_radar_opportunity(
                    "HEAD", "h1_watch_pending",
                    direction="SHORT",
                    levels={"last_close": 2911},
                    anomaly={"ratio": 2.03},
                    change_pct=-1.88,
                )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(open(opportunity_file, encoding="utf-8").read().splitlines()), 1)

    def test_calc_intraday_rebound_uses_low_before_close(self):
        candles = [
            {"low": 100.0, "high": 102.0, "close": 101.0, "begin": "2026-05-05 10:00:00"},
            {"low": 95.0, "high": 97.0, "close": 96.0, "begin": "2026-05-05 11:10:00"},
            {"low": 96.0, "high": 99.0, "close": 98.0, "begin": "2026-05-05 12:00:00"},
        ]

        result = mb.calc_intraday_rebound(candles)

        self.assertEqual(result["intraday_low"], 95.0)
        self.assertEqual(result["intraday_low_time"], "11:10")
        self.assertEqual(result["rebound_from_low_pct"], 3.16)
        self.assertEqual(result["rebound_high_from_low_pct"], 4.21)

    def test_index_rebound_radar_logs_liquid_beta_ticker(self):
        intraday = {
            "last": 91.5,
            "vwap": 90.4,
            "change_pct": 1.8,
            "rebound_from_low_pct": 2.7,
            "rebound_high_from_low_pct": 3.4,
            "intraday_low_time": "11:10",
        }
        index_intraday = {
            "change_pct": 1.0,
            "rebound_from_low_pct": 2.1,
            "intraday_low_time": "11:10",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            with patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file):
                mb._MARKET_RADAR_SEEN.clear()
                logged = mb.maybe_log_index_rebound_radar(
                    "VTBR", intraday, index_intraday,
                    {"last_close": 91.5}, {"ratio": 0.75}, 2.32,
                )

            rows = [json.loads(line) for line in open(opportunity_file, encoding="utf-8")]
            self.assertTrue(logged)
            self.assertEqual(rows[0]["reason"], "index_rebound_radar")
            self.assertEqual(rows[0]["direction"], "LONG")
            self.assertEqual(rows[0]["imoex_rebound_from_low_pct"], 2.1)

    def test_index_rebound_radar_rejects_below_vwap(self):
        logged = mb.maybe_log_index_rebound_radar(
            "SBER",
            {"last": 319.0, "vwap": 320.0, "rebound_from_low_pct": 2.0, "rebound_high_from_low_pct": 2.5},
            {"rebound_from_low_pct": 2.0},
            {"last_close": 319.0},
            {"ratio": 0.8},
            -0.1,
        )

        self.assertFalse(logged)

    def test_build_index_rebound_signal_rejects_below_vwap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            opportunity_file = os.path.join(tmpdir, "opportunity.jsonl")
            with patch.object(mb, "OPPORTUNITY_LOG_FILE", opportunity_file):
                signal = mb.build_index_rebound_signal(
                    "SBER",
                    {"last_close": 319.0, "support": 315.0, "resistance": 330.0, "atr": 2.0},
                    {},
                    {"last": 319.0, "vwap": 320.0, "rebound_from_low_pct": 2.0, "rebound_high_from_low_pct": 2.5},
                    {"rebound_from_low_pct": 2.0},
                    {"ratio": 0.8},
                )

            self.assertIsNone(signal)
            self.assertIn("index_rebound_below_vwap", open(opportunity_file, encoding="utf-8").read())

    def test_build_index_rebound_signal_is_tradable_strategy_signal(self):
        levels = {
            "last_close": 91.5,
            "support": 89.1,
            "resistance": 94.0,
            "atr": 1.0,
            "rsi": 58.0,
            "ma20": 90.0,
            "ma50": 89.0,
            "adx": 24.0,
            "obv_trend": "up",
            "obv_bull_div": False,
            "obv_bear_div": False,
            "ma_crossover": None,
        }
        intraday = {
            "last": 91.5,
            "vwap": 90.4,
            "last_begin": "2026-05-05 17:30:00",
            "change_pct": 1.8,
            "rebound_from_low_pct": 2.7,
            "rebound_high_from_low_pct": 3.4,
            "intraday_low_time": "11:10",
        }
        index_intraday = {
            "change_pct": 1.0,
            "rebound_from_low_pct": 2.1,
            "intraday_low_time": "11:10",
        }
        with patch.object(mb, "_tinvest_available", return_value=False), \
             patch.object(mb, "is_moex_open", return_value=False):
            signal = mb.build_index_rebound_signal(
                "VTBR", levels, {"imoex_regime": "bull"}, intraday, index_intraday, {"ratio": 0.75}
            )
            synthesized = mb.synthesize_signals([signal], [])

        self.assertIsNotNone(signal)
        self.assertEqual(signal["type"], "INDEX_REBOUND")
        self.assertEqual(signal["direction"], "LONG")
        self.assertEqual(signal["strategy"], "index_rebound")
        self.assertGreaterEqual(synthesized[0]["confidence_score"], 9)
        self.assertIn("index_rebound_signal", synthesized[0]["decision_reasons"])

    def test_sandbox_strategy_rejects_weak_index_rebound(self):
        reason = mb.sandbox_strategy_reject_reason({
            "strategy": "index_rebound",
            "confidence_score": 8,
            "vwap_confirm": True,
        })

        self.assertEqual(reason, "index_rebound_min_score")

    def test_sandbox_strategy_rejects_index_rebound_vwap_conflict(self):
        reason = mb.sandbox_strategy_reject_reason({
            "strategy": "index_rebound",
            "confidence_score": 10,
            "vwap_confirm": False,
        })

        self.assertEqual(reason, "index_rebound_vwap_required")

    def test_sandbox_strategy_rejects_stale_news_event(self):
        reason = mb.sandbox_strategy_reject_reason(
            {"strategy": "news_event", "confidence_score": 21, "vwap_confirm": True},
            {"stale_intraday"},
            {"news_event_signal"},
        )

        self.assertEqual(reason, "stale_intraday")

    def test_sandbox_strategy_rejects_weak_news_event(self):
        reason = mb.sandbox_strategy_reject_reason(
            {"strategy": "news_event", "confidence_score": 17, "vwap_confirm": True},
            set(),
            {"news_event_signal"},
        )

        self.assertEqual(reason, "news_event_min_score")

    def test_sandbox_strategy_rejects_conflicting_news_event(self):
        reason = mb.sandbox_strategy_reject_reason(
            {"strategy": "news_event", "confidence_score": 21, "vwap_confirm": True},
            set(),
            {"news_event_signal", "news_conflict"},
        )

        self.assertEqual(reason, "news_event_conflict")

    def test_setup_quality_requires_core_trade_thesis(self):
        quality, score, reasons = mb.evaluate_setup_quality({
            "direction": "LONG",
            "volume_ratio": 1.6,
            "vwap_confirm": None,
            "news_agree": 1,
            "news_oppose": 0,
            "data_quality_flags": [],
        })

        self.assertEqual(quality, "D")
        self.assertLess(score, 2)
        self.assertIn("news_aligned", reasons)

    def test_setup_quality_marks_h1_volume_vwap_as_tradable(self):
        quality, score, reasons = mb.evaluate_setup_quality({
            "direction": "SHORT",
            "h1_confirm": {"type": "momentum"},
            "volume_ratio": 3.5,
            "vwap_confirm": True,
            "weekly_aligned": True,
            "data_quality_flags": [],
        })

        self.assertEqual(quality, "A")
        self.assertGreaterEqual(score, 4)
        self.assertIn("h1_momentum", reasons)

    def test_setup_quality_rejects_low_quality_auto_order(self):
        self.assertEqual(
            mb.setup_quality_reject_reason({"setup_quality": "C"}),
            "setup_quality_low",
        )
        self.assertIsNone(mb.setup_quality_reject_reason({"setup_quality": "B"}))

    def test_recent_performance_guard_detects_bad_rolling_edge(self):
        trades = [
            {
                "ticker": f"T{i}",
                "exit_time": f"2026-05-{10 + i:02d} 18:00",
                "pnl_pct": pnl,
                "executed": True,
                "execution_status": "closed",
            }
            for i, pnl in enumerate([-1.0, -0.8, 0.5, -1.2, -0.4, 0.7, -1.1, -0.9])
        ]

        active, meta = mb.recent_performance_guard(trades, min_trades=8, min_pnl=0.0, min_winrate=45.0)

        self.assertTrue(active)
        self.assertEqual(meta["closed"], 8)
        self.assertLess(meta["total_pnl"], 0)
        self.assertLess(meta["winrate"], 45.0)

    def test_recent_performance_guard_allows_good_rolling_edge(self):
        trades = [
            {
                "ticker": f"T{i}",
                "exit_time": f"2026-05-{10 + i:02d} 18:00",
                "pnl_pct": pnl,
                "executed": True,
                "execution_status": "closed",
            }
            for i, pnl in enumerate([1.0, -0.8, 0.5, 1.2, -0.4, 0.7, 1.1, -0.9])
        ]

        active, meta = mb.recent_performance_guard(trades, min_trades=8, min_pnl=0.0, min_winrate=45.0)

        self.assertFalse(active)
        self.assertEqual(meta["closed"], 8)

    def test_count_open_portfolio_positions_ignores_cash(self):
        count = mb.count_open_portfolio_positions({
            "positions": [
                {"ticker": "RUB000UTSTOM", "quantity": 1000},
                {"ticker": "ROSN", "quantity": -249},
                {"ticker": "MOEX", "quantity": 0},
            ]
        })

        self.assertEqual(count, 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
