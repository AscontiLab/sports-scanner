#!/usr/bin/env python3
"""Einheitlicher Telegram-Versand fuer Scanner."""

import requests

from .credentials import load_credentials

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(
    text: str,
    token: str,
    chat_id: str,
    parse_mode: str = "HTML",
    disable_preview: bool = True,
) -> bool:
    """Sendet eine Nachricht via Telegram Bot API."""
    if not token or not chat_id:
        print("Telegram: Token oder Chat-ID fehlt.")
        return False

    url = TELEGRAM_API_BASE.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as exc:
        print(f"Telegram-Fehler: {exc}")
        return False


def send_alert(
    text: str,
    parse_mode: str = "HTML",
    credentials: dict | None = None,
) -> bool:
    """Liest Token/Chat-ID aus Credentials und sendet eine Nachricht."""
    creds = credentials or load_credentials()
    token = creds.get("ASCONTILAB_BOT_TOKEN", "") or creds.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = creds.get("ASCONTILAB_CHAT_ID", "") or creds.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("Telegram nicht konfiguriert (ASCONTILAB_BOT_TOKEN / ASCONTILAB_CHAT_ID fehlt)")
        return False

    return send_message(text, token, chat_id, parse_mode=parse_mode)
