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
  - Confidence Scoring (Edge, Model Reliability, Odds, Consensus, Data Depth, Odds-Praeferenz)
  - Edge-Kurve: Peak bei 5-7%, danach fallend (hohe Edges = Overconfidence)
  - O/U-Bonus (+12 Punkte), 1X2-Penalty (-4 Punkte, Draws ausgenommen)
  - Tiers: Strong Pick (>=70), Value Bet (>=45), Watch (<45)
  - Max 8 Bets/Tag, 15% Tagesrisiko
- `bankroll_manager.py`
  - Bankroll-Tracking, Quarter-Kelly Staking, Daily Snapshots
- `backtesting.py`
  - Speicherung, Auto-Resolve (Scores-API) und ROI-Auswertung
- `config.py`
  - Zentralisierte Konfiguration (Bankroll, Kelly, Limits, Confidence Weights)
  - TENNIS_ENABLED Toggle, LEAGUE_MIN_EDGE Map, Hard-Filter-Caps
  - Odds Sweet Spot (1.60-2.80), Skeptizismus-Schwellen
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

## Model-Tuning (2026-03-16)

Datengetriebenes Tuning basierend auf Backtesting (48 Bets, 18.8% Win-Rate):

- **Confidence Weights** neu gewichtet: Odds-Praeferenz und Markt-Konsens staerker, Edge und Datentiefe schwaecher
- **Edge-Kurve**: Peak bei 5-7%, danach fallend (hohe Edges korrelieren invers mit Erfolg)
- **O/U-Bonus**: +12 Punkte (41.7% Win-Rate vs. 12.1% bei 1X2 Home/Away)
- **1X2-Penalty**: -4 Punkte fuer Home/Away-Bets, Draws ausgenommen
- **Odds Sweet Spot**: 1.60-2.80 (engerer Bereich), Skeptizismus ab Edge 8% / Odds 2.80
- **Market-Gap**: Max 10pp Abweichung Modell vs. Konsens (vorher 15pp)
- **Bugfix**: Liga-Filter griff nie (`sport_key` vs. `sport` Key-Mismatch)
- **Tennis-Guard**: TENNIS_ENABLED Toggle in config.py (aktuell aktiv)
- **Review geplant**: 2026-03-30 mit mehr Daten

## Status

Produktionsnaher Sports-Scanner mit 7 Ligen + UEFA + Tennis, Bet Selection, Bankroll-Management, Auto-Resolve, Kicktipp und Hub-API.
