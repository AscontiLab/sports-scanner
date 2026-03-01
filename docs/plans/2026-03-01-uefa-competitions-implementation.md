# UEFA Competitions (CL/EL/ECL) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Champions League, Europa League und Conference League in den Sports Scanner einbauen — 1X2-Wetten via Club-Elo, Über/Unter-Wetten via Multi-Liga Poisson-Modell.

**Architecture:** Club-Elo API (`api.clubelo.com`) liefert Elo-Ratings für alle europäischen Clubs → `elo_to_football_1x2()` konvertiert diese in 1X2-Wahrscheinlichkeiten. Zusätzlich wird ein Multi-Liga Poisson-Modell (PL + LaLiga + SerieA + Ligue1 + Bundesliga 1+2) für O/U-Prognosen trainiert. `analyze_uefa_match()` kombiniert beide Modelle pro Match.

**Tech Stack:** Python 3.10+, scipy, pandas, requests — alles bereits installiert. Keine neuen Abhängigkeiten.

---

### Task 1: Konfiguration — UEFA_SPORTS, UEFA_LABELS, EUROPEAN_FDCO_URLS

**Files:**
- Modify: `sports_scanner.py` — Konfigurationsblock (nach Zeile 75, nach `ELO_YEARS`)

**Kontext:**
Der Konfigurationsblock in der Datei endet nach `ELO_YEARS = [...]`. Neue Konstanten werden direkt danach eingefügt. Der Konfigurationsblock wird mit einem neuen Kommentar-Separator strukturiert.

**Step 1: Direkt nach `ELO_YEARS = [2022, 2023, 2024, 2025]` einfügen:**

```python
# UEFA-Wettbewerbe
UEFA_SPORTS = [
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
]

UEFA_LABELS = {
    "soccer_uefa_champs_league":            "Champions League",
    "soccer_uefa_europa_league":            "Europa League",
    "soccer_uefa_europa_conference_league": "Conference League",
}

# Club-Elo API
CLUBELO_URL = "http://api.clubelo.com/{date}"

# Multi-Liga Poisson-Modell (Top-5-Ligen + Bundesliga 1+2)
EUROPEAN_FDCO_URLS = [
    "https://www.football-data.co.uk/mmz4281/2526/E0.csv",   # Premier League
    "https://www.football-data.co.uk/mmz4281/2425/E0.csv",
    "https://www.football-data.co.uk/mmz4281/2526/SP1.csv",  # La Liga
    "https://www.football-data.co.uk/mmz4281/2425/SP1.csv",
    "https://www.football-data.co.uk/mmz4281/2526/I1.csv",   # Serie A
    "https://www.football-data.co.uk/mmz4281/2425/I1.csv",
    "https://www.football-data.co.uk/mmz4281/2526/F1.csv",   # Ligue 1
    "https://www.football-data.co.uk/mmz4281/2425/F1.csv",
    "https://www.football-data.co.uk/mmz4281/2526/D1.csv",   # Bundesliga 1
    "https://www.football-data.co.uk/mmz4281/2425/D1.csv",
    "https://www.football-data.co.uk/mmz4281/2526/D2.csv",   # Bundesliga 2
    "https://www.football-data.co.uk/mmz4281/2425/D2.csv",
]
```

**Step 2: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```
Erwartet: `Syntax OK`

**Step 3: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add UEFA config constants (UEFA_SPORTS, UEFA_LABELS, EUROPEAN_FDCO_URLS)"
```

---

### Task 2: `download_clubelo` + `find_club_elo`

**Files:**
- Modify: `sports_scanner.py` — nach `bookie_consensus` (aktuell ca. Zeile 194)

**Kontext:**
Club-Elo stellt eine kostenlose CSV-API bereit: `http://api.clubelo.com/YYYY-MM-DD` liefert alle europäischen Club-Elo-Ratings für ein Datum. Das CSV hat die Spalten: Rank, Club, Country, Level, Elo, From, To.

`find_club_elo` nutzt die gleiche Fuzzy-Matching-Logik wie `find_team_in_model` (exakt → normalisiert → Teilstring → difflib) und ruft `normalize_name` auf.

**Step 1: Lese die aktuelle Datei und finde `bookie_consensus`. Füge DANACH (nach dem `return result`) ein:**

