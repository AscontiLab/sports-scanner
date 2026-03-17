#!/usr/bin/env python3
"""
Telegram-Alerts für High-Edge-Bets.
Sendet Bets mit Edge > 10% via Telegram Bot.

Credentials in ~/.stock_scanner_credentials:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
"""

import requests

from config import load_credentials


def load_telegram_creds() -> tuple[str, str]:
    """Gibt (bot_token, chat_id) zurück."""
    creds = load_credentials()
    return creds.get("TELEGRAM_BOT_TOKEN", ""), creds.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """Sendet eine Nachricht via Telegram Bot API."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"    Telegram-Fehler: {e}")
        return False


def send_high_edge_alerts(all_bets: list, min_edge: float = 10.0) -> int:
    """
    Sendet Telegram-Alert für Bets mit Edge >= min_edge.
    Gibt Anzahl gesendeter Alerts zurück.
    """
    bot_token, chat_id = load_telegram_creds()
    if not bot_token or not chat_id:
        print("    Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlt)")
        return 0

    high_edge = [b for b in all_bets if b.get("edge_pct", 0) >= min_edge]
    if not high_edge:
        return 0

    lines = [f"🚨 <b>{len(high_edge)} High-Edge Bets (≥{min_edge:.0f}%)</b>\n"]
    for b in sorted(high_edge, key=lambda x: -x.get("edge_pct", 0)):
        bet_type = b.get("type", b.get("bet_type", "?"))
        icon = "⚽" if "football" in bet_type else ("🎾" if bet_type == "tennis" else "🏆")
        lines.append(
            f"{icon} <b>{b['match']}</b>\n"
            f"   → {b.get('tip', '?')} @ {b.get('best_odds', 0):.2f}\n"
            f"   Edge: {b.get('edge_pct', 0):.1f}% | Kelly: {b.get('kelly_pct', 0):.1f}%"
        )

    message = "\n".join(lines)
    if send_telegram(message, bot_token, chat_id):
        print(f"    Telegram: {len(high_edge)} High-Edge Bets gesendet")
        return len(high_edge)
    return 0


def send_tuning_alert(tuning_report: dict, bankroll_info: dict) -> bool:
    """
    Sendet Telegram-Alert wenn Tuning-Report kritisch ist.
    Wird nach jedem Resolve aufgerufen.
    """
    alert_level = tuning_report.get("alert_level", "ok")
    if alert_level == "ok":
        return False

    bot_token, chat_id = load_telegram_creds()
    if not bot_token or not chat_id:
        return False

    overall = tuning_report.get("overall", {})
    recs = tuning_report.get("recommendations", [])
    bk = bankroll_info.get("bankroll", 0)
    pnl = bankroll_info.get("total_pnl", 0)

    icon = "🔴" if alert_level == "critical" else "🟡"
    lines = [
        f"{icon} <b>Sports Scanner Tuning ({alert_level.upper()})</b>\n",
        f"💰 Bankroll: {bk:.2f} EUR (PnL: {pnl:+.2f})",
        f"📊 Win-Rate: {overall.get('win_rate', 0):.0f}% | ROI: {overall.get('roi', 0):.1f}%",
        f"📈 Avg Edge: {overall.get('avg_edge', 0):.1f}% | Avg Odds: {overall.get('avg_odds', 0):.2f}",
        "",
    ]
    if recs:
        lines.append("<b>Empfehlungen:</b>")
        for r in recs:
            lines.append(f"⚠️ {r}")

    message = "\n".join(lines)
    if send_telegram(message, bot_token, chat_id):
        print(f"    Telegram: Tuning-Alert ({alert_level}) gesendet")
        return True
    return False
