# Sports Value Scanner

Findet Value Bets in Fussball und Tennis durch statistische Modelle und Quotenvergleich.
Inkl. Kicktipp-Prognosen, Backtesting, Dashboards und E-Mail-Reports.

## Features

- **Value Bets:** Poisson- und Elo-Modelle vs. Buchmacherquoten, Edge >= 3 %
- **Kicktipp:** Automatische Tipps fuer alle Kicktipp-Ligen (1X2 + Score)
- **Backtesting:** SQLite-basierte Auswertung (ROI, Hit-Rate, Rolling Stats)
- **Dashboards:** Neon Command Center Design via n8n Webhooks
- **E-Mail:** Taeglicher HTML-Report per Gmail SMTP
- **Alerts:** Telegram-Benachrichtigungen bei Value Bets

## Unterstuetzte Wettbewerbe

| Sport | Wettbewerbe | Modell |
|-------|-------------|--------|
| Fussball | 1. + 2. Bundesliga, Premier League, La Liga, Serie A, Ligue 1 | Dixon-Coles Poisson |
| Fussball | 3. Liga (OpenLigaDB) | Poisson |
| UEFA | Champions League, Europa League, Conference League | Club-Elo + Poisson |
| Tennis | ATP + WTA (alle aktiven Turniere) | Elo + Surface-Bias |

## Datenquellen

- **Fussball-Historie:** football-data.co.uk (Top-5-Ligen) + OpenLigaDB (3. Liga)
- **UEFA-Elo:** api.clubelo.com
- **Tennis-Historie:** github.com/JeffSackmann/tennis_atp + tennis_wta (4 Jahre)
- **Live-Quoten:** The Odds API (v4)

## Dateien

| Datei | Beschreibung |
|-------|-------------|
| `sports_scanner.py` | Hauptscanner — Modelle, Analyse, HTML/CSV-Output, Kicktipp |
| `backtesting.py` | SQLite-Persistence fuer Predictions + Ergebnisauswertung |
| `alerts.py` | Telegram-Bot-Integration fuer Value-Bet-Alerts |
| `serve_output.py` | HTTP-Server (Port 8099) fuer n8n-Dashboard-Zugriff |
| `send_sports_report.py` | Sendet Sports-Report per Gmail |
| `send_kicktipp_report.py` | Sendet Kicktipp-Report per Gmail |
| `run_sports_scanner.sh` | Wrapper-Script fuer Cron mit Lock + Logging |

## Installation

```bash
pip install requests pandas numpy scipy
```

## Konfiguration

Credentials in `~/.stock_scanner_credentials` (chmod 600):

```
ODDS_API_KEY=...
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
GMAIL_RECIPIENT=...
TELEGRAM_BOT_TOKEN=...      # optional
TELEGRAM_CHAT_ID=...        # optional
```

## Ausfuehrung

```bash
# Manuell
python3 sports_scanner.py

# Mit Log-Datei
bash run_sports_scanner.sh

# Nur Report senden
python3 send_sports_report.py
python3 send_kicktipp_report.py
```

## Automatisierung

Cron (taeglich 08:00 UTC):

```cron
0 8 * * * /home/claude-agent/sports-scanner/run_sports_scanner.sh
```

## Output

```
output/YYYY-MM-DD/
├── sports_signals.html      # HTML-Report (Value Bets)
├── sports_signals.csv       # CSV-Export
├── kicktipp_data.json       # Kicktipp-Prognosen (JSON)
└── kicktipp_report.html     # Kicktipp-Report
```

## Dashboards (n8n)

Alle Dashboards verwenden das "Neon Command Center" Design (Dark Theme, Cyan/Gold/Pink Akzente, Glassmorphism).

| Dashboard | URL | n8n Workflow |
|-----------|-----|-------------|
| Hub (Startseite) | `/webhook/hub` | `oB1lnybKPVW8SsmH` |
| Kicktipp | `/webhook/kicktipp` | `GW2llNiB5nNF0WDC` |
| Sports Report | `/webhook/sports-report` | `P0mBA9lPXEpE1hQO` |
| Stock Dashboard | `/webhook/stock-dashboard` | `Y4DA5bzf1FMF3JnS` |

## API-Server (serve_output.py)

Laeuft auf Port 8099, erreichbar vom n8n-Docker via `172.28.0.1:8099`.

```
GET /sports/{date}/sports_signals.html
GET /stock/{date}/cfd_setups.csv
GET /hub/ki_news.json
PUT /hub/ki_news.json              # nur localhost/Docker
GET /api/hub-summary               # Counts fuer Hub-Dashboard
GET /api/kicktipp-latest           # Neueste Kicktipp-Daten
GET /api/kicktipp-for-date?date=   # Kicktipp fuer bestimmtes Datum
GET /api/kicktipp-stats            # Aggregierte Kicktipp-Statistiken
```

**Security:** Path-Traversal-Schutz (resolve + is_relative_to), PUT nur von localhost/Docker-Netz, 1 MB Upload-Limit.

## Backtesting

```bash
python3 backtesting.py summary     # ROI-Auswertung
python3 backtesting.py open        # Offene Bets
python3 backtesting.py resolve     # Ergebnisse abrufen + abgleichen
```

## Schwellwerte

```python
MIN_EDGE_PCT = 3.0    # Mindest-Edge in %
MAX_EDGE_PCT = 50.0   # Max-Edge (Filter Ausreisser)
MIN_ODDS     = 1.25   # Mindestquote
MAX_KELLY    = 0.05   # Max. Kelly-Anteil (5 %)
```

## Architektur

```
sports_scanner.py
├── Fussball-Analyse
│   ├── load_football_data()     → football-data.co.uk CSVs
│   ├── train_poisson_model()    → Dixon-Coles MLE
│   ├── predict_football()       → 1X2 Wahrscheinlichkeiten
│   └── analyze_football_ou()    → Ueber/Unter
├── Tennis-Analyse
│   ├── compute_tennis_elo()     → ATP Elo (parallel download)
│   ├── compute_wta_elo()        → WTA Elo (parallel download)
│   └── analyze_tennis_match()   → Value Bets
├── UEFA-Analyse
│   ├── download_clubelo()       → Club-Elo Ratings
│   └── analyze_uefa_match()     → Elo + Poisson
├── Kicktipp
│   └── collect_kicktipp_predictions() → Tipps mit Fallback-Kette
├── Output
│   ├── generate_html()          → Sports Report
│   └── generate_kicktipp_html() → Kicktipp Report
└── Backtesting
    └── log_prediction()         → SQLite Persistence
```
