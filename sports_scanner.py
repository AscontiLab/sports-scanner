#!/usr/bin/env python3
"""
Sports Betting Value Scanner — Orchestrierung
──────────────────────────────────────────────
Koordiniert Datenquellen, Modelle, Analyse, Report und Alerts.

Module:
  - datasources/  → fdco, openligadb, clubelo, sackmann, odds_api
  - models/       → poisson, elo, value, matching
  - reports/      → html_generator
"""

import sys
import argparse
import json
import warnings
import subprocess
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

from config import (
    SCRIPT_DIR, OUTPUT_DIR,
    FOOTBALL_SPORTS, SPORT_LABELS,
    MIN_EDGE_PCT, MAX_EDGE_PCT, MIN_ODDS, MAX_KELLY,
    UEFA_SPORTS, UEFA_LABELS,
    INTERNATIONAL_SPORTS, INTERNATIONAL_LABELS,
    MIN_ODDS_API_REMAINING,
    KICKTIPP_FOOTBALL_SPORTS, KICKTIPP_UEFA_SPORTS,
    KICKTIPP_INTERNATIONAL_SPORTS, KICKTIPP_LABELS,
    TENNIS_ENABLED,
    load_credentials,
)

# Datenquellen
from datasources.fdco import (
    load_football_data, load_european_data,
    current_season_codes,
)
from datasources.clubelo import download_clubelo_with_fallback
from datasources.sackmann import detect_surface
from datasources.odds_api import (
    get_active_sports, get_odds,
    best_odds_from_match, best_ou_odds_from_match,
    bookie_consensus, enrich_bets_with_market_data,
)

# Modelle
from models.poisson import (
    fit_poisson_model, predict_football, predict_ou,
    predict_btts, predict_most_likely_score,
    elo_to_football_1x2,
)
from models.elo import (
    compute_tennis_elo, compute_wta_elo,
    predict_tennis_win_prob, find_player_elo,
    get_elo_years,
)
from models.value import compute_value
from models.matching import find_team_in_model, find_club_elo

# Reports
from reports.html_generator import (
    generate_html, generate_kicktipp_html,
    format_dt, CSS,
)

# Backtesting & Bankroll
from model_cache import get_or_train_model
from backtesting import (
    init_db, log_scan_run, log_prediction, resolve_results,
    update_prediction_selection, update_scan_run_training,
    reset_selection_for_date,
)
from alerts import send_high_edge_alerts, send_tuning_alert
from bankroll_manager import (
    init_bankroll, record_daily_snapshot,
    rebuild_all_snapshots,
)
from bet_selector import select_bets

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Mutable global — nicht in config.py
ODDS_API_REMAINING: int | None = None
HUB_DIR = SCRIPT_DIR.parent / "hub"


# ═══════════════════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN (Hub-Export, Signal-ID, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

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


def _upsert_hub_records(path: Path, records: list[dict],
                        key_field: str, system_name: str) -> None:
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


def _write_hub_exports(run_payload: dict, signals: list[dict],
                       hub_dir: Path = HUB_DIR) -> None:
    hub_dir.mkdir(parents=True, exist_ok=True)
    _upsert_hub_records(hub_dir / "latest_runs.json", [run_payload], "run_id", "sports-scanner")
    _upsert_hub_records(hub_dir / "latest_signals.json", signals, "signal_id", "sports-scanner")


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYSE-FUNKTIONEN
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

    best = best_odds_from_match(match)
    bets = []
    labels = {"home": home_api, "draw": "Unentschieden", "away": away_api}

    for outcome in ("home", "draw", "away"):
        odds = best[outcome]
        model_p = probs[outcome]
        if odds < MIN_ODDS:
            continue
        edge, kelly = compute_value(model_p, odds)
        if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
            bets.append({
                "type": "football", "sport": match.get("sport_key", ""),
                "match": f"{home_api} – {away_api}", "tip": labels[outcome],
                "kick_off": match["commence_time"], "model_prob": model_p,
                "best_odds": odds, "edge_pct": edge * 100,
                "kelly_pct": min(kelly, MAX_KELLY) * 100,
                "lam_home": probs["lam_home"], "lam_away": probs["lam_away"],
                "home_model": home_model, "away_model": away_model,
                "training_matches": model.get("training_matches"),
            })
    return bets


