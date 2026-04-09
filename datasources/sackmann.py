#!/usr/bin/env python3
"""
Jeff Sackmann Tennis-Daten
──────────────────────────
Laedt historische ATP- und WTA-Matchdaten von GitHub.
"""

import pandas as pd
from io import StringIO

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


ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"

# Surface-Mapping fuer Odds API Turniere → Belag
TOURNAMENT_SURFACE = {
    "Australian Open": "Hard", "US Open": "Hard",
    "French Open": "Clay", "Roland Garros": "Clay",
    "Wimbledon": "Grass",
    "Indian Wells": "Hard", "Miami Open": "Hard",
    "Monte Carlo": "Clay", "Monte-Carlo": "Clay",
    "Madrid": "Clay", "Rome": "Clay", "Roma": "Clay",
    "Barcelona": "Clay", "Hamburg": "Clay",
    "Cincinnati": "Hard", "Shanghai": "Hard",
    "Canada": "Hard", "Montreal": "Hard", "Toronto": "Hard",
    "Dubai": "Hard", "Doha": "Hard", "Brisbane": "Hard",
    "Halle": "Grass", "Queen's": "Grass", "Stuttgart": "Clay",
    "Basel": "Hard", "Vienna": "Hard", "ATP Finals": "Hard",
    "WTA Finals": "Hard",
}


def detect_surface(tournament_name: str) -> str | None:
    """Erkennt den Belag anhand des Turniernamens."""
    name_lower = tournament_name.lower()
    for key, surface in TOURNAMENT_SURFACE.items():
        if key.lower() in name_lower:
            return surface
    return None


_MAX_AVAILABLE_YEAR = 2024  # Jeff Sackmann Repo geht aktuell nur bis 2024


def download_atp_year(year: int) -> pd.DataFrame | None:
    if year > _MAX_AVAILABLE_YEAR:
        return None
    url = f"{ATP_BASE}/atp_matches_{year}.csv"
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        return df
    except Exception as e:
        print(f"    Warning: ATP {year}: {e}")
        return None


def download_wta_year(year: int) -> pd.DataFrame | None:
    if year > _MAX_AVAILABLE_YEAR:
        return None
    url = f"{WTA_BASE}/wta_matches_{year}.csv"
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        return df
    except Exception as e:
        print(f"    Warning: WTA {year}: {e}")
        return None
