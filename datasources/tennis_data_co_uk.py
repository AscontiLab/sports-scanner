#!/usr/bin/env python3
"""
Tennis-Data.co.uk Datenquelle
─────────────────────────────
Laedt historische ATP- und WTA-Matchdaten von tennis-data.co.uk (xlsx).
Ergaenzt die Sackmann-Daten ab 2025+.

Spalten-Mapping:
  tennis-data.co.uk   →  Sackmann-kompatibel
  Winner              →  winner_name
  Loser               →  loser_name
  Date                →  tourney_date (YYYYMMDD int)
  Surface             →  surface
"""

import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    from scanner_common.retry import request_with_retry as _sc_retry
    import requests

    def _request_with_retry(url: str, params: dict | None = None,
                            retries: int = 3, backoff: list | None = None,
                            timeout: int = 30, **kwargs) -> requests.Response:
        return _sc_retry(url, method="GET", retries=retries, backoff=backoff,
                         timeout=timeout, params=params, **kwargs)
except ImportError:
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


# Basis-URLs (HTTP, da HTTPS SSL-Probleme auf einigen Servern)
_ATP_URL = "http://www.tennis-data.co.uk/{year}/{year}.xlsx"
_WTA_URL = "http://www.tennis-data.co.uk/{year}w/{year}.xlsx"

# Cache-Verzeichnis
_CACHE_DIR = Path(__file__).parent.parent / "cache"

# Cache-Gueltigkeitsdauer in Sekunden (6 Stunden fuer laufendes Jahr, 30 Tage fuer abgeschlossene)
_CACHE_TTL_CURRENT = 6 * 3600
_CACHE_TTL_PAST = 30 * 24 * 3600


def _cache_path(tour: str, year: int) -> Path:
    """Cache-Dateipfad fuer heruntergeladene CSV-Daten."""
    return _CACHE_DIR / f"tennis_data_{tour}_{year}.csv"


def _cache_is_valid(path: Path, year: int) -> bool:
    """Prueft ob der Cache noch gueltig ist."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    # Laufendes Jahr: 6h Cache, abgeschlossene Jahre: 30 Tage
    ttl = _CACHE_TTL_CURRENT if year >= datetime.now().year else _CACHE_TTL_PAST
    return age < ttl


def _download_and_cache(url: str, cache_path: Path, year: int) -> pd.DataFrame | None:
    """Laedt xlsx von tennis-data.co.uk, konvertiert zu DataFrame, cached als CSV."""
    try:
        r = _request_with_retry(url, timeout=60)
        # xlsx in DataFrame einlesen
        from io import BytesIO
        try:
            df = pd.read_excel(BytesIO(r.content), engine="openpyxl")
        except ImportError:
            print("    Warning: openpyxl nicht installiert – tennis-data.co.uk nicht verfuegbar")
            return None

        if df.empty:
            return None

        # Spalten umbenennen fuer Sackmann-Kompatibilitaet
        rename_map = {
            "Winner": "winner_name",
            "Loser": "loser_name",
            "Surface": "surface",
        }
        df = df.rename(columns=rename_map)

        # Date → tourney_date (YYYYMMDD int)
        if "Date" in df.columns:
            df["tourney_date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y%m%d")
            df["tourney_date"] = pd.to_numeric(df["tourney_date"], errors="coerce")
            df = df.drop(columns=["Date"], errors="ignore")

        # Cache als CSV speichern (schnelleres Laden beim naechsten Mal)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path, index=False)
        return df

    except Exception as e:
        print(f"    Warning: tennis-data.co.uk {year}: {e}")
        return None


def _load_from_cache_or_download(tour: str, year: int) -> pd.DataFrame | None:
    """Laedt Daten aus Cache oder downloaded neu."""
    cache = _cache_path(tour, year)

    # Aus Cache laden falls gueltig
    if _cache_is_valid(cache, year):
        try:
            df = pd.read_csv(cache, low_memory=False)
            if not df.empty:
                return df
        except Exception:
            pass  # Cache korrupt → neu laden

    # Download
    url = _ATP_URL.format(year=year) if tour == "atp" else _WTA_URL.format(year=year)
    return _download_and_cache(url, cache, year)


def download_atp_year(year: int) -> pd.DataFrame | None:
    """Laedt ATP-Daten fuer ein Jahr von tennis-data.co.uk."""
    return _load_from_cache_or_download("atp", year)


def download_wta_year(year: int) -> pd.DataFrame | None:
    """Laedt WTA-Daten fuer ein Jahr von tennis-data.co.uk."""
    return _load_from_cache_or_download("wta", year)


def available_years() -> list[int]:
    """Gibt die verfuegbaren Jahre zurueck (2001 bis aktuelles Jahr)."""
    return list(range(2001, datetime.now().year + 1))