```python
# ═══════════════════════════════════════════════════════════════════════════════
# CLUB-ELO
# ═══════════════════════════════════════════════════════════════════════════════

def download_clubelo(date: str) -> dict:
    """
    Lädt Club-Elo-Ratings für ein Datum (Format: YYYY-MM-DD).
    Gibt {club_name: elo_rating} zurück.
    """
    url = CLUBELO_URL.format(date=date)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        result = {}
        for _, row in df.iterrows():
            club = row.get("Club")
            elo  = row.get("Elo")
            if pd.notna(club) and pd.notna(elo):
                result[str(club).strip()] = float(elo)
        return result
    except Exception as e:
        print(f"    Warning: Club-Elo ({date}): {e}")
        return {}


def find_club_elo(name: str, elo_dict: dict) -> float | None:
    """Findet Club-Elo-Rating via fuzzy Matching (analog find_team_in_model)."""
    if name in elo_dict:
        return elo_dict[name]
    norm = normalize_name(name)
    for club, elo in elo_dict.items():
        if normalize_name(club) == norm:
            return elo
    for club, elo in elo_dict.items():
        nc = normalize_name(club)
        if norm in nc or nc in norm:
            return elo
    close = difflib.get_close_matches(norm,
                                       [normalize_name(c) for c in elo_dict],
                                       n=1, cutoff=0.6)
    if close:
        for club, elo in elo_dict.items():
            if normalize_name(club) == close[0]:
                return elo
    return None
```

**Step 2: Schnelltest:**
```bash
cd /root/sports_scanner
python3 -c "
import sys; sys.path.insert(0, '.')
import sports_scanner as ss

# Test download_clubelo
elo = ss.download_clubelo('2026-03-01')
print(f'Club-Elo geladen: {len(elo)} Clubs')
assert len(elo) > 100, f'Zu wenig Clubs: {len(elo)}'

# Test find_club_elo - exakte Suche
# Bayern München kann unter verschiedenen Namen auftauchen
for name in ['Bayern Munich', 'FC Bayern Munchen', 'Bayern']:
    result = ss.find_club_elo(name, elo)
    print(f'  {name} → {result}')

print('OK')
"
```
Erwartet: > 100 Clubs geladen, Bayern-Variante gefunden (nicht None).

**Step 3: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 4: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add download_clubelo() and find_club_elo() for UEFA Elo ratings"
```

---

### Task 3: `elo_to_football_1x2`

**Files:**
- Modify: `sports_scanner.py` — nach `predict_ou` (aktuell ca. Zeile 381)

**Kontext:**
Elo-Differenz → erwartetes Ergebnis E_home ∈ [0,1]. Unentschieden-Wahrscheinlichkeit nimmt mit zunehmender Ungleichheit ab. Formel ist in der Fußball-Elo-Literatur weit verbreitet.

Verifikation der Mathematik:
- Bei Elo-Differenz 0 (gleiche Teams, home_adv=65): E_home ≈ 0.594, p_draw ≈ 0.28, p_home ≈ 0.42, p_away ≈ 0.30
- Bei Elo-Differenz +400 (starker Favorit): E_home ≈ 0.95, p_draw ≈ 0.09, p_home ≈ 0.86, p_away ≈ 0.05

**Step 1: Direkt nach `predict_ou` einfügen:**

```python
def elo_to_football_1x2(elo_home: float, elo_away: float,
                         home_adv: float = 65.0) -> tuple[float, float, float]:
    """
    Konvertiert Club-Elo-Ratings in 1X2-Wahrscheinlichkeiten für Fußball.
    home_adv: Heimvorteil in Elo-Punkten (Standard: 65 für UEFA-Heimspiele).
    Gibt (p_home, p_draw, p_away) zurück.
    """
    dr      = elo_home + home_adv - elo_away
    e_home  = 1.0 / (1.0 + 10.0 ** (-dr / 400.0))
    # Unentschieden: max ~28% bei ausgeglichenem Spiel, sinkt bei Favoriten
    p_draw  = 0.28 * math.exp(-2.0 * (e_home - 0.5) ** 2)
    remaining = 1.0 - p_draw
    p_home  = e_home * remaining
    p_away  = (1.0 - e_home) * remaining
    return p_home, p_draw, p_away
