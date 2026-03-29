#!/usr/bin/env python3
"""Generischer E-Mail-Versand via Gmail SMTP fuer Scanner."""

import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .credentials import load_credentials, require_keys

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
REQUIRED_KEYS = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_RECIPIENT"]


def send_report(
    subject: str,
    html_body: str,
    csv_attachments: list[Path] | Path | None = None,
    credentials: dict | None = None,
    sender_name: str = "Scanner",
) -> bool:
    """Sendet einen HTML-Report per Gmail SMTP."""
    creds = credentials or load_credentials()
    if not require_keys(creds, REQUIRED_KEYS):
        return False

    user = creds["GMAIL_USER"]
    password = creds["GMAIL_APP_PASSWORD"]
    recipient = creds["GMAIL_RECIPIENT"]

    msg = MIMEMultipart("mixed")
    msg["From"] = f"{sender_name} <{user}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if csv_attachments is not None:
        if isinstance(csv_attachments, Path):
            csv_attachments = [csv_attachments]
        for csv_path in csv_attachments:
            if csv_path and csv_path.exists():
                with open(csv_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={csv_path.name}")
                msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(user, recipient, msg.as_string())
        print(f"E-Mail gesendet an {recipient}: {subject}")
        return True
    except Exception as exc:
        print(f"E-Mail-Fehler: {exc}", file=sys.stderr)
        return False
