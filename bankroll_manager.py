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
