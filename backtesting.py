#!/usr/bin/env python3
"""
Backtesting-Modul für den Sports Value Scanner.

Speichert jede Vorhersage in SQLite, ermöglicht späteres Eintragen
der Spielergebnisse und Auswertung von ROI/Kalibrierung.

DB: sports_backtesting.db (im Scanner-Verzeichnis)

Verwendung in sports_scanner.py:
    from backtesting import init_db, log_scan_run, log_prediction, resolve_results

    init_db()
    run_id = log_scan_run(scanned_at, model_version="v1.0", elo_years=[2023,2024,2025,2026], training_matches=n)
    for bet in all_football_bets:
        log_prediction(run_id, bet, match_raw=match)
    resolve_results()   # am Ende: offene Bets gegen Scores-API auflösen

CLI:
    python3 backtesting.py summary          # Gesamtauswertung
    python3 backtesting.py open             # Offene Predictions
    python3 backtesting.py resolve          # Ergebnisse via API auflösen
    python3 backtesting.py stale            # Predictions >7 Tage ohne Ergebnis
    python3 backtesting.py manual <id> <h> <a>  # Manuelles Ergebnis eintragen
    python3 backtesting.py void <id>        # Spiel ausgefallen/verschoben
"""

import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from difflib import get_close_matches
from pathlib import Path

_DEFAULT_DB_PATH = Path(__file__).parent / "sports_backtesting.db"
DB_PATH = _DEFAULT_DB_PATH


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
    actual_outcome    TEXT,              -- "home" | "draw" | "away" | "over" | "under" | "void"

    -- Profit/Loss
    stake_units       REAL    DEFAULT 1.0,
    pnl_units         REAL,               -- befüllt nach Spielende

    -- Wettplan-System (neu)
    stake_eur         REAL,               -- Einsatz in EUR (via Quarter-Kelly)
    tier              TEXT,               -- "Strong Pick" | "Value Bet" | "Watch"
    confidence_score  REAL,               -- 0–100
    selected          INTEGER DEFAULT 0,  -- 1 = im Wettplan, 0 = nur Watch
    pnl_eur           REAL                -- EUR-Profit/Loss nach Spielende
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
    bet_type = bet_dict.get("type", "").lower()

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
    """Normalisiert das type-Feld auf "1x2" | "ou" | "tennis"."""
    raw = bet_dict.get("type", "").lower()
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


def _calc_market_meta(
    match_raw: dict | None,
    outcome_side: str | None = None,
) -> tuple[float | None, float | None, str | None]:
    """
    Berechnet outcome-spezifische Konsens-Wahrscheinlichkeit und Overround.
    Gibt (consensus_prob, overround, best_odds_bookie) für die gewählte Seite zurück.
    Alle Werte können None sein wenn match_raw nicht verfügbar.
    """
    if not match_raw:
        return None, None, None

    home_name = match_raw.get("home_team", "")
    away_name = match_raw.get("away_team", "")
    implied = {"home": [], "draw": [], "away": []}
    best_odds = {"home": 1.0, "draw": 1.0, "away": 1.0}
    best_bookies = {"home": None, "draw": None, "away": None}

    for bm in match_raw.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            o_map: dict[str, float] = {}
            for o in market["outcomes"]:
                price = float(o["price"])
                if o["name"] == home_name:
                    o_map["home"] = price
                elif o["name"] == away_name:
                    o_map["away"] = price
                elif o["name"] == "Draw":
                    o_map["draw"] = price
            for side, price in o_map.items():
                if price > best_odds[side]:
                    best_odds[side] = price
                    best_bookies[side] = bm.get("key")
            total_impl = sum(1 / p for p in o_map.values() if p > 0)
            if total_impl > 0:
                for k, p in o_map.items():
                    implied[k].append((1 / p) / total_impl)

    if not implied["home"]:
        fallback_bookie = best_bookies.get(outcome_side) if outcome_side else None
        return None, None, fallback_bookie

    import numpy as np
    consensus_probs = {
        side: float(np.mean(values)) if values else None
        for side, values in implied.items()
    }

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

    selected_consensus = consensus_probs.get(outcome_side) if outcome_side else None
    selected_bookie = best_bookies.get(outcome_side) if outcome_side else None
    return selected_consensus, overround, selected_bookie


