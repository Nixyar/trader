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

# Проверяем ключ
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "⚠️  ANTHROPIC_API_KEY не задан в .env"
    exit 1
fi

# Telegram-статус
if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    echo "📲 Telegram: включён"
else
    echo "💬 Telegram: выключен (нет токена в .env)"
fi

echo ""
python3 moex_bot.py "$@"