def analyze_football_ou(match: dict, model: dict) -> list:
    """Value Bets fuer Ueber/Unter-Maerkte via Poisson-Modell."""
    home_api = match["home_team"]
    away_api = match["away_team"]
    home_model = find_team_in_model(home_api, model["teams"])
    away_model = find_team_in_model(away_api, model["teams"])
    if not home_model or not away_model:
        return []

    probs = predict_football(home_model, away_model, model)
    if not probs:
        return []

    lam_home = probs["lam_home"]
    lam_away = probs["lam_away"]
    ou_lines = best_ou_odds_from_match(match)
    bets = []

    for entry in ou_lines:
        line = entry["line"]
        if abs(line - round(line)) < 1e-9:
            continue
        p_over, p_under = predict_ou(lam_home, lam_away, line)
        for side, model_p, odds in [
            ("Über", p_over, entry["over_odds"]),
            ("Unter", p_under, entry["under_odds"]),
        ]:
            if odds < MIN_ODDS:
                continue
            edge, kelly = compute_value(model_p, odds)
            if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                bets.append({
                    "type": "football_ou", "sport": match.get("sport_key", ""),
                    "match": f"{home_api} – {away_api}", "line": line,
                    "tip": f"{side} {line}", "kick_off": match["commence_time"],
                    "model_prob": model_p, "best_odds": odds,
                    "edge_pct": edge * 100, "kelly_pct": min(kelly, MAX_KELLY) * 100,
                    "lam_home": lam_home, "lam_away": lam_away,
                    "training_matches": model.get("training_matches"),
                })
    return bets


def analyze_football_btts(match: dict, model: dict) -> list[dict]:
    home_api = match["home_team"]
    away_api = match["away_team"]
    home_model = find_team_in_model(home_api, model["teams"])
    away_model = find_team_in_model(away_api, model["teams"])
    if not home_model or not away_model:
        return []
    probs = predict_football(home_model, away_model, model)
    if not probs:
        return []
    lam_home = probs["lam_home"]
    lam_away = probs["lam_away"]
    p_btts = predict_btts(lam_home, lam_away)
    return [{
        "match": f"{home_api} – {away_api}",
        "kick_off": match["commence_time"],
        "sport_key": match.get("sport_key", ""),
        "p_btts_yes": round(p_btts * 100, 1),
        "p_btts_no": round((1.0 - p_btts) * 100, 1),
        "lam_home": round(lam_home, 2),
        "lam_away": round(lam_away, 2),
        "signal": "Ja" if p_btts >= 0.55 else "Nein",
    }]


def analyze_uefa_match(match: dict, elo_dict: dict,
                       euro_model: dict | None) -> list:
    home_api = match["home_team"]
    away_api = match["away_team"]
    best = best_odds_from_match(match)
    bets = []

    # 1X2 via Club-Elo
    elo_home = find_club_elo(home_api, elo_dict)
    elo_away = find_club_elo(away_api, elo_dict)
    if elo_home is not None and elo_away is not None:
        p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
        labels = {"home": home_api, "draw": "Unentschieden", "away": away_api}
        probs = {"home": p_home, "draw": p_draw, "away": p_away}
        for outcome in ("home", "draw", "away"):
            odds = best[outcome]
            model_p = probs[outcome]
            if odds < MIN_ODDS:
                continue
            edge, kelly = compute_value(model_p, odds)
            if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                bets.append({
                    "type": "1x2", "sport": match.get("sport_key", ""),
                    "match": f"{home_api} – {away_api}", "tip": labels[outcome],
                    "kick_off": match["commence_time"], "model_prob": model_p,
                    "best_odds": odds, "edge_pct": edge * 100,
                    "kelly_pct": min(kelly, MAX_KELLY) * 100,
                    "model_source": "ClubElo",
                    "elo_home": elo_home, "elo_away": elo_away,
                    "training_matches": len(elo_dict),
                })

    # O/U via Poisson
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
                        continue
                    p_over, p_under = predict_ou(lam_home, lam_away, line)
                    for side, model_p, odds in [
                        ("Über", p_over, entry["over_odds"]),
                        ("Unter", p_under, entry["under_odds"]),
                    ]:
                        if odds < MIN_ODDS:
                            continue
                        edge, kelly = compute_value(model_p, odds)
                        if MIN_EDGE_PCT / 100 <= edge <= MAX_EDGE_PCT / 100:
                            bets.append({
                                "type": "ou", "sport": match.get("sport_key", ""),
                                "match": f"{home_api} – {away_api}",
                                "tip": f"{side} {line}",
                                "kick_off": match["commence_time"],
                                "model_prob": model_p, "best_odds": odds,
                                "edge_pct": edge * 100,
                                "kelly_pct": min(kelly, MAX_KELLY) * 100,
                                "model_source": "Poisson",
                                "lam_home": lam_home, "lam_away": lam_away,
                                "training_matches": euro_model.get("training_matches"),
                            })
    return bets


