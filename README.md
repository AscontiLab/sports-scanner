# Sports Value Scanner

Findet Value Bets in Fußball und Tennis durch statistische Modelle und Quotenvergleich.

## Was es macht

- Berechnet Gewinnwahrscheinlichkeiten mit Poisson- und Elo-Modellen
- Vergleicht mit den besten verfügbaren Buchmacherquoten
- Meldet Bets mit Edge ≥ 3 % und Kelly-Anteil bis max. 5 %

## Unterstützte Wettbewerbe

| Sport | Wettbewerbe |
|-------|-------------|
| Fußball | 1. + 2. Bundesliga, 3. Liga |
| UEFA | Champions League, Europa League, Conference League |
| Tennis | ATP (alle Turniere) |

## Modelle

| Modell | Eingesetzt für |
|--------|---------------|
| Dixon-Coles Poisson | Fußball 1X2 + Über/Unter |
| Club-Elo | UEFA 1X2 |
| Elo (Jeff Sackmann) | Tennis |

## Datenquellen

- **Fußball-Historie:** football-data.co.uk (D1, D2, 3. Liga via OpenLigaDB)
- **UEFA-Elo:** api.clubelo.com
- **Tennis-Historie:** github.com/JeffSackmann/tennis_atp (2022–2024)
- **Live-Quoten:** The Odds API (v4)

## Installation

```bash
pip install requests pandas numpy scipy
```

## Konfiguration

Credentials in `~/.stock_scanner_credentials`:

```
ODDS_API_KEY=...
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
GMAIL_RECIPIENT=...
```

## Ausführung

```bash
python3 sports_scanner.py
```

Oder mit Log-Datei:

```bash
bash run_sports_scanner.sh
```

## Output

```
output/YYYY-MM-DD/
├── sports_signals.html   # HTML-Report mit allen Value Bets
└── sports_signals.csv    # Alle Bets als CSV
```

## Report per E-Mail

```bash
python3 send_sports_report.py
```

## Schwellwerte

```python
MIN_EDGE_PCT = 3.0   # % Mindest-Edge
MIN_ODDS     = 1.25  # Mindestquote
MAX_KELLY    = 5.0   # % maximaler Kelly-Anteil
```
