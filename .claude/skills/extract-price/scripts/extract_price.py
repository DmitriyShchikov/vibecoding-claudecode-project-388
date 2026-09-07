#!/usr/bin/env python3
"""Извлекает цену товара с одной страницы по URL.

Контракт: вход — один URL страницы товара, выход — объект
{regular_price, sale_price, has_credit}.

Usage:
    python3 extract_price.py <URL> [--dest -1257786] [--full] [--raw]

Exit codes: 0 — цена найдена, 1 — источник ответил, но цены нет, 2 — сеть/антибот.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from selectolax.parser import HTMLParser

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}
TIMEOUT = 40
CONTRACT = ("regular_price", "sale_price", "has_credit")

# Маркеры страниц-заглушек антибота. Без этой проверки парсер молча вернёт
# "цена не найдена" там, где на самом деле нас просто не пустили.
ANTIBOT = ("js-challenge", "servicepipe", "__wbaas", "sp_rotated_captcha",
           "cf-browser-verification", "Just a moment", "qrator")


class ExtractError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def fetch(url, session):
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ExtractError(f"сеть недоступна: {type(e).__name__}", code=2)
    if r.status_code >= 400:
        raise ExtractError(f"HTTP {r.status_code}", code=2)
    # Кодировку определяем по содержимому: holodilnik.ru отдаёт windows-1251,
    # без этого кириллица превращается в мусор.
    html = r.content.decode(r.apparent_encoding or "utf-8", errors="replace")
    if any(m.lower() in html.lower() for m in ANTIBOT) and len(html) < 50_000:
        raise ExtractError("страница закрыта антиботом, нужен другой источник", code=2)
    return html


def to_number(value):
    """'40 374 ₽' -> 40374.0, '1 234,50' -> 1234.5. Разделитель тысяч отбрасывается."""
    text = re.sub(r"[^\d,.]", "", str(value or "")).replace(",", ".")
    # Точка отделяет копейки, только если после неё ровно одна-две цифры.
    m = re.fullmatch(r"(.*?)\.(\d{1,2})", text)
    whole, frac = (m.group(1), m.group(2)) if m else (text, "")
    whole = re.sub(r"\D", "", whole)
    if not whole:
        return None
    return float(f"{whole}.{frac}") if frac else float(whole)


def node_number(tree, selector):
    node = tree.css_first(selector)
    return to_number(node.text()) if node else None


def iter_jsonld(tree):
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        yield from (data if isinstance(data, list) else [data])


def contract(regular, sale, has_credit):
    """Нормализует контракт: скидка только если она реально ниже обычной цены."""
    if regular is None:
        raise ExtractError("цена не найдена на странице")
    if sale is not None and sale >= regular:
        sale = None
    return {"regular_price": regular, "sale_price": sale, "has_credit": has_credit}


# --- адаптеры -------------------------------------------------------------

def wildberries(url, session, dest):
    """Цены отдаёт публичный JSON фронтенда; сама карточка закрыта антиботом."""
    m = re.search(r"/catalog/(\d+)/detail\.aspx", urlparse(url).path)
    if not m:
        raise ExtractError("не удалось выделить артикул (nm) из ссылки")
    nm = m.group(1)
    payload = None
    # answered — API ответил корректным JSON, просто без товара. Это отличает
    # снятый с продажи товар (постоянная ошибка) от недоступного API.
    answered = False
    # WB версионирует эндпоинт без предупреждения: v2 уже мёртв, живёт v4.
    for version in ("v4", "v3", "v2", "v1"):
        try:
            r = session.get(f"https://card.wb.ru/cards/{version}/detail",
                            params={"appType": 1, "curr": "rub", "dest": dest, "nm": nm},
                            headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.content:
            try:
                products = r.json().get("products") or []
            except json.JSONDecodeError:
                continue
            answered = True
            if products:
                payload = products[0]
                break
    if payload is None:
        if answered:
            raise ExtractError(f"товар {nm} не найден — снят с продажи или неверный артикул")
        raise ExtractError(f"card.wb.ru недоступен (артикул {nm}, dest={dest})", code=2)

    size = next((s for s in payload.get("sizes", []) if s.get("price")), {})
    price = size.get("price", {})
    if not price.get("product"):
        raise ExtractError(f"у товара {nm} нет цены — снят с продажи?")
    qty = sum(st.get("qty", 0) for s in payload.get("sizes", [])
              for st in s.get("stocks", []))
    # Цены WB приходят в копейках: basic — без скидки, product — со скидкой.
    result = contract(price["basic"] / 100, price["product"] / 100,
                      # В card.wb.ru признака рассрочки нет, а карточка закрыта
                      # антиботом. Не выдумываем false — источник не знает.
                      None)
    result.update({
        "source": "wildberries.ru", "id": str(payload["id"]),
        "name": payload.get("name"), "currency": "RUB",
        "availability": "InStock" if qty else "OutOfStock", "stock": qty,
        # Цена зависит от региона, поэтому dest — часть результата.
        "region": dest, "method": "card.wb.ru",
    })
    return result


def holodilnik(url, session):
    """Карточка: текущая и зачёркнутая цены лежат в отдельных элементах."""
    tree = HTMLParser(fetch(url, session))
    current = node_number(tree, ".product-price__current")
    old = node_number(tree, ".product-price__old")
    if current is None:  # запасной путь — микроразметка (там всегда цена со скидкой)
        node = tree.css_first('meta[itemprop="price"]')
        current = to_number(node.attributes.get("content")) if node else None
    # Блок «Купить в рассрочку» появляется не у всех товаров: на позициях
    # дешевле ~1000 ₽ его нет, так что это реальный признак, а не константа.
    has_credit = bool(tree.css_first(".credit_cont"))
    # itemprop="name" на карточке бывает пустым контейнером — h1 надёжнее.
    title = tree.css_first("h1") or tree.css_first('[itemprop="name"]')
    availability = tree.css_first('meta[itemprop="availability"]')

    result = contract(old or current, current if old else None, has_credit)
    result.update({
        "source": "holodilnik.ru", "id": "",
        "name": re.sub(r"\s+", " ", title.text()).strip()[:120] if title else None,
        "currency": "RUB",
        "availability": str(availability.attributes.get("content", "")).split("/")[-1]
                        if availability else "",
        "stock": None, "region": None, "method": "html+microdata",
    })
    return result


def regard(url, session):
    """JSON-LD для цены, флаги оплаты — из состояния фронтенда."""
    html = fetch(url, session)
    tree = HTMLParser(html)
    price = name = sku = availability = None
    for data in iter_jsonld(tree):
        if data.get("@type") == "Product":
            offer = data.get("offers") or {}
            offer = offer[0] if isinstance(offer, list) and offer else offer
            price = to_number(offer.get("price"))
            name, sku = data.get("name"), str(data.get("sku") or "")
            availability = str(offer.get("availability", "")).split("/")[-1]
            break
    # Флаги вида "credit":{"enabled":true,"from":3000,"to":500000} — с лимитами
    # по сумме, поэтому одного enabled мало, цена должна попадать в диапазон.
    has_credit = False
    for kind in ("credit", "installment"):
        m = re.search(rf'"{kind}":\{{"enabled":(true|false),"from":(\d+),"to":(\d+)', html)
        if m and m.group(1) == "true" and price is not None:
            if float(m.group(2)) <= price <= float(m.group(3)):
                has_credit = True
    result = contract(price, None, has_credit)
    result.update({
        "source": "regard.ru", "id": sku, "name": name, "currency": "RUB",
        "availability": availability, "stock": None, "region": None,
        "method": "json-ld",
    })
    return result


def generic(url, session):
    """schema.org: сначала JSON-LD, затем микроразметка. Скидку не разбираем."""
    tree = HTMLParser(fetch(url, session))
    host = urlparse(url).netloc

    for data in iter_jsonld(tree):
        if data.get("@type") != "Product":
            continue
        offer = data.get("offers") or {}
        offer = offer[0] if isinstance(offer, list) and offer else offer
        if offer.get("price"):
            result = contract(to_number(offer["price"]), None, None)
            result.update({
                "source": host, "id": str(data.get("sku") or ""),
                "name": data.get("name"),
                "currency": offer.get("priceCurrency", "RUB"),
                "availability": str(offer.get("availability", "")).split("/")[-1],
                "stock": None, "region": None, "method": "json-ld",
            })
            return result

    attr = lambda sel: (tree.css_first(sel).attributes.get("content")
                        if tree.css_first(sel) else None)
    price = attr('meta[itemprop="price"]') or attr('meta[property="product:price:amount"]')
    if price:
        title = tree.css_first("h1") or tree.css_first("title")
        result = contract(to_number(price), None, None)
        result.update({
            "source": host, "id": "",
            "name": re.sub(r"\s+", " ", title.text()).strip()[:120] if title else None,
            "currency": (attr('meta[itemprop="priceCurrency"]')
                         or attr('meta[property="product:price:currency"]') or "RUB"),
            "availability": str(attr('meta[itemprop="availability"]') or "").split("/")[-1],
            "stock": None, "region": None, "method": "microdata",
        })
        return result

    raise ExtractError("на странице нет разметки schema.org — нужен адаптер под этот сайт")


ADAPTERS = (("wildberries.ru", wildberries), ("holodilnik.ru", holodilnik),
            ("regard.ru", regard))


def extract(url, dest=-1257786):
    host = urlparse(url).netloc.lower()
    session = requests.Session()
    for domain, adapter in ADAPTERS:
        if host == domain or host.endswith("." + domain):
            result = adapter(url, session, dest) if adapter is wildberries \
                else adapter(url, session)
            break
    else:
        result = generic(url, session)
    result["url"] = url
    result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def main():
    parser = argparse.ArgumentParser(description="Извлечь цену товара по URL")
    parser.add_argument("url")
    parser.add_argument("--dest", type=int, default=-1257786,
                        help="код региона Wildberries (по умолчанию Москва)")
    parser.add_argument("--full", action="store_true",
                        help="добавить к контракту название, наличие, регион и время")
    parser.add_argument("--raw", action="store_true", help="JSON одной строкой")
    args = parser.parse_args()

    try:
        data = extract(args.url, args.dest)
    except ExtractError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code

    if not args.full:
        data = {k: data[k] for k in CONTRACT}
    print(json.dumps(data, ensure_ascii=False, **({} if args.raw else {"indent": 2})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
