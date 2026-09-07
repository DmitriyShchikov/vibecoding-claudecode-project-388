#!/usr/bin/env python3
"""Обходит список отслеживаемых URL и собирает таблицу цен.

Список товаров зашит в TRACKED_URLS ниже — правится прямо здесь.
Цену с каждой страницы снимает скилл extract-price.

Usage:
    python3 tracker.py [--json | --csv] [--dest -1257786] [--delay 1.0]
    python3 tracker.py --save <каталог>   # файл прогона YYYY-MM-DD.json

Exit codes: 0 — все строки собраны, 1 — часть строк с ошибкой, 2 — ни одной цены.
"""

import argparse
import csv
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- отслеживаемые товары -------------------------------------------------
# Правьте этот список, чтобы добавить или убрать товар. Дубликаты можно не
# вычищать вручную — они отбрасываются при обходе с сохранением порядка.
TRACKED_URLS = [
    "https://www.wildberries.ru/catalog/899301731/detail.aspx",
    "https://www.wildberries.ru/catalog/1498440784/detail.aspx",
    "https://www.wildberries.ru/catalog/1405022749/detail.aspx",
    "https://www.wildberries.ru/catalog/1002936121/detail.aspx",
    "https://www.wildberries.ru/catalog/46467713/detail.aspx",
    "https://www.wildberries.ru/catalog/745723045/detail.aspx",
    "https://www.wildberries.ru/catalog/1208082523/detail.aspx",
    "https://www.wildberries.ru/catalog/200608562/detail.aspx",
    "https://www.wildberries.ru/catalog/1024426112/detail.aspx",
    "https://www.wildberries.ru/catalog/683651077/detail.aspx",
]
# --------------------------------------------------------------------------

# Строка прогона: URL плюс три поля контракта extract-price. error нужен, чтобы
# отличить "цены нет" от "снять не удалось", поэтому он в строке всегда.
ROW = ("url", "regular_price", "sale_price", "has_credit", "error")
# Контекст для истории и отчётов — добавляется флагом --full, строку не меняет.
EXTRA = ("name", "discount_pct", "availability", "checked_at")


def load_extract_price():
    """Подключает соседний скилл extract-price как модуль.

    tracker не разбирает страницы сам: вся логика извлечения цены (адаптеры под
    магазины, контракт, обработка антибота) живёт в extract-price и вызывается
    отсюда. Дублировать её здесь нельзя — разъедется контракт.
    """
    path = (Path(__file__).resolve().parents[2]
            / "extract-price" / "scripts" / "extract_price.py")
    if not path.exists():
        sys.exit(f"ОШИБКА: не найден скилл extract-price ({path})")
    spec = importlib.util.spec_from_file_location("extract_price", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(urls, dest, delay):
    """Обходит список и складывает по строке на товар в таблицу прогона."""
    ep = load_extract_price()
    rows = []
    # dict.fromkeys — дедупликация с сохранением исходного порядка.
    unique = list(dict.fromkeys(urls))
    for i, url in enumerate(unique):
        row = dict.fromkeys(ROW + EXTRA)
        row["url"] = url
        try:
            # Один вызов готового скилла на один URL — свой парсинг тут не заводим.
            data = ep.extract(url, dest)
        except ep.ExtractError as e:
            row["error"] = str(e)
        except Exception as e:  # адаптер упал неожиданно — строка не должна ронять обход
            row["error"] = f"{type(e).__name__}: {e}"
        else:
            row.update({k: data.get(k) for k in
                        ("regular_price", "sale_price", "has_credit",
                         "name", "availability", "checked_at")})
            if row["sale_price"] and row["regular_price"]:
                row["discount_pct"] = round(
                    100 - row["sale_price"] / row["regular_price"] * 100)
        rows.append(row)
        # Пауза между запросами: обход не должен выглядеть как наплыв ботов.
        if delay and i < len(unique) - 1:
            time.sleep(delay)
    return rows


CREDIT = {True: "да", False: "нет", None: "н/д"}


def run_filename(now=None):
    """Имя файла прогона: дата прогона в UTC, YYYY-MM-DD.json."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d") + ".json"


def save_run(rows, target):
    """Кладёт таблицу прогона в <каталог>/YYYY-MM-DD.json и возвращает путь.

    Файл готов к публикации в репозиторий tracker-data через GitHub MCP —
    локальный git здесь не задействован.
    """
    path = Path(target)
    if path.is_dir() or not path.suffix:
        path = path / run_filename()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def money(value):
    return f"{value:,.0f} ₽".replace(",", " ") if value is not None else "—"


def print_table(rows, full=False):
    """Одна строка на товар: URL и поля контракта extract-price."""
    width = max((len(r["url"]) for r in rows), default=40)
    head = f"{'url':<{width}} {'regular_price':>14} {'sale_price':>12} {'has_credit':>11}"
    if full:
        head += f"  {'скидка':>7}  товар"
    print(head)
    print("-" * min(len(head), 120))
    for r in rows:
        if r["error"]:
            print(f"{r['url']:<{width}} {'ОШИБКА: ' + r['error'][:52]}")
            continue
        line = (f"{r['url']:<{width}} {money(r['regular_price']):>14} "
                f"{money(r['sale_price']):>12} "
                f"{CREDIT[r['has_credit']]:>11}")
        if full:
            pct = f"{r['discount_pct']}%" if r["discount_pct"] else "—"
            line += f"  {pct:>7}  {(r['name'] or '')[:44]}"
        print(line)

    ok = [r for r in rows if not r["error"]]
    with_sale = sum(1 for r in ok if r["sale_price"])
    print("-" * min(len(head), 120))
    print(f"строк: {len(rows)} | собрано: {len(ok)} | со скидкой: {with_sale} | "
          f"ошибок: {len(rows) - len(ok)} | "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")


def main():
    parser = argparse.ArgumentParser(description="Обход списка URL и таблица цен")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="вывести JSON вместо таблицы")
    output.add_argument("--csv", action="store_true", help="вывести CSV вместо таблицы")
    parser.add_argument("--dest", type=int, default=-1257786,
                        help="код региона Wildberries (по умолчанию Москва)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="пауза между запросами в секундах (по умолчанию 1.0)")
    parser.add_argument("--full", action="store_true",
                        help="добавить к строке название, размер скидки, наличие и время")
    parser.add_argument("--save", metavar="КАТАЛОГ",
                        help="записать файл прогона YYYY-MM-DD.json в этот каталог")
    args = parser.parse_args()

    rows = collect(TRACKED_URLS, args.dest, args.delay)
    columns = ROW + EXTRA if args.full else ROW
    trimmed = [{k: r[k] for k in columns} for r in rows]

    if args.save:
        path = save_run(trimmed, args.save)
        print(f"файл прогона: {path}")
        print_table(rows, args.full)
    elif args.json:
        print(json.dumps(trimmed, ensure_ascii=False, indent=2))
    elif args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns)
        writer.writeheader()
        writer.writerows(trimmed)
    else:
        print_table(rows, args.full)

    ok = sum(1 for r in rows if not r["error"])
    return 0 if ok == len(rows) else (2 if ok == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
