#!/usr/bin/env python3
"""Отправляет текстовое сообщение в Telegram через Bot API.

Вход — готовый текст: аргументом или на stdin. Форматированием текста скрипт не
занимается, его дело — доставка.

Usage:
    python3 send.py "текст сообщения"
    python3 tracker.py --diff prev.json --notify   # текст готовит tracker

Токен и чат берутся из переменных окружения TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID, а если их нет — из файла .env в корне репозитория.

Exit codes: 0 — отправлено, 1 — Telegram отклонил или сеть недоступна,
2 — нет токена или chat_id, 3 — пустой текст.
"""

import argparse
import os
import sys
from pathlib import Path

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 30
# Жёсткий предел Telegram на одно сообщение — длинную сводку режем на части.
MAX_LEN = 4096
# .claude/skills/tracker/scripts/send.py -> корень репозитория
ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


class SendError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def read_env_file(path=ENV_FILE):
    """Разбирает .env в словарь. Файла нет — пустой словарь, это не ошибка:
    в облачном окружении те же переменные задаются в настройках контейнера."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credentials(token=None, chat_id=None):
    """Токен и чат: аргументы, иначе окружение, иначе .env."""
    if token and chat_id:
        return token, chat_id
    env = read_env_file()
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    missing = [name for name, value in
               (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)) if not value]
    if missing:
        raise SendError(
            "нет доступа к боту: не задано " + " и ".join(missing) +
            f" (переменные окружения или {ENV_FILE.name} в корне репозитория)", code=2)
    return token, chat_id


def split_message(text, limit=MAX_LEN):
    """Режет текст на куски не длиннее limit, по границам строк.

    Строка длиннее лимита рвётся посимвольно — иначе кусок не влезет и Telegram
    отклонит всё сообщение целиком.
    """
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def send(text, token=None, chat_id=None):
    """Отправляет текст в чат и возвращает id отправленных сообщений."""
    text = (text or "").strip()
    if not text:
        raise SendError("пустой текст: отправлять нечего", code=3)
    token, chat_id = credentials(token, chat_id)
    sent = []
    for chunk in split_message(text):
        payload = {"chat_id": chat_id, "text": chunk,
                   "disable_web_page_preview": True}
        try:
            r = requests.post(API.format(token=token), data=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SendError(f"сеть недоступна: {type(e).__name__}", code=1)
        try:
            data = r.json()
        except ValueError:
            raise SendError(f"Telegram ответил не JSON: HTTP {r.status_code}", code=1)
        if not data.get("ok"):
            # Токен в текст ошибки не попадает: описание от Telegram его не содержит.
            raise SendError(
                f"Telegram отклонил сообщение: HTTP {r.status_code} "
                f"{data.get('description', 'без описания')}", code=1)
        sent.append(data["result"]["message_id"])
    return sent


def main():
    parser = argparse.ArgumentParser(description="Отправка сообщения в Telegram")
    parser.add_argument("text", nargs="?",
                        help="текст сообщения; без аргумента читается со stdin")
    args = parser.parse_args()

    text = args.text if args.text is not None else sys.stdin.read()
    try:
        sent = send(text)
    except SendError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    print(f"отправлено в Telegram: сообщений {len(sent)}, id {', '.join(map(str, sent))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
