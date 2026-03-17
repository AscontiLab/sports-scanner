#!/usr/bin/env python3
"""
Schreibt Sports-Dashboard-Daten als JSON fuer das n8n Dashboard.

Wird nach jedem Scanner-Lauf aufgerufen und erzeugt:
- output/sports_bankroll.json — Bankroll-Verlauf + Performance-Stats
- output/sports_tuning.json — Auto-Tuning-Report mit Empfehlungen

Das n8n Dashboard liest diese Dateien ueber serve_output.py.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bankroll_manager import (
    get_bankroll_history,
    get_peak_and_drawdown,
    update_bankroll_from_results,
    generate_tuning_report,
)
from config import STARTING_BANKROLL, ALL_LABELS

_DB_PATH = Path(__file__).parent / "sports_backtesting.db"
_OUTPUT_DIR = Path(__file__).parent / "output"


def _get_todays_bets() -> list[dict]:
    """Liest heutige Selected Bets aus der DB (dedupliziert)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sport_key, home_team, away_team, tip,
               MAX(best_odds) AS best_odds,
               edge_pct, MAX(stake_eur) AS stake_eur, tier,
               confidence_score, bet_won, pnl_eur,
               commence_time, bet_type
        FROM predictions
        WHERE selected = 1
          AND SUBSTR(commence_time, 1, 10) = ?
        GROUP BY home_team, away_team, tip
        ORDER BY commence_time ASC
        """,
        (today,),
    ).fetchall()
    conn.close()

    labels = ALL_LABELS
    bets = []
    for r in rows:
        status = "offen"
        if r["bet_won"] == 1:
            status = "gewonnen"
        elif r["bet_won"] == 0:
            status = "verloren"

        bets.append({
            "league": labels.get(r["sport_key"], r["sport_key"]),
            "match": f"{r['home_team']} – {r['away_team']}",
            "tip": r["tip"],
            "odds": r["best_odds"],
            "edge": round(r["edge_pct"], 1),
            "stake": r["stake_eur"],
            "tier": r["tier"],
            "score": r["confidence_score"],
            "status": status,
            "pnl": round(r["pnl_eur"], 2) if r["pnl_eur"] is not None else None,
            "kick_off": r["commence_time"],
            "bet_type": r["bet_type"],
        })
    return bets


def _get_recent_bets(days: int = 7) -> list[dict]:
    """Liest die letzten N Tage Selected Bets aus der DB (dedupliziert)."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sport_key, home_team, away_team, tip,
               ROUND(MAX(best_odds), 2) AS best_odds,
               edge_pct, MAX(stake_eur) AS stake_eur, tier,
               confidence_score, bet_won, pnl_eur,
               commence_time, bet_type
        FROM predictions
        WHERE selected = 1
          AND bet_won IS NOT NULL
        GROUP BY home_team, away_team, tip
        ORDER BY commence_time DESC
        LIMIT 50
        """,
    ).fetchall()
    conn.close()

    labels = ALL_LABELS
    bets = []
    for r in rows:
        status = "gewonnen" if r["bet_won"] == 1 else "verloren"
        bets.append({
            "league": labels.get(r["sport_key"], r["sport_key"]),
            "match": f"{r['home_team']} – {r['away_team']}",
            "tip": r["tip"],
            "odds": r["best_odds"],
            "edge": round(r["edge_pct"], 1),
            "stake": r["stake_eur"],
            "tier": r["tier"],
            "score": r["confidence_score"],
            "status": status,
            "pnl": round(r["pnl_eur"], 2) if r["pnl_eur"] is not None else None,
            "kick_off": r["commence_time"],
        })
    return bets


def _calculate_streak() -> dict:
    """Berechnet aktuelle Win/Lose-Streak."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT bet_won FROM predictions
        WHERE selected = 1 AND bet_won IS NOT NULL
        ORDER BY commence_time DESC
        LIMIT 20
        """,
    ).fetchall()
    conn.close()

    if not rows:
        return {"type": "none", "count": 0}

    first = int(rows[0]["bet_won"])
    streak_type = "win" if first == 1 else "lose"
    count = 0
    for r in rows:
        if int(r["bet_won"]) == first:
            count += 1
        else:
            break

    return {"type": streak_type, "count": count}


def main():
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Bankroll-Daten
    bankroll_info = update_bankroll_from_results()
    history = get_bankroll_history(limit=60)
    peak_dd = get_peak_and_drawdown()
    streak = _calculate_streak()
    todays_bets = _get_todays_bets()
    recent_bets = _get_recent_bets()

    # Win-Rate und ROI berechnen
    total_resolved = bankroll_info["resolved_bets"]
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    won = conn.execute(
        "SELECT COUNT(*) AS cnt FROM predictions WHERE selected=1 AND bet_won=1"
    ).fetchone()["cnt"]
    total_stake = conn.execute(
        "SELECT COALESCE(SUM(stake_eur),0) AS total FROM predictions WHERE selected=1 AND bet_won IS NOT NULL"
    ).fetchone()["total"]
    conn.close()
    win_rate = round(won / total_resolved * 100, 1) if total_resolved > 0 else 0.0
    roi = round(bankroll_info["total_pnl"] / total_stake * 100, 1) if total_stake > 0 else 0.0

    bankroll_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bankroll": {
            "current": bankroll_info["bankroll"],
            "starting": STARTING_BANKROLL,
            "total_pnl": bankroll_info["total_pnl"],
            "resolved_bets": total_resolved,
            "won": won,
            "win_rate": win_rate,
            "roi": roi,
        },
        "peak_drawdown": peak_dd,
        "streak": streak,
        "history": list(reversed(history)),  # chronologisch
        "todays_bets": todays_bets,
        "recent_bets": recent_bets,
    }

    bankroll_path = _OUTPUT_DIR / "sports_bankroll.json"
    bankroll_path.write_text(
        json.dumps(bankroll_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[Dashboard] Bankroll-Daten: {bankroll_path}")

    # Tuning-Report
    tuning = generate_tuning_report()
    tuning["generated_at"] = datetime.now(timezone.utc).isoformat()

    tuning_path = _OUTPUT_DIR / "sports_tuning.json"
    tuning_path.write_text(
        json.dumps(tuning, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[Dashboard] Tuning-Report: {tuning_path}")
    print(f"[Dashboard] Alert-Level: {tuning['alert_level']}")

    if tuning["recommendations"]:
        print("[Dashboard] Empfehlungen:")
        for rec in tuning["recommendations"]:
            print(f"  ⚠ {rec}")


if __name__ == "__main__":
    main()
