# Design: Champions League / Europa League / Conference League

**Datum:** 2026-03-01

---

## Ziel

Champions League, Europa League und Conference League in den Sports Value Scanner einbauen.
1X2-Wetten via Club-Elo-Modell (analog Tennis-Elo), Über/Unter-Wetten via Multi-Liga Poisson-Modell.

---

## Datenquellen

### Club-Elo (für 1X2)

- **URL:** `http://api.clubelo.com/YYYY-MM-DD`
- **Format:** CSV mit Spalten: Rank, Club, Country, Level, Elo, From, To
- **Nutzung:** Einmalig pro Scan-Lauf geladen → `{clubname: elo}` Dict
- **Team-Matching:** Fuzzy-Matching (identische Logik wie bestehende `find_team_in_model`)
- **Home-Vorteil:** 65 Elo-Punkte für Heimspiele in UEFA-Wettbewerben

### Multi-Liga Poisson (für O/U)

Zusätzlich zu bestehenden `D1.csv`/`D2.csv` (Bundesliga 1+2):

| Datei | Liga |
|-------|------|
| `E0.csv` | Premier League |
| `SP1.csv` | La Liga |
| `I1.csv` | Serie A |
| `F1.csv` | Ligue 1 |

- Gleiche football-data.co.uk URLs wie Bundesliga-Daten
- `fit_poisson_model()` wird mit kombiniertem DataFrame aufgerufen → `euro_model`
- Dieses Modell ist **separat** vom deutschen Bundesliga-Modell (das bleibt unverändert)

---

## Neue Konfiguration

```python
UEFA_SPORTS = [
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
]

UEFA_LABELS = {
    "soccer_uefa_champs_league":              "Champions League",
    "soccer_uefa_europa_league":              "Europa League",
    "soccer_uefa_europa_conference_league":   "Conference League",
}

CLUBELO_URL = "http://api.clubelo.com/{date}"

EUROPEAN_FDCO_URLS = {
    "premier_league": [
        "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
        "https://www.football-data.co.uk/mmz4281/2425/E0.csv",
    ],
    "la_liga": [
        "https://www.football-data.co.uk/mmz4281/2526/SP1.csv",
        "https://www.football-data.co.uk/mmz4281/2425/SP1.csv",
    ],
    "serie_a": [
        "https://www.football-data.co.uk/mmz4281/2526/I1.csv",
        "https://www.football-data.co.uk/mmz4281/2425/I1.csv",
    ],
    "ligue_1": [
        "https://www.football-data.co.uk/mmz4281/2526/F1.csv",
        "https://www.football-data.co.uk/mmz4281/2425/F1.csv",
    ],
    # Bundesliga 1+2 werden auch hinzugefügt (aus FDCO_URLS)
}
```

---

## Neue Funktionen

### `download_clubelo(date: str) -> dict`
- Ruft `http://api.clubelo.com/{date}` auf
- Parst CSV → `{club_name: elo_rating}` Dict
- Fehlerbehandlung: bei Fehler leeres Dict zurückgeben + Warning

### `find_club_elo(name: str, elo_dict: dict) -> float | None`
- Exakte Suche → normalisierte Suche → Teilstring → difflib-Fuzzy (cutoff 0.6)
- Identische Logik wie `find_team_in_model`

### `elo_to_football_1x2(elo_home, elo_away, home_adv=65) -> tuple[float, float, float]`
```
dr = elo_home + home_adv - elo_away
E_home = 1 / (1 + 10^(-dr / 400))   # erwartetes Ergebnis [0,1]

# Unentschieden-Wahrscheinlichkeit: max ~28% bei gleichem Match, sinkt bei Favoriten
p_draw = 0.28 * math.exp(-2.0 * (E_home - 0.5) ** 2)

remaining = 1.0 - p_draw
p_home = E_home * remaining
p_away = (1.0 - E_home) * remaining

return p_home, p_draw, p_away
```

### `load_european_data() -> pd.DataFrame | None`
- Lädt E0, SP1, I1, F1 + D1, D2 via `download_fdco()` + `standardize_fdco()`
- Gibt kombinierten DataFrame zurück (wie `load_football_data` aber für alle Ligen)

### `analyze_uefa_match(match, elo_dict, euro_model) -> list`
- **1X2:** Suche beide Teams in `elo_dict` → `elo_to_football_1x2` → `compute_value` vs. `best_odds_from_match`
- **O/U:** Suche beide Teams in `euro_model` → `predict_football` → `predict_ou` → `compute_value` vs. `best_ou_odds_from_match`
- Fallback für 1X2: Wenn ein Team nicht in Club-Elo gefunden → kein 1X2 Bet
- Fallback für O/U: Wenn ein Team nicht in euro_model → kein O/U Bet
- Gibt kombinierte Liste zurück mit Feld `bet_type`: `"1x2"` oder `"ou"`

---

## HTML Report

### Neue Sektion

```
🏆 UEFA Value Bets (Champions League / Europa League / Conference League)
```

Tabelle mit Spalten: Wettbewerb | Spiel | Typ | Tipp | Anstoß | Modell-% | Beste Quote | Edge-% | Kelly-% | Modell

- „Typ" unterscheidet 1X2 von O/U
- „Modell" zeigt „Club-Elo" oder „Poisson"

### Neue Summary-Card

```
🏆 UEFA  [Anzahl]
```

---

## main() Ablauf (Ergänzung)

```
# Einmalig laden
elo_dict    = download_clubelo(today)
euro_df     = load_european_data()
euro_model  = fit_poisson_model(euro_df) if euro_df else None

# Pro UEFA-Wettbewerb
for sport_key in UEFA_SPORTS:
    matches = get_odds(api_key, sport_key)
    for match in matches:
        bets = analyze_uefa_match(match, elo_dict, euro_model)
        all_uefa_bets.extend(bets)
```

---

## Schwellwerte

Identisch mit bestehenden Märkten:
- MIN_EDGE_PCT = 3.0%
- MIN_ODDS = 1.25
- MAX_KELLY = 5%
