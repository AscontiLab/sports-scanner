#!/usr/bin/env python3
"""
Tennis Elo-Modell
─────────────────
Berechnet Elo-Ratings aus historischen ATP/WTA-Matches.
Nutzt Sackmann-Daten (bis 2024) + tennis-data.co.uk (2025+).
"""

import difflib
import pandas as pd
from datetime import datetime

from config import ELO_K_FACTOR, ELO_INITIAL
from datasources.sackmann import download_atp_year as sackmann_atp, download_wta_year as sackmann_wta
from datasources.sackmann import _MAX_AVAILABLE_YEAR as SACKMANN_MAX_YEAR

# tennis-data.co.uk als Ergaenzung fuer neuere Daten
try:
    from datasources.tennis_data_co_uk import download_atp_year as tdcouk_atp, download_wta_year as tdcouk_wta
    _TDCOUK_AVAILABLE = True
except ImportError:
    _TDCOUK_AVAILABLE = False


def get_elo_years(now: datetime | None = None, span: int = 4) -> list[int]:
    if now is None:
        now = datetime.now()
    return list(range(now.year - (span - 1), now.year + 1))


def _combined_download(sackmann_fn, tdcouk_fn, year: int) -> pd.DataFrame | None:
    """
    Laedt Daten fuer ein Jahr: Sackmann bis 2024, tennis-data.co.uk ab 2025.
    Fuer 2024 werden beide Quellen geladen (Sackmann hat vollstaendige Daten,
    tennis-data.co.uk startet Ende Dezember 2024).
    """
    frames = []
    # Sackmann-Daten (bis _MAX_AVAILABLE_YEAR)
    if year <= SACKMANN_MAX_YEAR:
        df = sackmann_fn(year)
        if df is not None and not df.empty:
            frames.append(df)

    # tennis-data.co.uk ab 2025 (oder falls Sackmann nichts liefert)
    if year > SACKMANN_MAX_YEAR and _TDCOUK_AVAILABLE and tdcouk_fn is not None:
        df = tdcouk_fn(year)
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    return pd.concat(frames, ignore_index=True)


def download_atp_year(year: int) -> pd.DataFrame | None:
    """Laedt ATP-Daten: Sackmann bis 2024, tennis-data.co.uk ab 2025."""
    tdcouk_fn = tdcouk_atp if _TDCOUK_AVAILABLE else None
    return _combined_download(sackmann_atp, tdcouk_fn, year)


def download_wta_year(year: int) -> pd.DataFrame | None:
    """Laedt WTA-Daten: Sackmann bis 2024, tennis-data.co.uk ab 2025."""
    tdcouk_fn = tdcouk_wta if _TDCOUK_AVAILABLE else None
    return _combined_download(sackmann_wta, tdcouk_fn, year)


def _build_name_map(frames: list[pd.DataFrame]) -> dict[str, str]:
    """
    Baut ein Name-Mapping von tennis-data.co.uk-Format ("Last F.") auf
    Sackmann-Format ("First Last"), damit Elo-Ratings ueber Datenquellen
    hinweg konsistent bleiben.
    """
    # Sammle alle Sackmann-Namen (enthalten Leerzeichen und kein Punkt am Ende)
    sackmann_names = set()
    for df in frames:
        for col in ["winner_name", "loser_name"]:
            if col in df.columns:
                for name in df[col].dropna().unique():
                    name = str(name).strip()
                    # Sackmann-Format: "First Last" (kein Punkt, mind. 2 Teile)
                    if name and not name.endswith(".") and " " in name:
                        sackmann_names.add(name)

    # Baue Mapping: "Last F." → "First Last"
    # Beruecksichtigt auch mehrteilige Nachnamen ("De Minaur", "Auger-Aliassime")
    # und Bindestrich-Varianten ("Auger Aliassime" vs "Auger-Aliassime")
    name_map = {}
    for full_name in sackmann_names:
        parts = full_name.split()
        if len(parts) >= 2:
            first = parts[0]
            initial = first[0] + "."
            # Einfacher Fall: "Jannik Sinner" → "Sinner J."
            last = parts[-1]
            short = f"{last} {initial}"
            if short not in name_map:
                name_map[short] = full_name
            # Mehrteiliger Nachname: "Alex De Minaur" → "De Minaur A."
            if len(parts) > 2:
                multi_last = " ".join(parts[1:])
                short_multi = f"{multi_last} {initial}"
                if short_multi not in name_map:
                    name_map[short_multi] = full_name
                # Bindestrich-Variante: "Auger Aliassime" → "Auger-Aliassime F."
                hyphen_last = "-".join(parts[1:])
                short_hyphen = f"{hyphen_last} {initial}"
                if short_hyphen not in name_map:
                    name_map[short_hyphen] = full_name
    return name_map


def _normalize_names(df: pd.DataFrame, name_map: dict[str, str]) -> pd.DataFrame:
    """Ersetzt abgekuerzte Namen durch vollstaendige Sackmann-Namen."""
    if not name_map:
        return df
    for col in ["winner_name", "loser_name"]:
        if col in df.columns:
            df[col] = df[col].map(lambda n: name_map.get(str(n).strip(), n))
    return df


def _compute_elo(download_fn, years: list, label: str) -> tuple[dict, dict, int, datetime | None]:
    """
    Berechnet Elo-Ratings aus historischen Tennis-Matches.
    Gibt (gesamt_elo, surface_elo, training_matches, max_date) zurueck.
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
        return {}, surface_elo, 0, None

    # Name-Mapping: tennis-data.co.uk "Last F." → Sackmann "First Last"
    name_map = _build_name_map(all_frames)
    if name_map:
        all_frames = [_normalize_names(df.copy(), name_map) for df in all_frames]
        print(f"    Name-Mapping: {len(name_map)} Spieler normalisiert ({label})")

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

    # Neuestes Turnierdatum ermitteln
    max_date = None
    if "tourney_date" in combined.columns:
        valid = combined["tourney_date"].dropna()
        if len(valid) > 0:
            max_val = int(valid.max())
            try:
                max_date = datetime.strptime(str(max_val), "%Y%m%d")
            except Exception:
                pass

    return elo, surface_elo, int(len(combined)), max_date


def compute_tennis_elo(years: list) -> tuple[dict, dict, int, datetime | None]:
    """Berechnet Elo-Ratings aus historischen ATP-Matches."""
    return _compute_elo(download_atp_year, years, "ATP")


def compute_wta_elo(years: list) -> tuple[dict, dict, int, datetime | None]:
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
