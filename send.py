#!/usr/bin/env python3
"""Отправляет сообщение в Telegram.

Только стандартная библиотека — ни requests, ни python-dotenv.

Usage:
    python3 send.py "текст"
    echo "текст" | python3 send.py -

Токен и адрес чата берутся из переменных окружения TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID, а если их там нет — из файла .env рядом со скриптом.

Exit codes: 0 — отправлено, 1 — нет токена или текста, 2 — ошибка сети или API.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 30
# Жёсткий лимит Telegram на одно сообщение. Более длинный текст API отклоняет,
# поэтому режем его на части сами.
MAX_LEN = 4096


class SendError(Exception):
    """Отправка не удалась. code — код возврата из контракта в докстроке.

    Обычное исключение, а не SystemExit: send.py зовут и как модуль (так его
    вызывает tracker), а модуль не должен ронять чужой процесс.
    """

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def read_dotenv(path):
    """Разбирает .env: KEY=VALUE построчно, комментарии и кавычки отбрасывает."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def credentials():
    """Переменные окружения приоритетнее .env: так окружение переопределяет файл."""
    dotenv = read_dotenv(Path(__file__).resolve().parent / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or dotenv.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or dotenv.get("TELEGRAM_CHAT_ID")
    missing = [name for name, value in
               (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
               if not value]
    if missing:
        # Код 1: это не сбой связи, а незаполненная конфигурация.
        raise SendError(f"не заданы {', '.join(missing)} — "
                        f"положите их в переменные окружения или в .env", code=1)
    return token, chat_id


def split_message(text, limit=MAX_LEN):
    """Режет длинный текст по границам строк, чтобы не рвать слова посередине."""
    if len(text) <= limit:
        return [text]
    parts, chunk = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:            # одна строка длиннее лимита
            # Набранное уходит первым: иначе куски длинной строки встанут
            # впереди него и сообщения придут в перепутанном порядке.
            if chunk:
                parts.append(chunk)
                chunk = ""
            parts.append(line[:limit])
            line = line[limit:]
        if chunk and len(chunk) + len(line) > limit:
            parts.append(chunk)
            chunk = ""
        chunk += line
    if chunk:
        parts.append(chunk)
    return parts


def send(text, token=None, chat_id=None):
    """Отправляет текст, при необходимости несколькими сообщениями."""
    if token is None or chat_id is None:
        token, chat_id = credentials()
    sent = []
    for part in split_message(text):
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        request = urllib.request.Request(API.format(token=token), data=payload)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Telegram кладёт причину отказа в тело ответа, а не в код статуса,
            # поэтому его нужно прочитать — иначе останется невнятное "400".
            try:
                detail = json.loads(e.read().decode("utf-8")).get("description", "")
            except Exception:
                detail = e.reason
            raise SendError(f"Telegram отклонил запрос — {detail}")
        except urllib.error.URLError as e:
            raise SendError(f"сеть недоступна — {e.reason}")
        if not answer.get("ok"):
            raise SendError(answer.get("description", "неизвестный отказ"))
        sent.append(answer["result"]["message_id"])
    return sent


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    text = sys.stdin.read() if argv[1] == "-" else argv[1]
    if not text.strip():
        print("ОШИБКА: пустой текст сообщения", file=sys.stderr)
        return 1
    try:
        ids = send(text)
    except SendError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    print(f"отправлено: {len(ids)} сообщ. (id {', '.join(map(str, ids))})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
