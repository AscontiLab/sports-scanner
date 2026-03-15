#!/usr/bin/env python3
"""
Sports Betting Value Scanner
────────────────────────────
Analysiert Fußball (1./2./3. Bundesliga, Premier League) und UEFA-Wettbewerbe
(Champions League, Europa League, Conference League) mit Poisson-Modell
sowie Tennis (ATP) mit Elo-Modell.

Datenquellen:
  - Fußball-History: football-data.co.uk (Bundesliga + Top-5-Ligen)
  - UEFA 1X2-Modell: Club-Elo (api.clubelo.com)
  - Tennis-History:  Jeff Sackmann / tennis_atp (GitHub)
  - Live-Odds:       The Odds API (v4)
"""

import sys
import argparse
import json
import math
import time
import difflib
import warnings
import requests
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime, timezone, timedelta
from pathlib import Path
from scipy.stats import poisson
from scipy.optimize import minimize
from backtesting import init_db, log_scan_run, log_prediction, resolve_results, get_summary, update_prediction_selection
from alerts import send_high_edge_alerts
from config import (
    SCRIPT_DIR, OUTPUT_DIR, CREDS_FILE,
    FOOTBALL_SPORTS, SPORT_LABELS, FDCO_LEAGUES, OPENLIGADB_BASE,
    MIN_EDGE_PCT, MAX_EDGE_PCT, MIN_ODDS, MAX_KELLY,
    ELO_K_FACTOR, ELO_INITIAL, ELO_YEARS,
    UEFA_SPORTS, UEFA_LABELS,
    CLUBELO_URL_HTTPS, CLUBELO_URL_HTTP,
    EUROPEAN_FDCO_LEAGUES,
    ODDS_API_BASE, MIN_ODDS_API_REMAINING,
    KICKTIPP_FOOTBALL_SPORTS, KICKTIPP_UEFA_SPORTS, KICKTIPP_LABELS,
)
from bankroll_manager import (
    init_bankroll, get_current_bankroll, get_daily_budget,
    record_daily_snapshot, get_peak_and_drawdown, rebuild_all_snapshots,
)
from bet_selector import select_bets

import subprocess

warnings.filterwarnings("ignore")

# Mutable global — nicht in config.py
ODDS_API_REMAINING: int | None = None
HUB_DIR = SCRIPT_DIR.parent / "hub"


def current_season_codes(now: datetime | None = None) -> list[str]:
    """
    Liefert Saison-Codes wie ['2526','2425'] (aktuell + Vorjahr).
    Annahme: Saison startet ab Juli.
    """
    if now is None:
        now = datetime.now()
    year = now.year
    if now.month < 7:
        start = year - 1
    else:
        start = year
    codes = []
    for s in [start, start - 1]:
        codes.append(f"{str(s)[-2:]}{str(s+1)[-2:]}")
    return codes


def build_fdco_urls(league_code: str, season_codes: list[str]) -> list[str]:
    return [
        f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
        for season in season_codes
    ]


def get_elo_years(now: datetime | None = None, span: int = 4) -> list[int]:
    if now is None:
        now = datetime.now()
    return list(range(now.year - (span - 1), now.year + 1))


def _slugify(value: str) -> str:
    value = value.lower().strip()
    cleaned = []
    for ch in value:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (" ", "-", "_", "/", ":", ".", "–"):
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "unknown"


def _competition_label(bet: dict) -> str:
    sport = bet.get("sport", "")
    if sport in SPORT_LABELS:
        return SPORT_LABELS[sport]
    if sport in UEFA_LABELS:
        return UEFA_LABELS[sport]
    return bet.get("tournament") or sport or "Unbekannt"


def _bet_market_label(bet: dict) -> str:
    bet_type = (bet.get("type") or "").lower()
    if bet_type in ("football", "1x2"):
        return "1x2"
    if bet_type in ("football_ou", "ou"):
        return "totals"
    if bet_type == "tennis":
        return "match_winner"
    return bet_type or "unknown"


def _bet_side(bet: dict) -> str:
    side = bet.get("outcome_side")
    if side:
        return side
    tip = bet.get("tip", "")
    match_str = bet.get("match", "")
    parts = match_str.split(" – ", 1)
    home = parts[0].strip() if parts else ""
    away = parts[1].strip() if len(parts) > 1 else ""
    if tip == home:
        return "home"
    if tip == away:
        return "away"
    if tip in ("Unentschieden", "Draw"):
        return "draw"
    if tip.startswith("Über") or tip.startswith("Over"):
        return "over"
    if tip.startswith("Unter") or tip.startswith("Under"):
        return "under"
    return "unknown"


def _bet_status(bet: dict) -> str:
    if bet.get("selected"):
        return "selected"
    if bet.get("tier") == "Watch":
        return "watch"
    return "candidate"


def _build_signal_id(bet: dict) -> str:
    parts = [
        "sports",
        _slugify(bet.get("sport", "unknown")),
        _slugify(bet.get("match", "unknown")),
        _slugify(_bet_side(bet)),
        _slugify(bet.get("kick_off", "")),
    ]
    return ":".join(parts)


def _build_sports_drivers(bet: dict) -> list[dict]:
    drivers = []
    consensus = bet.get("consensus_prob")
    model_prob = bet.get("model_prob")
    if consensus is not None and model_prob is not None:
        gap_pp = (model_prob - consensus) * 100
        drivers.append({
            "label": "Model vs Market Gap",
            "direction": "positive" if gap_pp >= 0 else "negative",
            "value": f"{gap_pp:+.1f}pp",
            "weight": 0.30,
        })
    if bet.get("edge_pct") is not None:
        drivers.append({
            "label": "Edge",
            "direction": "positive" if bet.get("edge_pct", 0) >= 0 else "negative",
            "value": f"{bet.get('edge_pct', 0):.1f}%",
            "weight": 0.15,
        })
    if bet.get("confidence_score") is not None:
        drivers.append({
            "label": "Confidence",
            "direction": "positive",
            "value": f"{bet.get('confidence_score', 0):.0f}/100",
            "weight": 0.20,
        })
    if bet.get("overround") is not None:
        drivers.append({
            "label": "Odds Quality",
            "direction": "positive" if bet.get("overround", 1) <= 0.08 else "neutral",
            "value": f"{bet.get('overround', 0) * 100:.1f}% overround",
            "weight": 0.10,
        })
    if bet.get("training_matches") is not None:
        drivers.append({
            "label": "Data Depth",
            "direction": "positive",
            "value": f"{int(bet.get('training_matches', 0))} matches",
            "weight": 0.10,
        })
    return drivers


def _build_sports_explainability(bet: dict) -> dict:
    model_prob = bet.get("model_prob")
    consensus = bet.get("consensus_prob")
    edge_pct = bet.get("edge_pct", 0.0)
    confidence = bet.get("confidence_score", 0.0)
    odds = bet.get("best_odds", 0.0)
    overround = bet.get("overround")
    market = _bet_market_label(bet)
    model_source = bet.get("model_source") or ("Poisson" if market != "match_winner" else "Elo")
    reasons_now = []
    confidence_reasons = []
    risk_flags = []
    invalidators = []

    if model_prob is not None and consensus is not None:
        gap_pp = (model_prob - consensus) * 100
        reasons_now.append(
            f"Model probability {model_prob*100:.1f}% liegt bei {gap_pp:+.1f} Prozentpunkten zum Marktkonsens."
        )
        if gap_pp > 0:
            confidence_reasons.append("Das Modell liegt ueber Markt und liefert damit eine spielbare Fehlbewertung.")
        else:
            risk_flags.append("Der Markt stuetzt die Modellmeinung nicht klar; das Signal lebt eher von der Quote.")
    if odds:
        reasons_now.append(f"Die beste verfuegbare Quote liegt bei {odds:.2f}.")
    if edge_pct:
        confidence_reasons.append(f"Die Edge liegt bei {edge_pct:.1f}% und stuetzt den positiven Erwartungswert.")
    if overround is not None:
        if overround <= 0.08:
            confidence_reasons.append("Der Markt ist relativ sauber bepreist, der Overround bleibt moderat.")
        else:
            risk_flags.append(f"Der Overround ist mit {overround*100:.1f}% relativ hoch und verschlechtert die Signalqualitaet.")
    if bet.get("training_matches"):
        confidence_reasons.append(
            f"Die Datentiefe fuer dieses Modell liegt bei {int(bet['training_matches'])} historischen Matches."
        )
    if bet.get("market_gap_flag"):
        risk_flags.append("Der Market-Gap-Filter hat das Signal bereits skeptischer eingestuft.")
    if odds >= 3.5:
        risk_flags.append("Hohe Quote bedeutet mehr Varianz als bei moderaten Favoritenmärkten.")

    invalidators.append("Starke Quotenbewegung oder neue Team-/Lineup-Informationen vor Kickoff.")
    if market == "totals":
        invalidators.append("Totals-Linie oder Marktstruktur veraendert sich vor dem Spiel deutlich.")
    else:
        invalidators.append("Das Modell verliert seine Relevanz, wenn Closing Odds die Edge weitgehend absorbieren.")

    summary = (
        f"{bet.get('tip', '?')} @ {odds:.2f} bleibt spielbar, "
        f"weil Modell und Markt aktuell eine verwertbare Differenz zeigen."
    )
    if not confidence_reasons:
        confidence_reasons.append("Das Signal ist nur schwach gestuetzt und sollte eher beobachtet werden.")
    if not risk_flags:
        risk_flags.append("Vor Kickoff bleiben Marktbewegungen und neue Informationen der wichtigste Unsicherheitsfaktor.")

    return {
        "summary": summary,
        "why_now": reasons_now or ["Das Signal entsteht aus der aktuellen Modellbewertung gegen den Marktpreis."],
        "model_basis": [
            f"Primäre Modellquelle: {model_source}",
            "Marktkonsens aus normalisierten Bookmaker-Quoten",
            f"Markttyp: {market}",
        ],
        "confidence_reason": confidence_reasons,
        "risk_flags": risk_flags,
        "invalidators": invalidators,
        "drivers": _build_sports_drivers(bet),
        "version": "v1",
    }


def _normalize_hub_signal(bet: dict, run_ref: str) -> dict:
    competition = _competition_label(bet)
    side = _bet_side(bet)
    market = _bet_market_label(bet)
    status = _bet_status(bet)
    explainability = _build_sports_explainability(bet)
    title = f"{bet.get('tip', '?')} @ {bet.get('best_odds', 0):.2f}"
    subtitle = f"{competition} | {bet.get('match', '?')}"
    return {
        "signal_id": _build_signal_id(bet),
        "run_id": run_ref,
        "system": "sports-scanner",
        "category": "bet",
        "status": status,
        "priority": int(round(bet.get("confidence_score", 0))),
        "title": title,
        "subtitle": subtitle,
        "entity": {
            "primary": bet.get("tip"),
            "secondary": bet.get("match"),
            "market": market,
            "side": side,
            "competition": competition,
        },
        "timing": {
            "event_time": bet.get("kick_off"),
            "expires_at": bet.get("kick_off"),
        },
        "metrics": {
            "model_prob": round(bet.get("model_prob", 0.0), 4) if bet.get("model_prob") is not None else None,
            "consensus_prob": round(bet.get("consensus_prob", 0.0), 4) if bet.get("consensus_prob") is not None else None,
            "edge_pct": round(bet.get("edge_pct", 0.0), 2),
            "best_odds": round(bet.get("best_odds", 0.0), 2),
            "overround": round(bet.get("overround", 0.0), 4) if bet.get("overround") is not None else None,
            "stake_eur": round(bet.get("stake_eur", 0.0), 2),
            "training_matches": int(bet.get("training_matches", 0) or 0),
            "tier": bet.get("tier"),
            "bookmaker": bet.get("best_odds_bookie"),
        },
        "explainability": explainability,
    }


