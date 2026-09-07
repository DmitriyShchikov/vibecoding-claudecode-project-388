#!/usr/bin/env python3
"""Обходит список отслеживаемых URL и собирает таблицу цен.

Список товаров зашит в TRACKED_URLS ниже — правится прямо здесь.
Цену с каждой страницы снимает скилл extract-price.

Usage:
    python3 tracker.py [--json | --csv] [--dest -1257786] [--delay 1.0]

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

COLUMNS = ("url", "name", "regular_price", "sale_price", "discount_pct",
           "has_credit", "availability", "checked_at", "error")


def load_extract_price():
    """Подключает соседний скилл extract-price как модуль."""
    path = (Path(__file__).resolve().parents[2]
            / "extract-price" / "scripts" / "extract_price.py")
    if not path.exists():
        sys.exit(f"ОШИБКА: не найден скилл extract-price ({path})")
    spec = importlib.util.spec_from_file_location("extract_price", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(urls, dest, delay):
    ep = load_extract_price()
    rows = []
    # dict.fromkeys — дедупликация с сохранением исходного порядка.
    unique = list(dict.fromkeys(urls))
    for i, url in enumerate(unique):
        row = dict.fromkeys(COLUMNS)
        row["url"] = url
        try:
            data = ep.extract(url, dest)
        except ep.ExtractError as e:
            row["error"] = str(e)
        except Exception as e:  # адаптер упал неожиданно — строка не должна ронять обход
            row["error"] = f"{type(e).__name__}: {e}"
        else:
            row.update({k: data.get(k) for k in
                        ("name", "regular_price", "sale_price", "has_credit",
                         "availability", "checked_at")})
            if row["sale_price"] and row["regular_price"]:
                row["discount_pct"] = round(
                    100 - row["sale_price"] / row["regular_price"] * 100)
        rows.append(row)
        # Пауза между запросами: обход не должен выглядеть как наплыв ботов.
        if delay and i < len(unique) - 1:
            time.sleep(delay)
    return rows


def money(value):
    return f"{value:,.0f} ₽".replace(",", " ") if value is not None else "—"


def print_table(rows):
    header = f"{'товар':<44} {'обычная':>12} {'скидка':>12} {'%':>5} {'рассрочка':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        if r["error"]:
            print(f"{(r['name'] or r['url'].split('/')[-2])[:44]:<44} "
                  f"ОШИБКА: {r['error'][:60]}")
            continue
        credit = {True: "да", False: "нет", None: "н/д"}[r["has_credit"]]
        print(f"{(r['name'] or '')[:44]:<44} {money(r['regular_price']):>12} "
              f"{money(r['sale_price']):>12} "
              f"{(str(r['discount_pct']) + '%') if r['discount_pct'] else '—':>5} "
              f"{credit:>10}")

    ok = [r for r in rows if not r["error"]]
    failed = len(rows) - len(ok)
    with_sale = sum(1 for r in ok if r["sale_price"])
    print("-" * len(header))
    print(f"собрано: {len(ok)} из {len(rows)} | со скидкой: {with_sale} | "
          f"ошибок: {failed} | {datetime.now(timezone.utc).isoformat(timespec='seconds')}")


def main():
    parser = argparse.ArgumentParser(description="Обход списка URL и таблица цен")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="вывести JSON вместо таблицы")
    output.add_argument("--csv", action="store_true", help="вывести CSV вместо таблицы")
    parser.add_argument("--dest", type=int, default=-1257786,
                        help="код региона Wildberries (по умолчанию Москва)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="пауза между запросами в секундах (по умолчанию 1.0)")
    args = parser.parse_args()

    rows = collect(TRACKED_URLS, args.dest, args.delay)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    else:
        print_table(rows)

    ok = sum(1 for r in rows if not r["error"])
    return 0 if ok == len(rows) else (2 if ok == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