```

**Step 2: Mathematik-Test:**
```bash
cd /root/sports_scanner
python3 -c "
import sys; sys.path.insert(0, '.')
import sports_scanner as ss

# Gleiche Teams (home_adv=65): Heimteam leichter Favorit
p_h, p_d, p_a = ss.elo_to_football_1x2(1500, 1500)
print(f'Gleich: Heim={p_h:.3f}, Unent={p_d:.3f}, Gast={p_a:.3f}')
assert abs(p_h + p_d + p_a - 1.0) < 1e-9, 'Summe ≠ 1'
assert p_h > p_a, 'Heimteam sollte Favorit sein (Heimvorteil)'
assert 0.20 < p_d < 0.35, f'Unentschieden-Wahrscheinlichkeit unrealistisch: {p_d}'

# Starker Favorit (+400 Elo): weniger Unentschieden
p_h2, p_d2, p_a2 = ss.elo_to_football_1x2(1800, 1400)
print(f'Favorit: Heim={p_h2:.3f}, Unent={p_d2:.3f}, Gast={p_a2:.3f}')
assert abs(p_h2 + p_d2 + p_a2 - 1.0) < 1e-9
assert p_h2 > 0.7, f'Starker Favorit erwartet: {p_h2}'
assert p_d2 < p_d, 'Unentschieden sinkt bei klarem Favoriten'

# Kein Heimvorteil (Neutralfeld): symmetrisch
p_h3, p_d3, p_a3 = ss.elo_to_football_1x2(1500, 1500, home_adv=0)
print(f'Neutral: Heim={p_h3:.3f}, Unent={p_d3:.3f}, Gast={p_a3:.3f}')
assert abs(p_h3 - p_a3) < 1e-9, 'Bei home_adv=0 muss p_home == p_away'

print('OK')
"
```
Erwartet: alle Assertions bestehen, plausible Werte.

**Step 3: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 4: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add elo_to_football_1x2() for UEFA 1X2 probability from Club-Elo"
```

---

### Task 4: `load_european_data`

**Files:**
- Modify: `sports_scanner.py` — nach `load_football_data` (aktuell ca. Zeile 294)

**Kontext:**
`download_fdco()` und `standardize_fdco()` existieren bereits und werden wiederverwendet. `EUROPEAN_FDCO_URLS` ist eine flache Liste aller URLs (wurde in Task 1 angelegt). Die Funktion lädt alle Ligen, kombiniert sie und entfernt Duplikate.

**Step 1: Direkt nach `load_football_data` einfügen:**

```python
def load_european_data() -> pd.DataFrame | None:
    """
    Lädt Matchdaten aus Top-5-Ligen + Bundesliga 1+2 für das europäische
    Poisson-Modell (O/U bei UEFA-Wettbewerben).
    """
    frames = []
    for url in EUROPEAN_FDCO_URLS:
        df_raw = download_fdco(url)
        if df_raw is not None:
            df = standardize_fdco(df_raw)
            if df is not None and len(df) > 5:
                frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    )
    return combined
```

**Step 2: Schnelltest:**
```bash
cd /root/sports_scanner
python3 -c "
import sys; sys.path.insert(0, '.')
import sports_scanner as ss

df = ss.load_european_data()
if df is None:
    print('WARN: Keine Daten geladen (Netzwerk?)')
else:
    print(f'Europäische Daten: {len(df)} Matches, {df[\"HomeTeam\"].nunique()} Teams')
    assert len(df) > 500, f'Zu wenig Matches: {len(df)}'
    assert df['HomeTeam'].nunique() > 50, 'Zu wenig Teams'
    # Prüfe Beispiel-Teams aus verschiedenen Ligen
    teams = set(df['HomeTeam'].tolist() + df['AwayTeam'].tolist())
    print(f'Teams Beispiele: {list(teams)[:5]}')
    print('OK')
"
```
Erwartet: > 500 Matches, > 50 Teams, keine Assertion-Fehler.