def _upsert_hub_records(
    path: Path,
    records: list[dict],
    key_field: str,
    system_name: str,
) -> None:
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing = [r for r in existing if r.get("system") != system_name]
    merged = existing + records
    merged.sort(key=lambda r: (r.get("generated_at") or r.get("run_id") or r.get(key_field) or ""), reverse=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_hub_exports(
    run_payload: dict,
    signals: list[dict],
    hub_dir: Path = HUB_DIR,
) -> None:
    hub_dir.mkdir(parents=True, exist_ok=True)
    _upsert_hub_records(hub_dir / "latest_runs.json", [run_payload], "run_id", "sports-scanner")
    _upsert_hub_records(hub_dir / "latest_signals.json", signals, "signal_id", "sports-scanner")


# ═══════════════════════════════════════════════════════════════════════════════
# RETRY-LOGIK
# ═══════════════════════════════════════════════════════════════════════════════

def _request_with_retry(url: str, params: dict | None = None,
                        retries: int = 3, backoff: list | None = None,
                        timeout: int = 30, **kwargs) -> requests.Response:
    """HTTP GET mit Retry-Logik und exponentiellem Backoff."""
    if backoff is None:
        backoff = [2, 4, 8]
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                wait = backoff[min(attempt, len(backoff) - 1)]
                print(f"    Retry ({attempt + 1}/{retries}): {e} – warte {wait}s …")
                time.sleep(wait)
    raise last_error


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════

def load_creds() -> dict:
    if not CREDS_FILE.exists():
        print(f"ERROR: Credentials-Datei fehlt: {CREDS_FILE}")
        return {}
    creds = {}
    try:
        with open(CREDS_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"ERROR: Credentials-Datei nicht gefunden: {CREDS_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Credentials laden fehlgeschlagen: {e}", file=sys.stderr)
    return creds


# ═══════════════════════════════════════════════════════════════════════════════
# THE ODDS API
# ═══════════════════════════════════════════════════════════════════════════════

def get_active_sports(api_key: str) -> list:
    r = _request_with_retry(f"{ODDS_API_BASE}/sports",
                            params={"apiKey": api_key}, timeout=15)
    return r.json()


def get_odds(api_key: str, sport_key: str, markets: str = "h2h,totals") -> list:
    params = {
        "apiKey":     api_key,
        "regions":    "eu",
        "markets":    markets,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = _request_with_retry(f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                                params=params, timeout=20)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    remaining = r.headers.get("x-requests-remaining", "?")
    try:
        remaining_int = int(remaining)
    except Exception:
        remaining_int = None
    global ODDS_API_REMAINING
    ODDS_API_REMAINING = remaining_int
    print(f"    → {len(r.json())} Matches | API-Requests verbleibend: {remaining}")
    return r.json()


def best_odds_from_match(match: dict) -> dict:
    """Bestes Decimal-Odd pro Outcome (home, draw, away) aus allen Bookies."""
    home = match["home_team"]
    away = match["away_team"]
    best = {"home": 1.0, "draw": 1.0, "away": 1.0}
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            for o in market["outcomes"]:
                price = float(o["price"])
                if o["name"] == home:
                    best["home"] = max(best["home"], price)
                elif o["name"] == away:
                    best["away"] = max(best["away"], price)
                elif o["name"] == "Draw":
                    best["draw"] = max(best["draw"], price)
    return best


def best_ou_odds_from_match(match: dict) -> list[dict]:
    """
    Extrahiert die besten Over/Under-Quoten pro Linie aus allen Bookies.
    Gibt Liste von {line, over_odds, under_odds} zurück.
    """
    best: dict[float, dict] = {}  # line → {over: float, under: float}
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "totals":
                continue
            for o in market["outcomes"]:
                line  = float(o.get("point", 0))
                price = float(o["price"])
                side  = o["name"].lower()  # "over" or "under"
                if line not in best:
                    best[line] = {"over": 1.0, "under": 1.0}
                best[line][side] = max(best[line][side], price)
    result = []
    for line, odds in sorted(best.items()):
        if odds["over"] > 1.0 and odds["under"] > 1.0:
            result.append({"line": line, "over_odds": odds["over"], "under_odds": odds["under"]})
    return result


def bookie_consensus(match: dict) -> dict:
    """Konsenswahrscheinlichkeiten (normalisierter Schnitt über alle Bookies)."""
    sums = {"home": [], "draw": [], "away": []}
    home = match["home_team"]
    away = match["away_team"]
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            o_map = {}
            for o in market["outcomes"]:
                if o["name"] == home:
                    o_map["home"] = 1 / float(o["price"])
                elif o["name"] == away:
                    o_map["away"] = 1 / float(o["price"])
                elif o["name"] == "Draw":
                    o_map["draw"] = 1 / float(o["price"])
            total = sum(o_map.values())
            if total > 0:
                for k in sums:
                    if k in o_map:
                        sums[k].append(o_map[k] / total)
    result = {}
    for k, vals in sums.items():
        result[k] = float(np.mean(vals)) if vals else None
    return result


def enrich_bets_with_market_data(bets: list, match: dict) -> None:
    """Reichert Bet-Dicts mit Konsens-Daten und Overround an (für Confidence Scoring)."""
    consensus = bookie_consensus(match)
    # Overround berechnen
    overrounds = []
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            s = sum(1 / float(o["price"]) for o in market["outcomes"] if float(o["price"]) > 0)
            if s > 0:
                overrounds.append(s - 1)
    avg_overround = float(np.mean(overrounds)) if overrounds else None

    for b in bets:
        b["overround"] = avg_overround
        # Konsens-Prob für die getippte Seite ermitteln
        outcome_side = None
        tip = b.get("tip", "")
        match_str = b.get("match", "")
        parts = match_str.split(" – ", 1)
        home = parts[0].strip() if parts else ""
        away = parts[1].strip() if len(parts) > 1 else ""
        if tip == home:
            outcome_side = "home"
        elif tip == away:
            outcome_side = "away"
        elif tip in ("Unentschieden", "Draw"):
            outcome_side = "draw"
        if outcome_side and consensus.get(outcome_side) is not None:
            b["consensus_prob"] = consensus[outcome_side]


# ═══════════════════════════════════════════════════════════════════════════════
# CLUB-ELO
# ═══════════════════════════════════════════════════════════════════════════════

def download_clubelo(date: str) -> dict:
    """
    Lädt Club-Elo-Ratings für ein Datum (Format: YYYY-MM-DD).
    Gibt {club_name: elo_rating} zurück.
    """
    for url in [CLUBELO_URL_HTTPS.format(date=date), CLUBELO_URL_HTTP.format(date=date)]:
        try:
            r = _request_with_retry(url, timeout=20)
            df = pd.read_csv(StringIO(r.text))
            result = {}
            for _, row in df.iterrows():
                club = row.get("Club")
                elo  = row.get("Elo")
                if pd.notna(club) and pd.notna(elo):
                    result[str(club).strip()] = float(elo)
            return result
        except Exception as e:
            last_err = e
            continue
    print(f"    Warning: Club-Elo ({date}): {last_err}")
    return {}


def download_clubelo_with_fallback(max_days_back: int = 3) -> tuple[str, dict]:
    """
    Versucht Club-Elo für heute, dann bis max_days_back Tage zurück.
    Gibt (date_str, elo_dict) zurück. date_str leer bei Fehlschlag.
    """
    today = datetime.now(timezone.utc).date()
    for i in range(max_days_back + 1):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        elo_dict = download_clubelo(date_str)
        if elo_dict:
            return date_str, elo_dict
    return "", {}


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL: DATEN LADEN
# ═══════════════════════════════════════════════════════════════════════════════

def download_fdco(url: str) -> pd.DataFrame | None:
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), encoding="latin-1")
        return df
    except Exception as e:
        print(f"    Warning: {url}: {e}")
        return None


def standardize_fdco(df: pd.DataFrame, is_new_format: bool = False) -> pd.DataFrame | None:
    """Einheitliche Spalten: HomeTeam, AwayTeam, FTHG, FTAG (+ Date wenn vorhanden)."""
    needed = ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    for col in needed:
        if col not in df.columns:
            print(f"    Warning: Spalte '{col}' fehlt")
            return None
    keep = needed + (["Date"] if "Date" in df.columns else [])
    df = df[keep].copy()
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"]  = pd.to_numeric(df["FTAG"],  errors="coerce")
    df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["HomeTeam"] = df["HomeTeam"].str.strip()
    df["AwayTeam"] = df["AwayTeam"].str.strip()
    return df


def load_liga3_data() -> pd.DataFrame | None:
    """Lädt 3. Liga Matchdaten von OpenLigaDB (laufende + vergangene Saison)."""
    frames = []
    current_year = datetime.now().year
    seasons = [current_year - 1, current_year - 2]  # z.B. 2025, 2024
    for season in seasons:
        url = f"{OPENLIGADB_BASE}/getmatchdata/bl3/{season}"
        try:
            r = _request_with_retry(url, timeout=30)
            matches = r.json()
        except Exception as e:
            print(f"    Warning: OpenLigaDB Saison {season}: {e}")
            continue

        rows = []
        for m in matches:
            if not m.get("matchIsFinished"):
                continue
            results = m.get("matchResults", [])
            if not results:
                continue
            # Endstand: resultTypeID=2 oder letzter Eintrag
            final = [r for r in results if r.get("resultTypeID") == 2]
            if not final:
                final = results
            r_data = final[-1]
            home = m["team1"]["teamName"].strip() if m.get("team1") else None
            away = m["team2"]["teamName"].strip() if m.get("team2") else None
            if home and away:
                rows.append({
                    "HomeTeam": home,
                    "AwayTeam": away,
                    "FTHG":     float(r_data["pointsTeam1"]),
                    "FTAG":     float(r_data["pointsTeam2"]),
                    "Date":     (m.get("matchDateTime") or "")[:10],
                })
        if rows:
            frames.append(pd.DataFrame(rows))
            print(f"    OpenLigaDB Saison {season}: {len(rows)} Matches")

    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)


def load_football_data(sport_key: str) -> pd.DataFrame | None:
    if sport_key == "soccer_germany_liga3":
        return load_liga3_data()

    season_codes = current_season_codes()
    league_code = FDCO_LEAGUES.get(sport_key)
    urls = build_fdco_urls(league_code, season_codes) if league_code else []
    frames = []
    for url in urls:
        df_raw = download_fdco(url)
        if df_raw is not None:
            df = standardize_fdco(df_raw, is_new_format=False)
            if df is not None and len(df) > 5:
                frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)


