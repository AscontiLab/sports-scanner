#!/usr/bin/env python3
"""
Sendet den täglichen Kicktipp-Report per E-Mail (Gmail SMTP).
"""

import re
import sys
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def load_credentials() -> dict:
    cred_file = Path.home() / ".stock_scanner_credentials"
    creds = {}
    with open(cred_file) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds


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
    creds = load_credentials()
    required_keys = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_RECIPIENT"]
    missing = [k for k in required_keys if not creds.get(k)]
    if missing:
        print(f"Fehler: Fehlende Credentials: {', '.join(missing)}", file=sys.stderr)
        print("Bitte in ~/.stock_scanner_credentials eintragen.", file=sys.stderr)
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
    html = dated_dir / "kicktipp_report.html"

    if not html.exists():
        html = base / "kicktipp_report.html"

    if not html.exists():
        print("Kein Kicktipp-Report gefunden — überspringe E-Mail.", file=sys.stderr)
        sys.exit(0)

    send_report(html)
