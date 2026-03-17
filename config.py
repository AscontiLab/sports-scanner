#!/usr/bin/env python3
"""
Zentrale Konfiguration für den Sports Value Scanner.
Alle Konstanten, die in sports_scanner.py, bet_selector.py und bankroll_manager.py
verwendet werden.
"""

import sys
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# PFADE
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "output"
CREDS_FILE = Path.home() / ".stock_scanner_credentials"

# ═══════════════════════════════════════════════════════════════════════════════
# FUSSBALL-LIGEN
# ═══════════════════════════════════════════════════════════════════════════════

FOOTBALL_SPORTS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_germany_liga3",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
]

SPORT_LABELS = {
    "soccer_germany_bundesliga":       "1. Bundesliga",
    "soccer_germany_bundesliga2":      "2. Bundesliga",
    "soccer_germany_liga3":            "3. Liga",
    "soccer_germany_dfb_pokal":        "DFB-Pokal",
    "soccer_epl":                      "Premier League",
    "soccer_spain_la_liga":            "La Liga",
    "soccer_italy_serie_a":            "Serie A",
    "soccer_france_ligue_one":         "Ligue 1",
}

FDCO_LEAGUES = {
    "soccer_germany_bundesliga": "D1",
    "soccer_germany_bundesliga2": "D2",
    "soccer_epl": "E0",
    "soccer_spain_la_liga": "SP1",
    "soccer_italy_serie_a": "I1",
    "soccer_france_ligue_one": "F1",
}

OPENLIGADB_BASE = "https://api.openligadb.de"

# ═══════════════════════════════════════════════════════════════════════════════
# VALUE BETTING SCHWELLWERTE
# ═══════════════════════════════════════════════════════════════════════════════

MIN_EDGE_PCT = 3.0
MAX_EDGE_PCT = 100.0
MIN_ODDS     = 1.25
MAX_KELLY    = 0.05

# ═══════════════════════════════════════════════════════════════════════════════
# TENNIS
# ═══════════════════════════════════════════════════════════════════════════════

ELO_K_FACTOR  = 32
ELO_INITIAL   = 1500
ELO_YEARS     = list(range(datetime.now().year - 3, datetime.now().year + 1))

# ═══════════════════════════════════════════════════════════════════════════════
# UEFA
# ═══════════════════════════════════════════════════════════════════════════════

UEFA_SPORTS = [
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
]

UEFA_LABELS = {
    "soccer_uefa_champs_league":            "Champions League",
    "soccer_uefa_europa_league":            "Europa League",
    "soccer_uefa_europa_conference_league": "Conference League",
    "soccer_germany_dfb_pokal":             "DFB-Pokal",
}

# ═══════════════════════════════════════════════════════════════════════════════
# CLUB-ELO
# ═══════════════════════════════════════════════════════════════════════════════

CLUBELO_URL_HTTPS = "https://api.clubelo.com/{date}"
CLUBELO_URL_HTTP  = "http://api.clubelo.com/{date}"

# ═══════════════════════════════════════════════════════════════════════════════
# EUROPÄISCHES POISSON-MODELL
# ═══════════════════════════════════════════════════════════════════════════════

EUROPEAN_FDCO_LEAGUES = [
    "E0",   # Premier League
    "SP1",  # La Liga
    "I1",   # Serie A
    "F1",   # Ligue 1
    "D1",   # Bundesliga 1
    "D2",   # Bundesliga 2
]

# ═══════════════════════════════════════════════════════════════════════════════
# ODDS API
# ═══════════════════════════════════════════════════════════════════════════════

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MIN_ODDS_API_REMAINING = 5

# ═══════════════════════════════════════════════════════════════════════════════
# KICKTIPP
# ═══════════════════════════════════════════════════════════════════════════════

KICKTIPP_FOOTBALL_SPORTS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
]

KICKTIPP_UEFA_SPORTS = [
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
]

KICKTIPP_LABELS = {
    "soccer_germany_bundesliga":  "1. Bundesliga",
    "soccer_germany_bundesliga2": "2. Bundesliga",
    "soccer_epl":                 "Premier League",
    "soccer_spain_la_liga":       "La Liga",
    "soccer_italy_serie_a":       "Serie A",
    "soccer_france_ligue_one":    "Ligue 1",
    "soccer_uefa_champs_league":  "Champions League",
    "soccer_uefa_europa_league":  "Europa League",
}