def load_european_data() -> pd.DataFrame | None:
    """
    Lädt Matchdaten aus Top-5-Ligen + Bundesliga 1+2 für das europäische
    Poisson-Modell (O/U bei UEFA-Wettbewerben).
    """
    frames = []
    season_codes = current_season_codes()
    for league_code in EUROPEAN_FDCO_LEAGUES:
        for url in build_fdco_urls(league_code, season_codes):
            df_raw = download_fdco(url)
            if df_raw is not None:
                df = standardize_fdco(df_raw)
                if df is not None and len(df) > 5:
                    frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL: POISSON-MODELL (Dixon-Coles-Stil)
# ═══════════════════════════════════════════════════════════════════════════════

def fit_poisson_model(df: pd.DataFrame, decay_rate: float = 0.005) -> dict:
    """
    Passt Attack/Defense-Parameter per Maximum-Likelihood an.
    log(λ_heim) = home_adv + attack[heim] – defense[gast]
    log(λ_gast) = attack[gast]            – defense[heim]

    Time-Decay: weight = exp(-decay_rate * days_ago)
    Halbwertszeit bei decay_rate=0.005 ≈ 140 Tage.
    """
    teams    = sorted(set(df["HomeTeam"]) | set(df["AwayTeam"]))
    n_teams  = len(teams)
    idx      = {t: i for i, t in enumerate(teams)}

    ht = df["HomeTeam"].map(idx).values
    at = df["AwayTeam"].map(idx).values
    hg = df["FTHG"].values.astype(float)
    ag = df["FTAG"].values.astype(float)

    # Time-Decay Gewichte berechnen
    weights = np.ones(len(df))
    if "Date" in df.columns and decay_rate > 0:
        today = pd.Timestamp.now()
        dates = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
        days_ago = (today - dates).dt.days.fillna(180).values.astype(float)
        weights = np.exp(-decay_rate * days_ago)

    n_params = 2 * n_teams + 1
    x0       = np.zeros(n_params)
    x0[-1]   = 0.25  # Home-Vorteil

    def neg_ll(x):
        att = x[:n_teams]
        dfs = x[n_teams:2*n_teams]
        ha  = x[-1]
        lh  = np.exp(ha  + att[ht] - dfs[at])
        la  = np.exp(att[at] - dfs[ht])
        ll  = (hg * np.log(lh + 1e-10) - lh
             + ag * np.log(la + 1e-10) - la)
        return -np.sum(weights * ll)

    constraints = [{"type": "eq", "fun": lambda x: x[0]}]
    res = minimize(neg_ll, x0, method="SLSQP",
                   constraints=constraints,
                   options={"maxiter": 2000, "ftol": 1e-9})
    if not res.success:
        raise RuntimeError(f"Poisson-Fit fehlgeschlagen: {res.message}")

    x   = res.x
    return {
        "attack":   {t: x[i]           for t, i in idx.items()},
        "defense":  {t: x[n_teams + i] for t, i in idx.items()},
        "home_adv": float(x[-1]),
        "teams":    teams,
        "training_matches": int(len(df)),
    }


def predict_football(home: str, away: str, model: dict, max_goals: int = 8):
    """Liefert P(Heim-Sieg), P(Unentschieden), P(Auswärtssieg) via Poisson."""
    attack  = model["attack"]
    defense = model["defense"]
    ha      = model["home_adv"]
    if home not in attack or away not in attack:
        return None
    lh = math.exp(ha  + attack[home] - defense[away])
    la = math.exp(attack[away] - defense[home])
    hp = [poisson.pmf(g, lh) for g in range(max_goals + 1)]
    ap = [poisson.pmf(g, la) for g in range(max_goals + 1)]
    p_h = p_d = p_a = 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = hp[hg] * ap[ag]
            if   hg > ag: p_h += p
            elif hg == ag: p_d += p
            else:          p_a += p
    total = p_h + p_d + p_a
    return {
        "home": p_h / total,
        "draw": p_d / total,
        "away": p_a / total,
        "lam_home": lh,
        "lam_away": la,
    }


def predict_ou(lam_home: float, lam_away: float, line: float) -> tuple[float, float]:
    """
    Berechnet P(Über line) und P(Unter line) via Poisson.
    Korrekt für .5-Linien (2.5, 3.5) und ganzzahlige Linien (2.0, 3.0).
    Gibt (p_over, p_under) zurück, die sich zu 1.0 summieren.
    """
    lam_total = lam_home + lam_away
    p_under = float(poisson.cdf(math.floor(line - 1e-9), lam_total))
    p_over  = 1.0 - p_under
    return p_over, p_under


def predict_most_likely_score(lam_home: float, lam_away: float,
                              max_goals: int = 6,
                              tendency: str | None = None) -> tuple[int, int]:
    """Gibt das wahrscheinlichste (Heim-Tore, Gast-Tore) zurück.
    Wenn tendency angegeben, wird nur innerhalb der Tendenz gesucht:
      Heimsieg → home > away, Unentschieden → home == away,
      Auswärtssieg → away > home.
    """
    best_p, best_h, best_a = 0.0, 1, 1
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            if tendency == "Heimsieg" and h <= a:
                continue
            if tendency == "Unentschieden" and h != a:
                continue
            if tendency == "Auswärtssieg" and a <= h:
                continue
            p = poisson.pmf(h, lam_home) * poisson.pmf(a, lam_away)
            if p > best_p:
                best_p, best_h, best_a = p, h, a
    return best_h, best_a


def elo_to_football_1x2(elo_home: float, elo_away: float,
                         home_adv: float = 65.0) -> tuple[float, float, float]:
    """
    Konvertiert Club-Elo-Ratings in 1X2-Wahrscheinlichkeiten für Fußball.
    home_adv: Heimvorteil in Elo-Punkten (Standard: 65 für UEFA-Heimspiele).
    Gibt (p_home, p_draw, p_away) zurück.
    """
    dr      = elo_home + home_adv - elo_away
    e_home  = 1.0 / (1.0 + 10.0 ** (-dr / 400.0))
    # Unentschieden: max ~28% bei ausgeglichenem Spiel, sinkt bei Favoriten
    p_draw  = 0.28 * math.exp(-2.0 * (e_home - 0.5) ** 2)
    remaining = 1.0 - p_draw
    p_home  = e_home * remaining
    p_away  = (1.0 - e_home) * remaining
    return p_home, p_draw, p_away


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL: TEAM-NAMEN-MATCHING
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_name(name: str) -> str:
    """Vereinheitlicht Sonderzeichen und Füllwörter."""
    replacements = {
        "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
        "Ä": "A", "Ö": "O", "Ü": "U",
    }
    for src, tgt in replacements.items():
        name = name.replace(src, tgt)
    for prefix in ["FC ", "SC ", "SV ", "VfL ", "VfB ", "TSG ", "SSV ", "FSV ",
                   "1. FC ", "1. FSV ", "SpVgg ", "SG ", "BV ", "BSC "]:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip().lower()


def find_team_in_model(api_name: str, model_teams: list) -> str | None:
    """Findet den passenden Modell-Teamnamen zum API-Teamnamen."""
    # 1) Exakt
    if api_name in model_teams:
        return api_name
    # 2) Normalisiert exakt
    norm_api = normalize_name(api_name)
    for t in model_teams:
        if normalize_name(t) == norm_api:
            return t
    # 3) Teilstring
    for t in model_teams:
        nt = normalize_name(t)
        if norm_api in nt or nt in norm_api:
            return t
    # 4) Difflib-Fuzzy
    close = difflib.get_close_matches(norm_api,
                                       [normalize_name(t) for t in model_teams],
                                       n=1, cutoff=0.6)
    if close:
        norm_match = close[0]
        for t in model_teams:
            if normalize_name(t) == norm_match:
                return t
    return None


def find_club_elo(name: str, elo_dict: dict) -> float | None:
    """Findet Club-Elo-Rating via fuzzy Matching (analog find_team_in_model)."""
    if name in elo_dict:
        return elo_dict[name]
    norm = normalize_name(name)
    for club, elo in elo_dict.items():
        if normalize_name(club) == norm:
            return elo
    for club, elo in elo_dict.items():
        nc = normalize_name(club)
        if norm in nc or nc in norm:
            return elo
    close = difflib.get_close_matches(norm,
                                       [normalize_name(c) for c in elo_dict],
                                       n=1, cutoff=0.6)
    if close:
        for club, elo in elo_dict.items():
            if normalize_name(club) == close[0]:
                return elo
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TENNIS: ELO-MODELL (Jeff Sackmann ATP-Daten)
# ═══════════════════════════════════════════════════════════════════════════════

ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"


def download_atp_year(year: int) -> pd.DataFrame | None:
    url = f"{ATP_BASE}/atp_matches_{year}.csv"
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        return df
    except Exception as e:
        print(f"    Warning: ATP {year}: {e}")
        return None


# Surface-Mapping für Odds API Turniere → Belag
TOURNAMENT_SURFACE = {
    "Australian Open": "Hard", "US Open": "Hard",
    "French Open": "Clay", "Roland Garros": "Clay",
    "Wimbledon": "Grass",
    "Indian Wells": "Hard", "Miami Open": "Hard",
    "Monte Carlo": "Clay", "Monte-Carlo": "Clay",
    "Madrid": "Clay", "Rome": "Clay", "Roma": "Clay",
    "Barcelona": "Clay", "Hamburg": "Clay",
    "Cincinnati": "Hard", "Shanghai": "Hard",
    "Canada": "Hard", "Montreal": "Hard", "Toronto": "Hard",
    "Dubai": "Hard", "Doha": "Hard", "Brisbane": "Hard",
    "Halle": "Grass", "Queen's": "Grass", "Stuttgart": "Clay",
    "Basel": "Hard", "Vienna": "Hard", "ATP Finals": "Hard",
    "WTA Finals": "Hard",
}


def _detect_surface(tournament_name: str) -> str | None:
    """Erkennt den Belag anhand des Turniernamens."""
    name_lower = tournament_name.lower()
    for key, surface in TOURNAMENT_SURFACE.items():
        if key.lower() in name_lower:
            return surface
    return None


def compute_tennis_elo(years: list) -> tuple[dict, dict, int]:
    """
    Berechnet Elo-Ratings aus historischen ATP-Matches.
    Gibt (gesamt_elo, surface_elo, training_matches) zurück.
    surface_elo = {"Hard": {name: elo}, "Clay": {...}, "Grass": {...}}
    """
    from concurrent.futures import ThreadPoolExecutor
    elo = {}
    surface_elo = {"Hard": {}, "Clay": {}, "Grass": {}}
    all_frames = []
    with ThreadPoolExecutor(max_workers=len(years)) as pool:
        results = list(pool.map(download_atp_year, years))
    for df in results:
        if df is not None and "winner_name" in df.columns:
            all_frames.append(df)
    if not all_frames:
        print("    Warning: Keine ATP-Daten geladen")
        return {}, surface_elo, 0

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.dropna(subset=["winner_name", "loser_name"])
    if "tourney_date" in combined.columns:
        combined["tourney_date"] = pd.to_numeric(combined["tourney_date"], errors="coerce")
        combined = combined.sort_values("tourney_date")

    def expected(ra, rb):
        return 1 / (1 + 10 ** ((rb - ra) / 400))

    for _, row in combined.iterrows():
        w = str(row["winner_name"]).strip()
        l = str(row["loser_name"]).strip()
        if not w or not l:
            continue
        # Gesamt-Elo
        elo.setdefault(w, ELO_INITIAL)
        elo.setdefault(l, ELO_INITIAL)
        e_w = expected(elo[w], elo[l])
        e_l = 1 - e_w
        elo[w] += ELO_K_FACTOR * (1 - e_w)
        elo[l] += ELO_K_FACTOR * (0 - e_l)
        # Surface-Elo
        surface = str(row.get("surface", "")).strip().capitalize() if pd.notna(row.get("surface")) else None
        if surface in surface_elo:
            s_elo = surface_elo[surface]
            s_elo.setdefault(w, ELO_INITIAL)
            s_elo.setdefault(l, ELO_INITIAL)
            se_w = expected(s_elo[w], s_elo[l])
            se_l = 1 - se_w
            s_elo[w] += ELO_K_FACTOR * (1 - se_w)
            s_elo[l] += ELO_K_FACTOR * (0 - se_l)

    return elo, surface_elo, int(len(combined))


