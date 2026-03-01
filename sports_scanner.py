#!/usr/bin/env python3
"""
Sports Betting Value Scanner
────────────────────────────
Analysiert Fußball (1./2./3. Bundesliga) mit Poisson-Modell
und Tennis (ATP) mit Elo-Modell.

Datenquellen:
  - Fußball-History: football-data.co.uk
  - Tennis-History:  Jeff Sackmann / tennis_atp (GitHub)
  - Live-Odds:       The Odds API (v4)
"""

import sys
import json
import math
import difflib
import warnings
import requests
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from scipy.stats import poisson
from scipy.optimize import minimize

warnings.filterwarnings("ignore")

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR  = SCRIPT_DIR / "output"
CREDS_FILE  = Path.home() / ".stock_scanner_credentials"

# ═══════════════════════════════════════════════════════════════════════════════
# KONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

FOOTBALL_SPORTS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_germany_liga3",
]

SPORT_LABELS = {
    "soccer_germany_bundesliga":  "1. Bundesliga",
    "soccer_germany_bundesliga2": "2. Bundesliga",
    "soccer_germany_liga3":       "3. Liga",
    "soccer_germany_dfb_pokal":   "DFB-Pokal",
}

# football-data.co.uk URLs (D1/D2 = Standardformat)
FDCO_URLS = {
    "soccer_germany_bundesliga":  [
        "https://www.football-data.co.uk/mmz4281/2526/D1.csv",
        "https://www.football-data.co.uk/mmz4281/2425/D1.csv",
    ],
    "soccer_germany_bundesliga2": [
        "https://www.football-data.co.uk/mmz4281/2526/D2.csv",
        "https://www.football-data.co.uk/mmz4281/2425/D2.csv",
    ],
    # 3. Liga kommt von OpenLigaDB (s. load_liga3_data)
}

# OpenLigaDB API für 3. Liga
OPENLIGADB_BASE = "https://api.openligadb.de"

# Schwellwerte Value Betting
MIN_EDGE_PCT = 3.0   # Mindest-Edge in %
MIN_ODDS     = 1.25  # Mindest-Quoten
MAX_KELLY    = 0.05  # Max. Kelly-Anteil (5 % des Bankrolls)

# Tennis Elo-Einstellungen
ELO_K_FACTOR  = 32
ELO_INITIAL   = 1500
ELO_YEARS     = [2022, 2023, 2024, 2025]   # ATP-Datenjahre


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════

def load_creds() -> dict:
    creds = {}
    with open(CREDS_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds


# ═══════════════════════════════════════════════════════════════════════════════
# THE ODDS API
# ═══════════════════════════════════════════════════════════════════════════════

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def get_active_sports(api_key: str) -> list:
    r = requests.get(f"{ODDS_API_BASE}/sports",
                     params={"apiKey": api_key}, timeout=15)
    r.raise_for_status()
    return r.json()


def get_odds(api_key: str, sport_key: str) -> list:
    params = {
        "apiKey":     api_key,
        "regions":    "eu",
        "markets":    "h2h,totals",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    r = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                     params=params, timeout=20)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining", "?")
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


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL: DATEN LADEN
# ═══════════════════════════════════════════════════════════════════════════════

def download_fdco(url: str) -> pd.DataFrame | None:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), encoding="latin-1")
        return df
    except Exception as e:
        print(f"    Warning: {url}: {e}")
        return None


def standardize_fdco(df: pd.DataFrame, is_new_format: bool = False) -> pd.DataFrame | None:
    """Einheitliche Spalten: HomeTeam, AwayTeam, FTHG, FTAG."""
    needed = ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    for col in needed:
        if col not in df.columns:
            print(f"    Warning: Spalte '{col}' fehlt")
            return None
    df = df[needed].copy()
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"]  = pd.to_numeric(df["FTAG"],  errors="coerce")
    df = df.dropna()
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
            r = requests.get(url, timeout=30)
            r.raise_for_status()
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
                })
        if rows:
            frames.append(pd.DataFrame(rows))
            print(f"    OpenLigaDB Saison {season}: {len(rows)} Matches")

    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    )


