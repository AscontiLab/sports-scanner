#!/usr/bin/env python3
"""
football-data.co.uk Datenquellen
────────────────────────────────
Laedt historische Fussball-Ergebnisse fuer Poisson-Modell-Training.
"""

import pandas as pd
from io import StringIO
from datetime import datetime

from config import FDCO_LEAGUES

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


def current_season_codes(now: datetime | None = None) -> list[str]:
    """
    Liefert Saison-Codes wie ['2526','2425'] (aktuell + Vorjahr).
    Annahme: Saison startet ab Juli.
    """
    if now is None:
        now = datetime.now()
    year = now.year
    if now.month < 7:
        start = year - 1
    else:
        start = year
    codes = []
    for s in [start, start - 1]:
        codes.append(f"{str(s)[-2:]}{str(s+1)[-2:]}")
    return codes


def build_fdco_urls(league_code: str, season_codes: list[str]) -> list[str]:
    return [
        f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
        for season in season_codes
    ]


def download_fdco(url: str) -> pd.DataFrame | None:
    try:
        r = _request_with_retry(url, timeout=30)
        df = pd.read_csv(StringIO(r.text), encoding="latin-1")
        return df
    except Exception as e:
        print(f"    Warning: {url}: {e}")
        return None


def standardize_fdco(df: pd.DataFrame, is_new_format: bool = False) -> pd.DataFrame | None:
    """Einheitliche Spalten: HomeTeam, AwayTeam, FTHG, FTAG (+ Date wenn vorhanden)."""
    needed = ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    for col in needed:
        if col not in df.columns:
            print(f"    Warning: Spalte '{col}' fehlt")
            return None
    keep = needed + (["Date"] if "Date" in df.columns else [])
    df = df[keep].copy()
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"]  = pd.to_numeric(df["FTAG"],  errors="coerce")
    df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["HomeTeam"] = df["HomeTeam"].str.strip()
    df["AwayTeam"] = df["AwayTeam"].str.strip()
    return df


def load_football_data(sport_key: str) -> pd.DataFrame | None:
    """Laedt historische Fussballdaten fuer eine Liga (via fdco oder OpenLigaDB fuer 3. Liga)."""
    if sport_key == "soccer_germany_liga3":
        from datasources.openligadb import load_liga3_data
        return load_liga3_data()

    season_codes = current_season_codes()
    league_code = FDCO_LEAGUES.get(sport_key)
    urls = build_fdco_urls(league_code, season_codes) if league_code else []
    frames = []
    for url in urls:
        df_raw = download_fdco(url)
        if df_raw is not None:
            df = standardize_fdco(df_raw, is_new_format=False)
            if df is not None and len(df) > 5:
                frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)


def load_european_data() -> pd.DataFrame | None:
    """
    Laedt Matchdaten aus Top-5-Ligen + Bundesliga 1+2 fuer das europaeische
    Poisson-Modell (O/U bei UEFA-Wettbewerben).
    """
    from config import EUROPEAN_FDCO_LEAGUES
    frames = []
    season_codes = current_season_codes()
    for league_code in EUROPEAN_FDCO_LEAGUES:
        for url in build_fdco_urls(league_code, season_codes):
            df_raw = download_fdco(url)
            if df_raw is not None:
                df = standardize_fdco(df_raw)
                if df is not None and len(df) > 5:
                    frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)