**Step 3: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 4: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add load_european_data() for multi-league Poisson model training"
```

---

### Task 5: `analyze_uefa_match`

**Files:**
- Modify: `sports_scanner.py` — nach `analyze_football_ou` (aktuell ca. Zeile 630)

**Kontext:**
Diese Funktion kombiniert beide Modelle:
- **1X2:** `find_club_elo` → `elo_to_football_1x2` → `best_odds_from_match` → `compute_value`
- **O/U:** `find_team_in_model` auf `euro_model` → `predict_football` → `best_ou_odds_from_match` → `predict_ou` → `compute_value`

Bet-Dict hat ein Feld `bet_type` ("1x2" oder "ou") und `model_src` (beschreibt das Modell).
Wenn Club-Elo kein Team findet → kein 1X2-Bet (kein Fallback).
Wenn euro_model kein Team findet → kein O/U-Bet (kein Fallback).

**Step 1: Direkt nach `analyze_football_ou` einfügen:**

```python
# ═══════════════════════════════════════════════════════════════════════════════
# UEFA ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_uefa_match(match: dict, elo_dict: dict,
                       euro_model: dict | None) -> list:
    """
    Value Bets für UEFA-Matches:
    - 1X2 via Club-Elo (elo_to_football_1x2)
    - O/U  via Multi-Liga Poisson-Modell (euro_model)
    """
    home_api = match["home_team"]
    away_api = match["away_team"]
    best     = best_odds_from_match(match)
    bets     = []

    # ── 1X2 via Club-Elo ────────────────────────────────────────────────────
    elo_home = find_club_elo(home_api, elo_dict)
    elo_away = find_club_elo(away_api, elo_dict)

    if elo_home is not None and elo_away is not None:
        p_home, p_draw, p_away = elo_to_football_1x2(elo_home, elo_away)
        labels = {"home": home_api, "draw": "Unentschieden", "away": away_api}
        probs  = {"home": p_home,   "draw": p_draw,          "away": p_away}

        for outcome in ("home", "draw", "away"):
            odds    = best[outcome]
            model_p = probs[outcome]
            if odds < MIN_ODDS:
                continue
            edge, kelly = compute_value(model_p, odds)
            if edge >= MIN_EDGE_PCT / 100:
                bets.append({
                    "bet_type":   "1x2",
                    "sport":      match.get("sport_key", ""),
                    "match":      f"{home_api} – {away_api}",
                    "tip":        labels[outcome],
                    "kick_off":   match["commence_time"],
                    "model_prob": model_p,
                    "best_odds":  odds,
                    "edge_pct":   edge * 100,
                    "kelly_pct":  min(kelly, MAX_KELLY) * 100,
                    "model_src":  f"Club-Elo ({int(elo_home)}/{int(elo_away)})",
                })

    # ── O/U via Poisson ─────────────────────────────────────────────────────
    if euro_model:
        home_model = find_team_in_model(home_api, euro_model["teams"])
        away_model = find_team_in_model(away_api, euro_model["teams"])

        if home_model and away_model:
            probs_eu = predict_football(home_model, away_model, euro_model)
            if probs_eu:
                lam_home = probs_eu["lam_home"]
                lam_away = probs_eu["lam_away"]
                ou_lines = best_ou_odds_from_match(match)

                for entry in ou_lines:
                    line = entry["line"]
                    if abs(line - round(line)) < 1e-9:
                        continue  # ganzzahlige Linien: Push nicht modelliert
                    p_over, p_under = predict_ou(lam_home, lam_away, line)

                    for side, model_p, odds in [
                        ("Über",  p_over,  entry["over_odds"]),
                        ("Unter", p_under, entry["under_odds"]),
                    ]:
                        if odds < MIN_ODDS:
                            continue
                        edge, kelly = compute_value(model_p, odds)
                        if edge >= MIN_EDGE_PCT / 100:
                            bets.append({
                                "bet_type":   "ou",
                                "sport":      match.get("sport_key", ""),
                                "match":      f"{home_api} – {away_api}",
                                "tip":        f"{side} {line}",
                                "kick_off":   match["commence_time"],
                                "model_prob": model_p,
                                "best_odds":  odds,
                                "edge_pct":   edge * 100,
                                "kelly_pct":  min(kelly, MAX_KELLY) * 100,
                                "model_src":  "Poisson",
                                "lam_home":   lam_home,
                                "lam_away":   lam_away,
                            })
    return bets