def load_football_data(sport_key: str) -> pd.DataFrame | None:
    if sport_key == "soccer_germany_liga3":
        return load_liga3_data()

    urls = FDCO_URLS.get(sport_key, [])
    frames = []
    for url in urls:
        df_raw = download_fdco(url)
        if df_raw is not None:
            df = standardize_fdco(df_raw, is_new_format=False)
            if df is not None and len(df) > 5:
                frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    )
    return combined


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTBALL: POISSON-MODELL (Dixon-Coles-Stil)
# ═══════════════════════════════════════════════════════════════════════════════

def fit_poisson_model(df: pd.DataFrame) -> dict:
    """
    Passt Attack/Defense-Parameter per Maximum-Likelihood an.
    log(λ_heim) = home_adv + attack[heim] – defense[gast]
    log(λ_gast) = attack[gast]            – defense[heim]
    """
    teams    = sorted(set(df["HomeTeam"]) | set(df["AwayTeam"]))
    n_teams  = len(teams)
    idx      = {t: i for i, t in enumerate(teams)}

    ht = df["HomeTeam"].map(idx).values
    at = df["AwayTeam"].map(idx).values
    hg = df["FTHG"].values.astype(float)
    ag = df["FTAG"].values.astype(float)

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
        return -np.sum(ll)

    constraints = [{"type": "eq", "fun": lambda x: x[0]}]
    res = minimize(neg_ll, x0, method="SLSQP",
                   constraints=constraints,
                   options={"maxiter": 2000, "ftol": 1e-9})

    x   = res.x
    return {
        "attack":   {t: x[i]           for t, i in idx.items()},
        "defense":  {t: x[n_teams + i] for t, i in idx.items()},
        "home_adv": float(x[-1]),
        "teams":    teams,
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


# ═══════════════════════════════════════════════════════════════════════════════
# TENNIS: ELO-MODELL (Jeff Sackmann ATP-Daten)
# ═══════════════════════════════════════════════════════════════════════════════

ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"


def download_atp_year(year: int) -> pd.DataFrame | None:
    url = f"{ATP_BASE}/atp_matches_{year}.csv"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        return df
    except Exception as e:
        print(f"    Warning: ATP {year}: {e}")
        return None


def compute_tennis_elo(years: list) -> dict:
    """
    Berechnet Elo-Ratings aus historischen ATP-Matches.
    Gibt dict name→elo zurück.
    """
    elo = {}
    all_frames = []
    for year in years:
        df = download_atp_year(year)
        if df is not None and "winner_name" in df.columns:
            all_frames.append(df)
    if not all_frames:
        print("    Warning: Keine ATP-Daten geladen")
        return {}

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.dropna(subset=["winner_name", "loser_name"])
    # Chronologisch sortieren
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

    return elo


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
        if edge >= MIN_EDGE_PCT / 100:
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
            })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# TENNIS ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_tennis_match(match: dict, tournament: str, elo_dict: dict) -> list:
    p1 = match["home_team"]
    p2 = match["away_team"]

    elo1 = find_player_elo(p1, elo_dict)
    elo2 = find_player_elo(p2, elo_dict)

    best = best_odds_from_match(match)
    bets = []

    if elo1 is not None and elo2 is not None:
        # Elo-basiertes Modell
        prob1 = predict_tennis_win_prob(elo1, elo2)
        prob2 = 1 - prob1
        model_source = "Elo"
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
        if edge >= MIN_EDGE_PCT / 100:
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
            })
    return bets


# ═══════════════════════════════════════════════════════════════════════════════
# HTML-REPORT
# ═══════════════════════════════════════════════════════════════════════════════

