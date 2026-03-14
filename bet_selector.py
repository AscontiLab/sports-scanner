#!/usr/bin/env python3
"""
Bet-Selektor für den Sports Value Scanner.

Bewertet alle Value Bets mit einem Confidence Score (0–100),
ordnet Tiers zu und selektiert die Top-N Bets für den Wettplan.
"""

from functools import lru_cache

from config import (
    CONFIDENCE_WEIGHTS,
    TIER_STRONG_PICK,
    TIER_VALUE_BET,
    MAX_DAILY_BETS,
    MAX_DAILY_RISK_PCT,
    MIN_STAKE_EUR,
    MAX_SAME_OUTCOME,
    MAX_MODEL_MARKET_GAP,
    EDGE_SKEPTICISM_THRESHOLD,
    ODDS_SKEPTICISM_THRESHOLD,
    MODEL_TRUST_MIN_BETS,
    MODEL_TRUST_EXPECTED_WIN_RATE,
    MODEL_TRUST_FLOOR,
    ODDS_PREF_SWEET_SPOT,
    ODDS_PREF_MAX,
)
from bankroll_manager import calculate_stake, get_current_bankroll


def _get_model_stats() -> dict:
    """
    Liest Backtesting-Statistiken pro Modell aus der DB.
    Returns: {model_source: {"resolved": int, "roi_pct": float, "won": int, "total": int}}
    """
    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).parent / "sports_backtesting.db"
    try:
        # Context-Manager für automatisches Schließen
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    model_source,
                    COUNT(*) AS total,
                    SUM(CASE WHEN bet_won = 1 THEN 1 ELSE 0 END) AS won,
                    ROUND(
                        100.0 * SUM(pnl_units) / NULLIF(SUM(stake_units), 0), 2
                    ) AS roi_pct
                FROM predictions
                WHERE bet_won IS NOT NULL
                GROUP BY model_source
            """).fetchall()
            return {
                r["model_source"]: {
                    "resolved": r["total"],
                    "roi_pct": float(r["roi_pct"]) if r["roi_pct"] else 0.0,
                    "won": r["won"],
                    "total": r["total"],
                }
                for r in rows
            }
    except Exception:
        return {}


# Gecacht pro Lauf – wird nur einmal aus der DB gelesen
@lru_cache(maxsize=1)
def _get_training_matches_count() -> int:
    """Liest die Anzahl der Training-Matches aus dem letzten Scan-Run."""
    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).parent / "sports_backtesting.db"
    try:
        # Context-Manager für automatisches Schließen
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT training_matches FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0


def compute_confidence_score(bet: dict, model_stats: dict | None = None) -> float:
    """
    Berechnet einen Confidence Score (0–100) für eine Bet.

    Faktoren (gewichtet):
    - Edge-Qualität (15%): 3% → 0, 15%+ → 100, mit Skeptizismus bei Longshots
    - Modell-Zuverlässigkeit (30%): ROI × Trust-Faktor (Win-Rate-adjusted)
    - Odds-Qualität (10%): Niedriger Overround = besser
    - Markt-Konsens-Abstand (20%): Kleine Abweichung = stabiler
    - Datentiefe (10%): Mehr Training-Matches = höher
    - Odds-Präferenz (15%): Moderate Quoten (1.50–3.00) bevorzugt
    """
    if model_stats is None:
        model_stats = _get_model_stats()

    w = CONFIDENCE_WEIGHTS
    score = 0.0

    # 1. Edge-Qualität: Linear 3% → 0, 15% → 100
    edge = bet.get("edge_pct", 0.0)
    odds = bet.get("best_odds", bet.get("odds", 0.0))
    edge_score = min(max((edge - 3.0) / 12.0, 0.0), 1.0) * 100

    # Edge-Skeptizismus: Hohe Edges auf hohe Odds werden gedaempft
    if edge > EDGE_SKEPTICISM_THRESHOLD and odds > ODDS_SKEPTICISM_THRESHOLD:
        excess_edge = edge - EDGE_SKEPTICISM_THRESHOLD
        skepticism_penalty = min(0.7, excess_edge / 50.0)
        edge_score *= (1.0 - skepticism_penalty)

    score += w["edge_quality"] * edge_score

    # 2. Modell-Zuverlaessigkeit: ROI + Trust-Faktor mit Win-Rate-Adjustment
    model_src = (
        bet.get("model_source")
        or _infer_model_source(bet)
    )
    stats = model_stats.get(model_src, {})
    resolved = stats.get("resolved", 0)
    won = stats.get("won", 0)

    sample_trust = min(1.0, resolved / 100.0)

    if resolved >= MODEL_TRUST_MIN_BETS:
        win_rate = won / resolved
        trust_modifier = min(1.0, win_rate / MODEL_TRUST_EXPECTED_WIN_RATE)
        trust_modifier = max(MODEL_TRUST_FLOOR, trust_modifier)
    else:
        trust_modifier = 0.5  # neutral bis genug Daten

    trust = sample_trust * trust_modifier

    roi = stats.get("roi_pct", 0.0)
    # ROI-Score: -20% → 0, 0% → 50, +20% → 100
    roi_score = min(max((roi + 20.0) / 40.0, 0.0), 1.0) * 100
    model_score = roi_score * trust
    score += w["model_reliability"] * model_score

    # 3. Odds-Qualität (15%): Overround
    # Overround von 0.05 (5%) = gut → 100, 0.15 (15%) = schlecht → 0
    overround = bet.get("overround")
    if overround is not None:
        odds_score = min(max((0.15 - overround) / 0.10, 0.0), 1.0) * 100
    else:
        odds_score = 50  # neutral wenn unbekannt
    score += w["odds_quality"] * odds_score

    # 4. Markt-Konsens-Abstand (15%)
    model_prob = bet.get("model_prob", 0.0)
    consensus = bet.get("consensus_prob")
    if consensus is not None and consensus > 0:
        # Abstand Model ↔ Konsens: 0% Diff → 100, 20% Diff → 0
        diff = abs(model_prob - consensus)
        consensus_score = min(max((0.20 - diff) / 0.20, 0.0), 1.0) * 100
    else:
        consensus_score = 50  # neutral wenn unbekannt
    score += w["market_consensus"] * consensus_score

    # 5. Datentiefe
    training = _get_training_matches_count()
    # 200 Matches → 50, 1000+ → 100
    depth_score = min(max(training / 1000.0, 0.0), 1.0) * 100
    score += w["data_depth"] * depth_score

    # 6. Odds-Praeferenz: Sweet Spot 1.50–3.00 → 100, ab 8.0 → 0
    low, high = ODDS_PREF_SWEET_SPOT
    if odds <= 0:
        odds_pref_score = 0
    elif low <= odds <= high:
        odds_pref_score = 100
    elif odds < low:
        # Unter Sweet Spot (z.B. 1.20): leicht reduziert
        odds_pref_score = max(0, 100 - (low - odds) * 200)
    else:
        # Ueber Sweet Spot: linear abfallend bis ODDS_PREF_MAX
        odds_pref_score = max(0, 100 * (1.0 - (odds - high) / (ODDS_PREF_MAX - high)))
    score += w["odds_preference"] * odds_pref_score

    return round(min(max(score, 0), 100), 1)


def _infer_model_source(bet: dict) -> str:
    """Leitet model_source aus type ab."""
    bet_type = bet.get("type", "").lower()
    if bet_type == "tennis":
        return "Elo"
    if bet_type in ("football", "football_ou", "1x2", "ou"):
        return "Poisson"
    return "Poisson"


def assign_tier(confidence_score: float) -> str:
    """Ordnet einen Tier basierend auf dem Confidence Score zu."""
    if confidence_score >= TIER_STRONG_PICK:
        return "Strong Pick"
    if confidence_score >= TIER_VALUE_BET:
        return "Value Bet"
    return "Watch"


def _match_key(bet: dict) -> str:
    """Erzeugt einen eindeutigen Schlüssel pro Spiel."""
    return bet.get("match", "") or f"{bet.get('home', '')} – {bet.get('away', '')}"


def _bet_market_type(bet: dict) -> str:
    """Bestimmt den Markttyp: '1x2', 'ou', oder 'tennis'."""
    raw = bet.get("type", "").lower()
    if raw in ("football_ou", "ou"):
        return "ou"
    if raw == "tennis":
        return "tennis"
    return "1x2"


def _outcome_type(bet: dict) -> str:
    """Klassifiziert den Outcome-Typ: 'draw', 'over', 'under', 'home', 'away'."""
    tip = bet.get("tip", "").lower()
    if "unentschieden" in tip or tip == "x" or tip == "draw":
        return "draw"
    if tip.startswith("über") or tip.startswith("over"):
        return "over"
    if tip.startswith("unter") or tip.startswith("under"):
        return "under"
    # Home vs Away anhand outcome_side
    side = bet.get("outcome_side", "").lower()
    if side == "home":
        return "home_win"
    if side == "away":
        return "away_win"
    return "other"


def filter_correlated_bets(bets: list) -> list:
    """
    Korrelations-Filter: Max 1 Bet pro Markttyp (1X2 / O/U) pro Spiel.
    Behält den Bet mit dem höchsten Confidence Score.
    """
    best_per_match_market: dict[tuple[str, str], dict] = {}

    for bet in bets:
        match_key = _match_key(bet)
        market = _bet_market_type(bet)
        key = (match_key, market)

        if key not in best_per_match_market:
            best_per_match_market[key] = bet
        elif bet.get("confidence_score", 0) > best_per_match_market[key].get("confidence_score", 0):
            best_per_match_market[key] = bet

    return list(best_per_match_market.values())


def _is_today(kick_off: str) -> bool:
    """Prüft ob ein Anstoß-Zeitpunkt heute ist (UTC)."""
    from datetime import datetime, timezone
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return kick_off[:10] == today_str


def select_bets(all_bets: list, bankroll: float | None = None) -> tuple[list, list]:
    """
    Hauptfunktion: Bewertet, filtert und selektiert die Top-Bets.

    Priorisierung: Heutige Bets werden bevorzugt. Restliche Slots
    werden mit Bets der nächsten Tage aufgefüllt.

    Args:
        all_bets:  Alle generierten Value Bets
        bankroll:  Aktuelle Bankroll in EUR (None → aus DB)

    Returns:
        (selected_bets, watch_bets) — jeweils mit confidence_score, tier, stake_eur
    """
    if bankroll is None:
        bankroll = get_current_bankroll()

    model_stats = _get_model_stats()
    max_risk = bankroll * MAX_DAILY_RISK_PCT

    # 1. Alle Bets scoren
    for bet in all_bets:
        bet["confidence_score"] = compute_confidence_score(bet, model_stats)
        bet["tier"] = assign_tier(bet["confidence_score"])

    # 1b. Market Disagreement Filter
    market_gap_count = 0
    for bet in all_bets:
        consensus = bet.get("consensus_prob")
        model_prob = bet.get("model_prob", 0.0)
        if consensus and consensus > 0:
            gap = model_prob - consensus
            if gap > MAX_MODEL_MARKET_GAP:
                bet["tier"] = "Watch"
                bet["market_gap_flag"] = True
                bet["confidence_score"] = min(bet["confidence_score"], TIER_VALUE_BET - 1)
                market_gap_count += 1

    if market_gap_count > 0:
        print(f"[Bet-Selektor] {market_gap_count} Bets wegen Market-Gap > "
              f"{MAX_MODEL_MARKET_GAP*100:.0f}pp auf Watch gesetzt (MKT)")

    # 2. Korrelations-Filter
    filtered = filter_correlated_bets(all_bets)

    # 3. Splitten: heute vs. später
    today_candidates = [b for b in filtered
                        if b["tier"] != "Watch" and _is_today(b.get("kick_off", ""))]
    later_candidates = [b for b in filtered
                        if b["tier"] != "Watch" and not _is_today(b.get("kick_off", ""))]

    today_candidates.sort(key=lambda b: b.get("confidence_score", 0), reverse=True)
    later_candidates.sort(key=lambda b: b.get("confidence_score", 0), reverse=True)

    # 4. Erst heute füllen, dann Rest mit später auffüllen
    selected = []
    total_risk = 0.0
    outcome_counts: dict[str, int] = {}

    for bet in today_candidates + later_candidates:
        if len(selected) >= MAX_DAILY_BETS:
            break

        # Outcome-Diversifikation: Max N gleiche Outcome-Art
        outcome_type = _outcome_type(bet)
        if outcome_counts.get(outcome_type, 0) >= MAX_SAME_OUTCOME:
            continue

        # Stake berechnen
        stake = calculate_stake(bet.get("kelly_pct", 0), bankroll)

        # Risiko-Budget prüfen
        if total_risk + stake > max_risk:
            # Stake reduzieren um Budget einzuhalten
            remaining = max_risk - total_risk
            if remaining >= MIN_STAKE_EUR:
                stake = round(remaining, 2)
            else:
                continue

        bet["stake_eur"] = stake
        bet["selected"] = 1
        total_risk += stake
        outcome_counts[outcome_type] = outcome_counts.get(outcome_type, 0) + 1
        selected.append(bet)

    n_today = sum(1 for b in selected if _is_today(b.get("kick_off", "")))

    # 5. Rest als Watch markieren
    selected_ids = {id(b) for b in selected}
    watch = []
    for bet in all_bets:
        if id(bet) not in selected_ids:
            bet["stake_eur"] = 0.0
            bet["selected"] = 0
            if "confidence_score" not in bet:
                bet["confidence_score"] = compute_confidence_score(bet, model_stats)
                bet["tier"] = assign_tier(bet["confidence_score"])
            watch.append(bet)

    print(f"[Bet-Selektor] {len(all_bets)} Bets → {len(filtered)} nach Filter → "
          f"{len(selected)} selektiert ({n_today} heute, {len(selected) - n_today} später)")
    print(f"[Bet-Selektor] Tagesrisiko: {total_risk:.2f} EUR "
          f"(Budget: {max_risk:.2f} EUR, Bankroll: {bankroll:.2f} EUR)")

    for bet in selected:
        tier_icon = "🔥" if bet["tier"] == "Strong Pick" else "✓"
        print(f"  {tier_icon} [{bet['tier']}] {bet.get('match', '?')} → {bet.get('tip', '?')} "
              f"| Score {bet['confidence_score']:.0f} | {bet['stake_eur']:.2f} EUR "
              f"| Edge {bet.get('edge_pct', 0):.1f}%")

    return selected, watch