def _fetch_with_retry(url: str, retries: int = 3) -> list[dict]:
    """HTTP GET mit Retry-Logik und exponentiellem Backoff."""
    import urllib.request

    delays = [2, 4, 8]
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                wait = delays[attempt]
                print(f"[Backtesting] API-Fehler (Versuch {attempt + 1}/{retries}): {e} – warte {wait}s …")
                time.sleep(wait)
    raise last_error


def _fuzzy_match_score(pred: dict, score_entry: dict, cutoff: float = 0.8) -> bool:
    """Prüft ob eine Prediction zu einem Score-Eintrag passt (Fuzzy-Matching)."""
    # Team-Namen vergleichen
    pred_home = pred.get("home_team", "").lower()
    pred_away = pred.get("away_team", "").lower()
    score_home = score_entry.get("home_team", "").lower()
    score_away = score_entry.get("away_team", "").lower()

    # Exakter Match
    if pred_home == score_home and pred_away == score_away:
        return True

    # Fuzzy-Matching: mindestens einer der Teamnamen muss matchen
    home_match = get_close_matches(pred_home, [score_home], n=1, cutoff=cutoff)
    away_match = get_close_matches(pred_away, [score_away], n=1, cutoff=cutoff)

    if home_match and away_match:
        return True

    # Datum prüfen (gleicher Tag) — beide Teams muessen fuzzy matchen,
    # um False Positives bei Doppelspieltagen zu vermeiden
    pred_date = pred.get("commence_time", "")[:10]
    score_date = score_entry.get("commence_time", "")[:10]
    if pred_date and score_date and pred_date == score_date:
        # Mit niedrigerem Cutoff nochmal versuchen wenn Datum stimmt
        home_loose = get_close_matches(pred_home, [score_home], n=1, cutoff=0.6)
        away_loose = get_close_matches(pred_away, [score_away], n=1, cutoff=0.6)
        if home_loose and away_loose:
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# ÖFFENTLICHE API
# ═══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: Path | None = None) -> None:
    """Erstellt die Datenbank und alle Tabellen/Indizes falls nicht vorhanden."""
    global DB_PATH
    if db_path is not None:
        DB_PATH = db_path
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Migration: neue Wettplan-Spalten zu bestehenden DBs hinzufügen
        _migrate_wettplan_columns(conn)
    print(f"[Backtesting] DB initialisiert: {DB_PATH}")


def _migrate_wettplan_columns(conn: sqlite3.Connection) -> None:
    """Fügt Wettplan-Spalten hinzu falls sie nicht existieren (für bestehende DBs)."""
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()
    }
    migrations = [
        ("stake_eur",        "REAL"),
        ("tier",             "TEXT"),
        ("confidence_score", "REAL"),
        ("selected",         "INTEGER DEFAULT 0"),
        ("pnl_eur",          "REAL"),
    ]
    for col_name, col_type in migrations:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col_name} {col_type}")
            print(f"[Backtesting] Migration: Spalte '{col_name}' hinzugefügt")


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
    consensus_prob, overround, best_bookie = _calc_market_meta(match_raw, outcome_side)

    # Modell-Source normalisieren
    model_source = (
        bet_dict.get("model_source")
        or ("Poisson" if bet_type in ("1x2", "ou") else "Elo")
    )

    # Elo-Werte: direkt aus bet_dict wenn vorhanden (Tennis: "elo",
    # UEFA: nicht verfügbar → None)
    elo_home = bet_dict.get("elo") if bet_dict.get("tip") == home_team else None
    elo_away = bet_dict.get("elo") if bet_dict.get("tip") == away_team else None

    # Wettplan-Felder (optional, befüllt vom Bet-Selektor)
    stake_eur = bet_dict.get("stake_eur")
    tier = bet_dict.get("tier")
    confidence_score = bet_dict.get("confidence_score")
    selected = bet_dict.get("selected", 0)

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions (
                run_id, odds_api_match_id, sport_key, bet_type,
                home_team, away_team, commence_time,
                tip, outcome_side, ou_line,
                model_prob, model_source, lam_home, lam_away, elo_home, elo_away,
                best_odds, best_odds_bookie, consensus_prob, overround,
                edge_pct, kelly_pct, stake_units,
                stake_eur, tier, confidence_score, selected
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?
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
                stake_eur,
                tier,
                confidence_score,
                selected,
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


def update_prediction_selection(
    prediction_id: int,
    confidence_score: float,
    tier: str,
    stake_eur: float,
    selected: int,
) -> None:
    """Aktualisiert eine Prediction mit Wettplan-Daten (nach Bet-Selektion)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE predictions SET
                confidence_score = ?,
                tier             = ?,
                stake_eur        = ?,
                selected         = ?
            WHERE id = ?
            """,
            (confidence_score, tier, stake_eur, selected, prediction_id),
        )