CSS = """
body { font-family:'Segoe UI',Arial,sans-serif; background:#ffffff; color:#1a1a2e; margin:0; padding:20px; }
h1   { color:#1a56a0; border-bottom:2px solid #dde3ed; padding-bottom:10px; font-size:1.6em; }
h2   { color:#c05a00; margin-top:32px; font-size:1.15em; }
.summary { display:flex; gap:16px; flex-wrap:wrap; margin:18px 0 24px; }
.card { background:#f0f5ff; border:1px solid #c8d8f0; border-radius:8px;
        padding:14px 22px; min-width:130px; }
.card .val { font-size:1.9em; font-weight:700; color:#1a56a0; }
.card .lbl { color:#555577; font-size:0.8em; margin-top:2px; }
table { width:100%; border-collapse:collapse; background:#ffffff;
        border:1px solid #dde3ed; border-radius:8px; overflow:hidden; margin:14px 0; }
th  { background:#eef2fa; padding:9px 12px; text-align:left;
      color:#444466; font-size:0.82em; border-bottom:1px solid #dde3ed; }
td  { padding:8px 12px; border-bottom:1px solid #eef2fa; font-size:0.88em; color:#1a1a2e; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:#f5f8ff; }
.g  { color:#1a7a30; font-weight:700; }
.y  { color:#a06000; font-weight:700; }
.o  { color:#b54000; font-weight:700; }
.tag{ background:#1a56a0; color:#fff; border-radius:4px;
      padding:2px 7px; font-size:0.75em; }
.tag2{ background:#e0ebff; color:#1a56a0; border:1px solid #b0ccee;
       border-radius:4px; padding:2px 7px; font-size:0.75em; }
.empty{ color:#777799; padding:18px; text-align:center;
        background:#f7f9ff; border:1px solid #dde3ed; border-radius:8px; }
.note { background:#fff8e8; border-left:3px solid #e09000; padding:10px 14px;
        color:#664400; font-size:0.82em; border-radius:0 6px 6px 0; margin:10px 0; }
.footer{ color:#777799; font-size:0.78em; margin-top:30px;
         border-top:1px solid #dde3ed; padding-top:14px; }
"""

def edge_class(e: float) -> str:
    if e >= 10: return "g"
    if e >= 5:  return "o"
    return "y"


