#!/usr/bin/env python3
"""
Backtesting-Modul für den Sports Value Scanner.

Speichert jede Vorhersage in SQLite, ermöglicht späteres Eintragen
der Spielergebnisse und Auswertung von ROI/Kalibrierung.

DB: sports_backtesting.db (im Scanner-Verzeichnis)

Verwendung in sports_scanner.py:
    from backtesting import init_db, log_scan_run, log_prediction

    init_db()
    run_id = log_scan_run(scanned_at, model_version="v1.0", elo_years=[2023,2024,2025,2026], training_matches=n)
    for bet in all_football_bets:
        log_prediction(run_id, bet, match_raw=match)
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "sports_backtesting.db"


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at       TEXT    NOT NULL,
    model_version    TEXT,
    elo_years        TEXT,               -- JSON-Array, z.B. "[2023,2024,2025,2026]"
    training_matches INTEGER,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES scan_runs(id),

    -- Match-Identifikation
    odds_api_match_id TEXT,              -- The Odds API match.id (zum späteren Abruf)
    sport_key         TEXT    NOT NULL,  -- "soccer_germany_bundesliga"
    bet_type          TEXT    NOT NULL,  -- "1x2" | "ou" | "tennis"
    home_team         TEXT    NOT NULL,
    away_team         TEXT    NOT NULL,
    commence_time     TEXT    NOT NULL,  -- ISO8601

    -- Tipp
    tip               TEXT    NOT NULL,  -- "Bayern München" | "Über 2.5" | "Djokovic"
    outcome_side      TEXT,              -- "home" | "draw" | "away" | "over" | "under"
    ou_line           REAL,              -- 2.5 | 3.5 | NULL bei 1x2/tennis

    -- Modell-Output
    model_prob        REAL    NOT NULL,  -- 0.0–1.0
    model_source      TEXT    NOT NULL,  -- "Poisson" | "Elo" | "ClubElo" | "Konsens"
    lam_home          REAL,              -- Poisson λ Heim (NULL bei Tennis)
    lam_away          REAL,              -- Poisson λ Gast (NULL bei Tennis)
    elo_home          REAL,              -- Elo Heim-Spieler/Club (NULL bei Fußball 1x2)
    elo_away          REAL,              -- Elo Gast-Spieler/Club

    -- Markt
    best_odds         REAL    NOT NULL,
    best_odds_bookie  TEXT,              -- welcher Bookie die beste Quote bot
    consensus_prob    REAL,              -- normalisierter Schnitt aller Bookies
    overround         REAL,             -- Buchmacher-Marge: sum(1/odds) - 1

    -- Value-Berechnung
    edge_pct          REAL    NOT NULL,
    kelly_pct         REAL    NOT NULL,  -- bereits gekappt auf MAX_KELLY

    -- Ergebnis (wird nach Spielende befüllt)
    result_fetched_at TEXT,
    home_score        INTEGER,           -- NULL = noch nicht gespielt
    away_score        INTEGER,
    bet_won           INTEGER,           -- 1 = gewonnen, 0 = verloren, NULL = offen
    actual_outcome    TEXT,              -- "home" | "draw" | "away" | "over" | "under"

    -- Profit/Loss
    stake_units       REAL    DEFAULT 1.0,
    pnl_units         REAL                -- befüllt nach Spielende
);

CREATE TABLE IF NOT EXISTS odds_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    bookie        TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,  -- "home" | "draw" | "away" | "over" | "under"
    price         REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_predictions_match   ON predictions(odds_api_match_id);
CREATE INDEX IF NOT EXISTS idx_predictions_status  ON predictions(bet_won);
CREATE INDEX IF NOT EXISTS idx_predictions_sport   ON predictions(sport_key);
CREATE INDEX IF NOT EXISTS idx_predictions_date    ON predictions(commence_time);
CREATE INDEX IF NOT EXISTS idx_odds_prediction     ON odds_snapshot(prediction_id);
"""


