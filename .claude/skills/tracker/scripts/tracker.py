#!/usr/bin/env python3
"""Обходит список отслеживаемых URL и собирает таблицу цен.

Список товаров зашит в TRACKED_URLS ниже — правится прямо здесь.
Цену с каждой страницы снимает скилл extract-price.

Usage:
    python3 tracker.py [--json | --csv] [--dest -1257786] [--delay 1.0]
    python3 tracker.py --save <каталог>   # файл прогона YYYY-MM-DD.json
    python3 tracker.py --diff <файл>      # сравнить с предыдущим прогоном

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
# Поля, изменение которых считается изменением товара.
COMPARED = ("regular_price", "sale_price", "has_credit")

# Пороги значимости — обоснование в KNOWLEDGE.md в корне репозитория.
# Изменение цены значимо, только если проходит ОБА порога сразу.
PRICE_MIN_PCT = 1.0    # не меньше 1% от прежней цены
PRICE_MIN_ABS = 50.0   # и не меньше 50 ₽


def effective_price(row):
    """Цена, которую платит покупатель: со скидкой, если она есть."""
    sale = row.get("sale_price")
    return sale if sale is not None else row.get("regular_price")


def price_signal(was, now):
    """Значимо ли изменение эффективной цены. Возвращает сигнал или None."""
    if was is None or now is None or was == now:
        return None
    delta = now - was
    pct = (delta / was * 100) if was else None
    # Оба порога обязательны: 1% на дешёвом товаре — копейки, 50 ₽ на дорогом —
    # шум. Вместе они означают "ощутимо и в деньгах, и в доле цены".
    if abs(delta) < PRICE_MIN_ABS or (pct is not None and abs(pct) < PRICE_MIN_PCT):
        return None
    return {"kind": "price", "from": was, "to": now,
            "delta": round(delta, 2), "pct": round(pct, 1) if pct is not None else None,
            "text": f"цена {'выросла' if delta > 0 else 'упала'}"}


def credit_signal(was, now):
    """Появление или пропажа рассрочки. Переходы через null незначимы:
    null означает "источник не сообщает", а не отсутствие рассрочки."""
    if was == now or was is None or now is None:
        return None
    return {"kind": "credit", "from": was, "to": now,
            "text": "появилась рассрочка" if now else "рассрочка пропала"}


def diff_runs(previous, current):
    """Сравнивает два прогона по URL и возвращает список изменений по товарам.

    previous — таблица прошлого прогона, current — текущего. Товары
    сопоставляются по url: он стабильный ключ, названия у магазинов плавают.
    """
    before = {r["url"]: r for r in previous}
    after = {r["url"]: r for r in current}
    result = []

    for url, row in after.items():
        old = before.get(url)
        if old is None:
            result.append({"url": url, "status": "new", "changes": {},
                           "signals": [], "significant": False})
            continue
        # Строку с ошибкой сравнивать нельзя: отсутствие цены здесь означает
        # "не смогли снять", а не "цена исчезла".
        if row.get("error") or old.get("error"):
            result.append({"url": url, "status": "error", "changes": {},
                           "signals": [], "significant": False,
                           "error": row.get("error") or old.get("error")})
            continue
        changes = {}
        for field in COMPARED:
            was, now = old.get(field), row.get(field)
            if was == now:
                continue
            change = {"from": was, "to": now}
            # bool в Python — подкласс int, поэтому его нужно исключить явно:
            # иначе has_credit false -> true даст "дельту" в рублях.
            if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (was, now)):
                change["delta"] = round(now - was, 2)
                change["pct"] = round((now - was) / was * 100, 1) if was else None
            changes[field] = change
        # Значимость считается от эффективной цены, а не от regular_price:
        # витринная "старая цена" двигается сама по себе (см. KNOWLEDGE.md).
        signals = [x for x in (price_signal(effective_price(old), effective_price(row)),
                               credit_signal(old.get("has_credit"), row.get("has_credit")))
                   if x]
        result.append({"url": url, "status": "changed" if changes else "unchanged",
                       "changes": changes, "signals": signals,
                       "significant": bool(signals)})

    # Товары, которые были в прошлом прогоне и пропали из списка отслеживания.
    for url in before:
        if url not in after:
            result.append({"url": url, "status": "gone", "changes": {},
                           "signals": [], "significant": False})
    return result


def short(url):
    parts = url.rstrip("/").split("/")
    return parts[-2] if len(parts) > 2 else url


def print_diff(diff, show_all=False):
    """Печатает изменения. По умолчанию — только значимые (см. KNOWLEDGE.md)."""
    label = {"changed": "изменилось", "unchanged": "без изменений",
             "new": "новый товар", "gone": "убран из списка", "error": "не сверено"}
    significant = [i for i in diff if i["significant"]]
    skipped = [i for i in diff if not i["significant"]]

    print()
    print("значимые изменения относительно предыдущего прогона")
    print("-" * 70)
    if not significant:
        print("  значимых изменений нет")
    for item in significant:
        print(f"  {short(item['url'])}")
        for sig in item["signals"]:
            if sig["kind"] == "price":
                sign = "+" if sig["delta"] > 0 else ""
                print(f"{'':>6}{sig['text']}: {money(sig['from'])} -> {money(sig['to'])}"
                      f"  {sign}{sig['delta']:,.0f} ₽ ({sign}{sig['pct']}%)".replace(",", " "))
            else:
                print(f"{'':>6}{sig['text']}")
    print("-" * 70)
    below = sum(1 for i in skipped if i["status"] == "changed")
    tail = f"  значимых: {len(significant)} | отброшено: {len(skipped)}"
    if below:
        tail += f" (из них {below} с изменениями ниже порога)"
    print(tail)

    if show_all and skipped:
        print()
        print("отброшено по правилам значимости")
        print("-" * 70)
        for item in skipped:
            note = ", ".join(f"{f}: {c['from']} -> {c['to']}"
                             for f, c in item["changes"].items())
            print(f"  {short(item['url']):>14}  {label[item['status']]}"
                  f"{'  [' + note + ']' if note else ''}")


def run_filename(now=None):
    """Имя файла прогона: дата прогона в UTC, YYYY-MM-DD.json."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d") + ".json"


