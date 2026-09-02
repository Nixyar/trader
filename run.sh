#!/bin/bash
# ────────────────────────────────────────────────────────────
#  MOEX Signal Bot — скрипт запуска
# ────────────────────────────────────────────────────────────
#  Режимы:
#    ./run.sh              — один скан, вывод в терминал
#    ./run.sh --watch      — фоновый режим (каждые 5 мин)
#    ./run.sh --news-only  — только новости, без рынка
#
#  Для Telegram: заполни TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в .env
# ────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

# Загружаем переменные из .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Проверяем ключ (опционален — без него бот падает на анализ по ключевым словам)
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "🤖 AI-анализ: выключен (нет ANTHROPIC_API_KEY в .env) — новости по ключевым словам"
else
    echo "🤖 AI-анализ: включён"
fi

# Telegram-статус
if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    echo "📲 Telegram: включён"
else
    echo "💬 Telegram: выключен (нет токена в .env)"
fi

echo ""
python3 moex_bot.py "$@"
