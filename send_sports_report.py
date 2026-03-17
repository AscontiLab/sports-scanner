#!/usr/bin/env python3
"""
Sendet den täglichen Sports-Value-Scanner-Report per E-Mail (Gmail SMTP).
"""

import re
import sys
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import load_credentials


def require_keys(creds: dict, keys: list[str]) -> bool:
    missing = [k for k in keys if not creds.get(k)]
    if missing:
        print(f"Fehler: Fehlende Credentials: {', '.join(missing)}", file=sys.stderr)
        return False
    return True


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
    creds     = load_credentials()
    if not require_keys(creds, ["GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_RECIPIENT"]):
        sys.exit(1)
    user      = creds["GMAIL_USER"]
    password  = creds["GMAIL_APP_PASSWORD"]
    recipient = creds["GMAIL_RECIPIENT"]
    subject   = build_subject(html_path)

    msg            = MIMEMultipart("mixed")
    msg["From"]    = f"Sports Scanner <{user}>"
    msg["To"]      = recipient
    msg["Subject"] = subject

    msg.attach(MIMEText(html_path.read_text(encoding="utf-8"), "html", "utf-8"))

    if csv_path and csv_path.exists():
        with open(csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f"attachment; filename={csv_path.name}")
        msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.sendmail(user, recipient, msg.as_string())

    print(f"E-Mail gesendet an {recipient}: {subject}")


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
