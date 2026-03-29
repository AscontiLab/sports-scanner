#!/usr/bin/env python3
"""
The Odds API Datenquelle
────────────────────────
Holt Live-Odds und aktive Sportarten von The Odds API (v4).
"""

import numpy as np

from config import ODDS_API_BASE

try:
    from scanner_common.retry import request_with_retry as _sc_retry
    import requests

    def _request_with_retry(url: str, params: dict | None = None,
                            retries: int = 3, backoff: list | None = None,
                            timeout: int = 30, **kwargs) -> requests.Response:
        return _sc_retry(url, method="GET", retries=retries, backoff=backoff,
                         timeout=timeout, params=params, **kwargs)
except ImportError:
    import time
    import requests

    def _request_with_retry(url: str, params: dict | None = None,
                            retries: int = 3, backoff: list | None = None,
                            timeout: int = 30, **kwargs) -> requests.Response:
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


def get_active_sports(api_key: str) -> list:
    r = _request_with_retry(f"{ODDS_API_BASE}/sports",
                            params={"apiKey": api_key}, timeout=15)
    return r.json()


def get_odds(api_key: str, sport_key: str, markets: str = "h2h,totals") -> tuple[list, int | None]:
    """Holt Odds von der API. Gibt (matches, remaining_quota) zurueck."""
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
            return [], None
        raise
    remaining = r.headers.get("x-requests-remaining", "?")
    try:
        remaining_int = int(remaining)
    except Exception:
        remaining_int = None
    data = r.json()
    print(f"    → {len(data)} Matches | API-Requests verbleibend: {remaining}")
    return data, remaining_int


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
    Gibt Liste von {line, over_odds, under_odds} zurueck.
    """
    best: dict[float, dict] = {}
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "totals":
                continue
            for o in market["outcomes"]:
                line  = float(o.get("point", 0))
                price = float(o["price"])
                side  = o["name"].lower()
                if line not in best:
                    best[line] = {"over": 1.0, "under": 1.0}
                best[line][side] = max(best[line][side], price)
    result = []
    for line, odds in sorted(best.items()):
        if odds["over"] > 1.0 and odds["under"] > 1.0:
            result.append({"line": line, "over_odds": odds["over"], "under_odds": odds["under"]})
    return result


def bookie_consensus(match: dict) -> dict:
    """Konsenswahrscheinlichkeiten (normalisierter Schnitt ueber alle Bookies)."""
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
    """Reichert Bet-Dicts mit Konsens-Daten und Overround an (fuer Confidence Scoring)."""
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

    # Totals-Konsens berechnen (Over/Under normalisiert ueber alle Bookies)
    totals_consensus: dict[float, dict[str, float]] = {}
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "totals":
                continue
            by_line: dict[float, dict[str, float]] = {}
            for o in market["outcomes"]:
                line = float(o.get("point", 0))
                price = float(o["price"])
                side = o["name"].lower()
                if line not in by_line:
                    by_line[line] = {}
                by_line[line][side] = price
            for line, sides in by_line.items():
                if "over" in sides and "under" in sides:
                    total_impl = 1 / sides["over"] + 1 / sides["under"]
                    if total_impl > 0:
                        if line not in totals_consensus:
                            totals_consensus[line] = {"over": [], "under": []}
                        totals_consensus[line]["over"].append((1 / sides["over"]) / total_impl)
                        totals_consensus[line]["under"].append((1 / sides["under"]) / total_impl)
    # Mittelwerte bilden
    totals_consensus_avg: dict[float, dict[str, float]] = {}
    for line, sides in totals_consensus.items():
        totals_consensus_avg[line] = {
            "over": float(np.mean(sides["over"])) if sides["over"] else 0.0,
            "under": float(np.mean(sides["under"])) if sides["under"] else 0.0,
        }

    for b in bets:
        b["overround"] = avg_overround
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
        elif tip.startswith("Über") or tip.startswith("Over"):
            line = b.get("line")
            if line is not None and line in totals_consensus_avg:
                b["consensus_prob"] = totals_consensus_avg[line]["over"]
            outcome_side = None
        elif tip.startswith("Unter") or tip.startswith("Under"):
            line = b.get("line")
            if line is not None and line in totals_consensus_avg:
                b["consensus_prob"] = totals_consensus_avg[line]["under"]
            outcome_side = None
        if outcome_side and consensus.get(outcome_side) is not None:
            b["consensus_prob"] = consensus[outcome_side]
