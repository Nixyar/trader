# 📈 MOEX Signal Bot

[Русская версия →](README.ru.md)

A trading-signal bot for the Moscow Exchange (MOEX). It scans liquid Russian
equities for volume anomalies, confirms them with technical indicators and RSS
news, and can place **paper trades** in the T-Investments sandbox.

It also ships with the research tooling used to decide whether the strategy is
actually worth trading — and, so far, the honest answer is **no**. See
[Honest assessment](#-honest-assessment-read-this-first).

---

## ⚠️ Honest assessment (read this first)

> A backtest over 4 years / ~3200 trades showed that the core strategy
> (volume anomaly + support/resistance levels) has **no durable edge that
> survives transaction costs**. The high win rate is misleading — expectancy
> after commission and spread is around zero or negative.
>
> This bot runs **in the sandbox only** (paper money). It should not be moved
> to real money unless `--edge` reports a statistically significant positive
> expectancy on a large sample.

This repository is published as an engineering and research artifact — a
reasonably thorough attempt to find an edge, including the tooling that proved
the edge wasn't there. It is **not** a money-making system, **not** investment
advice, and comes with no warranty. Trading carries risk of total loss.

---

## What it does

- Pulls daily and intraday candles from the **MOEX ISS API** (free, no key)
  and, optionally, from the **T-Invest API** (order book, portfolio, sandbox)
- Detects volume anomalies (volume > 2× the 20-day average) with a minimum
  absolute turnover filter
- Confirms with RSI(14, Wilder), MA20/MA50, ADX(14), Fibonacci levels,
  Bollinger squeeze, OBV and MA crossovers
- Parses RSS news from Russian financial media, deduplicated via a cache
- Optionally scores news with the **Anthropic API** in market context
  (CBR key rate, USDRUB, IMOEX, Brent, Gold)
- Synthesizes everything into a score and emits a **LONG** / **SHORT** signal
  with entry, ATR-based stop and three take-profit levels
- Sends signals and a daily EOD report to **Telegram**
- Places sandbox orders with position sizing and risk limits

## Requirements

- Python 3.10+
- Internet access to `iss.moex.com`
- Optional: Anthropic API key, Telegram bot, T-Invest token

## Install

```bash
git clone <your-repo-url> trader
cd trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The T-Invest SDK is optional and is **not** on public PyPI:

```bash
pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

Without it, the bot still runs on free MOEX ISS data; sandbox trading, order
book and portfolio features are disabled. Check your install with
`python3 check_sdk.py`.

## Configure

```bash
cp .env.example .env
```

Then fill in `.env`. Every value is optional except where noted:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | AI news analysis. Omit to skip that module. |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Notifications. Omit for console-only. |
| `TINVEST_TOKEN` | T-Invest data + sandbox. Omit for MOEX ISS only. |
| `TINVEST_SANDBOX_ACCOUNT_ID` | Filled by `--create-sandbox`. |
| `CBR_KEY_RATE` | CBR key rate, update after each rate decision. |
| `SANDBOX_MAX_*_PCT` | Position sizing and concentration limits. |
| `ENABLE_REAL_TRADING` | Keep `false`. Real orders are a stub. |

`.env` is git-ignored. **Never commit it.** All other tuning knobs
(`STOP_ATR_MULT`, `WEEKLY_*`, `NEWS_*`, `RECENT_PERF_*`, …) have defaults in
the code and only need to be set to override.

Set up the sandbox account once:

```bash
python3 tinvest_data.py --create-sandbox
python3 tinvest_data.py --fund-sandbox
python3 tinvest_data.py --portfolio
```

## Run

```bash
./run.sh                 # one scan, output to terminal
./run.sh --watch         # background mode, rescans every 5 min
./run.sh --news-only     # news only, skip market data
```

Or call the bot directly:

```bash
python3 moex_bot.py --watch
python3 moex_bot.py --trade-log      # show the trade log
python3 moex_bot.py --export-csv     # export trades to CSV
python3 moex_bot.py --edge           # is the edge real, or luck?
python3 moex_bot.py --close SBER     # manually close a position
```

Daily Telegram report (cron, 19:00 MSK):

```bash
0 16 * * 1-5 /full/path/to/trader/run_report.sh >> /full/path/to/trader/logs/report.log 2>&1
```

## Research tooling

This is the part worth reading. Each tool is built to *disprove* the strategy
rather than flatter it.

### `backtest.py` — test the idea on history

Runs the bot's actual functions over 2–4 years of MOEX history with realistic
costs and an out-of-sample split. Test any new hypothesis here **before**
touching the live bot.

```bash
python3 backtest.py --years 4 --commission 0.10 --split   # baseline + OOS
python3 backtest.py --years 2 --regime --split            # trend-following
python3 backtest.py --years 2 --direction SHORT           # shorts only
```

Key flags: `--quality`, `--min-vol-ratio`, `--type`, `--direction`,
`--tier 1|2`, `--regime` (IMOEX trend), `--split` (out-of-sample),
`--csv out.csv`.

### `moex_bot.py --edge` — is the edge real?

Computes expectancy per trade **after costs**, a 95% confidence interval and a
t-statistic over `trade_log.json`. The verdict is honest: with a small sample
(<30) or an interval crossing zero, it reports "edge not proven". The same line
is appended to the daily EOD Telegram report.

### `strategy_lab.py` — compare strategy families vs buy & hold

The fair professional benchmark is buy & hold, which most active managers fail
to beat after costs. This compares `bh`, `tsmom` (price > MA200),
`ma_cross` (MA20/MA100) and `rsi2` (Connors mean reversion), long-only,
equal-weight, out of sample.

### `brute_force.py` — parameter sweep with overfitting protection

Sweeping hundreds of configs and picking the best one on history is a reliable
way to invent an edge that doesn't exist. A config counts as a candidate only
if it passes **all** filters: positive return in *both* independent halves of
the window, total return above the risk-free rate, and ranking by the *worse*
half rather than the better one.

```bash
python3 brute_force.py --from 2016-01-01 --to 2021-12-31
```

### `pulse_tracker.py` — score other people's public calls

Turns "this trader is up +132%" into checkable statistics. Headline percentages
on social platforms aren't verifiable (deposits and withdrawals distort them),
but public *forecasts* are falsifiable. The tracker records the price at the
moment of the call and scores it against MOEX quotes. No access to anyone's
account is required — public data only.

```bash
python3 pulse_tracker.py add --ticker IMOEX --target 2480 --horizon 7
python3 pulse_tracker.py add --ticker SBER --dir long --horizon 5
python3 pulse_tracker.py score     # pull quotes, close out due forecasts
python3 pulse_tracker.py report    # hit rate, P&L if copied vs buy & hold
```

### `FORWARD_TEST_MODE` — gathering sandbox statistics

Production throttle gates squeeze trading down to ~6 trades a year, which will
never produce a usable sample for `--edge`. This flag loosens **only** those
throttle gates; the hard guards (`MAX_DAILY_LOSS_PCT`, `MAX_STOP_PCT`) stay
active. **Sandbox only.**

```bash
FORWARD_TEST_MODE=1 python3 moex_bot.py --watch
```

## Project layout

| File | Role |
|---|---|
| `moex_bot.py` | Main bot: scanning, scoring, signals, sandbox orders, Telegram |
| `tinvest_data.py` | T-Invest API: quotes, history, order book, sandbox trading |
| `daily_report.py` | Daily EOD report to Telegram |
| `moex_signal_bot.py` | Standalone minimal scanner (the original prototype) |
| `backtest.py` | Historical backtest of the core strategy |
| `strategy_lab.py` | Strategy families vs buy & hold |
| `brute_force.py` | Parameter sweep with overfitting protection |
| `pulse_tracker.py` | Objective tracker for public forecasts |
| `check_sdk.py` | T-Invest SDK install diagnostics |
| `test_bot.py` | Test suite (152 tests) |
| `run.sh`, `run_report.sh`, `export_logs.sh` | Launch, report and export helpers |

Runtime state (`signals_state.json`, `trade_log.json`, `*.jsonl` logs, caches,
`logs/`) is generated at run time and git-ignored.

## Tests

```bash
python3 test_bot.py
```

## Disclaimer

This software is provided for educational and research purposes only. It is
not investment advice, not a recommendation to buy or sell any security, and
not a solicitation. The author is not a licensed financial advisor. The
strategy has **no proven edge after costs**. If you run it, run it in the
sandbox. Any real-money use is entirely at your own risk.

## License

[MIT](LICENSE)
