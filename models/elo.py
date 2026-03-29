#!/usr/bin/env python3
"""
Tennis Elo-Modell
─────────────────
Berechnet Elo-Ratings aus historischen ATP/WTA-Matches (Jeff Sackmann Daten).
"""

import difflib
import pandas as pd
from datetime import datetime

from config import ELO_K_FACTOR, ELO_INITIAL
from datasources.sackmann import download_atp_year, download_wta_year


def get_elo_years(now: datetime | None = None, span: int = 4) -> list[int]:
    if now is None:
        now = datetime.now()
    return list(range(now.year - (span - 1), now.year + 1))


def _compute_elo(download_fn, years: list, label: str) -> tuple[dict, dict, int]:
    """
    Berechnet Elo-Ratings aus historischen Tennis-Matches.
    Gibt (gesamt_elo, surface_elo, training_matches) zurueck.
    surface_elo = {"Hard": {name: elo}, "Clay": {...}, "Grass": {...}}
    """
    from concurrent.futures import ThreadPoolExecutor
    elo = {}
    surface_elo = {"Hard": {}, "Clay": {}, "Grass": {}}
    all_frames = []
    with ThreadPoolExecutor(max_workers=len(years)) as pool:
        results = list(pool.map(download_fn, years))
    for df in results:
        if df is not None and "winner_name" in df.columns:
            all_frames.append(df)
    if not all_frames:
        print(f"    Warning: Keine {label}-Daten geladen")
        return {}, surface_elo, 0

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.dropna(subset=["winner_name", "loser_name"])
    if "tourney_date" in combined.columns:
        combined["tourney_date"] = pd.to_numeric(combined["tourney_date"], errors="coerce")
        combined = combined.sort_values("tourney_date")

    def expected(ra, rb):
        return 1 / (1 + 10 ** ((rb - ra) / 400))

    has_surface = "surface" in combined.columns
    for row in combined.itertuples(index=False):
        w = str(row.winner_name).strip()
        l = str(row.loser_name).strip()
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
        raw_surface = getattr(row, "surface", None) if has_surface else None
        surface = str(raw_surface).strip().capitalize() if pd.notna(raw_surface) else None
        if surface in surface_elo:
            s_elo = surface_elo[surface]
            s_elo.setdefault(w, ELO_INITIAL)
            s_elo.setdefault(l, ELO_INITIAL)
            se_w = expected(s_elo[w], s_elo[l])
            se_l = 1 - se_w
            s_elo[w] += ELO_K_FACTOR * (1 - se_w)
            s_elo[l] += ELO_K_FACTOR * (0 - se_l)

    return elo, surface_elo, int(len(combined))


def compute_tennis_elo(years: list) -> tuple[dict, dict, int]:
    """Berechnet Elo-Ratings aus historischen ATP-Matches."""
    return _compute_elo(download_atp_year, years, "ATP")


def compute_wta_elo(years: list) -> tuple[dict, dict, int]:
    """Berechnet Elo-Ratings aus historischen WTA-Matches."""
    return _compute_elo(download_wta_year, years, "WTA")


def predict_tennis_win_prob(elo_a: float, elo_b: float) -> float:
    """P(Spieler A schlaegt Spieler B)."""
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