# ═══════════════════════════════════════════════════════════════════════════════
# BANKROLL-MANAGEMENT (NEU)
# ═══════════════════════════════════════════════════════════════════════════════

STARTING_BANKROLL = 100.0       # EUR
KELLY_FRACTION = 0.25           # Quarter-Kelly
MAX_DAILY_BETS = 8              # Max 8 Bets pro Tag
MAX_DAILY_RISK_PCT = 0.15       # Max 15% der Bankroll pro Tag
MIN_STAKE_EUR = 1.0             # Minimum-Einsatz
MAX_SAME_OUTCOME = 3            # Max gleiche Outcome-Art (wenn O/U dominiert)

# ═══════════════════════════════════════════════════════════════════════════════
# BET-SELEKTOR: CONFIDENCE SCORING
# ═══════════════════════════════════════════════════════════════════════════════

# Gewichte für Confidence Score (Summe = 1.0)
CONFIDENCE_WEIGHTS = {
    "edge_quality":       0.10,
    "model_reliability":  0.25,
    "odds_quality":       0.10,
    "market_consensus":   0.25,
    "data_depth":         0.05,
    "odds_preference":    0.25,
}

# Odds-Praeferenz: Bevorzugt moderate Quoten, daempft extreme Aussenseiter
ODDS_PREF_SWEET_SPOT = (1.60, 2.80)   # Optimaler Odds-Bereich → Score 100
ODDS_PREF_MAX = 4.0                    # Ab hier Score 0

# Tier-Schwellen
TIER_STRONG_PICK = 70   # Score >= 70 → Strong Pick
TIER_VALUE_BET   = 45   # Score >= 45 → Value Bet
# Score < 45 → Watch (nur beobachten)

# ═══════════════════════════════════════════════════════════════════════════════
# SAFETY FILTERS
# ═══════════════════════════════════════════════════════════════════════════════

MAX_MODEL_MARKET_GAP = 0.10           # Max model_prob - consensus_prob bevor auto-Watch
EDGE_SKEPTICISM_THRESHOLD = 8.0       # Edge% ab dem Skeptizismus greift
ODDS_SKEPTICISM_THRESHOLD = 2.80      # Nur bei Odds ueber diesem Wert
MODEL_TRUST_MIN_BETS = 20             # Min. Bets bevor Win-Rate-Adjustment greift
MODEL_TRUST_EXPECTED_WIN_RATE = 0.40  # Win-Rate bei der Trust = 1.0
MODEL_TRUST_FLOOR = 0.05             # Minimaler Trust-Multiplikator

# ═══════════════════════════════════════════════════════════════════════════════
# HARD FILTERS (basierend auf Performance-Analyse)
# ═══════════════════════════════════════════════════════════════════════════════

MAX_EDGE_HARD_CAP = 99.0              # Deaktiviert — wird am 2026-03-30 mit Daten neu bewertet
MAX_ODDS_SELECTED = 99.0              # Deaktiviert — wird am 2026-03-30 mit Daten neu bewertet
OU_BONUS_POINTS = 12.0                # O/U-Wetten: +12 Punkte Confidence Bonus (41.7% Win-Rate)
PENALTY_1X2_POINTS = 4.0              # 1X2-Wetten: -4 Punkte (Draws ausgenommen, siehe bet_selector)

# Liga-spezifische Mindest-Edge (aktuell leer — zu wenig Daten für Ausschlüsse)
# Wird am 2026-03-30 mit mehr Backtesting-Daten neu bewertet
LEAGUE_MIN_EDGE = {}

# Tennis: Am 2026-03-30 mit mehr Daten neu bewerten
TENNIS_ENABLED = True

# ═══════════════════════════════════════════════════════════════════════════════
# KOMBINIERTE LABELS
# ═══════════════════════════════════════════════════════════════════════════════

ALL_LABELS = {**SPORT_LABELS, **UEFA_LABELS}


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════

def load_credentials(path: Path | None = None) -> dict:
    """Liest KEY=VALUE Credentials aus einer Datei (Standard: ~/.stock_scanner_credentials)."""
    cred_file = path or CREDS_FILE
    if not cred_file.exists():
        print(f"Fehler: Credentials-Datei fehlt: {cred_file}", file=sys.stderr)
        return {}
    creds = {}
    try:
        with open(cred_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except Exception as e:
        print(f"Fehler: Credentials laden fehlgeschlagen: {e}", file=sys.stderr)
    return creds