def build_football_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine Football-Value-Bets gefunden – Modell benötigt ausreichend historische Matches für alle Teams.</div>'
    headers = ["Liga", "Spiel", "Tipp", "Anstoß", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "λ Heim", "λ Gast"]
    rows = ""
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        tag   = SPORT_LABELS.get(b["sport"], b["sport"])
        ec    = edge_class(b["edge_pct"])
        rows += f"""<tr>
          <td><span class="tag">{tag}</span></td>
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
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{ths}</tr>{rows}</table>"


def build_tennis_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine aktiven Tennis-Turniere mit ausreichend Odds gefunden.</div>'
    headers = ["Turnier", "Spiel", "Tipp", "Zeitpunkt", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Elo", "Modell"]
    rows = ""
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        ec   = edge_class(b["edge_pct"])
        elo  = str(b["elo"]) if b["elo"] else "–"
        rows += f"""<tr>
          <td><span class="tag2">{b['tournament']}</span></td>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{elo}</td>
          <td style="color:#8b949e">{b['model_source']}</td>
        </tr>"""
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{ths}</tr>{rows}</table>"


def generate_html(football_bets: list, tennis_bets: list) -> str:
    date_str  = datetime.now().strftime("%d.%m.%Y")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    total     = len(football_bets) + len(tennis_bets)
    all_edges = [b["edge_pct"] for b in football_bets + tennis_bets]
    max_edge  = max(all_edges) if all_edges else 0.0

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Sports Value Scanner {date_str}</title>
<style>{CSS}</style>
</head>
<body>
<h1>📊 Sports Value Scanner — {date_str}</h1>

<div class="summary">
  <div class="card"><div class="val">{total}</div><div class="lbl">Value Bets gesamt</div></div>
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball</div></div>
  <div class="card"><div class="val">{len(tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
  <div class="card"><div class="val">{max_edge:.1f}%</div><div class="lbl">Max. Edge</div></div>
</div>

<div class="note">
  📌 <strong>Hinweis:</strong> Edge = (Modell-Wahrscheinlichkeit × Beste Quote) – 1.
  Nur Bets mit Edge ≥ {MIN_EDGE_PCT}% und Quote ≥ {MIN_ODDS} werden angezeigt.
  Kelly-Empfehlung maximal {MAX_KELLY*100:.0f}% des Bankrolls.
</div>

<h2>⚽ Fußball Value Bets (Poisson-Modell)</h2>
{build_football_table(football_bets)}

<h2>🎾 Tennis Value Bets (Elo-Modell)</h2>
{build_tennis_table(tennis_bets)}

<div class="footer">
  Generiert: {timestamp} &nbsp;|&nbsp;
  Fußball-Modell: Poisson MLE (football-data.co.uk) &nbsp;|&nbsp;
  Tennis-Modell: Elo (Jeff Sackmann ATP Data) &nbsp;|&nbsp;
  Odds: The Odds API<br>
  ⚠️ Diese Analyse dient ausschließlich zu Informationszwecken.
  Sportwetten sind mit erheblichen Verlustrisiken verbunden.
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 60)
    print(f"  Sports Value Scanner — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("=" * 60)

    creds = load_creds()
    api_key = creds.get("ODDS_API_KEY", "")
    if not api_key:
        print("ERROR: ODDS_API_KEY fehlt in ~/.stock_scanner_credentials")
        return 1

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_dir  = OUTPUT_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    all_football_bets: list = []
    all_tennis_bets:   list = []

    # ── FUSSBALL ────────────────────────────────────────────────────────────
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
            print(f"    OK. Home-Vorteil={model['home_adv']:.3f}")
        except Exception as e:
            print(f"    Modell-Fehler: {e}")

    print("\n[⚽ Fußball] Upcoming Matches via Odds API …")
    for sport_key in FOOTBALL_SPORTS:
        label = SPORT_LABELS.get(sport_key, sport_key)
        print(f"  {label}:")
        try:
            matches = get_odds(api_key, sport_key)
        except Exception as e:
            print(f"    Fehler: {e}")
            continue

        model = football_models.get(sport_key)
        if model is None:
            print(f"    Kein Modell – Odds werden ignoriert")
            continue

        for match in matches:
            bets = analyze_football_match(match, model)
            if bets:
                all_football_bets.extend(bets)
                for b in bets:
                    print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

    # ── TENNIS ──────────────────────────────────────────────────────────────
    print("\n[🎾 Tennis] Elo-Ratings berechnen …")
    elo_dict = compute_tennis_elo(ELO_YEARS)
    print(f"  {len(elo_dict)} Spieler im Elo-Dict")

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
            matches = get_odds(api_key, sport_key)
        except Exception as e:
            print(f"    Fehler: {e}")
            continue
        for match in matches:
            bets = analyze_tennis_match(match, title, elo_dict)
            if bets:
                all_tennis_bets.extend(bets)
                for b in bets:
                    print(f"    ✓ VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")

    # ── REPORT ──────────────────────────────────────────────────────────────
    print(f"\n[📊 Report] Football Bets: {len(all_football_bets)}")
    print(f"[📊 Report] Tennis Bets:   {len(all_tennis_bets)}")

    html      = generate_html(all_football_bets, all_tennis_bets)
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
            "λ-Heim":     f"{b['lam_home']:.2f}",
            "λ-Gast":     f"{b['lam_away']:.2f}",
        })
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
            "Elo":        str(b["elo"]) if b["elo"] else "",
        })

    if rows:
        csv_path = out_dir / "sports_signals.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        print(f"[📊 Report] CSV:  {csv_path}")

    print("\n✓ Fertig!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