def get_open_predictions() -> list[dict]:
    """
    Gibt alle Vorhersagen zurück, bei denen:
    - bet_won IS NULL  (Ergebnis noch nicht eingetragen)
    - actual_outcome != 'void'  (nicht abgesagt)
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
              AND (p.actual_outcome IS NULL OR p.actual_outcome != 'void')
              AND p.commence_time < ?
            ORDER BY p.commence_time
            """,
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stale_predictions(days: int = 7) -> list[dict]:
    """Gibt Predictions zurück, die älter als `days` Tage sind und kein Ergebnis haben."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*, s.scanned_at, s.model_version
            FROM predictions p
            JOIN scan_runs s ON s.id = p.run_id
            WHERE p.bet_won IS NULL
              AND (p.actual_outcome IS NULL OR p.actual_outcome != 'void')
              AND p.commence_time < ?
            ORDER BY p.commence_time
            """,
            (cutoff,),
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

        if row["bet_won"] is not None:
            raise ValueError(
                f"Prediction {prediction_id} wurde bereits aufgelöst "
                f"(bet_won={row['bet_won']}, {row['home_score']}:{row['away_score']})"
            )

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

        # EUR-PnL berechnen (nur wenn Stake in EUR vorhanden)
        pnl_eur = None
        stake_eur = row.get("stake_eur")
        if stake_eur and stake_eur > 0:
            if actual_outcome == "push":
                pnl_eur = 0.0
            elif bet_won == 1:
                pnl_eur = round((row["best_odds"] - 1.0) * stake_eur, 2)
            elif bet_won == 0:
                pnl_eur = round(-stake_eur, 2)

        fetched_at = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            UPDATE predictions SET
                result_fetched_at = ?,
                home_score        = ?,
                away_score        = ?,
                actual_outcome    = ?,
                bet_won           = ?,
                pnl_units         = ?,
                pnl_eur           = ?
            WHERE id = ?
            """,
            (fetched_at, home_score, away_score, actual_outcome,
             bet_won, pnl_units, pnl_eur, prediction_id),
        )

    return {
        "prediction_id": prediction_id,
        "home_score":     home_score,
        "away_score":     away_score,
        "actual_outcome": actual_outcome,
        "bet_won":        bet_won,
        "pnl_units":      pnl_units,
        "pnl_eur":        pnl_eur,
    }


def void_prediction(prediction_id: int) -> dict:
    """Markiert eine Prediction als void (Spiel ausgefallen/verschoben)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, home_team, away_team, bet_won, actual_outcome FROM predictions WHERE id = ?",
            (prediction_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Prediction {prediction_id} nicht gefunden")
        if row["bet_won"] is not None:
            raise ValueError(
                f"Prediction {prediction_id} wurde bereits aufgelöst (bet_won={row['bet_won']})"
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE predictions SET
                result_fetched_at = ?,
                actual_outcome    = 'void',
                bet_won           = NULL,
                pnl_units         = 0.0
            WHERE id = ?
            """,
            (fetched_at, prediction_id),
        )

    row = dict(row)
    return {
        "prediction_id": prediction_id,
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "actual_outcome": "void",
        "pnl_units": 0.0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ERGEBNISSE AUFLÖSEN
# ═══════════════════════════════════════════════════════════════════════════════

def _load_api_key() -> str:
    """Liest ODDS_API_KEY aus ~/.stock_scanner_credentials (KEY=VALUE-Format)."""
    creds_path = Path.home() / ".stock_scanner_credentials"
    try:
        for line in creds_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ODDS_API_KEY"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _extract_scores(entry: dict) -> tuple[int | None, int | None]:
    """Extrahiert home_score und away_score aus einem Scores-API-Eintrag."""
    home_name = entry.get("home_team", "")
    away_name = entry.get("away_team", "")
    home_score: int | None = None
    away_score: int | None = None
    for s in (entry.get("scores") or []):
        try:
            val = int(s["score"])
        except (ValueError, KeyError, TypeError):
            continue
        if s.get("name") == home_name:
            home_score = val
        elif s.get("name") == away_name:
            away_score = val
    return home_score, away_score


def resolve_results(api_key: str | None = None) -> dict:
    """
    Ruft The Odds API /scores auf und trägt Ergebnisse für offene Vorhersagen ein.

    Features:
        - Dynamisches daysFrom basierend auf ältester offener Prediction (max 7)
        - Retry-Logik mit exponentiellem Backoff (3 Versuche)
        - Fuzzy-Fallback für Predictions ohne odds_api_match_id
        - Stale-Warnung für Predictions >7 Tage ohne Ergebnis

    Returns:
        {"resolved": int, "still_open": int, "stale": int}
    """
    from collections import defaultdict

    if api_key is None:
        api_key = _load_api_key()

    if not api_key:
        print("[Backtesting] resolve_results: kein ODDS_API_KEY – übersprungen")
        return {"resolved": 0, "still_open": 0, "stale": 0}

    open_preds = get_open_predictions()
    if not open_preds:
        print("[Backtesting] Keine offenen Vorhersagen zum Auflösen.")
        return {"resolved": 0, "still_open": 0, "stale": 0}

    print(f"[Backtesting] {len(open_preds)} offene Vorhersagen – löse Ergebnisse auf …")

    # Dynamisches daysFrom: älteste offene Prediction bestimmt Lookback
    now = datetime.now(timezone.utc)
    oldest_commence = min(
        (p.get("commence_time", "") for p in open_preds),
        default=""
    )
    if oldest_commence:
        try:
            oldest_dt = datetime.fromisoformat(oldest_commence.replace("Z", "+00:00"))
            days_diff = (now - oldest_dt).days
            days_from = min(max(days_diff, 3), 7)
        except (ValueError, TypeError):
            days_from = 3
    else:
        days_from = 3

    print(f"[Backtesting] daysFrom={days_from} (basierend auf ältester offener Prediction)")

    # Gruppieren: sport_key → match_id → [predictions]
    # Separat: Predictions ohne Match-ID für Fuzzy-Fallback
    by_sport: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    no_id_by_sport: dict[str, list[dict]] = defaultdict(list)
    for p in open_preds:
        if p.get("odds_api_match_id"):
            by_sport[p["sport_key"]][p["odds_api_match_id"]].append(p)
        else:
            no_id_by_sport[p["sport_key"]].append(p)

    # Alle sport_keys sammeln (mit und ohne Match-ID)
    all_sport_keys = set(by_sport.keys()) | set(no_id_by_sport.keys())

    resolved   = 0
    still_open = 0

    for sport_key in all_sport_keys:
        match_map = by_sport.get(sport_key, {})
        no_id_preds = no_id_by_sport.get(sport_key, [])

        url = (
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores"
            f"?apiKey={api_key}&daysFrom={days_from}"
        )
        try:
            scores_data = _fetch_with_retry(url)
        except Exception as e:
            # API-Key aus Fehlermeldungen entfernen
            err_msg = str(e)
            if api_key and api_key in err_msg:
                err_msg = err_msg.replace(api_key, "***")
            print(f"[Backtesting] Scores-API Fehler ({sport_key}): {err_msg} – übersprungen")
            still_open += sum(len(v) for v in match_map.values()) + len(no_id_preds)
            continue

        scores_by_id: dict[str, dict] = {s["id"]: s for s in scores_data}
        completed_scores = [s for s in scores_data if s.get("completed")]

        # 1. Reguläre Auflösung via Match-ID
        for match_id, preds in match_map.items():
            entry = scores_by_id.get(match_id)

            if not entry or not entry.get("completed"):
                still_open += len(preds)
                continue

            home_score, away_score = _extract_scores(entry)
            if home_score is None or away_score is None:
                still_open += len(preds)
                continue

            for p in preds:
                try:
                    result = update_result(p["id"], home_score, away_score)
                    resolved += 1
                    icon    = "✓" if result["bet_won"] == 1 else ("✗" if result["bet_won"] == 0 else "~")
                    pnl     = result["pnl_units"]
                    pnl_str = f"{pnl:+.2f}u" if pnl is not None else "n/a"
                    print(
                        f"[Backtesting] {icon} {p['home_team']} – {p['away_team']} "
                        f"{home_score}:{away_score}  → {p['tip']}  PnL {pnl_str}"
                    )
                except Exception as e:
                    print(f"[Backtesting] update_result Fehler (ID {p['id']}): {e}")
                    still_open += 1

        # 2. Fuzzy-Fallback für Predictions ohne Match-ID
        for p in no_id_preds:
            matched = False
            for entry in completed_scores:
                if _fuzzy_match_score(p, entry):
                    home_score, away_score = _extract_scores(entry)
                    if home_score is None or away_score is None:
                        continue
                    try:
                        result = update_result(p["id"], home_score, away_score)
                        resolved += 1
                        matched = True
                        icon    = "✓" if result["bet_won"] == 1 else ("✗" if result["bet_won"] == 0 else "~")
                        pnl     = result["pnl_units"]
                        pnl_str = f"{pnl:+.2f}u" if pnl is not None else "n/a"
                        print(
                            f"[Backtesting] {icon} {p['home_team']} – {p['away_team']} "
                            f"{home_score}:{away_score}  → {p['tip']}  PnL {pnl_str}  (Fuzzy-Match)"
                        )
                        break
                    except Exception as e:
                        print(f"[Backtesting] update_result Fehler (ID {p['id']}): {e}")
                        still_open += 1
                        matched = True
                        break
            if not matched:
                still_open += 1

    # Stale-Warnung
    stale_preds = get_stale_predictions(days=7)
    stale_count = len(stale_preds)
    if stale_count > 0:
        print(f"\n[Backtesting] ⚠ {stale_count} Predictions sind >7 Tage alt ohne Ergebnis:")
        for p in stale_preds[:5]:
            age_days = (now - datetime.fromisoformat(
                p["commence_time"].replace("Z", "+00:00")
            )).days
            print(
                f"  [{p['id']:4d}] {p['commence_time'][:10]}  "
                f"{p['home_team']} – {p['away_team']}  ({age_days} Tage alt)"
            )
        if stale_count > 5:
            print(f"  … und {stale_count - 5} weitere. Nutze: python3 backtesting.py stale")
        print("  → Manuell auflösen: python3 backtesting.py manual <id> <heim> <gast>")
        print("  → Spiel void:       python3 backtesting.py void <id>")

    print(f"\n[Backtesting] Resolved: {resolved} bets | Still open: {still_open} bets | Stale: {stale_count}")
    return {"resolved": resolved, "still_open": still_open, "stale": stale_count}


# ═══════════════════════════════════════════════════════════════════════════════
# AUSWERTUNG
# ═══════════════════════════════════════════════════════════════════════════════

def get_summary() -> dict:
    """
    Gibt eine umfassende Zusammenfassung aller abgeschlossenen Bets zurück.

    Returns:
        dict mit overall, by_model, by_sport, calibration,
        daily_pnl, rolling, odds_range, edge_calibration
    """
    with _connect() as conn:
        overall = conn.execute("""
            SELECT
                COUNT(*)                                    AS total_bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                SUM(CASE WHEN bet_won = 0 THEN 1 ELSE 0 END) AS lost,
                ROUND(SUM(pnl_units), 4)                    AS total_pnl,
                ROUND(AVG(edge_pct), 2)                     AS avg_edge_pct,
                ROUND(AVG(best_odds), 2)                    AS avg_odds,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                           AS roi_pct,
                MIN(commence_time)                          AS first_bet,
                MAX(commence_time)                          AS last_bet
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

        # Zeitreihen-PnL: kumulativ pro Tag
        daily_pnl = conn.execute("""
            SELECT
                SUBSTR(commence_time, 1, 10)               AS day,
                COUNT(*)                                    AS bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_units), 4)                    AS day_pnl
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY day
            ORDER BY day
        """).fetchall()

        # Odds-Range-Analyse
        odds_range = conn.execute("""
            SELECT
                CASE
                    WHEN best_odds < 1.5 THEN '1.0-1.5'
                    WHEN best_odds < 2.0 THEN '1.5-2.0'
                    WHEN best_odds < 3.0 THEN '2.0-3.0'
                    WHEN best_odds < 5.0 THEN '3.0-5.0'
                    ELSE '5.0+'
                END                                         AS odds_bucket,
                COUNT(*)                                    AS bets,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                ROUND(SUM(pnl_units), 4)                    AS pnl,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                           AS roi_pct
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY odds_bucket
            ORDER BY MIN(best_odds)
        """).fetchall()

        # Edge-Kalibrierung
        edge_calibration = conn.execute("""
            SELECT
                CASE
                    WHEN edge_pct < 5.0  THEN '3-5%'
                    WHEN edge_pct < 10.0 THEN '5-10%'
                    WHEN edge_pct < 20.0 THEN '10-20%'
                    ELSE '20%+'
                END                                         AS edge_bucket,
                COUNT(*)                                    AS bets,
                ROUND(AVG(edge_pct), 1)                     AS avg_edge,
                ROUND(
                    100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                )                                           AS actual_roi_pct,
                SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won
            FROM predictions
            WHERE bet_won IS NOT NULL
            GROUP BY edge_bucket
            ORDER BY MIN(edge_pct)
        """).fetchall()

        # Rolling Metriken: letzte 7 und 30 Tage
        rolling = {}
        for label, days in [("7d", 7), ("30d", 30)]:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            row = conn.execute("""
                SELECT
                    COUNT(*)                                      AS bets,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                    ROUND(SUM(pnl_units), 4)                      AS pnl,
                    ROUND(
                        100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                    )                                             AS roi_pct
                FROM predictions
                WHERE bet_won IS NOT NULL
                  AND commence_time >= ?
            """, (cutoff,)).fetchone()
            rolling[label] = dict(row) if row else {}

        # Aktuelle Serie (Siege/Niederlagen in Folge)
        streak_rows = conn.execute("""
            SELECT bet_won
            FROM predictions
            WHERE bet_won IS NOT NULL
            ORDER BY commence_time DESC, id DESC
            LIMIT 50
        """).fetchall()
        streak_type = None
        streak_count = 0
        for r in streak_rows:
            if streak_type is None:
                streak_type = r["bet_won"]
                streak_count = 1
            elif r["bet_won"] == streak_type:
                streak_count += 1
            else:
                break
        rolling["streak"] = {
            "type": "W" if streak_type == 1 else ("L" if streak_type == 0 else "—"),
            "count": streak_count,
        }

    return {
        "overall":          dict(overall) if overall else {},
        "by_model":         [dict(r) for r in by_model],
        "by_sport":         [dict(r) for r in by_sport],
        "calibration":      [dict(r) for r in calibration],
        "daily_pnl":        [dict(r) for r in daily_pnl],
        "odds_range":       [dict(r) for r in odds_range],
        "edge_calibration": [dict(r) for r in edge_calibration],
        "rolling":          rolling,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI — python3 backtesting.py [summary|open|resolve|stale|manual|void]
# ═══════════════════════════════════════════════════════════════════════════════

def _print_table(headers: list[str], rows: list[list[str]], indent: int = 2) -> None:
    """Druckt eine formatierte Tabelle."""
    n_cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(min(len(row), n_cols)):
            widths[i] = max(widths[i], len(str(row[i])))

    prefix = " " * indent
    header_line = prefix + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = prefix + "  ".join("─" * w for w in widths)

    print(header_line)
    print(sep_line)
    for row in rows:
        print(prefix + "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


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

    elif cmd == "stale":
        preds = get_stale_predictions(days=7)
        if not preds:
            print("Keine stale Predictions (>7 Tage ohne Ergebnis).")
        else:
            now = datetime.now(timezone.utc)
            print(f"\n{len(preds)} stale Predictions (>7 Tage ohne Ergebnis):\n")
            for p in preds:
                try:
                    age_days = (now - datetime.fromisoformat(
                        p["commence_time"].replace("Z", "+00:00")
                    )).days
                except (ValueError, TypeError):
                    age_days = "?"
                has_id = "✓" if p.get("odds_api_match_id") else "✗"
                print(
                    f"  [{p['id']:4d}] {p['commence_time'][:10]}  "
                    f"{p['home_team']} – {p['away_team']}  "
                    f"→ {p['tip']} @ {p['best_odds']:.2f}  "
                    f"({age_days}d)  Match-ID: {has_id}"
                )
            print(f"\n  Manuell auflösen: python3 backtesting.py manual <id> <heim_score> <gast_score>")
            print(f"  Void markieren:   python3 backtesting.py void <id>")

    elif cmd == "manual":
        if len(sys.argv) < 5:
            print("Verwendung: python3 backtesting.py manual <prediction_id> <home_score> <away_score>")
            sys.exit(1)
        try:
            pred_id = int(sys.argv[2])
            h_score = int(sys.argv[3])
            a_score = int(sys.argv[4])
        except ValueError:
            print("Fehler: prediction_id, home_score und away_score müssen Ganzzahlen sein.")
            sys.exit(1)
        result = update_result(pred_id, h_score, a_score)
        icon = "✓" if result["bet_won"] == 1 else ("✗" if result["bet_won"] == 0 else "~")
        pnl = result["pnl_units"]
        pnl_str = f"{pnl:+.2f}u" if pnl is not None else "n/a"
        print(f"{icon} Prediction {pred_id}: {h_score}:{a_score} → {result['actual_outcome']}  PnL {pnl_str}")

    elif cmd == "void":
        if len(sys.argv) < 3:
            print("Verwendung: python3 backtesting.py void <prediction_id>")
            sys.exit(1)
        try:
            pred_id = int(sys.argv[2])
        except ValueError:
            print("Fehler: prediction_id muss eine Ganzzahl sein.")
            sys.exit(1)
        result = void_prediction(pred_id)
        print(f"~ Prediction {pred_id} als void markiert: {result['home_team']} – {result['away_team']}  PnL 0.00u")

    elif cmd == "summary":
        s = get_summary()
        o = s["overall"]
        if not o or not o.get("total_bets"):
            print("Noch keine abgeschlossenen Bets in der DB.")
            sys.exit(0)

        W = 60
        print(f"\n{'═' * W}")
        print(f"  BACKTESTING SUMMARY")
        print(f"{'═' * W}")

        # Zeitraum
        first = (o.get("first_bet") or "")[:10]
        last = (o.get("last_bet") or "")[:10]
        print(f"  Zeitraum    : {first} bis {last}")
        print(f"  Bets gesamt : {o['total_bets']}")
        print(f"  Gewonnen    : {o['won']}  |  Verloren: {o['lost']}  "
              f"({100 * o['won'] / o['total_bets']:.0f}% Trefferquote)")
        print(f"  PnL (Units) : {o['total_pnl']:+.2f}")
        print(f"  ROI         : {o['roi_pct']:+.2f}%")
        print(f"  Ø Edge      : {o['avg_edge_pct']:.1f}%")
        print(f"  Ø Quote     : {o.get('avg_odds', 0):.2f}")

        # Rolling
        rolling = s.get("rolling", {})
        streak = rolling.get("streak", {})
        print(f"\n{'─' * W}")
        print(f"  ROLLING METRIKEN")
        print(f"{'─' * W}")
        for label in ["7d", "30d"]:
            r = rolling.get(label, {})
            if r.get("bets"):
                print(f"  Letzte {label:>3}: {r['bets']:3d} Bets  "
                      f"PnL {r['pnl']:+.2f}  ROI {r['roi_pct']:+.1f}%  "
                      f"({r['won']}/{r['bets']} gewonnen)")
            else:
                print(f"  Letzte {label:>3}: keine Daten")
        if streak.get("count"):
            print(f"  Serie      : {streak['count']}× {streak['type']}")

        # Nach Modell
        print(f"\n{'─' * W}")
        print(f"  NACH MODELL")
        print(f"{'─' * W}")
        _print_table(
            ["Modell", "Bets", "Won", "PnL", "ROI"],
            [[r['model_source'], str(r['bets']), str(r['won']),
              f"{r['pnl']:+.2f}", f"{r['roi_pct']:+.1f}%"]
             for r in s["by_model"]],
        )

        # Nach Liga
        print(f"\n{'─' * W}")
        print(f"  NACH LIGA")
        print(f"{'─' * W}")
        _print_table(
            ["Liga", "Bets", "Won", "PnL", "ROI"],
            [[r['sport_key'], str(r['bets']), str(r['won']),
              f"{r['pnl']:+.2f}", f"{r['roi_pct']:+.1f}%"]
             for r in s["by_sport"]],
        )

        # Odds-Range
        if s.get("odds_range"):
            print(f"\n{'─' * W}")
            print(f"  NACH QUOTEN-RANGE")
            print(f"{'─' * W}")
            _print_table(
                ["Range", "Bets", "Won", "PnL", "ROI"],
                [[r['odds_bucket'], str(r['bets']), str(r['won']),
                  f"{r['pnl']:+.2f}", f"{r['roi_pct']:+.1f}%"]
                 for r in s["odds_range"]],
            )

        # Edge-Kalibrierung
        if s.get("edge_calibration"):
            print(f"\n{'─' * W}")
            print(f"  EDGE-KALIBRIERUNG (vorhergesagt vs. tatsächlich)")
            print(f"{'─' * W}")
            _print_table(
                ["Edge", "Bets", "Ø Edge", "Tats. ROI", "Delta"],
                [[r['edge_bucket'], str(r['bets']),
                  f"{r['avg_edge']:.1f}%",
                  f"{r['actual_roi_pct']:+.1f}%",
                  f"{(r['actual_roi_pct'] or 0) - (r['avg_edge'] or 0):+.1f}%"]
                 for r in s["edge_calibration"]],
            )

        # Modell-Kalibrierung
        print(f"\n{'─' * W}")
        print(f"  MODELL-KALIBRIERUNG (Wahrscheinlichkeit vs. Trefferquote)")
        print(f"{'─' * W}")
        _print_table(
            ["Bucket", "Bets", "Modell", "Tatsächlich", "Diff"],
            [[f"{r['prob_bucket']:.0%}", str(r['bets']),
              f"{r['avg_model_prob']:.1%}",
              f"{r['actual_win_rate'] or 0:.1%}",
              f"{((r['actual_win_rate'] or 0) - (r['avg_model_prob'] or 0)):+.1%}"
              + (" ↑" if ((r['actual_win_rate'] or 0) - (r['avg_model_prob'] or 0)) > 0.05
                 else (" ↓" if ((r['actual_win_rate'] or 0) - (r['avg_model_prob'] or 0)) < -0.05 else ""))]
             for r in s["calibration"]],
        )

        # Zeitreihe (letzte 10 Tage)
        daily = s.get("daily_pnl", [])
        if daily:
            print(f"\n{'─' * W}")
            print(f"  PnL-ZEITREIHE (letzte {min(len(daily), 10)} Tage)")
            print(f"{'─' * W}")
            cumulative = 0.0
            rows = []
            for d in daily:
                cumulative += d["day_pnl"] or 0
                rows.append([d["day"], str(d["bets"]),
                             f"{d['won']}/{d['bets']}",
                             f"{d['day_pnl']:+.2f}",
                             f"{cumulative:+.2f}"])
            _print_table(
                ["Tag", "Bets", "W/L", "Tag-PnL", "Kumulativ"],
                rows[-10:],
            )

        # Offene Bets Hinweis
        open_count = len(get_open_predictions())
        stale_count = len(get_stale_predictions())
        if open_count or stale_count:
            print(f"\n{'─' * W}")
            if open_count:
                print(f"  📋 {open_count} offene Bets → python3 backtesting.py open")
            if stale_count:
                print(f"  ⚠ {stale_count} stale Bets (>7d) → python3 backtesting.py stale")

        print(f"\n{'═' * W}")

    elif cmd == "resolve":
        result = resolve_results()
        print(f"Resolved: {result['resolved']} | Still open: {result['still_open']} | Stale: {result.get('stale', 0)}")

    else:
        print(f"Unbekannter Befehl: {cmd}")
        print("Verwendung: python3 backtesting.py [summary|open|resolve|stale|manual|void]")
        print()
        print("  summary                        — Gesamtauswertung")
        print("  open                           — Offene Predictions anzeigen")
        print("  resolve                        — Ergebnisse via API auflösen")
        print("  stale                          — Predictions >7 Tage ohne Ergebnis")
        print("  manual <id> <heim> <gast>      — Manuelles Ergebnis eintragen")
        print("  void <id>                      — Spiel als ausgefallen markieren")
        sys.exit(1)
