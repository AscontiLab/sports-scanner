# Sports Value Scanner

## Ueberblick

Scanner fuer Value Bets in Fussball und Tennis. Das System kombiniert statistische Modelle (Poisson, Club-Elo, Tennis-Elo), Quotenabgleich, Confidence-basierte Bet Selection, Bankroll-Management, Backtesting, E-Mail-Reports, Telegram-Alerts und Dashboard-Feeds.

## Zweck

- Wahrscheinlichkeiten fuer Sportereignisse modellieren
- Quoten von Buchmachern gegen Eigenmodelle pruefen
- Value Bets mit Confidence Scoring (0-100) selektieren und Einsaetze via Quarter-Kelly berechnen
- Kicktipp-Prognosen fuer mehrere Ligen erzeugen
- Ergebnisse fuer Backtesting, Alerts und Dashboards bereitstellen

## Ligen

| Liga | Quelle | Modell |
|------|--------|--------|
| 1. Bundesliga | football-data.co.uk | Poisson (Dixon-Coles) |
| 2. Bundesliga | football-data.co.uk | Poisson |
| 3. Liga | OpenLigaDB | Poisson |
| Premier League | football-data.co.uk | Poisson |
| La Liga | football-data.co.uk | Poisson |
| Serie A | football-data.co.uk | Poisson |
| Ligue 1 | football-data.co.uk | Poisson |
| Champions League | Club-Elo API | Club-Elo → Poisson |
| Europa League | Club-Elo API | Club-Elo → Poisson |
| Conference League | Club-Elo API | Club-Elo → Poisson |
| DFB-Pokal | The Odds API | Conditional |
| ATP Tennis | Jeff Sackmann GitHub | Elo (Surface-spezifisch) |

## Bestandteile

- `sports_scanner.py`
  - Hauptscanner fuer Modelle, Quotenvergleich und Reports
- `bet_selector.py`
  - Confidence Scoring (Edge, Model Reliability, Odds, Consensus, Data Depth)
  - Tiers: Strong Pick (>=70), Value Bet (>=45), Watch (<45)
  - Max 8 Bets/Tag, 15% Tagesrisiko
- `bankroll_manager.py`
  - Bankroll-Tracking, Quarter-Kelly Staking, Daily Snapshots
- `backtesting.py`
  - Speicherung, Auto-Resolve (Scores-API) und ROI-Auswertung
- `config.py`
  - Zentralisierte Konfiguration (Bankroll, Kelly, Limits)
- `alerts.py`
  - Telegram-Benachrichtigungen (High-Edge >= 10%)
- `serve_output.py`
  - HTTP-API (Port 8099): Output-Files, Hub-Summary, Kicktipp-API
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

## HTTP-API (Port 8099)

`serve_output.py` laeuft via `@reboot` Cron und stellt bereit:

- `GET /sports/{date}/` — Tagesausgaben (HTML, CSV, Kicktipp)
- `GET /stock/{date}/` — Stock Scanner Output (Cross-Referenz)
- `GET /api/hub-summary` — Aggregierte Counts fuer Hub Dashboard
- `GET /api/kicktipp-latest` — Neueste Kicktipp-Tipps
- `GET /api/kicktipp-for-date?date=YYYY-MM-DD` — Tipps fuer Datum
- `GET /api/kicktipp-stats` — Aggregierte Stats (30 Tage)

## Betriebshinweise

- Das Wrapper-Skript verhindert parallele Laeufe ueber eine Lock-Datei
- Nach erfolgreichem Scan werden E-Mail-Reports (Sports + Kicktipp) ausgeloest
- `serve_output.py` stellt generierte Dateien und APIs fuer Hub Dashboard und n8n bereit
- Telegram-Alerts sind implementiert, erfordern Credentials in `~/.stock_scanner_credentials`

## Status

Produktionsnaher Sports-Scanner mit 7 Ligen + UEFA + Tennis, Bet Selection, Bankroll-Management, Auto-Resolve, Kicktipp und Hub-API.
