# Sports Value Scanner

## Ueberblick

Scanner fuer Value Bets in Fussball und Tennis. Das System kombiniert statistische Modelle, Quotenabgleich, Backtesting, E-Mail-Reports, Telegram-Alerts und Dashboard-Feeds.

## Zweck

- Wahrscheinlichkeiten fuer Sportereignisse modellieren
- Quoten von Buchmachern gegen Eigenmodelle pruefen
- Value Bets und Kicktipp-Prognosen erzeugen
- Ergebnisse fuer Backtesting, Alerts und Dashboards bereitstellen

## Bestandteile

- `sports_scanner.py`
  - Hauptscanner fuer Modelle, Quotenvergleich und Reports
- `backtesting.py`
  - Speicherung und Auswertung vergangener Tipps
- `alerts.py`
  - Telegram-Benachrichtigungen
- `serve_output.py`
  - HTTP-Zugriff auf generierte Dateien fuer n8n
- `send_sports_report.py`
  - E-Mail-Versand des Sports-Reports
- `send_kicktipp_report.py`
  - E-Mail-Versand der Kicktipp-Prognosen
- `run_sports_scanner.sh`
  - Wrapper mit Locking, Logging und Folgeaktionen

## Voraussetzungen

- Python 3.10+
- Pakete aus `requirements.txt` oder mindestens:
  - `requests`
  - `pandas`
  - `numpy`
  - `scipy`

## Einrichtung

```bash
cd /home/claude-agent/sports-scanner
pip install -r requirements.txt
```

## Konfiguration

Credentials werden in `~/.stock_scanner_credentials` erwartet:

```bash
ODDS_API_KEY=...
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
GMAIL_RECIPIENT=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Nutzung

Manueller Lauf:

```bash
python3 sports_scanner.py
```

Wrapper:

```bash
bash run_sports_scanner.sh
```

Einzelaktionen:

```bash
python3 send_sports_report.py
python3 send_kicktipp_report.py
python3 backtesting.py summary
python3 backtesting.py open
python3 backtesting.py resolve
```

## Output

```text
output/YYYY-MM-DD/
├── sports_signals.html
├── sports_signals.csv
├── kicktipp_data.json
└── kicktipp_report.html
```

Weitere Artefakte:

- `sports_backtesting.db`
- `logs/scanner_YYYY-MM-DD.log`

## Betriebshinweise

- Das Wrapper-Skript verhindert parallele Laeufe ueber eine Lock-Datei
- Nach erfolgreichem Scan werden Folgeaktionen wie E-Mail-Reports ausgeloest
- `serve_output.py` stellt generierte Dateien fuer Dashboards oder n8n bereit

## Status

Produktionsnaher Sports-Scanner mit Modellierung, Reporting, Backtesting und Alerting.