```

**Step 2: Schnelltest mit Mock-Daten:**
```bash
cd /root/sports_scanner
python3 -c "
import sys; sys.path.insert(0, '.')
import sports_scanner as ss

mock_match = {
    'home_team': 'Bayern Munich',
    'away_team': 'Arsenal',
    'sport_key': 'soccer_uefa_champs_league',
    'commence_time': '2026-03-18T20:00:00Z',
    'bookmakers': [{'markets': [
        {'key': 'h2h', 'outcomes': [
            {'name': 'Bayern Munich', 'price': 1.85},
            {'name': 'Arsenal',       'price': 3.50},
            {'name': 'Draw',          'price': 3.60},
        ]},
        {'key': 'totals', 'outcomes': [
            {'name': 'Over',  'price': 1.80, 'point': 2.5},
            {'name': 'Under', 'price': 2.00, 'point': 2.5},
        ]},
    ]}],
}

# Mock Club-Elo dict
mock_elo = {'Bayern Munich': 1900.0, 'Arsenal': 1800.0}

# Mock Poisson-Modell
mock_euro_model = {
    'attack':  {'Bayern Munich': 0.5, 'Arsenal': 0.3},
    'defense': {'Bayern Munich': 0.1, 'Arsenal': 0.2},
    'home_adv': 0.25,
    'teams': ['Bayern Munich', 'Arsenal'],
}

bets = ss.analyze_uefa_match(mock_match, mock_elo, mock_euro_model)
print(f'{len(bets)} Bet(s) gefunden:')
for b in bets:
    typ = b['bet_type'].upper()
    print(f'  [{typ}] {b[\"tip\"]} @ {b[\"best_odds\"]} | Edge {b[\"edge_pct\"]:.1f}% | Modell: {b[\"model_src\"]}')
    assert b['edge_pct'] >= 3.0
    assert b['kelly_pct'] <= 5.0
    assert b['bet_type'] in ('1x2', 'ou')

# Test: kein Elo → kein 1X2-Bet
bets_no_elo = ss.analyze_uefa_match(mock_match, {}, mock_euro_model)
assert not any(b['bet_type'] == '1x2' for b in bets_no_elo), 'Ohne Elo darf kein 1X2-Bet entstehen'

# Test: kein Modell → kein O/U-Bet
bets_no_model = ss.analyze_uefa_match(mock_match, mock_elo, None)
assert not any(b['bet_type'] == 'ou' for b in bets_no_model), 'Ohne Modell darf kein O/U-Bet entstehen'

print('OK')
"
```
Erwartet: Keine Assertion-Fehler. Anzahl Bets kann 0 oder mehr sein.

**Step 3: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 4: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add analyze_uefa_match() for CL/EL/ECL 1X2 (Club-Elo) and O/U (Poisson)"
```

---

### Task 6: `build_uefa_table` + CSS `.tag3`

**Files:**
- Modify: `sports_scanner.py` — CSS-String (Variable `CSS`) + nach `build_ou_table`

**Kontext:**
`build_uefa_table` ist ähnlich wie `build_football_table`, hat aber eine zusätzliche Spalte „Typ" (1X2 / O/U) und „Modell". Die CSS-Klasse `.tag3` für UEFA-Wettbewerbs-Tags wird in lila/violett gehalten (UEFA-Farbe).

**Step 1: Im CSS-String (Variable `CSS`, ca. Zeile 692) die letzte Zeile vor dem abschließenden `"""` ergänzen:**

Finde im CSS-String die Zeile:
```css
.footer{ color:#777799; font-size:0.78em; margin-top:30px;
         border-top:1px solid #dde3ed; padding-top:14px; }
```

Direkt DAVOR einfügen:
```css
.tag3{ background:#5c2d91; color:#fff; border-radius:4px;
       padding:2px 7px; font-size:0.75em; }
```

**Step 2: Nach `build_ou_table` einfügen:**