# ═══════════════════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ═══════════════════════════════════════════════════════════════════════════════

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _infer_outcome_side(bet_dict: dict) -> str | None:
    """
    Leitet outcome_side aus tip + match-Feld ab, da der Scanner dieses
    Feld nicht explizit speichert.

    Rückgabe: "home" | "draw" | "away" | "over" | "under" | None
    """
    tip      = bet_dict.get("tip", "")
    bet_type = (bet_dict.get("type") or bet_dict.get("bet_type") or "").lower()

    # Over/Under
    if "ou" in bet_type or bet_dict.get("ou_line") is not None:
        if tip.startswith("Über"):
            return "over"
        if tip.startswith("Unter"):
            return "under"
        return None

    # Unentschieden (Fußball 1x2)
    if tip in ("Unentschieden", "Draw"):
        return "draw"

    # Heim vs. Gast: aus dem "match"-Feld ("Home – Away") ableiten
    match_str = bet_dict.get("match", "")
    parts = match_str.split(" – ", 1)
    home = parts[0].strip() if parts else ""
    away = parts[1].strip() if len(parts) > 1 else ""

    if tip == home:
        return "home"
    if tip == away:
        return "away"

    # Fuzzy: tip ist Teil des Teamnamens (bei Fuzzy-Matching im Scanner)
    if home and tip.lower() in home.lower():
        return "home"
    if away and tip.lower() in away.lower():
        return "away"

    return None


def _infer_bet_type(bet_dict: dict) -> str:
    """Normalisiert die verschiedenen bet_type-Felder auf "1x2" | "ou" | "tennis"."""
    raw = (bet_dict.get("type") or bet_dict.get("bet_type") or "").lower()
    if raw in ("football_ou", "ou"):
        return "ou"
    if raw == "tennis":
        return "tennis"
    return "1x2"


def _parse_teams(bet_dict: dict) -> tuple[str, str]:
    """Extrahiert home_team und away_team aus dem 'match'-Feld."""
    match_str = bet_dict.get("match", " – ")
    parts = match_str.split(" – ", 1)
    home = parts[0].strip() if parts else "?"
    away = parts[1].strip() if len(parts) > 1 else "?"
    return home, away


def _calc_consensus(match_raw: dict | None) -> tuple[float | None, float | None, str | None]:
    """
    Berechnet Konsens-Wahrscheinlichkeit und Overround aus rohen Match-Daten.
    Gibt (consensus_home_prob, overround, best_odds_bookie) zurück.
    Alle Werte können None sein wenn match_raw nicht verfügbar.
    """
    if not match_raw:
        return None, None, None

    home_name = match_raw.get("home_team", "")
    away_name = match_raw.get("away_team", "")
    implied   = {"home": [], "draw": [], "away": []}
    best_h    = 1.0
    best_b    = None

    for bm in match_raw.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            o_map: dict[str, float] = {}
            for o in market["outcomes"]:
                price = float(o["price"])
                if o["name"] == home_name:
                    o_map["home"] = price
                    if price > best_h:
                        best_h = price
                        best_b = bm.get("key")
                elif o["name"] == away_name:
                    o_map["away"] = price
                elif o["name"] == "Draw":
                    o_map["draw"] = price
            total_impl = sum(1 / p for p in o_map.values() if p > 0)
            if total_impl > 0:
                for k, p in o_map.items():
                    implied[k].append((1 / p) / total_impl)

    if not implied["home"]:
        return None, None, best_b

    import numpy as np
    consensus_home = float(np.mean(implied["home"]))

    # Overround: Schnitt der (sum(1/odds) - 1) über alle Bookies
    overrounds = []
    for bm in match_raw.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            s = sum(1 / float(o["price"]) for o in market["outcomes"] if float(o["price"]) > 0)
            if s > 0:
                overrounds.append(s - 1)
    overround = float(np.mean(overrounds)) if overrounds else None

    return consensus_home, overround, best_b


# ═══════════════════════════════════════════════════════════════════════════════
# ÖFFENTLICHE API
# ═══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: Path = DB_PATH) -> None:
    """Erstellt die Datenbank und alle Tabellen/Indizes falls nicht vorhanden."""
    global DB_PATH
    DB_PATH = db_path
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    print(f"[Backtesting] DB initialisiert: {DB_PATH}")


