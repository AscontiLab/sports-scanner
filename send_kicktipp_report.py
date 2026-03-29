#!/usr/bin/env python3
"""
Sendet den täglichen Kicktipp-Report per E-Mail (Gmail SMTP).

Nutzt scanner_common fuer Credentials und E-Mail-Versand.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

# --- Neue zentrale Imports aus scanner_common ---
from scanner_common import load_credentials
from scanner_common import send_report as _send_report_generic

# Abwaertskompatibilitaet: config.load_credentials bleibt als Fallback
# from config import load_credentials


def build_subject(html_path: Path) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")
    try:
        content = html_path.read_text(encoding="utf-8")
        cards = re.findall(r'<div class="val">(\d+)</div>', content)
        n_matches = cards[0] if cards else "?"
        return f"🎯 Kicktipp-Tipps {date_str} — {n_matches} Spiele"
    except Exception:
        return f"🎯 Kicktipp-Tipps {date_str}"


def send_report(html_path: Path):
    """Sendet den Kicktipp-Report via scanner_common."""
    subject = build_subject(html_path)
    html_content = html_path.read_text(encoding="utf-8")

    if not _send_report_generic(
        subject=subject,
        html_body=html_content,
        sender_name="Sports Scanner",
    ):
        sys.exit(1)


if __name__ == "__main__":
    base     = Path(__file__).parent
    date_str = datetime.now().strftime("%Y-%m-%d")

    dated_dir = base / "output" / date_str
    html = dated_dir / "kicktipp_report.html"

    if not html.exists():
        html = base / "kicktipp_report.html"

    if not html.exists():
        print("Kein Kicktipp-Report gefunden — überspringe E-Mail.", file=sys.stderr)
        sys.exit(0)

    send_report(html)
