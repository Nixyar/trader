"""
test_bot.py — юнит-тесты для moex_signal_bot.py и moex_bot.py
Запуск: python test_bot.py
Зависимости: только стандартная библиотека + unittest.mock
feedparser мокируется через sys.modules (не нужен для unit-тестов).
"""

import sys
import os
import glob
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from statistics import mean

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


# ═══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
