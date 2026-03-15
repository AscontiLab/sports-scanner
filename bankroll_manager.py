#!/usr/bin/env python3
"""
Bankroll-Manager für den Sports Value Scanner.

Verwaltet die Bankroll über SQLite-Tabellen in der bestehenden
sports_backtesting.db. Berechnet Stakes via Quarter-Kelly und
trackt tägliche Snapshots.
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import (
    STARTING_BANKROLL,
    KELLY_FRACTION,
    MAX_DAILY_BETS,
    MAX_DAILY_RISK_PCT,
    MIN_STAKE_EUR,
    SPORT_LABELS,
    UEFA_LABELS,
)

_DB_PATH = Path(__file__).parent / "sports_backtesting.db"

_BANKROLL_SCHEMA = """
CREATE TABLE IF NOT EXISTS bankroll_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL UNIQUE,
    bankroll     REAL    NOT NULL,
    day_pnl      REAL    DEFAULT 0.0,
    bets_placed  INTEGER DEFAULT 0,
    bets_won     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bankroll_config (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    starting_bankroll  REAL    NOT NULL,
    kelly_fraction     REAL    NOT NULL,
    max_daily_risk     REAL    NOT NULL,
    max_bets_per_day   INTEGER NOT NULL,
    min_stake_eur      REAL    NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_bankroll(starting: float = STARTING_BANKROLL) -> None:
    """Erstellt Bankroll-Tabellen und initialisiert Config + ersten Snapshot."""
    with _connect() as conn:
        conn.executescript(_BANKROLL_SCHEMA)

        # Config einfügen falls nicht vorhanden
        existing = conn.execute("SELECT id FROM bankroll_config WHERE id = 1").fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO bankroll_config
                   (id, starting_bankroll, kelly_fraction, max_daily_risk,
                    max_bets_per_day, min_stake_eur)
                   VALUES (1, ?, ?, ?, ?, ?)""",
                (starting, KELLY_FRACTION, MAX_DAILY_RISK_PCT,
                 MAX_DAILY_BETS, MIN_STAKE_EUR),
            )

        # Erster Snapshot falls keine vorhanden
        has_snapshot = conn.execute(
            "SELECT id FROM bankroll_snapshots LIMIT 1"
        ).fetchone()
        if not has_snapshot:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT OR IGNORE INTO bankroll_snapshots (date, bankroll) VALUES (?, ?)",
                (today, starting),
            )
            print(f"[Bankroll] Initialisiert: {starting:.2f} EUR")
        else:
            print(f"[Bankroll] Bereits initialisiert")


def get_current_bankroll() -> float:
    """Gibt den aktuellen Bankroll-Stand aus dem letzten Snapshot zurück."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT bankroll FROM bankroll_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            return float(row["bankroll"])
        return STARTING_BANKROLL


def calculate_stake(kelly_pct: float, bankroll: float | None = None) -> float:
    """
    Berechnet den Einsatz in EUR via Quarter-Kelly.

    kelly_pct: Kelly-Prozent aus dem Scanner (z.B. 2.5 für 2.5%)
    bankroll:  aktuelle Bankroll in EUR (None → aus DB lesen)

    Returns: Stake in EUR (mindestens MIN_STAKE_EUR, max Bankroll × MAX_DAILY_RISK_PCT)
    """
    if bankroll is None:
        bankroll = get_current_bankroll()

    # kelly_pct ist als Prozent gespeichert (z.B. 2.5 = 2.5%)
    kelly_fraction_of_bankroll = (kelly_pct / 100.0) * KELLY_FRACTION * bankroll

    # Minimum und Maximum
    stake = max(kelly_fraction_of_bankroll, MIN_STAKE_EUR)
    max_single = bankroll * MAX_DAILY_RISK_PCT * 0.5  # Einzelbet max 50% des Tagesbudgets
    stake = min(stake, max_single)

    # Auf Cent runden
    return round(stake, 2)


def get_daily_budget(bankroll: float | None = None) -> dict:
    """
    Gibt das heutige Budget zurück.

    Returns:
        {
            "bankroll": float,
            "max_risk_eur": float,
            "max_bets": int,
            "budget_per_bet_avg": float,
        }
    """
    if bankroll is None:
        bankroll = get_current_bankroll()

    max_risk = bankroll * MAX_DAILY_RISK_PCT

    return {
        "bankroll": bankroll,
        "max_risk_eur": round(max_risk, 2),
        "max_bets": MAX_DAILY_BETS,
        "budget_per_bet_avg": round(max_risk / MAX_DAILY_BETS, 2),
    }


def record_daily_snapshot(
    date_str: str | None = None,
    bankroll: float | None = None,
) -> None:
    """
    Speichert den Tages-Snapshot in die DB.
    Berechnet day_pnl, bets_placed, bets_won aus den resolved Predictions des Tages.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _connect() as conn:
        # Tages-Statistik aus Predictions berechnen
        stats = conn.execute(
            """
            SELECT
                COALESCE(SUM(pnl_eur), 0.0) AS day_pnl,
                COUNT(*) AS bets_placed,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS bets_won
            FROM predictions
            WHERE selected = 1
              AND SUBSTR(commence_time, 1, 10) = ?
              AND bet_won IS NOT NULL
            """,
            (date_str,),
        ).fetchone()

        day_pnl = float(stats["day_pnl"]) if stats["day_pnl"] else 0.0
        bets_placed = int(stats["bets_placed"]) if stats["bets_placed"] else 0
        bets_won = int(stats["bets_won"]) if stats["bets_won"] else 0

        if bankroll is None:
            # Für idempotente Tages-Snapshots immer vom Stand vor diesem Datum ausgehen.
            previous_snapshot = conn.execute(
                """
                SELECT bankroll
                FROM bankroll_snapshots
                WHERE date < ?
                ORDER BY date DESC
                LIMIT 1
                """,
                (date_str,),
            ).fetchone()
            base_bankroll = (
                float(previous_snapshot["bankroll"])
                if previous_snapshot
                else STARTING_BANKROLL
            )
            bankroll = base_bankroll + day_pnl

        conn.execute(
            """INSERT OR REPLACE INTO bankroll_snapshots
               (date, bankroll, day_pnl, bets_placed, bets_won)
               VALUES (?, ?, ?, ?, ?)""",
            (date_str, round(bankroll, 2), round(day_pnl, 2),
             bets_placed, bets_won),
        )
        print(f"[Bankroll] Snapshot {date_str}: {bankroll:.2f} EUR "
              f"(PnL: {day_pnl:+.2f}, Bets: {bets_placed}, Won: {bets_won})")


def rebuild_all_snapshots() -> None:
    """
    Erstellt/aktualisiert Snapshots fuer ALLE Tage mit resolved Selected Bets.

    Das Problem: record_daily_snapshot(today) schreibt nur den heutigen Snapshot,
    aber Bets von gestern werden erst heute resolved. Diese Funktion baut alle
    historischen Snapshots korrekt auf, kumulativ ab STARTING_BANKROLL.
    """
    with _connect() as conn:
        # Alle Tage mit resolved selected Bets
        days = conn.execute(
            """
            SELECT DISTINCT SUBSTR(commence_time, 1, 10) AS day
            FROM predictions
            WHERE selected = 1 AND bet_won IS NOT NULL
            ORDER BY day
            """
        ).fetchall()

        if not days:
            print("[Bankroll] Keine resolved Bets — keine Snapshots zu rebuilden.")
            return

        config = conn.execute(
            "SELECT starting_bankroll FROM bankroll_config WHERE id = 1"
        ).fetchone()
        starting = float(config["starting_bankroll"]) if config else STARTING_BANKROLL

        cumulative_bankroll = starting
        rebuilt = 0

        for row in days:
            day = row["day"]
            stats = conn.execute(
                """
                SELECT
                    COALESCE(SUM(pnl_eur), 0.0) AS day_pnl,
                    COUNT(*) AS bets_placed,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS bets_won
                FROM predictions
                WHERE selected = 1
                  AND SUBSTR(commence_time, 1, 10) = ?
                  AND bet_won IS NOT NULL
                """,
                (day,),
            ).fetchone()

            day_pnl = float(stats["day_pnl"]) if stats["day_pnl"] else 0.0
            bets_placed = int(stats["bets_placed"]) if stats["bets_placed"] else 0
            bets_won = int(stats["bets_won"]) if stats["bets_won"] else 0

            cumulative_bankroll += day_pnl

            conn.execute(
                """INSERT OR REPLACE INTO bankroll_snapshots
                   (date, bankroll, day_pnl, bets_placed, bets_won)
                   VALUES (?, ?, ?, ?, ?)""",
                (day, round(cumulative_bankroll, 2), round(day_pnl, 2),
                 bets_placed, bets_won),
            )
            rebuilt += 1

        print(f"[Bankroll] {rebuilt} Snapshots rebuilt. "
              f"Bankroll: {starting:.2f} → {cumulative_bankroll:.2f} EUR")


def generate_tuning_report() -> dict:
    """
    Analysiert die Performance und generiert Tuning-Empfehlungen.

    Returns:
        {
            "overall": {...},
            "by_league": [...],
            "by_edge_range": [...],
            "by_odds_range": [...],
            "by_bet_type": [...],
            "recommendations": [str, ...],
            "alert_level": "ok" | "warning" | "critical"
        }
    """
    with _connect() as conn:
        # Gesamtperformance (selected resolved)
        overall = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_eur), 2) AS total_pnl,
                ROUND(SUM(stake_eur), 2) AS total_stake,
                ROUND(AVG(edge_pct), 1) AS avg_edge,
                ROUND(AVG(best_odds), 2) AS avg_odds
            FROM predictions
            WHERE selected = 1 AND bet_won IS NOT NULL
            """
        ).fetchone()

        total = int(overall["total"]) if overall["total"] else 0
        won = int(overall["won"]) if overall["won"] else 0
        total_pnl = float(overall["total_pnl"]) if overall["total_pnl"] else 0.0
        total_stake = float(overall["total_stake"]) if overall["total_stake"] else 0.0
        win_rate = (won / total * 100) if total > 0 else 0.0
        roi = (total_pnl / total_stake * 100) if total_stake > 0 else 0.0

        overall_data = {
            "total": total, "won": won, "win_rate": round(win_rate, 1),
            "total_pnl": total_pnl, "total_stake": total_stake,
            "roi": round(roi, 1),
            "avg_edge": float(overall["avg_edge"]) if overall["avg_edge"] else 0.0,
            "avg_odds": float(overall["avg_odds"]) if overall["avg_odds"] else 0.0,
        }

        # Performance nach Liga
        by_league = []
        rows = conn.execute(
            """
            SELECT sport_key,
                COUNT(*) AS total,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_eur), 2) AS pnl,
                ROUND(SUM(stake_eur), 2) AS stake
            FROM predictions
            WHERE selected = 1 AND bet_won IS NOT NULL
            GROUP BY sport_key ORDER BY pnl
            """
        ).fetchall()
        _labels = {**SPORT_LABELS, **UEFA_LABELS}
        for r in rows:
            t = int(r["total"])
            w = int(r["won"]) if r["won"] else 0
            pnl = float(r["pnl"]) if r["pnl"] else 0.0
            stake = float(r["stake"]) if r["stake"] else 0.0
            by_league.append({
                "league": _labels.get(r["sport_key"], r["sport_key"]),
                "total": t, "won": w,
                "win_rate": round(w / t * 100, 1) if t else 0,
                "pnl": pnl,
                "roi": round(pnl / stake * 100, 1) if stake else 0,
            })

        # Performance nach Edge-Range
        by_edge = []
        for low, high, label in [(3, 10, "3-10%"), (10, 15, "10-15%"),
                                  (15, 20, "15-20%"), (20, 100, "20%+")]:
            r = conn.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                    ROUND(SUM(pnl_eur), 2) AS pnl
                FROM predictions
                WHERE selected = 1 AND bet_won IS NOT NULL
                  AND edge_pct >= ? AND edge_pct < ?
                """,
                (low, high),
            ).fetchone()
            t = int(r["total"]) if r["total"] else 0
            if t > 0:
                w = int(r["won"]) if r["won"] else 0
                by_edge.append({
                    "range": label, "total": t, "won": w,
                    "win_rate": round(w / t * 100, 1),
                    "pnl": float(r["pnl"]) if r["pnl"] else 0.0,
                })

        # Performance nach Odds-Range
        by_odds = []
        for low, high, label in [(1.0, 2.5, "1.0-2.5"), (2.5, 3.5, "2.5-3.5"),
                                  (3.5, 4.5, "3.5-4.5"), (4.5, 20, "4.5+")]:
            r = conn.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                    ROUND(SUM(pnl_eur), 2) AS pnl
                FROM predictions
                WHERE selected = 1 AND bet_won IS NOT NULL
                  AND best_odds >= ? AND best_odds < ?
                """,
                (low, high),
            ).fetchone()
            t = int(r["total"]) if r["total"] else 0
            if t > 0:
                w = int(r["won"]) if r["won"] else 0
                by_odds.append({
                    "range": label, "total": t, "won": w,
                    "win_rate": round(w / t * 100, 1),
                    "pnl": float(r["pnl"]) if r["pnl"] else 0.0,
                })

        # Performance nach Bet-Typ (1X2 vs O/U)
        by_type = []
        for bt, label in [("1x2", "1X2"), ("ou", "O/U")]:
            r = conn.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                    ROUND(SUM(pnl_eur), 2) AS pnl,
                    ROUND(SUM(stake_eur), 2) AS stake
                FROM predictions
                WHERE selected = 1 AND bet_won IS NOT NULL AND bet_type = ?
                """,
                (bt,),
            ).fetchone()
            t = int(r["total"]) if r["total"] else 0
            if t > 0:
                w = int(r["won"]) if r["won"] else 0
                pnl = float(r["pnl"]) if r["pnl"] else 0.0
                stake = float(r["stake"]) if r["stake"] else 0.0
                by_type.append({
                    "type": label, "total": t, "won": w,
                    "win_rate": round(w / t * 100, 1),
                    "pnl": pnl,
                    "roi": round(pnl / stake * 100, 1) if stake else 0,
                })

    # Empfehlungen generieren
    recommendations = []
    alert_level = "ok"

    if total >= 20 and win_rate < 30:
        recommendations.append(
            f"Win-Rate kritisch niedrig ({win_rate:.0f}%). "
            "Scoring-Anpassung dringend noetig."
        )
        alert_level = "critical"
    elif total >= 10 and win_rate < 40:
        recommendations.append(
            f"Win-Rate unter Erwartung ({win_rate:.0f}%). Beobachten."
        )
        if alert_level == "ok":
            alert_level = "warning"

    # Liga-spezifische Warnungen
    for league in by_league:
        if league["total"] >= 3 and league["win_rate"] == 0:
            recommendations.append(
                f"{league['league']}: 0% Win-Rate bei {league['total']} Bets. "
                "Liga ausschliessen oder Min-Edge erhoehen."
            )

    # Edge-Range Warnungen
    for edge in by_edge:
        if edge["range"] == "20%+" and edge["total"] >= 3 and edge["win_rate"] < 15:
            recommendations.append(
                f"Edge 20%+: Nur {edge['win_rate']:.0f}% Win-Rate. "
                "Hard Cap bei 20% Edge setzen."
            )

    # Odds-Warnungen
    for odds in by_odds:
        if odds["range"] == "4.5+" and odds["total"] >= 3 and odds["win_rate"] < 15:
            recommendations.append(
                f"Odds 4.5+: Nur {odds['win_rate']:.0f}% Win-Rate. "
                "Max-Odds auf 4.50 beschraenken."
            )

    if roi < -20:
        if alert_level != "critical":
            alert_level = "critical"

    return {
        "overall": overall_data,
        "by_league": by_league,
        "by_edge_range": by_edge,
        "by_odds_range": by_odds,
        "by_bet_type": by_type,
        "recommendations": recommendations,
        "alert_level": alert_level,
    }


def update_bankroll_from_results() -> dict:
    """
    Aktualisiert die Bankroll basierend auf allen resolved Bets mit pnl_eur.
    Berechnet die aktuelle Bankroll als: STARTING_BANKROLL + Summe aller pnl_eur
    der selektierten Bets.

    Returns:
        {"bankroll": float, "total_pnl": float, "resolved_bets": int}
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(pnl_eur), 0.0) AS total_pnl,
                COUNT(*) AS resolved_bets
            FROM predictions
            WHERE selected = 1
              AND bet_won IS NOT NULL
              AND pnl_eur IS NOT NULL
            """
        ).fetchone()

        total_pnl = float(row["total_pnl"]) if row["total_pnl"] else 0.0
        resolved_bets = int(row["resolved_bets"]) if row["resolved_bets"] else 0

        # Config lesen
        config = conn.execute(
            "SELECT starting_bankroll FROM bankroll_config WHERE id = 1"
        ).fetchone()
        starting = float(config["starting_bankroll"]) if config else STARTING_BANKROLL

        bankroll = starting + total_pnl

    return {
        "bankroll": round(bankroll, 2),
        "total_pnl": round(total_pnl, 2),
        "resolved_bets": resolved_bets,
    }


def get_bankroll_history(limit: int = 30) -> list[dict]:
    """Gibt die letzten N Bankroll-Snapshots zurück."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT date, bankroll, day_pnl, bets_placed, bets_won
               FROM bankroll_snapshots
               ORDER BY date DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_peak_and_drawdown() -> dict:
    """Berechnet Peak-Bankroll und aktuellen Drawdown."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT bankroll FROM bankroll_snapshots ORDER BY date"
        ).fetchall()

    if not rows:
        return {"peak": STARTING_BANKROLL, "current": STARTING_BANKROLL,
                "drawdown_eur": 0.0, "drawdown_pct": 0.0}

    bankrolls = [float(r["bankroll"]) for r in rows]
    peak = max(bankrolls)
    current = bankrolls[-1]
    drawdown = peak - current

    return {
        "peak": round(peak, 2),
        "current": round(current, 2),
        "drawdown_eur": round(drawdown, 2),
        "drawdown_pct": round(drawdown / peak * 100, 2) if peak > 0 else 0.0,
    }
