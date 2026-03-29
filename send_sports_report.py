#!/usr/bin/env python3
"""
Sendet den täglichen Sports-Value-Scanner-Report per E-Mail (Gmail SMTP).

Nutzt scanner_common fuer Credentials und E-Mail-Versand.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

# --- Neue zentrale Imports aus scanner_common ---
from scanner_common import load_credentials, require_keys
from scanner_common import send_report as _send_report_generic

# Abwaertskompatibilitaet: config.load_credentials bleibt als Fallback
# from config import load_credentials


# ── DEPRECATED: Alte lokale require_keys ────────────────────────────────────
# Nutze scanner_common.require_keys() stattdessen.
# ────────────────────────────────────────────────────────────────────────────


def build_subject(html_path: Path) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")
    try:
        content = html_path.read_text(encoding="utf-8")
        # Summary-Cards Reihenfolge: Total, FB 1X2, O/U, UEFA, Tennis, Max Edge
        cards = re.findall(r'<div class="val">(\d+)</div>', content)
        total = cards[0] if len(cards) > 0 else "?"
        fb    = cards[1] if len(cards) > 1 else "?"
        ou    = cards[2] if len(cards) > 2 else "?"
        uefa  = cards[3] if len(cards) > 3 else "?"
        tn    = cards[4] if len(cards) > 4 else "?"
        return f"⚽🎾 Sports Value Scanner {date_str} — {total} Bets (FB:{fb} O/U:{ou} UEFA:{uefa} TN:{tn})"
    except Exception:
        return f"⚽🎾 Sports Value Scanner {date_str}"


def send_report(html_path: Path, csv_path: Path | None = None):
    """Sendet den Sports-Scanner-Report via scanner_common."""
    subject = build_subject(html_path)
    html_content = html_path.read_text(encoding="utf-8")

    if not _send_report_generic(
        subject=subject,
        html_body=html_content,
        csv_attachments=csv_path,
        sender_name="Sports Scanner",
    ):
        sys.exit(1)


if __name__ == "__main__":
    base     = Path(__file__).parent
    date_str = datetime.now().strftime("%Y-%m-%d")

    dated_dir = base / "output" / date_str
    html = dated_dir / "sports_signals.html"
    csv  = dated_dir / "sports_signals.csv"

    if not html.exists():
        html = base / "sports_signals.html"
        csv  = base / "sports_signals.csv"

    if not html.exists():
        print("Fehler: Kein HTML-Report gefunden.", file=sys.stderr)
        sys.exit(1)

    send_report(html, csv if csv.exists() else None)