```python
def build_uefa_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine UEFA Value Bets gefunden.</div>'
    headers = ["Wettbewerb", "Spiel", "Typ", "Tipp", "Anstoß",
               "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Modell"]
    rows = ""
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        tag = UEFA_LABELS.get(b["sport"], b["sport"])
        ec  = edge_class(b["edge_pct"])
        typ_label = "1X2" if b["bet_type"] == "1x2" else "O/U"
        rows += f"""<tr>
          <td><span class="tag3">{tag}</span></td>
          <td><strong>{b['match']}</strong></td>
          <td>{typ_label}</td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b['model_src']}</td>
        </tr>"""
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{ths}</tr>{rows}</table>"
```

**Step 3: Schnelltest:**
```bash
cd /root/sports_scanner
python3 -c "
import sys; sys.path.insert(0, '.')
import sports_scanner as ss

mock_bets = [
    {
        'bet_type': '1x2', 'sport': 'soccer_uefa_champs_league',
        'match': 'Bayern Munich – Arsenal', 'tip': 'Bayern Munich',
        'kick_off': '2026-03-18T20:00:00Z', 'model_prob': 0.58,
        'best_odds': 1.85, 'edge_pct': 7.3, 'kelly_pct': 2.4,
        'model_src': 'Club-Elo (1900/1800)',
    },
    {
        'bet_type': 'ou', 'sport': 'soccer_uefa_europa_league',
        'match': 'Eintracht Frankfurt – Ajax', 'tip': 'Unter 2.5',
        'kick_off': '2026-03-19T18:45:00Z', 'model_prob': 0.54,
        'best_odds': 2.00, 'edge_pct': 8.0, 'kelly_pct': 2.7,
        'model_src': 'Poisson', 'lam_home': 1.3, 'lam_away': 1.1,
    },
]
html = ss.build_uefa_table(mock_bets)
assert '<table>' in html
assert 'Champions League' in html
assert 'Europa League' in html
assert '1X2' in html
assert 'O/U' in html
assert 'tag3' in html
print('build_uefa_table OK')

html_empty = ss.build_uefa_table([])
assert 'empty' in html_empty
print('build_uefa_table empty OK')
"
```

**Step 4: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 5: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: add build_uefa_table() with .tag3 CSS for UEFA report section"
```

---

### Task 7: `generate_html`, `main`, CSV — Alles zusammenführen

**Files:**
- Modify: `sports_scanner.py` — `generate_html` (aktuell ca. Zeile 798), `main` (ca. Zeile 854), CSV-Export-Block

**Kontext:**
Dies ist der letzte Task. Er verbindet alle vorherigen Bausteine.

**Step 1: `generate_html` Signatur und Inhalt — lese die aktuelle Funktion zuerst**

Signatur ändern von:
```python
def generate_html(football_bets: list, ou_bets: list, tennis_bets: list) -> str:
```
zu:
```python
def generate_html(football_bets: list, ou_bets: list,
                  tennis_bets: list, uefa_bets: list) -> str:
```

`total` und `all_edges` ergänzen:
```python
    total     = len(football_bets) + len(ou_bets) + len(tennis_bets) + len(uefa_bets)
    all_edges = [b["edge_pct"] for b in football_bets + ou_bets + tennis_bets + uefa_bets]