def analyze_tennis_match(match: dict, tournament: str, elo_dict: dict,
                         surface_elo: dict | None = None,
                         training_matches: int = 0,
                         elo_blend_weight: float = 1.0) -> list:
    p1 = match["home_team"]
    p2 = match["away_team"]

    surface = detect_surface(tournament) if surface_elo else None
    s_elo = surface_elo.get(surface, {}) if surface and surface_elo else {}

    elo1_s = find_player_elo(p1, s_elo) if s_elo else None
    elo2_s = find_player_elo(p2, s_elo) if s_elo else None
    elo1_g = find_player_elo(p1, elo_dict)
    elo2_g = find_player_elo(p2, elo_dict)

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

    # Bookie-Konsens immer holen (fuer Blend oder Fallback)
    consensus = bookie_consensus(match)
    cons_p1 = consensus.get("home")
    cons_p2 = consensus.get("away")

    if elo1 is not None and elo2 is not None:
        elo_prob1 = predict_tennis_win_prob(elo1, elo2)
        elo_prob2 = 1 - elo_prob1
        # Bei veralteten Elo-Daten mit Konsens blenden
        if elo_blend_weight < 1.0 and cons_p1 and cons_p2:
            w = elo_blend_weight
            prob1 = w * elo_prob1 + (1 - w) * cons_p1
            prob2 = w * elo_prob2 + (1 - w) * cons_p2
            model_source = f"{model_source}+Konsens ({w:.0%}/{1-w:.0%})"
        else:
            prob1, prob2 = elo_prob1, elo_prob2
    else:
        prob1 = cons_p1
        prob2 = cons_p2
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
                "type": "tennis", "sport": match.get("sport_key", ""),
                "tournament": tournament,
                "match": f"{p1} – {p2}", "tip": player,
                "kick_off": match["commence_time"],
                "model_prob": model_p, "best_odds": odds,
                "edge_pct": edge * 100,
                "kelly_pct": min(kelly, MAX_KELLY) * 100,
                "elo": round(elo_val) if elo_val else None,
                "model_source": model_source,
                "training_matches": training_matches,
            })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# KICKTIPP
# ═══════════════════════════════════════════════════════════════════════════════

