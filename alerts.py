#!/usr/bin/env python3
"""
Telegram-Alerts für High-Edge-Bets.
Sendet Bets mit Edge > 10% via Telegram Bot.

Nutzt scanner_common.telegram fuer den eigentlichen Versand.

Credentials in ~/.stock_scanner_credentials:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
"""

# --- Neue zentrale Imports aus scanner_common ---
from scanner_common.telegram import send_message as _send_message_common
from scanner_common.credentials import load_credentials

# Abwaertskompatibilitaet: config.load_credentials bleibt als Fallback
# from config import load_credentials


def load_telegram_creds() -> tuple[str, str]:
    """Gibt (bot_token, chat_id) zurueck. Primaer AscontiLab Bot, Fallback alter Token."""
    creds = load_credentials()
    token = creds.get("ASCONTILAB_BOT_TOKEN", "") or creds.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = creds.get("ASCONTILAB_CHAT_ID", "") or creds.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """
    Sendet eine Nachricht via Telegram Bot API.

    DEPRECATED: Nutze scanner_common.telegram.send_message() direkt.
    Bleibt fuer Abwaertskompatibilitaet (wird intern von Scanner-spezifischen
    Funktionen aufgerufen).
    """
    return _send_message_common(message, bot_token, chat_id)


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
        bet_line = (
            f"{icon} <b>{b['match']}</b>\n"
            f"   → {b.get('tip', '?')} @ {b.get('best_odds', 0):.2f}\n"
            f"   Edge: {b.get('edge_pct', 0):.1f}% | Kelly: {b.get('kelly_pct', 0):.1f}%"
        )
        # CLV info if available
        clv_pct = b.get("clv_pct")
        closing_odds = b.get("closing_odds")
        if clv_pct is not None and closing_odds is not None:
            clv_icon = "✅" if clv_pct > 0 else "⚠️"
            bet_line += f"\n   {clv_icon} CLV: {clv_pct:+.1f}%"
        lines.append(bet_line)

    message = "\n".join(lines)
    if send_telegram(message, bot_token, chat_id):
        print(f"    Telegram: {len(high_edge)} High-Edge Bets gesendet")
        return len(high_edge)
    return 0


def send_freebet_advice(mode: str, params: dict) -> bool:
    """
    Sendet Freebet-Vorschlaege via Telegram.
    mode: 'qualifying' oder 'freebet'
    params: min_odds, amount, sport, etc.
    """
    from freebet_advisor import handle_api_request, format_telegram

    bot_token, chat_id = load_telegram_creds()
    if not bot_token or not chat_id:
        print("    Telegram nicht konfiguriert")
        return False

    params["mode"] = mode
    result = handle_api_request(params)

    if result.get("error"):
        send_telegram(f"Fehler: {result['error']}", bot_token, chat_id)
        return False

    if result["count"] == 0:
        send_telegram("Keine passenden Vorschlaege gefunden.", bot_token, chat_id)
        return True

    msg = result["telegram"]
    return send_telegram(msg, bot_token, chat_id)


def send_clv_summary_alert(days: int = 7) -> bool:
    """
    Sendet eine CLV-Zusammenfassung der letzten N Tage via Telegram.
    Gibt True zurueck wenn Nachricht gesendet wurde.
    """
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timezone, timedelta

    bot_token, chat_id = load_telegram_creds()
    if not bot_token or not chat_id:
        return False

    db_path = Path(__file__).parent / "sports_backtesting.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Check if clv_pct column exists
    cols = [c[1] for c in conn.execute("PRAGMA table_info(predictions)").fetchall()]
    if "clv_pct" not in cols:
        conn.close()
        return False

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT clv_pct, bet_won
        FROM predictions
        WHERE placed = 1
          AND bet_won IS NOT NULL
          AND clv_pct IS NOT NULL
          AND commence_time >= ?
        """,
        (cutoff,),
    ).fetchall()
    conn.close()

    if not rows:
        return False

    clv_values = [r["clv_pct"] for r in rows]
    won_clv = [r["clv_pct"] for r in rows if r["bet_won"] == 1]
    lost_clv = [r["clv_pct"] for r in rows if r["bet_won"] == 0]
    positive_count = sum(1 for v in clv_values if v > 0)
    total = len(clv_values)
    avg_clv = sum(clv_values) / total
    avg_won = sum(won_clv) / len(won_clv) if won_clv else 0
    avg_lost = sum(lost_clv) / len(lost_clv) if lost_clv else 0

    lines = [
        f"📊 <b>CLV-Zusammenfassung (letzte {days} Tage)</b>\n",
        f"∅ CLV: {avg_clv:+.1f}%",
        f"Positive CLV: {positive_count / total * 100:.0f}% ({positive_count}/{total})",
        f"∅ CLV gewonnen: {avg_won:+.1f}%",
        f"∅ CLV verloren: {avg_lost:+.1f}%",
        "",
    ]
    if avg_clv > 0:
        lines.append("→ Dein Modell schlaegt den Markt ✓")
    else:
        lines.append("→ Markt schliesst zu deinen Gunsten — Edge pruefen ⚠")

    message = "\n".join(lines)
    if send_telegram(message, bot_token, chat_id):
        print(f"    Telegram: CLV-Zusammenfassung ({days} Tage) gesendet")
        return True
    return False


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
