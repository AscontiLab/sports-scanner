"""
Poisson-Modell-Cache
────────────────────
Cached trainierte Dixon-Coles-Parameter als JSON.
Vermeidet unnötiges Neu-Training wenn sich die Daten kaum ändern.

Cache-Key:  {league}_{sha256_hash_der_input_daten}
TTL:        24 Stunden
Fallback:   Bei Cache-Fehler wird einfach neu trainiert.
"""

import hashlib
import json
import time
import logging
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 Stunden

logger = logging.getLogger(__name__)


def _ensure_cache_dir():
    """Erstellt Cache-Verzeichnis falls nicht vorhanden."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _compute_data_hash(df: pd.DataFrame) -> str:
    """Berechnet SHA-256 Hash über die relevanten Spalten des DataFrames."""
    # Nur die Spalten nutzen die fürs Training relevant sind
    cols = [c for c in ["HomeTeam", "AwayTeam", "FTHG", "FTAG", "Date"] if c in df.columns]
    # Sortiert um Reihenfolge-unabhängig zu sein
    content = df[cols].to_csv(index=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _cache_path(league: str, data_hash: str) -> Path:
    """Gibt den Pfad zur Cache-Datei zurück."""
    # Liga-Name bereinigen (Sonderzeichen entfernen)
    safe_league = league.replace("/", "_").replace(" ", "_").replace(".", "_")
    return CACHE_DIR / f"{safe_league}_{data_hash}.json"


def get_cached_model(league: str, data_df: pd.DataFrame) -> Optional[dict]:
    """
    Lädt gecachte Modell-Parameter wenn vorhanden und nicht abgelaufen.

    Returns:
        dict mit Modell-Parametern oder None bei Cache-Miss.
    """
    try:
        _ensure_cache_dir()
        data_hash = _compute_data_hash(data_df)
        path = _cache_path(league, data_hash)

        if not path.exists():
            return None

        # TTL prüfen
        age_seconds = time.time() - path.stat().st_mtime
        if age_seconds > CACHE_TTL_SECONDS:
            logger.info(f"Cache abgelaufen für {league} (Alter: {age_seconds/3600:.1f}h)")
            path.unlink(missing_ok=True)
            return None

        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)

        logger.info(f"Modell aus Cache geladen: {league} (Alter: {age_seconds/60:.0f} Min)")
        return cached

    except Exception as e:
        logger.warning(f"Cache-Lesefehler für {league}: {e}")
        return None


def save_model_cache(league: str, data_df: pd.DataFrame, params: dict) -> None:
    """
    Speichert Modell-Parameter als JSON im Cache.

    Args:
        league: Liga-Identifier (z.B. 'soccer_germany_bundesliga')
        data_df: Trainings-DataFrame (für Hash-Berechnung)
        params: dict mit attack, defense, home_adv, teams, training_matches
    """
    try:
        _ensure_cache_dir()
        data_hash = _compute_data_hash(data_df)
        path = _cache_path(league, data_hash)

        # Metadaten hinzufügen
        cache_entry = {
            **params,
            "_cache_meta": {
                "league": league,
                "data_hash": data_hash,
                "cached_at": time.time(),
                "training_matches": params.get("training_matches", 0),
            }
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache_entry, f, ensure_ascii=False, indent=2)

        # Alte Cache-Dateien für dieselbe Liga aufräumen
        _cleanup_old_caches(league, data_hash)

        logger.info(f"Modell im Cache gespeichert: {league} ({path.name})")

    except Exception as e:
        logger.warning(f"Cache-Schreibfehler für {league}: {e}")


def _cleanup_old_caches(league: str, current_hash: str) -> None:
    """Entfernt veraltete Cache-Dateien der gleichen Liga."""
    safe_league = league.replace("/", "_").replace(" ", "_").replace(".", "_")
    prefix = f"{safe_league}_"
    current_file = f"{safe_league}_{current_hash}.json"

    for path in CACHE_DIR.glob(f"{prefix}*.json"):
        if path.name != current_file:
            try:
                path.unlink()
            except OSError:
                pass


def get_or_train_model(
    league: str,
    data_df: pd.DataFrame,
    train_fn: Callable[[pd.DataFrame], dict],
) -> dict:
    """
    Prüft Cache und trainiert nur bei Cache-Miss.

    Args:
        league: Liga-Identifier
        data_df: Trainings-DataFrame
        train_fn: Funktion die df nimmt und Modell-Parameter zurückgibt

    Returns:
        dict mit Modell-Parametern (attack, defense, home_adv, teams, ...)
    """
    # Cache prüfen
    cached = get_cached_model(league, data_df)
    if cached is not None:
        # Meta-Daten entfernen bevor wir zurückgeben
        cached.pop("_cache_meta", None)
        print(f"    ✅ Modell aus Cache geladen ({league})")
        return cached

    # Neu trainieren
    print(f"    🔄 Modell neu trainiert ({league})")
    params = train_fn(data_df)

    # Im Cache speichern
    save_model_cache(league, data_df, params)

    return params