def download_wta_year(year: int) -> pd.DataFrame | None:
    url = f"{WTA_BASE}/wta_matches_{year}.csv"
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        return df
    except Exception as e:
        print(f"    Warning: WTA {year}: {e}")
        return None


def compute_wta_elo(years: list) -> tuple[dict, dict, int]:
    """Berechnet Elo-Ratings aus historischen WTA-Matches.
    Gibt (gesamt_elo, surface_elo, training_matches) zurück."""
    from concurrent.futures import ThreadPoolExecutor
    elo = {}
    surface_elo = {"Hard": {}, "Clay": {}, "Grass": {}}
    all_frames = []
    with ThreadPoolExecutor(max_workers=len(years)) as pool:
        results = list(pool.map(download_wta_year, years))
    for df in results:
        if df is not None and "winner_name" in df.columns:
            all_frames.append(df)
    if not all_frames:
        print("    Warning: Keine WTA-Daten geladen")
        return {}, surface_elo, 0

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.dropna(subset=["winner_name", "loser_name"])
    if "tourney_date" in combined.columns:
        combined["tourney_date"] = pd.to_numeric(combined["tourney_date"], errors="coerce")
        combined = combined.sort_values("tourney_date")

    def expected(ra, rb):
        return 1 / (1 + 10 ** ((rb - ra) / 400))

    for _, row in combined.iterrows():
        w = str(row["winner_name"]).strip()
        l = str(row["loser_name"]).strip()
        if not w or not l:
            continue
        elo.setdefault(w, ELO_INITIAL)
        elo.setdefault(l, ELO_INITIAL)
        e_w = expected(elo[w], elo[l])
        e_l = 1 - e_w
        elo[w] += ELO_K_FACTOR * (1 - e_w)
        elo[l] += ELO_K_FACTOR * (0 - e_l)
        surface = str(row.get("surface", "")).strip().capitalize() if pd.notna(row.get("surface")) else None
        if surface in surface_elo:
            s_elo = surface_elo[surface]
            s_elo.setdefault(w, ELO_INITIAL)
            s_elo.setdefault(l, ELO_INITIAL)
            se_w = expected(s_elo[w], s_elo[l])
            se_l = 1 - se_w
            s_elo[w] += ELO_K_FACTOR * (1 - se_w)
            s_elo[l] += ELO_K_FACTOR * (0 - se_l)

    return elo, surface_elo, int(len(combined))


def predict_tennis_win_prob(elo_a: float, elo_b: float) -> float:
    """P(Spieler A schlägt Spieler B)."""
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))


def find_player_elo(name: str, elo_dict: dict) -> float | None:
    """Findet Elo-Rating eines Spielers (fuzzy)."""
    if name in elo_dict:
        return elo_dict[name]
    # Teilstring-Match
    name_lower = name.lower()
    for player, rating in elo_dict.items():
        if name_lower in player.lower() or player.lower() in name_lower:
            return rating
    # Difflib
    close = difflib.get_close_matches(name, list(elo_dict.keys()), n=1, cutoff=0.7)
    if close:
        return elo_dict[close[0]]
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# VALUE BETTING
# ═══════════════════════════════════════════════════════════════════════════════

def compute_value(model_prob: float, odds: float) -> tuple[float, float]:
    """
    Edge  = model_prob × odds – 1
    Kelly = edge / (odds – 1)
    """
    if odds <= 1.0 or model_prob <= 0:
        return 0.0, 0.0
    edge  = model_prob * odds - 1.0
    kelly = edge / (odds - 1.0)
    return edge, kelly


def format_dt(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        return iso_str[:16]


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_football_match(match: dict, model: dict) -> list:
    home_api = match["home_team"]
    away_api = match["away_team"]
    model_teams = model["teams"]

    home_model = find_team_in_model(home_api, model_teams)
    away_model = find_team_in_model(away_api, model_teams)

    if not home_model or not away_model:
        return []

    probs = predict_football(home_model, away_model, model)
    if not probs:
        return []

    best   = best_odds_from_match(match)
    bets   = []
    labels = {"home": home_api, "draw": "Unentschieden", "away": away_api}

    for outcome in ("home", "draw", "away"):
        odds     = best[outcome]
        model_p  = probs[outcome]
        if odds < MIN_ODDS:
            continue
        edge, kelly = compute_value(model_p, odds)
        if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
            bets.append({
                "type":       "football",
                "sport":      match.get("sport_key", ""),
                "match":      f"{home_api} – {away_api}",
                "tip":        labels[outcome],
                "kick_off":   match["commence_time"],
                "model_prob": model_p,
                "best_odds":  odds,
                "edge_pct":   edge * 100,
                "kelly_pct":  min(kelly, MAX_KELLY) * 100,
                "lam_home":   probs["lam_home"],
                "lam_away":   probs["lam_away"],
                "home_model": home_model,
                "away_model": away_model,
                "training_matches": model.get("training_matches"),
            })
    return bets


def analyze_football_ou(match: dict, model: dict) -> list:
    """Value Bets für Über/Unter-Märkte via Poisson-Modell."""
    home_api = match["home_team"]
    away_api = match["away_team"]
    model_teams = model["teams"]

    home_model = find_team_in_model(home_api, model_teams)
    away_model = find_team_in_model(away_api, model_teams)
    if not home_model or not away_model:
        return []

    probs = predict_football(home_model, away_model, model)
    if not probs:
        return []

    lam_home = probs["lam_home"]
    lam_away = probs["lam_away"]
    ou_lines  = best_ou_odds_from_match(match)
    bets      = []

    for entry in ou_lines:
        line       = entry["line"]
        if abs(line - round(line)) < 1e-9:
            continue  # ganzzahlige Linien: Push-Fall nicht modelliert
        p_over, p_under = predict_ou(lam_home, lam_away, line)

        for side, model_p, odds in [
            ("Über",  p_over,  entry["over_odds"]),
            ("Unter", p_under, entry["under_odds"]),
        ]:
            if odds < MIN_ODDS:
                continue
            edge, kelly = compute_value(model_p, odds)
            if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                bets.append({
                    "type":       "football_ou",
                    "sport":      match.get("sport_key", ""),
                    "match":      f"{home_api} – {away_api}",
                    "line":       line,
                    "tip":        f"{side} {line}",
                    "kick_off":   match["commence_time"],
                    "model_prob": model_p,
                    "best_odds":  odds,
                    "edge_pct":   edge * 100,
                    "kelly_pct":  min(kelly, MAX_KELLY) * 100,
                    "lam_home":   lam_home,
                    "lam_away":   lam_away,
                    "training_matches": model.get("training_matches"),
                })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# UEFA ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_uefa_match(match: dict, elo_dict: dict,
                       euro_model: dict | None) -> list:
    """
    Value Bets für UEFA-Matches:
    - 1X2 via Club-Elo (elo_to_football_1x2)
    - O/U  via Multi-Liga Poisson-Modell (euro_model)
    """
    home_api = match["home_team"]
    away_api = match["away_team"]
    best     = best_odds_from_match(match)
    bets     = []

    # ── 1X2 via Club-Elo ────────────────────────────────────────────────────
    elo_home = find_club_elo(home_api, elo_dict)
    elo_away = find_club_elo(away_api, elo_dict)

    if elo_home is not None and elo_away is not None:
        p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
        labels = {"home": home_api, "draw": "Unentschieden", "away": away_api}
        probs  = {"home": p_home,   "draw": p_draw,          "away": p_away}

        for outcome in ("home", "draw", "away"):
            odds    = best[outcome]
            model_p = probs[outcome]
            if odds < MIN_ODDS:
                continue
            edge, kelly = compute_value(model_p, odds)
            if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                bets.append({
                    "type":           "1x2",
                    "sport":          match.get("sport_key", ""),
                    "match":          f"{home_api} – {away_api}",
                    "tip":            labels[outcome],
                    "kick_off":       match["commence_time"],
                    "model_prob":     model_p,
                    "best_odds":      odds,
                    "edge_pct":       edge * 100,
                    "kelly_pct":      min(kelly, MAX_KELLY) * 100,
                    "model_source":   "ClubElo",
                    "elo_home":       elo_home,
                    "elo_away":       elo_away,
                    "training_matches": len(elo_dict),
                })

    # ── O/U via Poisson ─────────────────────────────────────────────────────
    if euro_model:
        home_model = find_team_in_model(home_api, euro_model["teams"])
        away_model = find_team_in_model(away_api, euro_model["teams"])

        if home_model and away_model:
            probs_eu = predict_football(home_model, away_model, euro_model)
            if probs_eu:
                lam_home = probs_eu["lam_home"]
                lam_away = probs_eu["lam_away"]
                ou_lines = best_ou_odds_from_match(match)

                for entry in ou_lines:
                    line = entry["line"]
                    if abs(line - round(line)) < 1e-9:
                        continue  # ganzzahlige Linien: Push nicht modelliert
                    p_over, p_under = predict_ou(lam_home, lam_away, line)

                    for side, model_p, odds in [
                        ("Über",  p_over,  entry["over_odds"]),
                        ("Unter", p_under, entry["under_odds"]),
                    ]:
                        if odds < MIN_ODDS:
                            continue
                        edge, kelly = compute_value(model_p, odds)
                        if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                            bets.append({
                                "type":           "ou",
                                "sport":          match.get("sport_key", ""),
                                "match":          f"{home_api} – {away_api}",
                                "tip":            f"{side} {line}",
                                "kick_off":       match["commence_time"],
                                "model_prob":     model_p,
                                "best_odds":      odds,
                                "edge_pct":       edge * 100,
                                "kelly_pct":      min(kelly, MAX_KELLY) * 100,
                                "model_source":   "Poisson",
                                "lam_home":       lam_home,
                                "lam_away":       lam_away,
                                "training_matches": euro_model.get("training_matches"),
                            })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# TENNIS ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_tennis_match(
    match: dict,
    tournament: str,
    elo_dict: dict,
    surface_elo: dict | None = None,
    training_matches: int = 0,
) -> list:
    p1 = match["home_team"]
    p2 = match["away_team"]

    # Surface-spezifisches Elo bevorzugen
    surface = _detect_surface(tournament) if surface_elo else None
    s_elo = surface_elo.get(surface, {}) if surface and surface_elo else {}

    elo1_s = find_player_elo(p1, s_elo) if s_elo else None
    elo2_s = find_player_elo(p2, s_elo) if s_elo else None
    elo1_g = find_player_elo(p1, elo_dict)
    elo2_g = find_player_elo(p2, elo_dict)

    # Surface-Elo verwenden wenn für beide Spieler vorhanden, sonst Gesamt-Elo
    if elo1_s is not None and elo2_s is not None:
        elo1, elo2 = elo1_s, elo2_s
        model_source = f"Elo ({surface})" if surface else "Elo"
    elif elo1_g is not None and elo2_g is not None:
        elo1, elo2 = elo1_g, elo2_g
        model_source = "Elo"
    else:
        elo1 = elo2 = None
        model_source = None

    best = best_odds_from_match(match)
    bets = []

    if elo1 is not None and elo2 is not None:
        prob1 = predict_tennis_win_prob(elo1, elo2)
        prob2 = 1 - prob1
    else:
        # Fallback: Konsens der Bookies als Modell
        consensus = bookie_consensus(match)
        prob1 = consensus.get("home")
        prob2 = consensus.get("away")
        if not prob1 or not prob2:
            return []
        model_source = "Konsens"
        elo1 = elo2 = None

    for player, model_p, odds, elo_val in [
        (p1, prob1, best["home"], elo1),
        (p2, prob2, best["away"], elo2),
    ]:
        if odds < MIN_ODDS or not model_p:
            continue
        edge, kelly = compute_value(model_p, odds)
        if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
            bets.append({
                "type":         "tennis",
                "sport":        match.get("sport_key", ""),
                "tournament":   tournament,
                "match":        f"{p1} – {p2}",
                "tip":          player,
                "kick_off":     match["commence_time"],
                "model_prob":   model_p,
                "best_odds":    odds,
                "edge_pct":     edge * 100,
                "kelly_pct":    min(kelly, MAX_KELLY) * 100,
                "elo":          round(elo_val) if elo_val else None,
                "model_source": model_source,
                "training_matches": training_matches,
            })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# HTML-REPORT
