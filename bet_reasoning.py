"""
Bet Reasoning — Gemma erklaert warum ein Bet Value hat.
Generiert 1-2 Saetze pro Bet basierend auf den vorhandenen Daten.
"""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://172.28.0.20:11434"
MODEL = "gemma4:e4b"
TIMEOUT = 60
PAUSE = 1

SYSTEM_PROMPT = """\
Du bist ein erfahrener Sportwetten-Analyst. Erklaere in 1-2 kurzen, praegnanten Saetzen auf Deutsch, \
warum dieser Bet Value hat. Sei konkret — nenne Zahlen und den Kern-Grund. \
Keine Floskeln, keine Disclamer, kein "Fazit". Nur die Analyse.

Antworte NUR mit dem Erklaerungstext, kein JSON, keine Aufzaehlung.\
"""


def _build_prompt(bet: dict) -> str:
    """Baut den Reasoning-Prompt aus den Bet-Daten."""
    parts = []

    match = f"{bet.get('home_team', '?')} vs {bet.get('away_team', '?')}"
    league = bet.get("sport_key", "")
    tip = bet.get("tip", "")
    bet_type = bet.get("bet_type", "1x2")
    odds = bet.get("best_odds", 0)
    model_prob = bet.get("model_prob", 0)
    consensus = bet.get("consensus_prob", 0)
    edge = bet.get("edge_pct", 0)
    score = bet.get("confidence_score", 0)
    model_src = bet.get("model_source", "")
    bookie = bet.get("best_odds_bookie", "")

    parts.append(f"Match: {match}")
    if league:
        parts.append(f"Liga: {league}")
    parts.append(f"Tipp: {tip} ({bet_type})")
    parts.append(f"Quote: {odds:.2f} (bei {bookie})" if bookie else f"Quote: {odds:.2f}")
    parts.append(f"Modell-Wahrscheinlichkeit: {model_prob*100:.1f}%")
    parts.append(f"Markt-Konsens: {consensus*100:.1f}%")
    parts.append(f"Edge: {edge:.1f}%")
    parts.append(f"Confidence: {score:.0f}/100")
    parts.append(f"Modell: {model_src}")

    # Poisson-Daten (Fussball)
    lam_h = bet.get("lam_home")
    lam_a = bet.get("lam_away")
    if lam_h and lam_a:
        parts.append(f"Erwartete Tore: {lam_h:.2f} (Heim) vs {lam_a:.2f} (Auswaerts)")

    # Elo-Daten (Tennis/Club Elo)
    elo_h = bet.get("elo_home")
    elo_a = bet.get("elo_away")
    if elo_h and elo_a:
        parts.append(f"Elo: {elo_h:.0f} vs {elo_a:.0f} (Differenz: {elo_h-elo_a:+.0f})")

    # Training-Daten
    matches = bet.get("training_matches")
    if matches:
        parts.append(f"Trainings-Basis: {matches} Spiele")

    # Filter-Flags
    if bet.get("hard_filter"):
        parts.append(f"Filter-Warnung: {bet['hard_filter']}")
    if bet.get("market_gap_flag"):
        parts.append("Achtung: Grosse Abweichung Modell vs. Markt")

    parts.append("\nErklaere in 1-2 Saetzen warum dieser Bet Value hat (oder nicht).")

    return "\n".join(parts)


def generate_reasoning(bet: dict) -> str | None:
    """Generiert Reasoning fuer einen einzelnen Bet via Gemma."""
    prompt = _build_prompt(bet)

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"num_predict": 150},
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == 0:
                time.sleep(3)
                continue
            logger.warning("Reasoning Ollama-Fehler: %s", e)
            return None

    text = resp.json().get("message", {}).get("content", "").strip()

    # Bereinigen: Markdown, Aufzaehlungen, Leerzeilen entfernen
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    lines = [l.lstrip("- •*·") .strip() for l in lines]
    text = " ".join(lines)

    # Auf 300 Zeichen begrenzen
    if len(text) > 300:
        text = text[:297].rsplit(" ", 1)[0] + "..."

    return text if len(text) > 20 else None


def add_reasoning_to_bets(bets: list[dict], max_bets: int = 10) -> int:
    """Fuegt Reasoning zu den Top-Bets hinzu. Gibt Anzahl zurueck."""
    # Nur selected + Strong Pick / Value Bet
    candidates = [b for b in bets if b.get("selected") and b.get("tier") != "Watch"]
    candidates = candidates[:max_bets]

    if not candidates:
        return 0

    # Ollama-Check
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).raise_for_status()
    except requests.RequestException:
        print("[Reasoning] Ollama nicht erreichbar — uebersprungen")
        return 0

    count = 0
    for i, bet in enumerate(candidates):
        reasoning = generate_reasoning(bet)
        if reasoning:
            bet["reasoning"] = reasoning
            count += 1
            match = f"{bet.get('home_team', '?')} vs {bet.get('away_team', '?')}"
            print(f"  [Reasoning] {match}: {reasoning[:80]}...")
        if i < len(candidates) - 1:
            time.sleep(PAUSE)

    print(f"[Reasoning] {count}/{len(candidates)} Bets erklaert")
    return count
