#!/usr/bin/env python3
"""Извлекает цену товара с одной страницы по URL.

Usage:
    python3 extract_price.py <URL> [--dest -1257786] [--raw]

Exit codes: 0 — цена найдена, 1 — не найдена/ошибка разбора, 2 — сеть/антибот.
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
    # Кодировку берём из заголовков/мета, иначе кириллица превращается в мусор
    # (holodilnik.ru отдаёт windows-1251).
    html = r.content.decode(r.apparent_encoding or "utf-8", errors="replace")
    if any(m.lower() in html.lower() for m in ANTIBOT) and len(html) < 50_000:
        raise ExtractError("страница закрыта антиботом, нужен другой источник", code=2)
    return html


def to_number(value):
    digits = re.sub(r"[^\d]", "", str(value or "").split(",")[0].split(".")[0])
    return int(digits) if digits else None


def iter_jsonld(tree):
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        yield from (data if isinstance(data, list) else [data])


# --- адаптеры -------------------------------------------------------------

def wildberries(url, session, dest):
    """Цены отдаёт публичный JSON фронтенда; сама карточка закрыта антиботом."""
    m = re.search(r"/catalog/(\d+)/detail\.aspx", urlparse(url).path)
    if not m:
        raise ExtractError("не удалось выделить артикул (nm) из ссылки")
    nm = m.group(1)
    payload = None
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
            if products:
                payload = products[0]
                break
    if payload is None:
        raise ExtractError(f"card.wb.ru не вернул товар {nm} (проверь dest={dest})", code=2)

    size = next((s for s in payload.get("sizes", []) if s.get("price")), {})
    price = size.get("price", {})
    if not price.get("product"):
        raise ExtractError(f"у товара {nm} нет цены — снят с продажи?")
    qty = sum(st.get("qty", 0) for s in payload.get("sizes", [])
              for st in s.get("stocks", []))
    return {
        "source": "wildberries.ru",
        "id": str(payload["id"]),
        "name": payload.get("name"),
        # Цены WB приходят в копейках.
        "price": price["product"] / 100,
        "price_old": (price.get("basic") or 0) / 100 or None,
        "currency": "RUB",
        "availability": "InStock" if qty else "OutOfStock",
        "stock": qty,
        # Цена зависит от региона, поэтому dest — часть результата, а не деталь запроса.
        "region": dest,
    }


def generic(url, session):
    """schema.org: сначала JSON-LD, затем микроразметка, затем og-теги."""
    tree = HTMLParser(fetch(url, session))
    host = urlparse(url).netloc

    for data in iter_jsonld(tree):
        if data.get("@type") != "Product":
            continue
        offer = data.get("offers") or {}
        offer = offer[0] if isinstance(offer, list) and offer else offer
        if offer.get("price"):
            return {
                "source": host,
                "id": str(data.get("sku") or ""),
                "name": data.get("name"),
                "price": float(offer["price"]),
                "price_old": None,
                "currency": offer.get("priceCurrency", "RUB"),
                "availability": str(offer.get("availability", "")).split("/")[-1],
                "stock": None,
                "region": None,
                "method": "json-ld",
            }

    attr = lambda sel: (tree.css_first(sel).attributes.get("content")
                        if tree.css_first(sel) else None)
    price = attr('meta[itemprop="price"]') or attr('meta[property="product:price:amount"]')
    if price:
        title = tree.css_first("h1") or tree.css_first("title")
        return {
            "source": host,
            "id": "",
            "name": re.sub(r"\s+", " ", title.text()).strip()[:120] if title else None,
            "price": float(to_number(price) or 0),
            "price_old": None,
            "currency": (attr('meta[itemprop="priceCurrency"]')
                         or attr('meta[property="product:price:currency"]') or "RUB"),
            "availability": str(attr('meta[itemprop="availability"]') or "").split("/")[-1],
            "stock": None,
            "region": None,
            "method": "microdata",
        }

    raise ExtractError("на странице нет разметки schema.org — нужен адаптер под этот сайт")


def extract(url, dest=-1257786):
    host = urlparse(url).netloc.lower()
    session = requests.Session()
    if host.endswith("wildberries.ru"):
        result = wildberries(url, session, dest)
        result["method"] = "card.wb.ru"
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
    parser.add_argument("--raw", action="store_true", help="вывести JSON без форматирования")
    args = parser.parse_args()

    try:
        data = extract(args.url, args.dest)
    except ExtractError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code

    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
