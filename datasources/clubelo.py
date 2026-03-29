#!/usr/bin/env python3
"""
Club Elo API Datenquelle
────────────────────────
Laedt Club-Elo-Ratings von api.clubelo.com.
"""

import pandas as pd
from io import StringIO
from datetime import datetime, timezone, timedelta

from config import CLUBELO_URL_HTTPS, CLUBELO_URL_HTTP

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


def download_clubelo(date: str) -> dict:
    """
    Laedt Club-Elo-Ratings fuer ein Datum (Format: YYYY-MM-DD).
    Gibt {club_name: elo_rating} zurueck.
    """
    last_err = None
    for url in [CLUBELO_URL_HTTPS.format(date=date), CLUBELO_URL_HTTP.format(date=date)]:
        try:
            r = _request_with_retry(url, timeout=20)
            df = pd.read_csv(StringIO(r.text))
            result = {}
            for _, row in df.iterrows():
                club = row.get("Club")
                elo  = row.get("Elo")
                if pd.notna(club) and pd.notna(elo):
                    result[str(club).strip()] = float(elo)
            return result
        except Exception as e:
            last_err = e
            continue
    print(f"    Warning: Club-Elo ({date}): {last_err}")
    return {}


def download_clubelo_with_fallback(max_days_back: int = 3) -> tuple[str, dict]:
    """
    Versucht Club-Elo fuer heute, dann bis max_days_back Tage zurueck.
    Gibt (date_str, elo_dict) zurueck. date_str leer bei Fehlschlag.
    """
    today = datetime.now(timezone.utc).date()
    for i in range(max_days_back + 1):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        elo_dict = download_clubelo(date_str)
        if elo_dict:
            return date_str, elo_dict
    return "", {}
