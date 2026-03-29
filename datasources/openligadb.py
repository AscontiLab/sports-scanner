#!/usr/bin/env python3
"""
OpenLigaDB Datenquelle
──────────────────────
Laedt 3. Liga Matchdaten von api.openligadb.de.
"""

import pandas as pd
from datetime import datetime

from config import OPENLIGADB_BASE

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


def load_liga3_data() -> pd.DataFrame | None:
    """Laedt 3. Liga Matchdaten von OpenLigaDB (laufende + vergangene Saison)."""
    frames = []
    current_year = datetime.now().year
    seasons = [current_year - 1, current_year - 2]
    for season in seasons:
        url = f"{OPENLIGADB_BASE}/getmatchdata/bl3/{season}"
        try:
            r = _request_with_retry(url, timeout=30)
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
                    "Date":     (m.get("matchDateTime") or "")[:10],
                })
        if rows:
            frames.append(pd.DataFrame(rows))
            print(f"    OpenLigaDB Saison {season}: {len(rows)} Matches")

    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    dedup_cols = ["HomeTeam", "AwayTeam", "Date"] if "Date" in combined.columns else ["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    return combined.drop_duplicates(subset=dedup_cols)