# ═══════════════════════════════════════════════════════════════════════════════

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{--bg:#0a0a1a;--surface:rgba(15,15,35,0.8);--border:rgba(0,240,255,0.15);--cyan:#00f0ff;--gold:#c8aa6e;--pink:#ff006e;--green:#00ff88;--text:#e8e8f0;--dim:#6e6e80}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Inter,sans-serif;background:var(--bg);color:var(--text);padding:20px;min-height:100vh}
h1{color:var(--cyan);font-size:1.6em;padding-bottom:10px;border-bottom:1px solid var(--border);margin-bottom:6px}
h2{color:var(--gold);margin-top:32px;font-size:1.15em;padding-left:10px;border-left:3px solid var(--gold)}
.summary{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
      padding:14px 22px;min-width:130px;backdrop-filter:blur(16px);transition:all .25s}
.card:hover{border-color:var(--cyan);box-shadow:0 0 20px rgba(0,240,255,0.1)}
.card .val{font-family:'JetBrains Mono',monospace;font-size:1.9em;font-weight:700;color:var(--cyan)}
.card .lbl{color:var(--dim);font-size:0.8em;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--surface);
      border:1px solid var(--border);border-radius:12px;overflow:hidden;margin:14px 0}
th{background:rgba(0,240,255,0.06);padding:9px 12px;text-align:left;
   color:var(--gold);font-size:0.78em;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:0.88em;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,240,255,0.04)}
.g{color:var(--green);font-weight:700}
.y{color:var(--gold);font-weight:700}
.o{color:var(--pink);font-weight:700}
.tag{background:var(--cyan);color:var(--bg);border-radius:4px;
     padding:2px 7px;font-size:0.75em;font-weight:600}
.tag2{background:rgba(0,240,255,0.1);color:var(--cyan);border:1px solid var(--border);
      border-radius:4px;padding:2px 7px;font-size:0.75em}
.empty{color:var(--dim);padding:18px;text-align:center;
       background:var(--surface);border:1px solid var(--border);border-radius:12px}
.note{background:rgba(200,170,110,0.08);border-left:3px solid var(--gold);padding:10px 14px;
      color:var(--gold);font-size:0.82em;border-radius:0 8px 8px 0;margin:10px 0}
.tag3{background:rgba(255,0,110,0.15);color:var(--pink);border:1px solid rgba(255,0,110,0.3);
      border-radius:4px;padding:2px 7px;font-size:0.75em}
.league-header{color:var(--text);font-size:0.95em;margin:18px 0 4px 0;padding:0;border:none}
.league-header .tag,.league-header .tag2,.league-header .tag3{font-size:0.85em;padding:3px 10px}
details{margin:10px 0}
details summary{list-style:none}
details summary::-webkit-details-marker{display:none}
details[open] summary{margin-bottom:10px}
.footer{color:var(--dim);font-size:0.78em;margin-top:30px;
        border-top:1px solid var(--border);padding-top:14px}
"""

# ═══════════════════════════════════════════════════════════════════════════════
# KICKTIPP: PROGNOSEN SAMMELN + HTML/JSON GENERIEREN
# ═══════════════════════════════════════════════════════════════════════════════

def collect_kicktipp_predictions(football_models: dict, club_elo_dict: dict,
                                  euro_model: dict | None,
                                  loaded_matches: dict) -> list:
    """
    Sammelt Kicktipp-Tipps für alle Kicktipp-Ligen.
    Nutzt bereits geladene Matches (kein erneuter API-Call).

    Fallback-Kette wenn Poisson-Modell fehlschlägt:
    1. Poisson-Modell → Probs + Score
    2. Club-Elo → elo_to_football_1x2() für Probs, Durchschnitts-Lambda für Score
    3. Bookie-Konsens → normalisierte Quoten für Probs
    """
    results = []

    # ── Fußball-Ligen via Poisson (+ Fallbacks) ─────────────────────────────
    for sport_key in KICKTIPP_FOOTBALL_SPORTS:
        label = KICKTIPP_LABELS.get(sport_key, sport_key)
        model = football_models.get(sport_key)
        matches = loaded_matches.get(sport_key, [])
        print(f"  [Kicktipp] {label}: {len(matches)} Spiele …")

        for match in matches:
            home_api = match["home_team"]
            away_api = match["away_team"]
            kick_off = match.get("commence_time", "")

            p_home = p_draw = p_away = None
            score_home = score_away = None
            model_src = None

            # Fallback 1: Poisson-Modell
            lam_h = lam_a = None
            if model:
                home_model = find_team_in_model(home_api, model["teams"])
                away_model = find_team_in_model(away_api, model["teams"])
                if home_model and away_model:
                    probs = predict_football(home_model, away_model, model)
                    if probs:
                        p_home = probs["home"]
                        p_draw = probs["draw"]
                        p_away = probs["away"]
                        lam_h = probs["lam_home"]
                        lam_a = probs["lam_away"]
                        model_src = "Poisson"

            # Fallback 2: Club-Elo
            if p_home is None and club_elo_dict:
                elo_home = find_club_elo(home_api, club_elo_dict)
                elo_away = find_club_elo(away_api, club_elo_dict)
                if elo_home is not None and elo_away is not None:
                    p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "ClubElo"

            # Fallback 3: Bookie-Konsens
            if p_home is None:
                consensus = bookie_consensus(match)
                if consensus and consensus.get("home"):
                    p_home = consensus["home"]
                    p_draw = consensus["draw"]
                    p_away = consensus["away"]
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "Konsens"

            # Tendenz bestimmen
            if p_home is not None:
                if p_home >= p_draw and p_home >= p_away:
                    tendency = "Heimsieg"
                elif p_draw >= p_home and p_draw >= p_away:
                    tendency = "Unentschieden"
                else:
                    tendency = "Auswärtssieg"
            else:
                tendency = "?"

            # Score passend zur Tendenz berechnen
            if lam_h is not None and lam_a is not None:
                score_home, score_away = predict_most_likely_score(
                    lam_h, lam_a, tendency=tendency if tendency != "?" else None
                )

            results.append({
                "league":      label,
                "sport_key":   sport_key,
                "match":       f"{home_api} – {away_api}",
                "home_team":   home_api,
                "away_team":   away_api,
                "kick_off":    kick_off,
                "p_home":      p_home,
                "p_draw":      p_draw,
                "p_away":      p_away,
                "tendency":    tendency,
                "score_home":  score_home,
                "score_away":  score_away,
                "model_source": model_src,
            })

    # ── UEFA via Club-Elo + euro_model ───────────────────────────────────────
    for sport_key in KICKTIPP_UEFA_SPORTS:
        label = KICKTIPP_LABELS.get(sport_key, sport_key)
        matches = loaded_matches.get(sport_key, [])
        print(f"  [Kicktipp] {label}: {len(matches)} Spiele …")

        for match in matches:
            home_api = match["home_team"]
            away_api = match["away_team"]
            kick_off = match.get("commence_time", "")

            p_home = p_draw = p_away = None
            score_home = score_away = None
            model_src = None

            # Club-Elo für 1X2
            lam_h = lam_a = None
            if club_elo_dict:
                elo_home = find_club_elo(home_api, club_elo_dict)
                elo_away = find_club_elo(away_api, club_elo_dict)
                if elo_home is not None and elo_away is not None:
                    p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "ClubElo"

            # Score via euro_model falls vorhanden
            if euro_model:
                home_model = find_team_in_model(home_api, euro_model["teams"])
                away_model = find_team_in_model(away_api, euro_model["teams"])
                if home_model and away_model:
                    probs_eu = predict_football(home_model, away_model, euro_model)
                    if probs_eu:
                        lam_h = probs_eu["lam_home"]
                        lam_a = probs_eu["lam_away"]
                        if model_src is None:
                            p_home = probs_eu["home"]
                            p_draw = probs_eu["draw"]
                            p_away = probs_eu["away"]
                            model_src = "Poisson-EU"

            # Fallback Bookie-Konsens
            if p_home is None:
                consensus = bookie_consensus(match)
                if consensus and consensus.get("home"):
                    p_home = consensus["home"]
                    p_draw = consensus["draw"]
                    p_away = consensus["away"]
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "Konsens"

            # Tendenz bestimmen
            if p_home is not None:
                if p_home >= p_draw and p_home >= p_away:
                    tendency = "Heimsieg"
                elif p_draw >= p_home and p_draw >= p_away:
                    tendency = "Unentschieden"
                else:
                    tendency = "Auswärtssieg"
            else:
                tendency = "?"

            # Score passend zur Tendenz berechnen
            if lam_h is not None and lam_a is not None:
                score_home, score_away = predict_most_likely_score(
                    lam_h, lam_a, tendency=tendency if tendency != "?" else None
                )

            results.append({
                "league":      label,
                "sport_key":   sport_key,
                "match":       f"{home_api} – {away_api}",
                "home_team":   home_api,
                "away_team":   away_api,
                "kick_off":    kick_off,
                "p_home":      p_home,
                "p_draw":      p_draw,
                "p_away":      p_away,
                "tendency":    tendency,
                "score_home":  score_home,
                "score_away":  score_away,
                "model_source": model_src,
            })

    results.sort(key=lambda x: x["kick_off"])
    return results


def generate_kicktipp_html(matches: list) -> str:
    """Generiert HTML-Report für Kicktipp-Tipps."""
    date_str  = datetime.now().strftime("%d.%m.%Y")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    n_matches = len(matches)

    league_order = [
        "1. Bundesliga", "2. Bundesliga", "Premier League",
        "La Liga", "Serie A", "Ligue 1",
        "Champions League", "Europa League",
    ]
    leagues_present = []
    for lg in league_order:
        if any(m["league"] == lg for m in matches):
            leagues_present.append(lg)
    for m in matches:
        if m["league"] not in leagues_present:
            leagues_present.append(m["league"])

    def prob_cell(val, is_max):
        if val is None:
            return "<td>–</td>"
        pct = f"{val*100:.1f}%"
        if is_max:
            return f'<td style="color:#1a7a30;font-weight:700">{pct}</td>'
        return f"<td>{pct}</td>"

    sections = ""
    for league in leagues_present:
        league_matches = [m for m in matches if m["league"] == league]
        if not league_matches:
            continue

        rows = ""
        for m in league_matches:
            ph = m["p_home"]
            pd_ = m["p_draw"]
            pa = m["p_away"]

            vals = [v for v in [ph, pd_, pa] if v is not None]
            max_p = max(vals) if vals else None

            score = (
                f"{m['score_home']}:{m['score_away']}"
                if m["score_home"] is not None else "–"
            )

            tendency = m["tendency"]
            if tendency == "Heimsieg":
                tend_label = f'<strong style="color:#1a56a0">{m["home_team"]}</strong>'
            elif tendency == "Auswärtssieg":
                tend_label = f'<strong style="color:#c05a00">{m["away_team"]}</strong>'
            elif tendency == "Unentschieden":
                tend_label = '<strong style="color:#555577">Unentschieden</strong>'
            else:
                tend_label = "?"

            src_tag = f' <span class="tag2">{m.get("model_source", "?")}</span>' if m.get("model_source") else ""

            rows += f"""<tr>
              <td><strong>{m['home_team']} – {m['away_team']}</strong>{src_tag}</td>
              <td>{format_dt(m['kick_off'])}</td>
              <td>{tend_label}</td>
              <td style="font-weight:700;color:#333">{score}</td>
              {prob_cell(ph,  ph is not None and max_p is not None and ph == max_p)}
              {prob_cell(pd_, pd_ is not None and max_p is not None and pd_ == max_p)}
              {prob_cell(pa,  pa is not None and max_p is not None and pa == max_p)}
            </tr>"""

        icon = "🏆" if league in ("Champions League", "Europa League") else "⚽"
        sections += f"""<h2>{icon} {league}</h2>
