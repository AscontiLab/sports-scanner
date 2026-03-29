#!/usr/bin/env python3
"""
Team-Namen-Matching
───────────────────
Fuzzy-Matching fuer Fussball-Teams und Club-Elo-Zuordnung.
"""

import difflib


def normalize_name(name: str) -> str:
    """Vereinheitlicht Sonderzeichen und Fuellwoerter."""
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