def collect_kicktipp_predictions(football_models: dict, club_elo_dict: dict,
                                  euro_model: dict | None,
                                  loaded_matches: dict) -> list:
    from reports.html_generator import _determine_tendency_and_score
    results = []

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
            lam_h = lam_a = None
            model_src = None

            if model:
                home_model = find_team_in_model(home_api, model["teams"])
                away_model = find_team_in_model(away_api, model["teams"])
                if home_model and away_model:
                    probs = predict_football(home_model, away_model, model)
                    if probs:
                        p_home, p_draw, p_away = probs["home"], probs["draw"], probs["away"]
                        lam_h, lam_a = probs["lam_home"], probs["lam_away"]
                        model_src = "Poisson"

            if p_home is None and club_elo_dict:
                elo_home = find_club_elo(home_api, club_elo_dict)
                elo_away = find_club_elo(away_api, club_elo_dict)
                if elo_home is not None and elo_away is not None:
                    p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "ClubElo"

            if p_home is None:
                consensus = bookie_consensus(match)
                if consensus and consensus.get("home"):
                    p_home, p_draw, p_away = consensus["home"], consensus["draw"], consensus["away"]
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "Konsens"

            tendency, score_home, score_away = _determine_tendency_and_score(
                p_home, p_draw, p_away, lam_h, lam_a
            )
            results.append({
                "league": label, "sport_key": sport_key,
                "match": f"{home_api} – {away_api}",
                "home_team": home_api, "away_team": away_api,
                "kick_off": kick_off,
                "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
                "tendency": tendency,
                "score_home": score_home, "score_away": score_away,
                "model_source": model_src,
            })

    for sport_key in KICKTIPP_UEFA_SPORTS:
        label = KICKTIPP_LABELS.get(sport_key, sport_key)
        matches = loaded_matches.get(sport_key, [])
        print(f"  [Kicktipp] {label}: {len(matches)} Spiele …")

        for match in matches:
            home_api = match["home_team"]
            away_api = match["away_team"]
            kick_off = match.get("commence_time", "")
            p_home = p_draw = p_away = None
            lam_h = lam_a = None
            model_src = None

            if club_elo_dict:
                elo_home = find_club_elo(home_api, club_elo_dict)
                elo_away = find_club_elo(away_api, club_elo_dict)
                if elo_home is not None and elo_away is not None:
                    p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "ClubElo"

            if euro_model:
                home_model = find_team_in_model(home_api, euro_model["teams"])
                away_model = find_team_in_model(away_api, euro_model["teams"])
                if home_model and away_model:
                    probs_eu = predict_football(home_model, away_model, euro_model)
                    if probs_eu:
                        lam_h, lam_a = probs_eu["lam_home"], probs_eu["lam_away"]
                        if model_src is None:
                            p_home, p_draw, p_away = probs_eu["home"], probs_eu["draw"], probs_eu["away"]
                            model_src = "Poisson-EU"

            if p_home is None:
                consensus = bookie_consensus(match)
                if consensus and consensus.get("home"):
                    p_home, p_draw, p_away = consensus["home"], consensus["draw"], consensus["away"]
                    lam_h = 1.4 + 0.3 * (p_home - 0.33)
                    lam_a = 1.4 + 0.3 * (p_away - 0.33)
                    model_src = "Konsens"

            tendency, score_home, score_away = _determine_tendency_and_score(
                p_home, p_draw, p_away, lam_h, lam_a
            )
            results.append({
                "league": label, "sport_key": sport_key,
                "match": f"{home_api} – {away_api}",
                "home_team": home_api, "away_team": away_api,
                "kick_off": kick_off,
                "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
                "tendency": tendency,
                "score_home": score_home, "score_away": score_away,
                "model_source": model_src,
            })

    for sport_key in KICKTIPP_INTERNATIONAL_SPORTS:
        label = KICKTIPP_LABELS.get(sport_key, sport_key)
        matches = loaded_matches.get(sport_key, [])
        if not matches:
            continue
        print(f"  [Kicktipp] {label}: {len(matches)} Spiele …")

        for match in matches:
            home_api = match["home_team"]
            away_api = match["away_team"]
            kick_off = match.get("commence_time", "")
            p_home = p_draw = p_away = None
            lam_h = lam_a = None
            model_src = None

            consensus = bookie_consensus(match)
            if consensus and consensus.get("home"):
                p_home, p_draw, p_away = consensus["home"], consensus["draw"], consensus["away"]
                lam_h = 1.4 + 0.3 * (p_home - 0.33)
                lam_a = 1.4 + 0.3 * (p_away - 0.33)
                model_src = "Konsens"

            tendency, score_home, score_away = _determine_tendency_and_score(
                p_home, p_draw, p_away, lam_h, lam_a
            )
            results.append({
                "league": label, "sport_key": sport_key,
                "match": f"{home_api} – {away_api}",
                "home_team": home_api, "away_team": away_api,
                "kick_off": kick_off,
                "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
                "tendency": tendency,
                "score_home": score_home, "score_away": score_away,
                "model_source": model_src,
            })

    results.sort(key=lambda x: x["kick_off"])
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN-RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sports Value Scanner")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Keine externen API-Calls; erzeugt leeren Report fuer Smoke-Check.",
    )
    return parser.parse_args()