<table>
<tr>
  <th>Spiel</th><th>Anstoß</th><th>Tipp (Tendenz)</th><th>Score</th>
  <th>P(Heim)</th><th>P(X)</th><th>P(Ausw.)</th>
</tr>
{rows}
</table>
"""

    if not sections:
        sections = '<div class="empty">Keine Kicktipp-Spiele gefunden.</div>'

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>🎯 Kicktipp-Tipps {date_str}</title>
<style>{CSS}
.kt-note {{ background:#f0fff4; border-left:3px solid #1a7a30; padding:10px 14px;
            color:#1a4a20; font-size:0.82em; border-radius:0 6px 6px 0; margin:10px 0; }}
</style>
</head>
<body>
<h1>🎯 Kicktipp-Tipps — {date_str}</h1>

<div class="summary">
  <div class="card"><div class="val">{n_matches}</div><div class="lbl">Spiele gesamt</div></div>
</div>

<div class="kt-note">
  📌 <strong>Hinweis:</strong> Tendenz und Score basieren auf dem Poisson-Modell (BL1/BL2/PL/LaLiga/SerieA/L1)
  bzw. Club-Elo-Modell (CL/EL). Grün hervorgehoben = höchste Wahrscheinlichkeit.
  Kein Ersatz für eigene Einschätzung!
</div>

{sections}

<div class="footer">
  Generiert: {timestamp} &nbsp;|&nbsp;
  Fußball: Poisson MLE (football-data.co.uk) &nbsp;|&nbsp;
  CL/EL: Club-Elo + Poisson &nbsp;|&nbsp;
  Matches: The Odds API<br>
  ⚠️ Diese Tipps dienen ausschließlich zu Unterhaltungszwecken.
</div>
</body>
</html>"""


def edge_class(e: float) -> str:
    if e >= 10: return "g"
    if e >= 5:  return "y"
    return "o"


def _group_by_league(bets: list, label_map: dict, key: str = "sport") -> dict:
    """Gruppiert Bets nach Liga/Turnier, sortiert innerhalb nach Anstoß + Edge."""
    groups = {}
    for b in bets:
        league = label_map.get(b[key], b[key]) if label_map else b[key]
        groups.setdefault(league, []).append(b)
    for league in groups:
        groups[league].sort(key=lambda x: (x["kick_off"], -x["edge_pct"]))
    # Sortiere Ligen nach frühestem Anstoß
    return dict(sorted(groups.items(), key=lambda kv: kv[1][0]["kick_off"]))