def save_run(rows, target, run_at=None):
    """Кладёт прогон в <каталог>/YYYY-MM-DD.json и возвращает путь.

    Файл готов к публикации в репозиторий tracker-data через GitHub MCP —
    локальный git здесь не задействован.
    """
    path = Path(target)
    if path.is_dir() or not path.suffix:
        path = path / run_filename()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wrap_run(rows, run_at), ensure_ascii=False, indent=2)
                    + "\n", encoding="utf-8")
    return path


def wrap_run(rows, run_at=None):
    """Прогон в файле: время прогона плюс таблица.

    Имя файла даёт только дату, а run_at — точный момент: по нему видно,
    сколько прошло между прогонами, и какой из двух прогонов за день записан.
    """
    return {"run_at": run_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": rows}


def load_run(path):
    """Читает файл прогона. Понимает и объект {run_at, rows}, и голый массив:
    первые прогоны писались до появления времени прогона."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("rows", []), data.get("run_at")
    return data, None


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


def exit_code(rows):
    ok = sum(1 for r in rows if not r["error"])
    return 0 if ok == len(rows) else (2 if ok == 0 else 1)


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
    parser.add_argument("--diff", metavar="ФАЙЛ",
                        help="файл предыдущего прогона: сравнить с ним текущий")
    parser.add_argument("--all-changes", action="store_true",
                        help="показать и незначимые изменения, отброшенные по правилам")
    args = parser.parse_args()

    rows = collect(TRACKED_URLS, args.dest, args.delay)
    columns = ROW + EXTRA if args.full else ROW
    trimmed = [{k: r[k] for k in columns} for r in rows]

    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    diff = None
    previous_at = None
    if args.diff:
        previous, previous_at = load_run(args.diff)
        diff = diff_runs(previous, trimmed)

    if args.json and diff is not None:
        print(json.dumps({"run_at": run_at, "run": trimmed, "diff": diff},
                         ensure_ascii=False, indent=2))
        return exit_code(rows)

    if args.save:
        path = save_run(trimmed, args.save, run_at)
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

    if diff is not None and not args.csv:
        if previous_at:
            print(f"\nпредыдущий прогон: {previous_at}")
        print_diff(diff, args.all_changes)
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