```

Neue Summary-Card einfügen. Den bestehenden Block mit den 4 Cards:
```python
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball 1X2</div></div>
  <div class="card"><div class="val">{len(ou_bets)}</div><div class="lbl">⚽ Über/Unter</div></div>
  <div class="card"><div class="val">{len(tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
```
ersetzen durch:
```python
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball 1X2</div></div>
  <div class="card"><div class="val">{len(ou_bets)}</div><div class="lbl">⚽ Über/Unter</div></div>
  <div class="card"><div class="val">{len(tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
  <div class="card"><div class="val">{len(uefa_bets)}</div><div class="lbl">🏆 UEFA</div></div>
```

Neue UEFA-Sektion im HTML-Body. Direkt VOR der Tennis-Sektion einfügen:
```
<h2>🏆 UEFA Value Bets (Champions / Europa / Conference League)</h2>
{build_uefa_table(uefa_bets)}

```

Footer-Zeile ergänzen — bestehend endet mit:
```python
  Odds: The Odds API<br>
```
erweitern zu:
```python
  Odds: The Odds API &nbsp;|&nbsp;
  UEFA-Modell: Club-Elo + Poisson (football-data.co.uk)<br>
```

**Step 2: `main()` — UEFA-Daten laden und Bets sammeln — lese die aktuelle `main`-Funktion zuerst**

Nach `all_tennis_bets: list = []` ergänzen:
```python
    all_uefa_bets:     list = []
```

Nach dem Tennis-Block (nach dem Tennis-Report-Abschnitt, vor `# ── REPORT ──`) einfügen:
```python
    # ── UEFA ────────────────────────────────────────────────────────────────
    print("\n[🏆 UEFA] Club-Elo-Ratings laden …")
    elo_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    club_elo_dict = download_clubelo(elo_date)
    print(f"  {len(club_elo_dict)} Clubs im Elo-Dict")

    print("\n[🏆 UEFA] Europäisches Poisson-Modell trainieren …")
    euro_df = load_european_data()
    euro_model = None
    if euro_df is not None and len(euro_df) >= 20:
        n_teams = euro_df["HomeTeam"].nunique()
        print(f"  {len(euro_df)} Matches, {n_teams} Teams → trainiere Poisson …")
        try:
            euro_model = fit_poisson_model(euro_df)
            print(f"  OK. Home-Vorteil={euro_model['home_adv']:.3f}")
        except Exception as e:
            print(f"  Modell-Fehler: {e}")
    else:
        print("  Nicht genug Daten für europäisches Modell")

    print("\n[🏆 UEFA] Matches via Odds API …")
    for sport_key in UEFA_SPORTS:
        label = UEFA_LABELS.get(sport_key, sport_key)
        print(f"  {label}:")
        try:
            matches = get_odds(api_key, sport_key)
        except Exception as e:
            print(f"    Fehler: {e}")
            continue
        for match in matches:
            bets = analyze_uefa_match(match, club_elo_dict, euro_model)
            if bets:
                all_uefa_bets.extend(bets)
                for b in bets:
                    typ = b["bet_type"].upper()
                    print(f"    ✓ UEFA VALUE [{typ}]: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
```

Den `generate_html`-Aufruf ändern von:
```python
    html      = generate_html(all_football_bets, all_ou_bets, all_tennis_bets)
```
zu:
```python
    html      = generate_html(all_football_bets, all_ou_bets, all_tennis_bets, all_uefa_bets)
```

Report-Ausgabe ergänzen. Nach `print(f"[📊 Report] O/U Bets: ...")`:
```python
    print(f"[📊 Report] UEFA Bets:      {len(all_uefa_bets)}")
```

**Step 3: CSV-Export — UEFA Bets hinzufügen**

Nach dem O/U-CSV-Block (nach dem `for b in all_ou_bets:` Block), VOR dem Tennis-Block einfügen:
```python
    for b in all_uefa_bets:
        row = {
            "Typ":        f"UEFA {b['bet_type'].upper()}",
            "Liga":       UEFA_LABELS.get(b["sport"], b["sport"]),
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "Modell":     b["model_src"],
        }
        if b["bet_type"] == "ou":
            row["λ-Heim"] = f"{b.get('lam_home', 0):.2f}"
            row["λ-Gast"] = f"{b.get('lam_away', 0):.2f}"
        rows.append(row)
```

**Step 4: Syntax-Check:**
```bash
cd /root/sports_scanner && python3 -c "import sports_scanner; print('Syntax OK')"
```

**Step 5: Vollständiger Trockenlauf:**
```bash
cd /root/sports_scanner
timeout 300 python3 sports_scanner.py 2>&1 | tail -25
```
Erwartet: Kein Traceback. Am Ende:
- `[📊 Report] UEFA Bets: X` (X ≥ 0)
- `✓ Fertig!`

**Step 6: Commit:**
```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: integrate UEFA bets (CL/EL/ECL) into main loop, HTML report, and CSV"
```

---

## Fertig!

Nach Task 7 ist die komplette UEFA-Erweiterung fertig:
- **Club-Elo** liefert 1X2-Wahrscheinlichkeiten für alle CL/EL/ECL-Teams
- **Multi-Liga Poisson** (Top 5 + Bundesliga) liefert O/U-Prognosen
- **HTML-Report** hat eine neue Sektion `🏆 UEFA Value Bets`
- **CSV** enthält UEFA-Zeilen mit Typ `UEFA 1X2` oder `UEFA OU`