def build_football_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine Football-Value-Bets gefunden – Modell benötigt ausreichend historische Matches für alle Teams.</div>'
    headers = ["Spiel", "Tipp", "Anstoß", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "λ Heim", "λ Gast"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_bets in _group_by_league(bets, SPORT_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag">{league}</span> ({len(league_bets)})</h3>'
        rows = ""
        for b in league_bets:
            ec = edge_class(b["edge_pct"])
            rows += f"""<tr>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b['lam_home']:.2f}</td>
          <td style="color:#8b949e">{b['lam_away']:.2f}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_ou_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine Über/Unter Value Bets gefunden.</div>'
    headers = ["Spiel", "Tipp", "Anstoß", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "λ Heim", "λ Gast"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_bets in _group_by_league(bets, SPORT_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag">{league}</span> ({len(league_bets)})</h3>'
        rows = ""
        for b in league_bets:
            ec = edge_class(b["edge_pct"])
            rows += f"""<tr>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b['lam_home']:.2f}</td>
          <td style="color:#8b949e">{b['lam_away']:.2f}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_uefa_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine UEFA Value Bets gefunden.</div>'
    headers = ["Spiel", "Typ", "Tipp", "Anstoß",
               "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Modell", "Elo"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_bets in _group_by_league(bets, UEFA_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag3">{league}</span> ({len(league_bets)})</h3>'
        rows = ""
        for b in league_bets:
            ec = edge_class(b["edge_pct"])
            typ_label = "1X2" if b.get("type", "") == "1x2" else "O/U"
            elo_h = b.get("elo_home")
            elo_a = b.get("elo_away")
            elo_str = f"{int(elo_h)}/{int(elo_a)}" if elo_h and elo_a else "–"
            rows += f"""<tr>
          <td><strong>{b['match']}</strong></td>
          <td>{typ_label}</td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b.get("model_source", "")}</td>
          <td style="color:#8b949e">{elo_str}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_tennis_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine aktiven Tennis-Turniere mit ausreichend Odds gefunden.</div>'
    headers = ["Spiel", "Tipp", "Zeitpunkt", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Elo", "Modell"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for tournament, t_bets in _group_by_league(bets, None, key="tournament").items():
        html += f'<h3 class="league-header"><span class="tag2">{tournament}</span> ({len(t_bets)})</h3>'
        rows = ""
        for b in t_bets:
            ec        = edge_class(b["edge_pct"])
            elo       = str(b["elo"]) if b["elo"] else "–"
            is_konsens = b.get("model_source") == "Konsens"
            warn      = " ⚠️" if is_konsens else ""
            row_style = ' style="opacity:0.65"' if is_konsens else ""
            rows += f"""<tr{row_style}>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}{warn}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{elo}</td>
          <td style="color:#8b949e">{b['model_source']}{warn}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_backtesting_section() -> str:
    """Erzeugt HTML-Sektion mit Backtesting-Feedback."""
    try:
        summary = get_summary()
    except Exception:
        return '<div class="empty">Backtesting-Daten nicht verfügbar.</div>'

    ov = summary.get("overall", {})
    if not ov or not ov.get("total_bets"):
        return '<div class="empty">Noch keine abgeschlossenen Bets im Backtesting.</div>'

    rolling = summary.get("rolling", {})
    r7  = rolling.get("7d", {})
    r30 = rolling.get("30d", {})
    streak = rolling.get("streak", {})

    total = ov.get("total_bets", 0)
    won   = ov.get("won", 0)
    roi   = ov.get("roi_pct", 0) or 0
    pnl   = ov.get("total_pnl", 0) or 0
    hit_rate = f"{won/total*100:.1f}" if total > 0 else "0"

    r7_roi  = r7.get("roi_pct", 0) or 0
    r7_bets = r7.get("bets", 0) or 0
    r30_roi = r30.get("roi_pct", 0) or 0
    r30_bets = r30.get("bets", 0) or 0

    streak_str = f"{streak.get('count', 0)}× {streak.get('type', '—')}"

    roi_class = "g" if roi > 0 else ("o" if roi > -5 else "y")
    r7_class  = "g" if r7_roi > 0 else ("o" if r7_roi > -5 else "y")
    r30_class = "g" if r30_roi > 0 else ("o" if r30_roi > -5 else "y")

    html = f"""
<div class="summary">
  <div class="card"><div class="val {roi_class}">{roi:+.1f}%</div><div class="lbl">Gesamt-ROI ({total} Bets)</div></div>
  <div class="card"><div class="val {r7_class}">{r7_roi:+.1f}%</div><div class="lbl">7-Tage ROI ({r7_bets})</div></div>
  <div class="card"><div class="val {r30_class}">{r30_roi:+.1f}%</div><div class="lbl">30-Tage ROI ({r30_bets})</div></div>
  <div class="card"><div class="val">{hit_rate}%</div><div class="lbl">Trefferquote ({won}/{total})</div></div>
  <div class="card"><div class="val">{pnl:+.2f}</div><div class="lbl">PnL (Units)</div></div>
  <div class="card"><div class="val">{streak_str}</div><div class="lbl">Aktuelle Serie</div></div>
</div>"""

    # Modell-Breakdown
    by_model = summary.get("by_model", [])
    if by_model:
        html += '<table><tr><th>Modell</th><th>Bets</th><th>Won</th><th>PnL</th><th>ROI</th></tr>'
        for m in by_model:
            m_roi = m.get("roi_pct", 0) or 0
            mc = "g" if m_roi > 0 else "y"
            html += f'<tr><td>{m["model_source"]}</td><td>{m["bets"]}</td><td>{m["won"]}</td>'
            html += f'<td class="{mc}">{m["pnl"]:+.2f}</td><td class="{mc}">{m_roi:+.1f}%</td></tr>'
        html += '</table>'

    return html


def build_wettplan_section(selected_bets: list) -> str:
    """Erzeugt die Wettplan-Sektion mit Bankroll-Status und selektierten Bets."""
    if not selected_bets:
        return '<div class="empty">Kein Wettplan für heute — keine Bets selektiert.</div>'

    budget = get_daily_budget()
    dd = get_peak_and_drawdown()
    total_stake = sum(b.get("stake_eur", 0) for b in selected_bets)
    n_strong = sum(1 for b in selected_bets if b.get("tier") == "Strong Pick")
    n_value = sum(1 for b in selected_bets if b.get("tier") == "Value Bet")

    bankroll = budget["bankroll"]
    risk_pct = (total_stake / bankroll * 100) if bankroll > 0 else 0

    dd_class = "g" if dd["drawdown_pct"] < 5 else ("y" if dd["drawdown_pct"] < 15 else "o")

    html = f"""
<div class="summary">
  <div class="card"><div class="val" style="color:var(--green)">{bankroll:.2f} €</div><div class="lbl">Bankroll</div></div>
  <div class="card"><div class="val">{len(selected_bets)}</div><div class="lbl">Bets heute</div></div>
  <div class="card"><div class="val">{total_stake:.2f} €</div><div class="lbl">Tagesrisiko ({risk_pct:.1f}%)</div></div>
  <div class="card"><div class="val">{n_strong} / {n_value}</div><div class="lbl">Strong / Value</div></div>
  <div class="card"><div class="val {dd_class}">{dd['drawdown_pct']:.1f}%</div><div class="lbl">Drawdown (Peak: {dd['peak']:.0f} €)</div></div>
</div>

<table>
<tr>
  <th>Tier</th><th>Spiel</th><th>Tipp</th><th>Anstoß</th>
  <th>Score</th><th>Modell-%</th><th>Beste Quote</th>
  <th>Edge-%</th><th>Stake</th>
</tr>"""

    for b in selected_bets:
        tier = b.get("tier", "Value Bet")
        if tier == "Strong Pick":
            tier_badge = '<span style="background:var(--green);color:var(--bg);border-radius:4px;padding:2px 7px;font-size:0.75em;font-weight:600">Strong Pick</span>'
        else:
            tier_badge = '<span style="background:rgba(0,240,255,0.1);color:var(--cyan);border:1px solid var(--border);border-radius:4px;padding:2px 7px;font-size:0.75em">Value Bet</span>'

        ec = edge_class(b.get("edge_pct", 0))
        score = b.get("confidence_score", 0)
        score_color = "var(--green)" if score >= 70 else ("var(--cyan)" if score >= 45 else "var(--dim)")

        html += f"""<tr>
  <td>{tier_badge}</td>
  <td><strong>{b.get('match', '?')}</strong></td>
  <td>{b.get('tip', '?')}</td>
  <td>{format_dt(b.get('kick_off', ''))}</td>
  <td style="color:{score_color};font-weight:700">{score:.0f}</td>
  <td>{b.get('model_prob', 0)*100:.1f}%</td>
  <td>{b.get('best_odds', 0):.2f}</td>
  <td class="{ec}">{b.get('edge_pct', 0):.1f}%</td>
  <td style="color:var(--green);font-weight:700">{b.get('stake_eur', 0):.2f} €</td>
</tr>"""

    html += "</table>"
    return html


def generate_html(football_bets: list, ou_bets: list,
                  tennis_bets: list, uefa_bets: list,
                  selected_bets: list | None = None) -> str:
    date_str  = datetime.now().strftime("%d.%m.%Y")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    real_tennis_bets = [b for b in tennis_bets if b.get("model_source") != "Konsens"]
    total     = len(football_bets) + len(ou_bets) + len(real_tennis_bets) + len(uefa_bets)
    all_edges = [b["edge_pct"] for b in football_bets + ou_bets + tennis_bets + uefa_bets]
    max_edge  = max(all_edges) if all_edges else 0.0

    quota_str = f"API-Quota: {ODDS_API_REMAINING}" if ODDS_API_REMAINING is not None else "API-Quota: ?"

    if selected_bets is None:
        selected_bets = []

    wettplan_html = build_wettplan_section(selected_bets)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Sports Value Scanner {date_str}</title>
<style>{CSS}</style>
</head>
<body>
<h1>📊 Sports Value Scanner — {date_str}</h1>

<h2>🎯 Wettplan</h2>
{wettplan_html}

<div class="summary">
  <div class="card"><div class="val">{total}</div><div class="lbl">Value Bets gesamt</div></div>
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball 1X2</div></div>
  <div class="card"><div class="val">{len(ou_bets)}</div><div class="lbl">⚽ Über/Unter</div></div>
  <div class="card"><div class="val">{len(uefa_bets)}</div><div class="lbl">🏆 UEFA/Pokal</div></div>
  <div class="card"><div class="val">{len(real_tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
  <div class="card"><div class="val">{max_edge:.1f}%</div><div class="lbl">Max. Edge</div></div>
</div>

<div class="note">
  📌 <strong>Hinweis:</strong> Edge = (Modell-Wahrscheinlichkeit × Beste Quote) – 1.
  Nur Bets mit Edge ≥ {MIN_EDGE_PCT}% und ≤ {MAX_EDGE_PCT:.0f}% sowie Quote ≥ {MIN_ODDS} werden angezeigt.
  Quarter-Kelly Bankroll-Management aktiv.
</div>

<h2>📈 Backtesting-Performance</h2>
{build_backtesting_section()}

<details>
<summary style="cursor:pointer;color:var(--gold);font-size:1.1em;font-weight:600;margin:20px 0 10px">
  📋 Alle Signale ({total}) — zum Aufklappen klicken
</summary>

<h2>⚽ Fußball Value Bets (Poisson-Modell, Time-Decay)</h2>
{build_football_table(football_bets)}

<h2>⚽ Über/Unter Value Bets (Poisson-Modell)</h2>
{build_ou_table(ou_bets)}

<h2>🏆 UEFA / DFB-Pokal Value Bets</h2>
{build_uefa_table(uefa_bets)}

<h2>🎾 Tennis Value Bets (Elo-Modell, ATP + WTA, Surface-Bias)</h2>
{build_tennis_table(tennis_bets)}

</details>

<div class="footer">
  Generiert: {timestamp} &nbsp;|&nbsp;
  Fußball: Poisson MLE + Time-Decay (football-data.co.uk) &nbsp;|&nbsp;
  Tennis: Elo ATP+WTA + Surface-Bias (Jeff Sackmann) &nbsp;|&nbsp;
  Odds: The Odds API &nbsp;|&nbsp;
  UEFA/Pokal: Club-Elo + Poisson &nbsp;|&nbsp;
  {quota_str}<br>
  ⚠️ Diese Analyse dient ausschließlich zu Informationszwecken.
  Sportwetten sind mit erheblichen Verlustrisiken verbunden.
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sports Value Scanner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Keine externen API-Calls; erzeugt leeren Report für Smoke-Check.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scan_started_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_ref = f"sports-{scan_started_at.isoformat()}"
    print("=" * 60)
    print(f"  Sports Value Scanner — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("=" * 60)

    creds = load_creds()
    api_key = creds.get("ODDS_API_KEY", "")
    if not args.dry_run and not api_key:
        print("ERROR: ODDS_API_KEY fehlt in ~/.stock_scanner_credentials")
        return 1

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_dir  = OUTPUT_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── BACKTESTING ──────────────────────────────────────────────────────────
    try:
        _git_hash = subprocess.check_output(
            ["git", "-C", str(SCRIPT_DIR), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        _git_hash = "unknown"
    init_db()
    _n_training: int = 0
    _run_id: int | None = None

    all_football_bets: list = []
    all_ou_bets:       list = []
    all_tennis_bets:   list = []
    all_uefa_bets:     list = []
    all_sports = None  # Wird bei Bedarf von get_active_sports() befüllt
    loaded_matches: dict = {}  # sport_key → [match, …] für Kicktipp-Wiederverwendung

    if args.dry_run:
        print("\n[DRY-RUN] Keine externen API-Calls. Erzeuge leeren Report …")
    else:
        # ── FUSSBALL ────────────────────────────────────────────────────────
        print("\n[⚽ Fußball] Daten laden & Modelle trainieren …")
        football_models = {}

        for sport_key in FOOTBALL_SPORTS:
            label = SPORT_LABELS.get(sport_key, sport_key)
            print(f"  {label}:")
            df = load_football_data(sport_key)
            if df is None or len(df) < 20:
                print(f"    Nicht genug Daten ({len(df) if df is not None else 0} Matches) – übersprungen")
                continue
            n_teams = df["HomeTeam"].nunique()
            print(f"    {len(df)} Matches, {n_teams} Teams → trainiere Poisson …")
            try:
                model = fit_poisson_model(df)
                football_models[sport_key] = model
                _n_training += len(df)
                print(f"    OK. Home-Vorteil={model['home_adv']:.3f}")
            except Exception as e:
                print(f"    Modell-Fehler: {e}")

        _run_id = log_scan_run(
            scanned_at=scan_started_at.isoformat(),
            model_version=_git_hash,
            training_matches=_n_training,
        )

        print("\n[⚽ Fußball] Upcoming Matches via Odds API …")
        for sport_key in FOOTBALL_SPORTS:
            label = SPORT_LABELS.get(sport_key, sport_key)
            print(f"  {label}:")
            try:
                matches = get_odds(api_key, sport_key)
            except Exception as e:
                print(f"    Fehler: {e}")
                continue
            loaded_matches[sport_key] = matches  # für Kicktipp wiederverwenden
            if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
                print(f"    Hinweis: API-Quota sehr niedrig ({ODDS_API_REMAINING}) – stoppe weitere Odds-Calls.")
                break

            model = football_models.get(sport_key)
            if model is None:
                print(f"    Kein Modell – Odds werden ignoriert")
                continue

            for match in matches:
                bets = analyze_football_match(match, model)
                if bets:
                    enrich_bets_with_market_data(bets, match)
                    all_football_bets.extend(bets)
                    for b in bets:
                        b["_pred_id"] = log_prediction(_run_id, b, match_raw=match)
                        print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
                ou_bets_match = analyze_football_ou(match, model)
                if ou_bets_match:
                    enrich_bets_with_market_data(ou_bets_match, match)
                    all_ou_bets.extend(ou_bets_match)
                    for b in ou_bets_match:
                        b["_pred_id"] = log_prediction(_run_id, b, match_raw=match)
                        print(f"    ✓ O/U VALUE: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

        # ── TENNIS ──────────────────────────────────────────────────────────
        print("\n[🎾 Tennis] ATP + WTA Elo-Ratings parallel berechnen …")
        elo_years = get_elo_years()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            atp_future = pool.submit(compute_tennis_elo, elo_years)
            wta_future = pool.submit(compute_wta_elo, elo_years)
            atp_elo_dict, atp_surface_elo, atp_training_matches = atp_future.result()
            wta_elo_dict, wta_surface_elo, wta_training_matches = wta_future.result()
        print(f"  ATP: {len(atp_elo_dict)} Spieler im Elo-Dict")
        for surf, sdict in atp_surface_elo.items():
            print(f"    {surf}: {len(sdict)} Spieler")
        print(f"  WTA: {len(wta_elo_dict)} Spielerinnen im Elo-Dict")
        for surf, sdict in wta_surface_elo.items():
            print(f"    {surf}: {len(sdict)} Spielerinnen")

        # Kombiniertes Dict für Lookup (ATP + WTA)
        combined_elo = {**atp_elo_dict, **wta_elo_dict}
        combined_tennis_training = atp_training_matches + wta_training_matches
        combined_surface_elo = {}
        for surf in ["Hard", "Clay", "Grass"]:
            combined_surface_elo[surf] = {**atp_surface_elo.get(surf, {}),
                                          **wta_surface_elo.get(surf, {})}

        print("\n[🎾 Tennis] Aktive Turniere suchen …")
        try:
            all_sports   = get_active_sports(api_key)
            tennis_sports = [s for s in all_sports
                             if s["key"].startswith("tennis_") and s["active"]]
            print(f"  {len(tennis_sports)} aktive Tennis-Turniere:")
            for s in tennis_sports:
                print(f"    - {s['key']} ({s['title']})")
        except Exception as e:
            print(f"  Fehler: {e}")
            tennis_sports = []

        for sport in tennis_sports:
            sport_key = sport["key"]
            title     = sport["title"]
            print(f"  {title}:")
            try:
                matches = get_odds(api_key, sport_key, markets="h2h")
            except Exception as e:
                print(f"    Fehler: {e}")
                continue
            if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
                print(f"    Hinweis: API-Quota sehr niedrig ({ODDS_API_REMAINING}) – stoppe weitere Odds-Calls.")
                break
            for match in matches:
                bets = analyze_tennis_match(
                    match,
                    title,
                    combined_elo,
                    combined_surface_elo,
                    training_matches=combined_tennis_training,
                )
                if bets:
                    enrich_bets_with_market_data(bets, match)
                    all_tennis_bets.extend(bets)
                    for b in bets:
                        b["_pred_id"] = log_prediction(_run_id, b, match_raw=match)
                        print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

        # ── UEFA ────────────────────────────────────────────────────────────
        print("\n[🏆 UEFA] Club-Elo-Ratings laden …")
        elo_date, club_elo_dict = download_clubelo_with_fallback(max_days_back=3)
        if elo_date:
            print(f"  {len(club_elo_dict)} Clubs im Elo-Dict (Datum: {elo_date})")
        else:
            print("  Warning: Keine Club-Elo-Daten gefunden (letzte 3 Tage)")

        print("\n[🏆 UEFA] Europäisches Poisson-Modell trainieren …")
        euro_df = load_european_data()
        euro_model = None
        if euro_df is not None and len(euro_df) >= 20:
            n_teams = euro_df["HomeTeam"].nunique()
            print(f"  {len(euro_df)} Matches, {n_teams} Teams → trainiere Poisson …")
            try:
                euro_model = fit_poisson_model(euro_df)
                print(f"  OK. Home-Vorteil={euro_model['home_adv']:.3f}")
            except Exception as e:
                print(f"  Modell-Fehler: {e}")
        else:
            print("  Nicht genug Daten für europäisches Modell")

        print("\n[🏆 UEFA] Matches via Odds API …")
        for sport_key in UEFA_SPORTS:
            label = UEFA_LABELS.get(sport_key, sport_key)
            print(f"  {label}:")
            try:
                matches = get_odds(api_key, sport_key)
            except Exception as e:
                print(f"    Fehler: {e}")
                continue
            loaded_matches[sport_key] = matches  # für Kicktipp wiederverwenden
            if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
                print(f"    Hinweis: API-Quota sehr niedrig ({ODDS_API_REMAINING}) – stoppe weitere Odds-Calls.")
                break
            for match in matches:
                bets = analyze_uefa_match(match, club_elo_dict, euro_model)
                if bets:
                    enrich_bets_with_market_data(bets, match)
                    all_uefa_bets.extend(bets)
                    for b in bets:
                        b["_pred_id"] = log_prediction(_run_id, b, match_raw=match)
                        typ = b.get("type", "").upper()
                        print(f"    ✓ UEFA VALUE [{typ}]: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

        # ── DFB-POKAL (konditionell) ──────────────────────────────────────
        dfb_key = "soccer_germany_dfb_pokal"
        try:
            if all_sports is None:
                all_sports = get_active_sports(api_key)
            dfb_active = any(s["key"] == dfb_key and s["active"]
                             for s in all_sports)
        except Exception:
            dfb_active = False

        if dfb_active:
            print("\n[🏆 DFB-Pokal] Matches via Odds API …")
            try:
                matches = get_odds(api_key, dfb_key)
                for match in matches:
                    bets = analyze_uefa_match(match, club_elo_dict, euro_model)
                    if bets:
                        # Tag als DFB-Pokal in den Bets setzen
                        for b in bets:
                            b["sport"] = dfb_key
                        enrich_bets_with_market_data(bets, match)
                        all_uefa_bets.extend(bets)
                        for b in bets:
                            b["_pred_id"] = log_prediction(_run_id, b, match_raw=match)
                            typ = b.get("type", "").upper()
                            print(f"    ✓ DFB-Pokal [{typ}]: {b['match']} → {b['tip']} "
                                  f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
            except Exception as e:
                print(f"    Fehler: {e}")
        else:
            print("\n[🏆 DFB-Pokal] Keine aktive Runde – übersprungen")

    # ── KICKTIPP ────────────────────────────────────────────────────────────
    kicktipp_matches = []
    if not args.dry_run:
        print("\n[🎯 Kicktipp] Tipps sammeln (nutzt bereits geladene Matches) …")
        kicktipp_matches = collect_kicktipp_predictions(
            football_models, club_elo_dict, euro_model, loaded_matches
        )
        print(f"  → {len(kicktipp_matches)} Kicktipp-Spiele gesammelt")

        if kicktipp_matches:
            kt_html = generate_kicktipp_html(kicktipp_matches)
            kt_html_path = out_dir / "kicktipp_report.html"
            kt_html_path.write_text(kt_html, encoding="utf-8")
            print(f"  HTML: {kt_html_path}")

            # JSON für Dashboard
            kt_json = []
            for m in kicktipp_matches:
                kt_json.append({
                    "league":     m["league"],
                    "home":       m["home_team"],
                    "away":       m["away_team"],
                    "kick_off":   m["kick_off"],
                    "p_home":     round(m["p_home"], 4) if m["p_home"] else None,
                    "p_draw":     round(m["p_draw"], 4) if m["p_draw"] else None,
                    "p_away":     round(m["p_away"], 4) if m["p_away"] else None,
                    "score_home": m["score_home"],
                    "score_away": m["score_away"],
                    "tendency":   m["tendency"],
                    "model_source": m.get("model_source"),
                })
            kt_json_path = out_dir / "kicktipp_data.json"
            kt_json_path.write_text(json.dumps(kt_json, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            print(f"  JSON: {kt_json_path}")

    # ── BANKROLL & BET-SELEKTION ─────────────────────────────────────────────
    all_bets_combined = all_football_bets + all_ou_bets + all_tennis_bets + all_uefa_bets

    selected_bets = []
    watch_bets = []

    if all_bets_combined and not args.dry_run:
        print(f"\n[🎯 Wettplan] {len(all_bets_combined)} Bets bewerten & selektieren …")
        init_bankroll()
        selected_bets, watch_bets = select_bets(all_bets_combined)

        # DB-Predictions mit Selektions-Daten aktualisieren
        for b in selected_bets + watch_bets:
            pred_id = b.get("_pred_id")
            if pred_id:
                update_prediction_selection(
                    pred_id,
                    confidence_score=b.get("confidence_score", 0),
                    tier=b.get("tier", "Watch"),
                    stake_eur=b.get("stake_eur", 0),
                    selected=b.get("selected", 0),
                )

    # ── REPORT ──────────────────────────────────────────────────────────────
    print(f"\n[📊 Report] Football Bets: {len(all_football_bets)}")
    print(f"[📊 Report] O/U Bets:      {len(all_ou_bets)}")
    print(f"[📊 Report] Tennis Bets:   {len(all_tennis_bets)}")
    print(f"[📊 Report] UEFA Bets:     {len(all_uefa_bets)}")
    print(f"[📊 Report] Wettplan:      {len(selected_bets)} selektiert")

    html      = generate_html(all_football_bets, all_ou_bets, all_tennis_bets,
                              all_uefa_bets, selected_bets)
    html_path = out_dir / "sports_signals.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"[📊 Report] HTML: {html_path}")

    # CSV
    rows = []
    for b in all_football_bets:
        rows.append({
            "Typ":        "Fußball",
            "Liga":       SPORT_LABELS.get(b["sport"], b["sport"]),
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "Score":      f"{b.get('confidence_score', 0):.0f}",
            "Tier":       b.get("tier", ""),
            "Stake":      f"{b.get('stake_eur', 0):.2f}",
            "λ-Heim":     f"{b['lam_home']:.2f}",
            "λ-Gast":     f"{b['lam_away']:.2f}",
        })
    for b in all_ou_bets:
        rows.append({
            "Typ":        "Fußball O/U",
            "Liga":       SPORT_LABELS.get(b["sport"], b["sport"]),
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "Score":      f"{b.get('confidence_score', 0):.0f}",
            "Tier":       b.get("tier", ""),
            "Stake":      f"{b.get('stake_eur', 0):.2f}",
            "λ-Heim":     f"{b['lam_home']:.2f}",
            "λ-Gast":     f"{b['lam_away']:.2f}",
        })
    for b in all_uefa_bets:
        row = {
            "Typ":        f"UEFA {b.get('type', '').upper()}",
            "Liga":       UEFA_LABELS.get(b["sport"], b["sport"]),
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "Score":      f"{b.get('confidence_score', 0):.0f}",
            "Tier":       b.get("tier", ""),
            "Stake":      f"{b.get('stake_eur', 0):.2f}",
            "Modell":     b.get("model_source", ""),
        }
        if b.get("type", "") == "ou":
            row["λ-Heim"] = f"{b.get('lam_home', 0):.2f}"
            row["λ-Gast"] = f"{b.get('lam_away', 0):.2f}"
        rows.append(row)
    for b in all_tennis_bets:
        rows.append({
            "Typ":        "Tennis",
            "Liga":       b["tournament"],
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "Score":      f"{b.get('confidence_score', 0):.0f}",
            "Tier":       b.get("tier", ""),
            "Stake":      f"{b.get('stake_eur', 0):.2f}",
            "Elo":        str(b["elo"]) if b["elo"] else "",
        })

    if rows:
        csv_path = out_dir / "sports_signals.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        print(f"[📊 Report] CSV:  {csv_path}")

    if not args.dry_run:
        hub_signals = [
            _normalize_hub_signal(bet, run_ref)
            for bet in sorted(selected_bets + watch_bets, key=lambda b: b.get("confidence_score", 0), reverse=True)
        ]
        run_payload = {
            "run_id": run_ref,
            "system": "sports-scanner",
            "generated_at": scan_started_at.isoformat(),
            "status": "ok",
            "summary": {
                "total_candidates": len(all_bets_combined),
                "selected_count": len(selected_bets),
                "watch_count": len(watch_bets),
                "warnings_count": sum(1 for bet in selected_bets + watch_bets if bet.get("market_gap_flag")),
            },
        }
        _write_hub_exports(run_payload, hub_signals)
        print(f"[📊 Hub] JSON: {HUB_DIR / 'latest_runs.json'}")
        print(f"[📊 Hub] JSON: {HUB_DIR / 'latest_signals.json'}")

    if _run_id is not None:
        resolve_results()

    # ── BANKROLL SNAPSHOT ─────────────────────────────────────────────────
    if not args.dry_run:
        rebuild_all_snapshots()
        record_daily_snapshot(date_str)

    # ── TELEGRAM ALERTS ───────────────────────────────────────────────────
    if all_bets_combined:
        print("\n[📱 Telegram] High-Edge Alerts …")
        send_high_edge_alerts(all_bets_combined, min_edge=10.0)

    print("\n✓ Fertig!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