def run_football_scan(api_key: str, run_id: int, loaded_matches: dict) -> tuple[list, list, list, dict, int]:
    """Fussball-Ligen scannen. Gibt (football_bets, ou_bets, btts_signals, football_models, n_training) zurueck."""
    global ODDS_API_REMAINING
    all_football_bets = []
    all_ou_bets = []
    all_btts_signals = []
    n_training = 0

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
            model = get_or_train_model(sport_key, df, fit_poisson_model)
            football_models[sport_key] = model
            n_training += len(df)
            print(f"    OK. Home-Vorteil={model['home_adv']:.3f}")
        except Exception as e:
            print(f"    Modell-Fehler: {e}")

    print("\n[⚽ Fußball] Upcoming Matches via Odds API …")
    for sport_key in FOOTBALL_SPORTS:
        label = SPORT_LABELS.get(sport_key, sport_key)
        print(f"  {label}:")
        try:
            matches, remaining = get_odds(api_key, sport_key)
        except Exception as e:
            print(f"    Fehler: {e}")
            continue
        ODDS_API_REMAINING = remaining
        loaded_matches[sport_key] = matches
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
                    b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                    print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
            ou_bets_match = analyze_football_ou(match, model)
            if ou_bets_match:
                enrich_bets_with_market_data(ou_bets_match, match)
                all_ou_bets.extend(ou_bets_match)
                for b in ou_bets_match:
                    b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                    print(f"    ✓ O/U VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
            btts_sigs = analyze_football_btts(match, model)
            all_btts_signals.extend(btts_sigs)

    return all_football_bets, all_ou_bets, all_btts_signals, football_models, n_training


def run_tennis_scan(api_key: str, run_id: int) -> tuple[list, list | None]:
    """ATP/WTA Tennis scannen. Gibt (tennis_bets, all_sports) zurueck."""
    global ODDS_API_REMAINING
    all_tennis_bets = []
    all_sports = None

    if not TENNIS_ENABLED:
        print("\n[🎾 Tennis] Deaktiviert (TENNIS_ENABLED=False)")
        return all_tennis_bets, all_sports

    print("\n[🎾 Tennis] ATP + WTA Elo-Ratings parallel berechnen …")
    elo_years = get_elo_years()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        atp_future = pool.submit(compute_tennis_elo, elo_years)
        wta_future = pool.submit(compute_wta_elo, elo_years)
        atp_elo_dict, atp_surface_elo, atp_training_matches, atp_max_date = atp_future.result()
        wta_elo_dict, wta_surface_elo, wta_training_matches, wta_max_date = wta_future.result()
    print(f"  ATP: {len(atp_elo_dict)} Spieler im Elo-Dict")
    for surf, sdict in atp_surface_elo.items():
        print(f"    {surf}: {len(sdict)} Spieler")
    print(f"  WTA: {len(wta_elo_dict)} Spielerinnen im Elo-Dict")
    for surf, sdict in wta_surface_elo.items():
        print(f"    {surf}: {len(sdict)} Spielerinnen")

    combined_elo = {**atp_elo_dict, **wta_elo_dict}
    combined_tennis_training = atp_training_matches + wta_training_matches
    combined_surface_elo = {}
    for surf in ["Hard", "Clay", "Grass"]:
        combined_surface_elo[surf] = {**atp_surface_elo.get(surf, {}),
                                      **wta_surface_elo.get(surf, {})}

    # Elo-Daten-Alter pruefen und bei veralteten Daten Richtung Default daempfen
    from datetime import datetime as _dt
    newest_date = max(filter(None, [atp_max_date, wta_max_date]), default=None)
    elo_stale_days = (_dt.now() - newest_date).days if newest_date else 999
    if elo_stale_days > 180:
        print(f"  ⚠ Elo-Daten {elo_stale_days} Tage alt — Blend mit Bookie-Konsens aktiv")
    elo_blend_weight = max(0.2, min(1.0, 1.0 - (elo_stale_days - 90) / 540))
    # 0-90 Tage: 100% Elo, 90-630 Tage: linear bis 20%, nie unter 20%
    # Elo behaelt immer min. 20% Gewicht (langfristige Spielerstaerke bleibt relevant)
    print(f"  Elo-Gewicht: {elo_blend_weight:.0%} (Daten {elo_stale_days}d alt)")

    print("\n[🎾 Tennis] Aktive Turniere suchen …")
    try:
        all_sports = get_active_sports(api_key)
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
        title = sport["title"]
        print(f"  {title}:")
        try:
            matches, remaining = get_odds(api_key, sport_key, markets="h2h")
        except Exception as e:
            print(f"    Fehler: {e}")
            continue
        ODDS_API_REMAINING = remaining
        if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
            print(f"    Hinweis: API-Quota sehr niedrig ({ODDS_API_REMAINING}) – stoppe weitere Odds-Calls.")
            break
        for match in matches:
            bets = analyze_tennis_match(
                match, title, combined_elo, combined_surface_elo,
                training_matches=combined_tennis_training,
                elo_blend_weight=elo_blend_weight,
            )
            if bets:
                enrich_bets_with_market_data(bets, match)
                all_tennis_bets.extend(bets)
                for b in bets:
                    b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                    print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

    return all_tennis_bets, all_sports


def run_uefa_scan(api_key: str, run_id: int, loaded_matches: dict,
                  all_sports: list | None = None) -> tuple[list, dict, dict | None]:
    """UEFA/DFB-Pokal scannen. Gibt (uefa_bets, club_elo_dict, euro_model) zurueck."""
    global ODDS_API_REMAINING
    all_uefa_bets = []

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
            euro_model = get_or_train_model("uefa_european", euro_df, fit_poisson_model)
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
            matches, remaining = get_odds(api_key, sport_key)
        except Exception as e:
            print(f"    Fehler: {e}")
            continue
        ODDS_API_REMAINING = remaining
        loaded_matches[sport_key] = matches
        if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
            print(f"    Hinweis: API-Quota sehr niedrig ({ODDS_API_REMAINING}) – stoppe weitere Odds-Calls.")
            break
        for match in matches:
            bets = analyze_uefa_match(match, club_elo_dict, euro_model)
            if bets:
                enrich_bets_with_market_data(bets, match)
                all_uefa_bets.extend(bets)
                for b in bets:
                    b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                    typ = b.get("type", "").upper()
                    print(f"    ✓ UEFA VALUE [{typ}]: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

    # DFB-Pokal (konditionell)
    dfb_key = "soccer_germany_dfb_pokal"
    try:
        if all_sports is None:
            all_sports = get_active_sports(api_key)
        dfb_active = any(s["key"] == dfb_key and s["active"] for s in all_sports)
    except Exception:
        dfb_active = False

    if dfb_active:
        print("\n[🏆 DFB-Pokal] Matches via Odds API …")
        try:
            matches, remaining = get_odds(api_key, dfb_key)
            ODDS_API_REMAINING = remaining
            for match in matches:
                bets = analyze_uefa_match(match, club_elo_dict, euro_model)
                if bets:
                    for b in bets:
                        b["sport"] = dfb_key
                    enrich_bets_with_market_data(bets, match)
                    all_uefa_bets.extend(bets)
                    for b in bets:
                        b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                        typ = b.get("type", "").upper()
                        print(f"    ✓ DFB-Pokal [{typ}]: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
        except Exception as e:
            print(f"    Fehler: {e}")
    else:
        print("\n[🏆 DFB-Pokal] Keine aktive Runde – übersprungen")

    # Laenderspiele
    print("\n[🌍 Länderspiele] Matches via Odds API …")
    for int_key in INTERNATIONAL_SPORTS:
        int_label = INTERNATIONAL_LABELS.get(int_key, int_key)
        try:
            if all_sports is None:
                all_sports = get_active_sports(api_key)
            int_active = any(s["key"] == int_key and s["active"] for s in all_sports)
        except Exception:
            int_active = False

        if not int_active:
            print(f"  [{int_label}] Keine aktiven Spiele – übersprungen")
            continue

        print(f"  [{int_label}]:")
        if ODDS_API_REMAINING is not None and ODDS_API_REMAINING <= MIN_ODDS_API_REMAINING:
            print(f"    API-Quota niedrig ({ODDS_API_REMAINING}) – übersprungen")
            break
        try:
            matches, remaining = get_odds(api_key, int_key)
            ODDS_API_REMAINING = remaining
            loaded_matches[int_key] = matches
            for match in matches:
                bets = analyze_uefa_match(match, club_elo_dict, euro_model)
                if bets:
                    for b in bets:
                        b["sport"] = int_key
                    enrich_bets_with_market_data(bets, match)
                    all_uefa_bets.extend(bets)
                    for b in bets:
                        b["_pred_id"] = log_prediction(run_id, b, match_raw=match)
                        typ = b.get("type", "").upper()
                        print(f"    ✓ {int_label} [{typ}]: {b['match']} → {b['tip']} "
                              f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
            if not matches:
                print(f"    Keine Spiele gefunden")
        except Exception as e:
            print(f"    Fehler: {e}")

    return all_uefa_bets, club_elo_dict, euro_model


def run_kicktipp_predictions(football_models: dict, club_elo_dict: dict,
                              euro_model: dict | None, loaded_matches: dict,
                              out_dir: Path) -> list:
    """Kicktipp-Tipps sammeln und speichern. Gibt kicktipp_matches zurueck."""
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

    return kicktipp_matches


def run_report_and_alerts(
    all_football_bets: list, all_ou_bets: list, all_tennis_bets: list,
    all_uefa_bets: list, all_btts_signals: list,
    selected_bets: list, watch_bets: list,
    out_dir: Path, run_ref: str, scan_started_at: datetime,
    date_str: str, run_id: int | None, dry_run: bool,
) -> None:
    """HTML/CSV Report generieren, Hub-Exports, Bankroll-Snapshot und Telegram-Alerts."""
    all_bets_combined = all_football_bets + all_ou_bets + all_tennis_bets + all_uefa_bets

    print(f"\n[📊 Report] Football Bets: {len(all_football_bets)}")
    print(f"[📊 Report] O/U Bets:      {len(all_ou_bets)}")
    print(f"[📊 Report] Tennis Bets:   {len(all_tennis_bets)}")
    print(f"[📊 Report] UEFA Bets:     {len(all_uefa_bets)}")
    print(f"[📊 Report] Wettplan:      {len(selected_bets)} selektiert")

    btts_yes_count = sum(1 for s in all_btts_signals if s["signal"] == "Ja")
    print(f"[⚽ BTTS] {len(all_btts_signals)} Spiele analysiert, "
          f"{btts_yes_count} mit BTTS-Signal (>= 55%)")

    html = generate_html(all_football_bets, all_ou_bets, all_tennis_bets,
                         all_uefa_bets, selected_bets,
                         btts_signals=all_btts_signals,
                         odds_api_remaining=ODDS_API_REMAINING)
    html_path = out_dir / "sports_signals.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"[📊 Report] HTML: {html_path}")

    # CSV
    rows = []
    for b in all_football_bets:
        rows.append({
            "Typ": "Fußball",
            "Liga": SPORT_LABELS.get(b["sport"], b["sport"]),
            "Spiel": b["match"], "Tipp": b["tip"],
            "Anstoß": b["kick_off"],
            "Modell-%": f"{b['model_prob']*100:.1f}",
            "BestOdds": f"{b['best_odds']:.2f}",
            "Edge-%": f"{b['edge_pct']:.1f}",
            "Kelly-%": f"{b['kelly_pct']:.1f}",
            "Score": f"{b.get('confidence_score', 0):.0f}",
            "Tier": b.get("tier", ""),
            "Stake": f"{b.get('stake_eur', 0):.2f}",
            "λ-Heim": f"{b['lam_home']:.2f}",
            "λ-Gast": f"{b['lam_away']:.2f}",
        })
    for b in all_ou_bets:
        rows.append({
            "Typ": "Fußball O/U",
            "Liga": SPORT_LABELS.get(b["sport"], b["sport"]),
            "Spiel": b["match"], "Tipp": b["tip"],
            "Anstoß": b["kick_off"],
            "Modell-%": f"{b['model_prob']*100:.1f}",
            "BestOdds": f"{b['best_odds']:.2f}",
            "Edge-%": f"{b['edge_pct']:.1f}",
            "Kelly-%": f"{b['kelly_pct']:.1f}",
            "Score": f"{b.get('confidence_score', 0):.0f}",
            "Tier": b.get("tier", ""),
            "Stake": f"{b.get('stake_eur', 0):.2f}",
            "λ-Heim": f"{b['lam_home']:.2f}",
            "λ-Gast": f"{b['lam_away']:.2f}",
        })
    for b in all_uefa_bets:
        row = {
            "Typ": f"UEFA {b.get('type', '').upper()}",
            "Liga": UEFA_LABELS.get(b["sport"], b["sport"]),
            "Spiel": b["match"], "Tipp": b["tip"],
            "Anstoß": b["kick_off"],
            "Modell-%": f"{b['model_prob']*100:.1f}",
            "BestOdds": f"{b['best_odds']:.2f}",
            "Edge-%": f"{b['edge_pct']:.1f}",
            "Kelly-%": f"{b['kelly_pct']:.1f}",
            "Score": f"{b.get('confidence_score', 0):.0f}",
            "Tier": b.get("tier", ""),
            "Stake": f"{b.get('stake_eur', 0):.2f}",
            "Modell": b.get("model_source", ""),
        }
        if b.get("type", "") == "ou":
            row["λ-Heim"] = f"{b.get('lam_home', 0):.2f}"
            row["λ-Gast"] = f"{b.get('lam_away', 0):.2f}"
        rows.append(row)
    for b in all_tennis_bets:
        rows.append({
            "Typ": "Tennis",
            "Liga": b["tournament"],
            "Spiel": b["match"], "Tipp": b["tip"],
            "Anstoß": b["kick_off"],
            "Modell-%": f"{b['model_prob']*100:.1f}",
            "BestOdds": f"{b['best_odds']:.2f}",
            "Edge-%": f"{b['edge_pct']:.1f}",
            "Kelly-%": f"{b['kelly_pct']:.1f}",
            "Score": f"{b.get('confidence_score', 0):.0f}",
            "Tier": b.get("tier", ""),
            "Stake": f"{b.get('stake_eur', 0):.2f}",
            "Elo": str(b["elo"]) if b["elo"] else "",
        })

    if rows:
        csv_path = out_dir / "sports_signals.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        print(f"[📊 Report] CSV:  {csv_path}")

    if not dry_run:
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

    if run_id is not None:
        resolve_results()

    # Bankroll Snapshot
    if not dry_run:
        rebuild_all_snapshots()
        record_daily_snapshot(date_str)

    # Telegram Alerts
    if all_bets_combined:
        print("\n[📱 Telegram] High-Edge Alerts …")
        send_high_edge_alerts(all_bets_combined, min_edge=10.0)

    # Tuning Alert (bei kritischer Performance)
    if not dry_run:
        from bankroll_manager import generate_tuning_report, update_bankroll_from_results
        print("\n[📱 Telegram] Tuning-Alert …")
        tuning = generate_tuning_report()
        bk_info = update_bankroll_from_results()
        send_tuning_alert(tuning, bk_info)


def main() -> int:
    args = parse_args()
    scan_started_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_ref = f"sports-{scan_started_at.isoformat()}"
    print("=" * 60)
    print(f"  Sports Value Scanner — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("=" * 60)

    creds = load_credentials()
    api_key = creds.get("ODDS_API_KEY", "")
    if not args.dry_run and not api_key:
        print("ERROR: ODDS_API_KEY fehlt in ~/.stock_scanner_credentials")
        return 1

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_dir  = OUTPUT_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Backtesting
    try:
        _git_hash = subprocess.check_output(
            ["git", "-C", str(SCRIPT_DIR), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        _git_hash = "unknown"
    init_db()
    _run_id: int | None = None

    all_football_bets: list = []
    all_ou_bets:       list = []
    all_btts_signals:  list = []
    all_tennis_bets:   list = []
    all_uefa_bets:     list = []
    football_models:   dict = {}
    club_elo_dict:     dict = {}
    euro_model = None
    loaded_matches: dict = {}

    if args.dry_run:
        print("\n[DRY-RUN] Keine externen API-Calls. Erzeuge leeren Report …")
    else:
        _run_id = log_scan_run(
            scanned_at=scan_started_at.isoformat(),
            model_version=_git_hash,
        )

        all_football_bets, all_ou_bets, all_btts_signals, football_models, _n_training = \
            run_football_scan(api_key, _run_id, loaded_matches)

        update_scan_run_training(_run_id, _n_training)
        all_tennis_bets, all_sports = run_tennis_scan(api_key, _run_id)

        all_uefa_bets, club_elo_dict, euro_model = run_uefa_scan(
            api_key, _run_id, loaded_matches, all_sports
        )

    # Kicktipp
    if not args.dry_run:
        run_kicktipp_predictions(
            football_models, club_elo_dict, euro_model, loaded_matches, out_dir
        )

    # Bankroll & Bet-Selektion
    all_bets_combined = all_football_bets + all_ou_bets + all_tennis_bets + all_uefa_bets
    selected_bets = []
    watch_bets = []

    if all_bets_combined and not args.dry_run:
        print(f"\n[🎯 Wettplan] {len(all_bets_combined)} Bets bewerten & selektieren …")
        init_bankroll()
        reset_count = reset_selection_for_date(date_str)
        if reset_count:
            print(f"[🎯 Wettplan] {reset_count} alte Selektionen zurueckgesetzt")
        selected_bets, watch_bets = select_bets(all_bets_combined)

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

    # Report & Alerts
    run_report_and_alerts(
        all_football_bets, all_ou_bets, all_tennis_bets,
        all_uefa_bets, all_btts_signals,
        selected_bets, watch_bets,
        out_dir, run_ref, scan_started_at,
        date_str, _run_id, args.dry_run,
    )

    print("\n✓ Fertig!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