def log_scan_run(
    scanned_at: str,
    model_version: str | None = None,
    elo_years: list | None = None,
    training_matches: int | None = None,
    notes: str | None = None,
) -> int:
    """
    Erstellt einen neuen Scan-Run-Eintrag.

    Returns:
        run_id (int) — ID des neuen Eintrags
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO scan_runs (scanned_at, model_version, elo_years, training_matches, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scanned_at,
                model_version,
                json.dumps(elo_years) if elo_years else None,
                training_matches,
                notes,
            ),
        )
        return cur.lastrowid


def log_prediction(
    run_id: int,
    bet_dict: dict,
    match_raw: dict | None = None,
    stake_units: float = 1.0,
) -> int:
    """
    Speichert eine Vorhersage aus dem Scanner in der DB.

    Args:
        run_id:     ID aus log_scan_run()
        bet_dict:   Bet-Dict aus analyze_football_match() / analyze_tennis_match() etc.
        match_raw:  Originales Match-Dict aus The Odds API (für odds_snapshot + consensus)
        stake_units: Einsatz in Einheiten (default 1.0)

    Returns:
        prediction_id (int)
    """
    home_team, away_team = _parse_teams(bet_dict)
    bet_type             = _infer_bet_type(bet_dict)
    outcome_side         = _infer_outcome_side(bet_dict)
    consensus_prob, overround, best_bookie = _calc_consensus(match_raw)

    # Modell-Source normalisieren (Scanner verwendet "model_source" bei Tennis,
    # "model_src" bei UEFA)
    model_source = (
        bet_dict.get("model_source")
        or bet_dict.get("model_src")
        or ("Poisson" if bet_type in ("1x2", "ou") else "Elo")
    )

    # Elo-Werte: direkt aus bet_dict wenn vorhanden (Tennis: "elo",
    # UEFA: aus model_src-String nicht abrufbar → None)
    elo_home = bet_dict.get("elo") if bet_dict.get("tip") == home_team else None
    elo_away = bet_dict.get("elo") if bet_dict.get("tip") == away_team else None

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions (
                run_id, odds_api_match_id, sport_key, bet_type,
                home_team, away_team, commence_time,
                tip, outcome_side, ou_line,
                model_prob, model_source, lam_home, lam_away, elo_home, elo_away,
                best_odds, best_odds_bookie, consensus_prob, overround,
                edge_pct, kelly_pct, stake_units
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                run_id,
                match_raw.get("id") if match_raw else None,
                bet_dict.get("sport", ""),
                bet_type,
                home_team,
                away_team,
                bet_dict.get("kick_off", ""),
                bet_dict.get("tip", ""),
                outcome_side,
                bet_dict.get("ou_line") or bet_dict.get("line"),
                bet_dict.get("model_prob", 0.0),
                model_source,
                bet_dict.get("lam_home"),
                bet_dict.get("lam_away"),
                elo_home,
                elo_away,
                bet_dict.get("best_odds", 0.0),
                best_bookie,
                consensus_prob,
                overround,
                bet_dict.get("edge_pct", 0.0),
                bet_dict.get("kelly_pct", 0.0),
                stake_units,
            ),
        )
        prediction_id = cur.lastrowid

        # odds_snapshot: alle Bookmaker-Quoten speichern
        if match_raw:
            home_name = match_raw.get("home_team", "")
            away_name = match_raw.get("away_team", "")
            for bm in match_raw.get("bookmakers", []):
                bookie = bm.get("key", "")
                for market in bm.get("markets", []):
                    mkey = market.get("key", "")
                    if mkey not in ("h2h", "totals"):
                        continue
                    for o in market.get("outcomes", []):
                        if mkey == "h2h":
                            if o["name"] == home_name:
                                outcome = "home"
                            elif o["name"] == away_name:
                                outcome = "away"
                            elif o["name"] == "Draw":
                                outcome = "draw"
                            else:
                                continue
                        else:  # totals
                            side = o.get("name", "").lower()
                            if side == "over":
                                outcome = "over"
                            elif side == "under":
                                outcome = "under"
                            else:
                                continue
                        conn.execute(
                            "INSERT INTO odds_snapshot (prediction_id, bookie, outcome, price) VALUES (?, ?, ?, ?)",
                            (prediction_id, bookie, outcome, float(o["price"])),
                        )

    return prediction_id


def get_open_predictions() -> list[dict]:
    """
    Gibt alle Vorhersagen zurück, bei denen:
    - bet_won IS NULL  (Ergebnis noch nicht eingetragen)
    - commence_time < jetzt  (Spiel müsste bereits abgeschlossen sein)

    Returns:
        Liste von dicts mit allen Prediction-Feldern
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*, s.scanned_at, s.model_version
            FROM predictions p
            JOIN scan_runs s ON s.id = p.run_id
            WHERE p.bet_won IS NULL
              AND p.commence_time < ?
            ORDER BY p.commence_time
            """,
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_result(
    prediction_id: int,
    home_score: int,
    away_score: int,
) -> dict:
    """
    Trägt das Spielergebnis ein und berechnet bet_won + pnl_units.

    Logik:
        1x2:  actual = "home" | "draw" | "away" je nach Scoreline
        ou:   actual = "over" | "under" je ob Tore > ou_line
        tennis: actual = "home" | "away" (kein Unentschieden möglich)

    pnl_units:
        Sieg:    (best_odds - 1) × stake_units
        Verlust: -stake_units

    Returns:
        dict mit den aktualisierten Feldern
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()

        if row is None:
            raise ValueError(f"Prediction {prediction_id} nicht gefunden")

        row = dict(row)

        # actual_outcome bestimmen
        bet_type     = row["bet_type"]
        outcome_side = row["outcome_side"]
        ou_line      = row["ou_line"]
        total_goals  = home_score + away_score

        if bet_type == "ou" and ou_line is not None:
            if total_goals > ou_line:
                actual_outcome = "over"
            elif total_goals < ou_line:
                actual_outcome = "under"
            else:
                # Push bei ganzzahliger Linie — sollte laut Scanner nicht vorkommen
                actual_outcome = "push"
        elif bet_type == "tennis":
            # Tennis: home_score = Sätze Spieler 1, away_score = Sätze Spieler 2
            if home_score > away_score:
                actual_outcome = "home"
            elif away_score > home_score:
                actual_outcome = "away"
            else:
                actual_outcome = None  # unentschieden bei Tennis nicht möglich
        else:
            # 1x2 Fußball
            if home_score > away_score:
                actual_outcome = "home"
            elif home_score == away_score:
                actual_outcome = "draw"
            else:
                actual_outcome = "away"

        # bet_won berechnen
        if actual_outcome == "push":
            bet_won   = None   # Einsatz zurück, kein Gewinn/Verlust
            pnl_units = 0.0
        elif actual_outcome is None:
            bet_won   = None
            pnl_units = None
        elif outcome_side == actual_outcome:
            bet_won   = 1
            pnl_units = (row["best_odds"] - 1.0) * row["stake_units"]
        else:
            bet_won   = 0
            pnl_units = -row["stake_units"]

        fetched_at = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            UPDATE predictions SET
                result_fetched_at = ?,
                home_score        = ?,
                away_score        = ?,
                actual_outcome    = ?,
                bet_won           = ?,
                pnl_units         = ?
            WHERE id = ?
            """,
            (fetched_at, home_score, away_score, actual_outcome, bet_won, pnl_units, prediction_id),
        )

    return {
        "prediction_id": prediction_id,
        "home_score":     home_score,
        "away_score":     away_score,
        "actual_outcome": actual_outcome,
        "bet_won":        bet_won,
        "pnl_units":      pnl_units,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AUSWERTUNG
# ═══════════════════════════════════════════════════════════════════════════════

def get_summary() -> dict:
    """
    Gibt eine Zusammenfassung aller abgeschlossenen Bets zurück.

    Returns:
        dict mit roi_pct, total_bets, won, lost, total_pnl, avg_edge,
        aufgeteilt nach model_source und sport_key
    """
    with _connect() as conn:
        overall = conn.execute("""
            SELECT
                COUNT(*)                                    AS total_bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                SUM(CASE WHEN bet_won = 0 THEN 1 ELSE 0 END) AS lost,
                ROUND(SUM(pnl_units), 4)                    AS total_pnl,
                ROUND(AVG(edge_pct), 2)                     AS avg_edge_pct,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                           AS roi_pct
            FROM predictions
            WHERE bet_won IS NOT NULL
        """).fetchone()

        by_model = conn.execute("""
            SELECT
                model_source,
                COUNT(*)                                      AS bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_units), 4)                      AS pnl,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                             AS roi_pct
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY model_source
            ORDER BY pnl DESC
        """).fetchall()

        by_sport = conn.execute("""
            SELECT
                sport_key,
                COUNT(*)                                      AS bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_units), 4)                      AS pnl,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                             AS roi_pct
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY sport_key
            ORDER BY pnl DESC
        """).fetchall()

        calibration = conn.execute("""
            SELECT
                ROUND(model_prob * 10) / 10.0               AS prob_bucket,
                COUNT(*)                                     AS bets,
                ROUND(AVG(bet_won), 3)                       AS actual_win_rate,
                ROUND(AVG(model_prob), 3)                    AS avg_model_prob
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY prob_bucket
            ORDER BY prob_bucket
        """).fetchall()

    return {
        "overall":     dict(overall) if overall else {},
        "by_model":    [dict(r) for r in by_model],
        "by_sport":    [dict(r) for r in by_sport],
        "calibration": [dict(r) for r in calibration],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI — python3 backtesting.py [summary | open]
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    init_db()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "open":
        preds = get_open_predictions()
        if not preds:
            print("Keine offenen Vorhersagen.")
        else:
            print(f"\n{len(preds)} offene Vorhersagen (Spiel bereits vorbei):\n")
            for p in preds:
                print(
                    f"  [{p['id']:4d}] {p['commence_time'][:16]}  "
                    f"{p['home_team']} – {p['away_team']}  "
                    f"→ {p['tip']} @ {p['best_odds']:.2f}  "
                    f"Edge {p['edge_pct']:.1f}%  [{p['sport_key']}]"
                )

    elif cmd == "summary":
        s = get_summary()
        o = s["overall"]
        if not o or not o.get("total_bets"):
            print("Noch keine abgeschlossenen Bets in der DB.")
            sys.exit(0)

        print(f"\n{'='*55}")
        print(f"  Backtesting Summary")
        print(f"{'='*55}")
        print(f"  Bets gesamt : {o['total_bets']}")
        print(f"  Gewonnen    : {o['won']}  |  Verloren: {o['lost']}")
        print(f"  PnL (Units) : {o['total_pnl']:+.2f}")
        print(f"  ROI         : {o['roi_pct']:+.2f}%")
        print(f"  Ø Edge      : {o['avg_edge_pct']:.1f}%")

        print(f"\n  Nach Modell:")
        for r in s["by_model"]:
            print(f"    {r['model_source']:<12} {r['bets']:3d} Bets  "
                  f"PnL {r['pnl']:+.2f}  ROI {r['roi_pct']:+.1f}%")

        print(f"\n  Nach Liga:")
        for r in s["by_sport"]:
            print(f"    {r['sport_key']:<40} {r['bets']:3d} Bets  "
                  f"PnL {r['pnl']:+.2f}  ROI {r['roi_pct']:+.1f}%")

        print(f"\n  Kalibrierung (Modell-Prob vs. Trefferquote):")
        print(f"  {'Bucket':>8}  {'Bets':>5}  {'Modell':>8}  {'Tatsächlich':>11}")
        for r in s["calibration"]:
            diff = (r["actual_win_rate"] or 0) - (r["avg_model_prob"] or 0)
            flag = " ↑" if diff > 0.05 else (" ↓" if diff < -0.05 else "")
            print(f"  {r['prob_bucket']:>7.0%}  {r['bets']:>5d}  "
                  f"{r['avg_model_prob']:>7.1%}  {r['actual_win_rate'] or 0:>10.1%}{flag}")

    else:
        print(f"Unbekannter Befehl: {cmd}")
        print("Verwendung: python3 backtesting.py [summary|open]")
        sys.exit(1)
