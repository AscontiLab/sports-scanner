#!/usr/bin/env python3
"""
Freebet-Advisor fuer den Sports Scanner.

Zwei Modi:
  1. QUALIFYING: Finde den sichersten Value Bet mit Quote >= Mindestquote
  2. FREEBET: Finde den besten EV-Einsatz fuer eine gewonnene Freebet

Aufruf:
  python3 freebet_advisor.py qualifying --min-odds 2.0
  python3 freebet_advisor.py qualifying --min-odds 1.8 --sport football
  python3 freebet_advisor.py freebet --amount 5
  python3 freebet_advisor.py freebet --amount 10 --min-odds 3.0 --max-odds 5.0
  python3 freebet_advisor.py qualifying --min-odds 2.0 --json  (API-Modus)
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "sports_backtesting.db"


def _get_upcoming_bets(sport_filter: str = None, lookback_days: int = 2) -> list[dict]:
    """Laedt alle unaufgeloesten Value Bets aus der DB die noch bevorstehen."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT p.*, r.scanned_at FROM predictions p
        JOIN scan_runs r ON p.run_id = r.id
        WHERE p.bet_won IS NULL
          AND p.best_odds > 1.0
          AND p.edge_pct > 0
          AND p.commence_time > datetime('now', '-6 hours')
          AND date(r.scanned_at) >= date('now', ? || ' days')
        ORDER BY p.commence_time ASC
    """, (f"-{lookback_days}",)).fetchall()
    conn.close()

    bets = [dict(r) for r in rows]

    if sport_filter:
        sf = sport_filter.lower()
        INTERNATIONAL_KEYS = {
            "soccer_fifa_world_cup", "soccer_fifa_world_cup_qualifiers_europe",
            "soccer_uefa_nations_league", "soccer_uefa_euro",
            "soccer_fifa_world_cup_qualifiers_africa",
            "soccer_fifa_world_cup_qualifiers_asia",
            "soccer_fifa_world_cup_qualifiers_south_america",
        }
        filtered = []
        for b in bets:
            sk = (b.get("sport_key") or "").lower()
            if sf == "football" and "tennis" not in sk:
                filtered.append(b)
            elif sf == "tennis" and "tennis" in sk:
                filtered.append(b)
            elif sf in ("international", "laenderspiele"):
                if sk in INTERNATIONAL_KEYS:
                    filtered.append(b)
            elif sf in sk:
                filtered.append(b)
        bets = filtered

    # Deduplizieren: gleiches Spiel + gleicher Tipp → nur neuester Scan
    seen = {}
    for b in bets:
        key = f"{b['home_team']}_{b['away_team']}_{b['tip']}"
        if key not in seen or b["scanned_at"] > seen[key]["scanned_at"]:
            seen[key] = b
    return list(seen.values())


def find_qualifying_bets(min_odds: float, sport: str = None,
                          max_results: int = 5) -> list[dict]:
    """
    QUALIFYING-MODUS: Finde sichere Value Bets mit Quote >= min_odds.

    Strategie: Maximale Sicherheit bei Mindestquote.
    Sortierung: Hoechste model_prob zuerst (sicherster Tipp).
    """
    bets = _get_upcoming_bets(sport)

    qualifying = []
    for b in bets:
        odds = b["best_odds"]
        if odds < min_odds:
            continue

        model_prob = b["model_prob"]
        edge_pct = b["edge_pct"]
        confidence = b.get("confidence_score") or 0

        if edge_pct < 2.0:
            continue

        # Qualifying-Score: Sicherheit zuerst
        odds_proximity = 1.0 / (1.0 + abs(odds - min_odds))
        qual_score = (model_prob * 60) + (odds_proximity * 20) + (min(confidence, 80) / 80 * 20)

        qualifying.append({
            "match": f"{b['home_team']} vs {b['away_team']}",
            "league": b.get("sport_key", "?"),
            "kickoff": b["commence_time"],
            "tip": b["tip"],
            "odds": round(odds, 2),
            "bookie": b.get("best_odds_bookie", "?"),
            "model_prob": round(model_prob * 100, 1),
            "edge_pct": round(edge_pct, 1),
            "confidence": round(confidence, 0),
            "qual_score": round(qual_score, 1),
            "reason": _qualifying_reason(model_prob, odds, min_odds, edge_pct),
            "mode": "qualifying",
        })

    qualifying.sort(key=lambda x: x["qual_score"], reverse=True)
    return qualifying[:max_results]


def find_freebet_plays(freebet_amount: float, min_odds: float = 2.5,
                        max_odds: float = 6.0, sport: str = None,
                        max_results: int = 5) -> list[dict]:
    """
    FREEBET-MODUS: Finde den besten Einsatz fuer eine gewonnene Freebet.

    Bei Freebets bekommst du nur den GEWINN (nicht den Einsatz).
    EV = model_prob x (odds - 1) x freebet_amount
    Hohe Quoten mit Edge sind besser als sichere niedrige.
    """
    bets = _get_upcoming_bets(sport)

    freebet_plays = []
    for b in bets:
        odds = b["best_odds"]
        if odds < min_odds or odds > max_odds:
            continue

        model_prob = b["model_prob"]
        edge_pct = b["edge_pct"]

        if edge_pct < 2.0:
            continue

        ev = model_prob * (odds - 1) * freebet_amount
        freebet_roi = model_prob * (odds - 1) * 100

        # Sweet Spot fuer Freebets: 3.0-5.0
        if 3.0 <= odds <= 5.0:
            odds_quality = 100
        elif 2.5 <= odds < 3.0:
            odds_quality = 70
        elif 5.0 < odds <= 6.0:
            odds_quality = 60
        else:
            odds_quality = 40

        fb_score = (freebet_roi * 0.5) + (edge_pct * 0.3) + (odds_quality * 0.2)

        freebet_plays.append({
            "match": f"{b['home_team']} vs {b['away_team']}",
            "league": b.get("sport_key", "?"),
            "kickoff": b["commence_time"],
            "tip": b["tip"],
            "odds": round(odds, 2),
            "bookie": b.get("best_odds_bookie", "?"),
            "model_prob": round(model_prob * 100, 1),
            "edge_pct": round(edge_pct, 1),
            "expected_profit": round(ev, 2),
            "freebet_roi": round(freebet_roi, 1),
            "fb_score": round(fb_score, 1),
            "reason": _freebet_reason(model_prob, odds, ev, freebet_amount),
            "mode": "freebet",
            "freebet_amount": freebet_amount,
        })

    freebet_plays.sort(key=lambda x: x["fb_score"], reverse=True)
    return freebet_plays[:max_results]


def _qualifying_reason(model_prob, odds, min_odds, edge):
    parts = []
    if model_prob >= 0.55:
        parts.append(f"Hohe Gewinnchance ({model_prob*100:.0f}%)")
    elif model_prob >= 0.45:
        parts.append(f"Solide Chance ({model_prob*100:.0f}%)")
    if abs(odds - min_odds) < 0.3:
        parts.append(f"Quote nahe Mindestquote")
    if edge >= 5:
        parts.append(f"Starker Edge ({edge:.1f}%)")
    return " · ".join(parts) if parts else "Value Bet mit positivem Edge"


def _freebet_reason(model_prob, odds, ev, amount):
    parts = []
    if ev > amount * 0.5:
        parts.append(f"Hoher EV ({ev:.2f} EUR)")
    if 3.0 <= odds <= 5.0:
        parts.append(f"Optimale Freebet-Quote ({odds:.2f})")
    if model_prob >= 0.35:
        parts.append(f"Realistische Chance ({model_prob*100:.0f}%)")
    roi = model_prob * (odds - 1) * 100
    if roi > 80:
        parts.append(f"ROI {roi:.0f}%")
    return " · ".join(parts) if parts else "Positiver Expected Value"


def find_qualifying_combos(target_odds: float, sport: str = None,
                            max_legs: int = 3, max_results: int = 5) -> list[dict]:
    """
    KOMBI-MODUS: Finde 2-3er Kombiwetten die zusammen >= target_odds ergeben.

    Strategie: Kombiniere 2-3 sehr sichere Einzelwetten (hohe model_prob)
    deren Quoten multipliziert die Mindestquote erreichen.
    z.B. 1.40 x 1.50 = 2.10 >= 2.0

    Vorteil: Jede Einzelwette hat hohe Gewinnchance,
    Gesamtchance ist Produkt der Einzelwahrscheinlichkeiten.
    """
    from itertools import combinations

    bets = _get_upcoming_bets(sport)

    # Nur Bets mit hoher Sicherheit und positvem Edge
    safe_bets = [b for b in bets
                 if b["model_prob"] >= 0.45
                 and b["edge_pct"] >= 2.0
                 and 1.20 <= b["best_odds"] <= 2.50]

    # Deduplizieren: max 1 Bet pro Spiel
    seen_matches = {}
    for b in safe_bets:
        match_key = f"{b['home_team']}_{b['away_team']}"
        if match_key not in seen_matches or b["model_prob"] > seen_matches[match_key]["model_prob"]:
            seen_matches[match_key] = b
    safe_bets = list(seen_matches.values())

    # Sortiere nach Sicherheit
    safe_bets.sort(key=lambda x: x["model_prob"], reverse=True)
    # Maximal 15 kandidaten um Kombinatorik zu begrenzen
    safe_bets = safe_bets[:15]

    combos = []

    # 2er-Kombis
    for a, b in combinations(safe_bets, 2):
        combo_odds = a["best_odds"] * b["best_odds"]
        if combo_odds >= target_odds:
            combo_prob = a["model_prob"] * b["model_prob"]
            combos.append(_build_combo([a, b], combo_odds, combo_prob, target_odds))

    # 3er-Kombis (nur wenn max_legs >= 3)
    if max_legs >= 3:
        for a, b, c in combinations(safe_bets, 3):
            combo_odds = a["best_odds"] * b["best_odds"] * c["best_odds"]
            if combo_odds >= target_odds:
                combo_prob = a["model_prob"] * b["model_prob"] * c["model_prob"]
                combos.append(_build_combo([a, b, c], combo_odds, combo_prob, target_odds))

    # Sortiere nach hoechster Kombi-Wahrscheinlichkeit
    combos.sort(key=lambda x: x["combo_prob"], reverse=True)
    return combos[:max_results]


def _build_combo(legs: list[dict], combo_odds: float, combo_prob: float,
                  target_odds: float) -> dict:
    """Baut ein Kombi-Ergebnis-Dict."""
    leg_details = []
    for b in legs:
        leg_details.append({
            "match": f"{b['home_team']} vs {b['away_team']}",
            "tip": b["tip"],
            "odds": round(b["best_odds"], 2),
            "model_prob": round(b["model_prob"] * 100, 1),
            "edge_pct": round(b["edge_pct"], 1),
            "league": b.get("sport_key", "?"),
            "kickoff": b["commence_time"],
        })

    return {
        "legs": leg_details,
        "num_legs": len(legs),
        "combo_odds": round(combo_odds, 2),
        "combo_prob": round(combo_prob * 100, 1),
        "target_odds": target_odds,
        "odds_surplus": round(combo_odds - target_odds, 2),
        "mode": "qualifying_combo",
        "reason": (f"{len(legs)}er-Kombi: {' × '.join(f'{l['odds']:.2f}' for l in leg_details)}"
                   f" = {combo_odds:.2f} (>= {target_odds:.2f})"
                   f" | Gesamtchance: {combo_prob*100:.0f}%"),
    }


def format_text(results: list[dict], mode: str) -> str:
    """CLI/Text-Ausgabe."""
    if not results:
        return "Keine passenden Vorschlaege gefunden."

    if mode == "qualifying":
        lines = ["QUALIFYING-VORSCHLAEGE (sicherste Wetten)",
                  "Ziel: Freebet freischalten mit minimalem Risiko",
                  "=" * 50]
    else:
        amt = results[0].get("freebet_amount", "?")
        lines = [f"FREEBET-VORSCHLAEGE ({amt} EUR)",
                  "Ziel: Maximaler erwarteter Gewinn",
                  "=" * 50]

    for i, r in enumerate(results, 1):
        lines.append(f"\n{i}. {r['match']}")
        lines.append(f"   {r['league']} | {r['kickoff'][:16]}")
        lines.append(f"   Tipp: {r['tip']}  |  Quote: {r['odds']:.2f} ({r['bookie']})")
        lines.append(f"   Modell: {r['model_prob']}%  |  Edge: {r['edge_pct']}%")
        if mode == "freebet":
            lines.append(f"   Erwarteter Gewinn: {r['expected_profit']:.2f} EUR  |  ROI: {r['freebet_roi']}%")
        lines.append(f"   {r['reason']}")
    return "\n".join(lines)


def format_telegram(results: list[dict], mode: str) -> str:
    """Telegram HTML-Nachricht."""
    if not results:
        return "Keine passenden Vorschlaege gefunden."

    if mode == "qualifying":
        lines = ["<b>Qualifying-Vorschlaege</b>"]
    else:
        amt = results[0].get("freebet_amount", "?")
        lines = [f"<b>Freebet-Vorschlaege ({amt} EUR)</b>"]

    for i, r in enumerate(results, 1):
        lines.append(f"\n<b>{i}. {r['tip']}</b> @ {r['odds']:.2f}")
        lines.append(f"   {r['match']}")
        lines.append(f"   Modell: {r['model_prob']}% | Edge: {r['edge_pct']}%")
        if mode == "freebet":
            lines.append(f"   EV: <b>{r['expected_profit']:.2f} EUR</b> ({r['freebet_roi']}% ROI)")
        lines.append(f"   <i>{r['reason']}</i>")
    return "\n".join(lines)


def recalc_ev(match_query: str, actual_odds: float, freebet_amount: float = 0) -> dict:
    """
    EV-Rechner mit eigener Quote (z.B. Winamax-Quote statt Scanner-Quote).

    match_query: Suchbegriff (Teamname, z.B. "Malta")
    actual_odds: Die echte Quote bei deinem Bookie
    freebet_amount: Wenn > 0, berechne Freebet-EV (nur Gewinn, kein Einsatz zurueck)
    """
    bets = _get_upcoming_bets()

    # Suche nach Match
    query = match_query.lower()
    matches = []
    for b in bets:
        searchable = f"{b['home_team']} {b['away_team']} {b['tip']}".lower()
        if query in searchable:
            matches.append(b)

    if not matches:
        return {"error": f"Kein Match gefunden fuer '{match_query}'", "results": []}

    results = []
    for b in matches:
        model_prob = b["model_prob"]
        # Normaler EV: model_prob * odds - 1
        edge = model_prob * actual_odds - 1.0
        edge_pct = edge * 100

        if freebet_amount > 0:
            # Freebet: nur Gewinn (kein Einsatz zurueck)
            ev_eur = model_prob * (actual_odds - 1) * freebet_amount
            freebet_roi = model_prob * (actual_odds - 1) * 100
        else:
            ev_eur = None
            freebet_roi = None

        results.append({
            "match": f"{b['home_team']} vs {b['away_team']}",
            "league": b.get("sport_key", "?"),
            "kickoff": b["commence_time"],
            "tip": b["tip"],
            "scanner_odds": round(b["best_odds"], 2),
            "scanner_bookie": b.get("best_odds_bookie", "?"),
            "your_odds": round(actual_odds, 2),
            "model_prob": round(model_prob * 100, 1),
            "edge_pct": round(edge_pct, 1),
            "has_value": edge_pct > 0,
            "expected_profit": round(ev_eur, 2) if ev_eur is not None else None,
            "freebet_roi": round(freebet_roi, 1) if freebet_roi is not None else None,
            "verdict": _ev_verdict(edge_pct, freebet_roi),
        })

    results.sort(key=lambda x: x["edge_pct"], reverse=True)
    return {"mode": "ev-check", "count": len(results), "results": results}


def _ev_verdict(edge_pct: float, freebet_roi: float = None) -> str:
    """Einfache Bewertung."""
    if freebet_roi is not None:
        if freebet_roi > 80:
            return "Sehr guter Freebet-Einsatz"
        elif freebet_roi > 50:
            return "Solider Freebet-Einsatz"
        elif freebet_roi > 0:
            return "OK, aber es gibt vermutlich Besseres"
        else:
            return "Kein Value — Finger weg"
    else:
        if edge_pct > 10:
            return "Starker Value"
        elif edge_pct > 3:
            return "Guter Value"
        elif edge_pct > 0:
            return "Minimaler Value"
        else:
            return "Kein Value bei dieser Quote"


# --- API-Funktionen (fuer serve_output.py und n8n) ---

def handle_api_request(params: dict) -> dict:
    """
    Verarbeitet eine API-Anfrage (von Dashboard oder Telegram).

    params:
      mode: "qualifying" oder "freebet"
      min_odds: float (Qualifying) oder min. Quote (Freebet)
      amount: float (nur Freebet)
      max_odds: float (optional, Freebet)
      sport: str (optional)
    """
    mode = params.get("mode", "qualifying")

    if mode == "qualifying":
        results = find_qualifying_bets(
            min_odds=float(params.get("min_odds", 2.0)),
            sport=params.get("sport"),
            max_results=int(params.get("max_results", 5)),
        )
    elif mode == "freebet":
        results = find_freebet_plays(
            freebet_amount=float(params.get("amount", 5)),
            min_odds=float(params.get("min_odds", 2.5)),
            max_odds=float(params.get("max_odds", 6.0)),
            sport=params.get("sport"),
            max_results=int(params.get("max_results", 5)),
        )
    elif mode == "ev-check":
        return recalc_ev(
            match_query=params.get("match", ""),
            actual_odds=float(params.get("odds", 2.0)),
            freebet_amount=float(params.get("amount", 0)),
        )
    else:
        return {"error": f"Unbekannter Modus: {mode}"}

    return {
        "mode": mode,
        "count": len(results),
        "results": results,
        "text": format_text(results, mode),
        "telegram": format_telegram(results, mode),
    }


def main():
    parser = argparse.ArgumentParser(description="Freebet Advisor")
    sub = parser.add_subparsers(dest="mode", required=True)

    q = sub.add_parser("qualifying", help="Sichere Wetten fuer Qualifying")
    q.add_argument("--min-odds", type=float, required=True)
    q.add_argument("--sport", type=str, default=None)
    q.add_argument("--max-results", type=int, default=5)
    q.add_argument("--json", action="store_true")

    f = sub.add_parser("freebet", help="Beste Einsaetze fuer Freebet")
    f.add_argument("--amount", type=float, required=True)
    f.add_argument("--min-odds", type=float, default=2.5)
    f.add_argument("--max-odds", type=float, default=6.0)
    f.add_argument("--sport", type=str, default=None)
    f.add_argument("--max-results", type=int, default=5)
    f.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.mode == "qualifying":
        results = find_qualifying_bets(args.min_odds, args.sport, args.max_results)
    else:
        results = find_freebet_plays(args.amount, args.min_odds, args.max_odds,
                                      args.sport, args.max_results)

    if args.json:
        print(json.dumps({"mode": args.mode, "results": results},
                          ensure_ascii=False, indent=2))
    else:
        print(format_text(results, args.mode))


if __name__ == "__main__":
    main()
