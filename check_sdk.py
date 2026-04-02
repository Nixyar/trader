#!/usr/bin/env python3
"""
Диагностика T-Invest SDK — запусти и пришли вывод.
"""
import sys
import subprocess

print(f"Python: {sys.version}")
print(f"Exec:   {sys.executable}")
print()

# --- pip show ---
print("=" * 50)
print("pip show t-tech-investments:")
r = subprocess.run(
    [sys.executable, "-m", "pip", "show", "t-tech-investments"],
    capture_output=True, text=True
)
print(r.stdout.strip() or "(пусто)")
if r.stderr.strip():
    print("STDERR:", r.stderr.strip())

# --- Список файлов пакета ---
print()
print("=" * 50)
print("Файлы пакета (pip show -f):")
r2 = subprocess.run(
    [sys.executable, "-m", "pip", "show", "-f", "t-tech-investments"],
    capture_output=True, text=True
)
lines = r2.stdout.splitlines()
in_files = False
count = 0
for line in lines:
    if line.startswith("Files:"):
        in_files = True
        print("Files:")
        continue
    if in_files:
        print(" ", line.strip())
        count += 1
        if count > 40:
            print("  ... (truncated)")
            break

# --- Попытки импорта ---
print()
print("=" * 50)
print("Попытки импорта:")
candidates = [
    "t_tech.invest",       # t-tech-investments ≥0.3 (новый namespace T-Bank)
    "t_tech",
    "tinkoff.invest",
    "tinkoff",
    "tinkoff_invest",
    "t_invest",
    "t_tech_investments",
    "tinvest",
    "invest",
]
for name in candidates:
    try:
        import importlib
        mod = importlib.import_module(name)
        print(f"  ✅ import {name}  →  {getattr(mod, '__file__', '?')}")
    except ImportError as e:
        print(f"  ❌ import {name}  →  ImportError: {e}")
    except Exception as e:
        print(f"  ⚠️  import {name}  →  {type(e).__name__}: {e}")

# Дополнительно — пробуем прямой импорт Client
print()
print("Попытка: from t_tech.invest import Client")
try:
    from t_tech.invest import Client
    print(f"  ✅ Client импортирован: {Client}")
except Exception as e:
    print(f"  ❌ {e}")

# --- Если tinkoff установлен — что внутри? ---
print()
print("=" * 50)
print("Поиск 'tinkoff' в sys.path:")
import importlib.util
for check_name in ("tinkoff", "t_tech"):
    spec = importlib.util.find_spec(check_name)
    if spec:
        print(f"  {check_name} найден: {spec.origin or spec.submodule_search_locations}")
        sub = importlib.util.find_spec(f"{check_name}.invest")
        if sub:
            print(f"  {check_name}.invest найден: {sub.origin}")
        else:
            print(f"  {check_name}.invest НЕ найден (namespace есть, но invest нет)")
    else:
        print(f"  {check_name} НЕ найден вообще")

# --- Поиск по site-packages ---
print()
print("=" * 50)
print("Директории в site-packages с 'tinkoff' или 't_invest' или 't_tech':")
import site, os
for sp in site.getsitepackages():
    try:
        entries = os.listdir(sp)
        matched = [e for e in entries if any(
            kw in e.lower() for kw in ("tinkoff", "t_invest", "t_tech", "tinvest")
        )]
        if matched:
            print(f"  {sp}:")
            for m in matched:
                print(f"    {m}")
    except Exception:
        pass

print()
print("Готово. Пришли этот вывод.")
