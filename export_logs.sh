#!/bin/bash
# ════════════════════════════════════════════════════════════════
#  MOEX Bot — экспорт всех аналитических данных
#  Использование: ./export_logs.sh
#  Результат:     trader_export_YYYY-MM-DD.tar.gz (в папке trader/)
#
#  Что входит в архив:
#    trade_log.json          — все сделки (результаты, P&L, score_breakdown)
#    signals_score_log.jsonl — все оцененные сигналы + причины отклонений
#    news_memory.json        — накопленный новостной сентимент
#    signals_state.json      — текущие открытые позиции
#    bot.log                 — текущий лог
#    logs/*.log              — все исторические логи
#    cbr_rate_cache.json     — кэш ставки ЦБ
# ════════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

DATE=$(date +%Y-%m-%d)
ARCHIVE="trader_export_${DATE}.tar.gz"
TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/trader_export_clean.XXXXXX")
cleanup() {
    rm -rf "$TMPDIR"
}
trap cleanup EXIT

echo "📦 Упаковываем данные бота за ${DATE}..."

# Файлы для включения (только если существуют)
FILES=()
for f in \
    "trade_log.json" \
    "signals_score_log.jsonl" \
    "news_memory.json" \
    "signals_state.json" \
    "cbr_rate_cache.json" \
    "bot.log" \
    "h1_watch.json"
do
    [ -f "$f" ] && FILES+=("$f")
done

# Логи из папки logs/
if [ -d "logs" ]; then
    FILES+=("logs/")
fi

if [ ${#FILES[@]} -eq 0 ]; then
    echo "❌ Нет файлов для упаковки"
    exit 1
fi

python3 - "$TMPDIR" "${FILES[@]}" <<'PY'
import pathlib
import re
import shutil
import sys

tmp_root = pathlib.Path(sys.argv[1])
paths = [pathlib.Path(p) for p in sys.argv[2:]]
patterns = [
    (re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+"), r"\1***"),
    (re.compile(r"(bot)[0-9]{6,}:[A-Za-z0-9_-]+"), r"\1***"),
    (re.compile(r"(TELEGRAM_TOKEN\s*=\s*)\S+"), r"\1***"),
]

def redact(text: str) -> str:
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text

def copy_sanitized(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except Exception:
        shutil.copy2(src, dst)
        return
    dst.write_text(redact(text), encoding="utf-8")

for src in paths:
    if src.is_dir():
        for child in src.rglob("*"):
            if child.is_file():
                copy_sanitized(child, tmp_root / src.name / child.relative_to(src))
    elif src.is_file():
        copy_sanitized(src, tmp_root / src.name)
PY

tar -czf "$ARCHIVE" -C "$TMPDIR" .

SIZE=$(du -sh "$ARCHIVE" | cut -f1)
echo ""
echo "✅ Архив создан: $ARCHIVE ($SIZE)"
echo ""
echo "Содержимое:"
tar -tzf "$ARCHIVE" | sed 's/^/   /'
echo ""
echo "📋 Статистика:"

# Показываем краткую сводку
if [ -f "trade_log.json" ]; then
    TOTAL=$(python3 -c "import json; d=json.load(open('trade_log.json')); print(len(d))" 2>/dev/null || echo "?")
    CLOSED=$(python3 -c "import json; d=json.load(open('trade_log.json')); print(sum(1 for t in d if t.get('result')))" 2>/dev/null || echo "?")
    echo "   trade_log:    $TOTAL сигналов, $CLOSED закрытых"
fi

if [ -f "signals_score_log.jsonl" ]; then
    SCORE_LINES=$(wc -l < "signals_score_log.jsonl" 2>/dev/null || echo "?")
    TRADED=$(grep -c '"action": "traded"' "signals_score_log.jsonl" 2>/dev/null || echo "0")
    SKIPPED=$(grep -c '"action": "skipped"' "signals_score_log.jsonl" 2>/dev/null || echo "0")
    BLOCKED=$(grep -c '"action": "blocked"' "signals_score_log.jsonl" 2>/dev/null || echo "0")
    echo "   score_log:    $SCORE_LINES строк  (traded=$TRADED  skipped=$SKIPPED  blocked=$BLOCKED)"
fi

if [ -f "news_memory.json" ]; then
    TICKERS=$(python3 -c "import json; d=json.load(open('news_memory.json')); print(len(d))" 2>/dev/null || echo "?")
    echo "   news_memory:  $TICKERS тикеров"
fi

echo ""
echo "Передай файл: scp user@server:$(pwd)/$ARCHIVE ./"
echo "Или скачай из папки trader на десктопе."
