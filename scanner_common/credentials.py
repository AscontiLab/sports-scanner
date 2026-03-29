#!/usr/bin/env python3
"""Zentrale Credentials-Verwaltung fuer Scanner."""

import sys
from pathlib import Path

DEFAULT_CREDS_PATH = Path.home() / ".stock_scanner_credentials"


def load_credentials(path: Path | str | None = None) -> dict:
    """Liest KEY=VALUE Credentials aus einer Datei."""
    cred_file = Path(path) if path is not None else DEFAULT_CREDS_PATH

    if not cred_file.exists():
        print(f"Fehler: Credentials-Datei fehlt: {cred_file}", file=sys.stderr)
        return {}

    creds = {}
    try:
        with open(cred_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except Exception as exc:
        print(f"Fehler: Credentials laden fehlgeschlagen: {exc}", file=sys.stderr)

    return creds


def require_keys(creds: dict, keys: list[str]) -> bool:
    """Prueft, ob alle benoetigten Keys im Credentials-Dict vorhanden sind."""
    missing = [k for k in keys if not creds.get(k)]
    if missing:
        print(f"Fehler: Fehlende Credentials: {', '.join(missing)}", file=sys.stderr)
        return False
    return True
