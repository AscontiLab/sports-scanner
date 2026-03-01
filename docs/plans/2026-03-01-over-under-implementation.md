# Over/Under Value Bets — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fußball Über/Unter-Wahrscheinlichkeiten aus dem bestehenden Poisson-Modell berechnen und als eigene Value-Bet-Sektion im Report ausgeben.

**Architecture:** Das bestehende Poisson-Modell liefert bereits `lam_home` und `lam_away`. Die Summe zweier unabhängiger Poisson-Variablen ist wieder Poisson mit `λ_total = lam_home + lam_away`, womit `P(Über/Unter X.5)` direkt berechenbar ist. Der `totals`-Market der Odds API wird zusammen mit `h2h` in einem einzigen API-Call abgefragt.

**Tech Stack:** Python 3.10+, scipy.stats.poisson (bereits vorhanden), The Odds API v4

---

### Task 1: API-Call um `totals`-Market erweitern

**Files:**
- Modify: `sports_scanner.py:110` (markets Parameter in `get_odds`)

**Kontext:**
Die `get_odds`-Funktion (Zeile 107–122) holt aktuell nur den `h2h`-Market. Die Odds API gibt `totals` Daten im selben Call zurück, wenn `markets=h2h,totals` gesetzt ist. Der `totals`-Market sieht so aus:
```json
{
  "key": "totals",
  "outcomes": [
    {"name": "Over",  "price": 1.85, "point": 2.5},
    {"name": "Under", "price": 1.95, "point": 2.5}
  ]
}
```

**Step 1: Zeile 110 in `sports_scanner.py` ändern**

Alte Zeile:
```python
        "markets":    "h2h",
```

Neue Zeile:
```python
        "markets":    "h2h,totals",
```

**Step 2: Manuell verifizieren**

```bash
cd /root/sports_scanner
python3 -c "
import json, sys
sys.path.insert(0, '.')
from sports_scanner import load_creds, get_odds
creds = load_creds()
matches = get_odds(creds['ODDS_API_KEY'], 'soccer_germany_bundesliga')
if matches:
    bm = matches[0].get('bookmakers', [])
    for b in bm[:2]:
        for m in b.get('markets', []):
            if m['key'] == 'totals':
                print('totals OK:', m['outcomes'])
                break
    else:
        print('Kein totals-Market (Liga evtl. keine laufenden Spiele)')
else:
    print('Keine Matches')
"
```

Erwartetes Ergebnis: Entweder `totals OK: [...]` oder Hinweis dass kein Spieltag läuft (beides OK).

**Step 3: Commit**

```bash
cd /root/sports_scanner
git add sports_scanner.py
git commit -m "feat: fetch totals market alongside h2h in single API call"
```

---

### Task 2: `predict_ou` — Über/Unter-Wahrscheinlichkeit berechnen

**Files:**
- Modify: `sports_scanner.py` — neue Funktion nach `predict_football` (nach Zeile 343)

**Kontext:**
`predict_football` gibt bereits `lam_home` und `lam_away` zurück. Wir nutzen:
- `λ_total = lam_home + lam_away`
- `P(Unter X.5) = poisson.cdf(floor(X.5), λ_total)` — also `poisson.cdf(2, λ)` für 2.5-Linie
- `P(Über X.5) = 1 - P(Unter X.5)`

**Step 1: Funktion nach `predict_football` (nach Zeile 343) einfügen**

```python
def predict_ou(lam_home: float, lam_away: float, line: float) -> tuple[float, float]:
    """
    Berechnet P(Über line) und P(Unter line) via Poisson.
    Beispiel: line=2.5 → P(Unter 2.5) = P(total ≤ 2)
    """
    lam_total = lam_home + lam_away
    p_under = float(poisson.cdf(int(line), lam_total))
    p_over  = 1.0 - p_under
    return p_over, p_under
```

**Step 2: Schnelltest in Python**

```bash
python3 -c "
from scipy.stats import poisson
def predict_ou(lam_home, lam_away, line):
    lam_total = lam_home + lam_away
    p_under = float(poisson.cdf(int(line), lam_total))
    p_over  = 1.0 - p_under
    return p_over, p_under

# Erwartete Tore je 1.5 → λ_total=3.0
# P(Über 2.5) = P(total>=3) bei λ=3 ≈ 0.577
p_over, p_under = predict_ou(1.5, 1.5, 2.5)
print(f'Über 2.5: {p_over:.3f}, Unter 2.5: {p_under:.3f}')
assert 0.55 < p_over < 0.65, f'Unerwartet: {p_over}'
assert abs(p_over + p_under - 1.0) < 1e-9

# Wenig Tore: λ_total=1.5 → Unter 2.5 wahrscheinlicher
p_over2, p_under2 = predict_ou(0.7, 0.8, 2.5)
print(f'Über 2.5: {p_over2:.3f}, Unter 2.5: {p_under2:.3f}')
assert p_under2 > p_over2
print('OK')
"
```

Erwartetes Ergebnis:
```
Über 2.5: 0.577, Unter 2.5: 0.423
Über 2.5: 0.191, Unter 2.5: 0.809
OK
```

**Step 3: Commit**

```bash
git add sports_scanner.py
git commit -m "feat: add predict_ou() for over/under probability via Poisson"
```

---

### Task 3: `best_ou_odds_from_match` — Beste O/U-Quoten extrahieren

**Files:**
- Modify: `sports_scanner.py` — neue Funktion nach `best_odds_from_match` (nach Zeile 142)

**Kontext:**
Die Odds API liefert `totals`-Outcomes mit `name` ("Over"/"Under") und `point` (die Linie z.B. 2.5). Wir wollen für jede Linie die besten Over- und Under-Quoten über alle Bookies hinweg.

**Step 1: Funktion nach `best_odds_from_match` (nach Zeile 142) einfügen**

```python
def best_ou_odds_from_match(match: dict) -> list[dict]:
    """
    Extrahiert die besten Over/Under-Quoten pro Linie aus allen Bookies.
    Gibt Liste von {line, over_odds, under_odds} zurück.
    """
    best: dict[float, dict] = {}  # line → {over: float, under: float}
    for bm in match.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] != "totals":
                continue
            for o in market["outcomes"]:
                line  = float(o.get("point", 0))
                price = float(o["price"])
                side  = o["name"].lower()  # "over" or "under"
                if line not in best:
                    best[line] = {"over": 1.0, "under": 1.0}
                best[line][side] = max(best[line][side], price)
    result = []
    for line, odds in sorted(best.items()):
        if odds["over"] > 1.0 and odds["under"] > 1.0:
            result.append({"line": line, "over_odds": odds["over"], "under_odds": odds["under"]})
    return result
```

**Step 2: Schnelltest**

```bash
python3 -c "
def best_ou_odds_from_match(match):
    best = {}
    for bm in match.get('bookmakers', []):
        for market in bm.get('markets', []):
            if market['key'] != 'totals': continue
            for o in market['outcomes']:
                line  = float(o.get('point', 0))
                price = float(o['price'])
                side  = o['name'].lower()
                if line not in best: best[line] = {'over': 1.0, 'under': 1.0}
                best[line][side] = max(best[line][side], price)
    result = []
    for line, odds in sorted(best.items()):
        if odds['over'] > 1.0 and odds['under'] > 1.0:
            result.append({'line': line, 'over_odds': odds['over'], 'under_odds': odds['under']})
    return result

mock = {'bookmakers': [
    {'markets': [{'key': 'totals', 'outcomes': [
        {'name': 'Over',  'price': 1.85, 'point': 2.5},
        {'name': 'Under', 'price': 1.95, 'point': 2.5},
        {'name': 'Over',  'price': 2.10, 'point': 3.5},
        {'name': 'Under', 'price': 1.70, 'point': 3.5},
    ]}]},
    {'markets': [{'key': 'totals', 'outcomes': [
        {'name': 'Over',  'price': 1.90, 'point': 2.5},  # besser als 1.85
        {'name': 'Under', 'price': 1.93, 'point': 2.5},
    ]}]},
]}
result = best_ou_odds_from_match(mock)
print(result)
assert len(result) == 2
assert result[0]['line'] == 2.5
assert result[0]['over_odds'] == 1.90, f'{result[0][\"over_odds\"]} != 1.90'
assert result[1]['line'] == 3.5
print('OK')
"
```

Erwartetes Ergebnis:
```
[{'line': 2.5, 'over_odds': 1.9, 'under_odds': 1.95}, {'line': 3.5, 'over_odds': 2.1, 'under_odds': 1.7}]
OK
```

**Step 3: Commit**

```bash
git add sports_scanner.py
git commit -m "feat: add best_ou_odds_from_match() to extract best O/U odds per line"
```

---

### Task 4: `analyze_football_ou` — O/U Value Bets berechnen

**Files:**
- Modify: `sports_scanner.py` — neue Funktion nach `analyze_football_match` (nach Zeile 541)

**Kontext:**
Diese Funktion kombiniert `predict_football` (→ lam_home, lam_away), `best_ou_odds_from_match` (→ Linien + Quoten), `predict_ou` (→ p_over, p_under) und `compute_value` (→ edge, kelly). Struktur analog zu `analyze_football_match`.

**Step 1: Funktion nach `analyze_football_match` einfügen**

```python
def analyze_football_ou(match: dict, model: dict) -> list:
    """Value Bets für Über/Unter-Märkte via Poisson-Modell."""
    home_api = match["home_team"]
    away_api = match["away_team"]
    model_teams = model["teams"]

    home_model = find_team_in_model(home_api, model_teams)
    away_model = find_team_in_model(away_api, model_teams)
    if not home_model or not away_model:
        return []

    probs = predict_football(home_model, away_model, model)
    if not probs:
        return []

    lam_home = probs["lam_home"]
    lam_away = probs["lam_away"]
    ou_lines  = best_ou_odds_from_match(match)
    bets      = []

    for entry in ou_lines:
        line       = entry["line"]
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
                    "type":       "football_ou",
                    "sport":      match.get("sport_key", ""),
                    "match":      f"{home_api} – {away_api}",
                    "line":       line,
                    "tip":        f"{side} {line}",
                    "kick_off":   match["commence_time"],
                    "model_prob": model_p,
                    "best_odds":  odds,
                    "edge_pct":   edge * 100,
                    "kelly_pct":  min(kelly, MAX_KELLY) * 100,
                    "lam_home":   lam_home,
                    "lam_away":   lam_away,
                })
    return bets
```

**Step 2: Schnelltest mit Mock-Daten**

```bash
python3 -c "
import sys; sys.path.insert(0, '/root/sports_scanner')
# Patch get_odds to avoid real API call
import sports_scanner as ss

mock_match = {
    'home_team': 'Bayern Munich',
    'away_team': 'Borussia Dortmund',
    'sport_key': 'soccer_germany_bundesliga',
    'commence_time': '2026-03-15T14:30:00Z',
    'bookmakers': [{'markets': [
        {'key': 'h2h', 'outcomes': [
            {'name': 'Bayern Munich',       'price': 1.6},
            {'name': 'Borussia Dortmund',  'price': 4.5},
            {'name': 'Draw',                'price': 4.0},
        ]},
        {'key': 'totals', 'outcomes': [
            {'name': 'Over',  'price': 1.70, 'point': 2.5},
            {'name': 'Under', 'price': 2.10, 'point': 2.5},
            {'name': 'Over',  'price': 2.50, 'point': 3.5},
            {'name': 'Under', 'price': 1.50, 'point': 3.5},
        ]},
    ]}],
}

# Einfaches Mock-Modell
mock_model = {
    'attack':  {'Bayern Munich': 0.5, 'Borussia Dortmund': 0.3},
    'defense': {'Bayern Munich': 0.1, 'Borussia Dortmund': 0.2},
    'home_adv': 0.25,
    'teams': ['Bayern Munich', 'Borussia Dortmund'],
}
bets = ss.analyze_football_ou(mock_match, mock_model)
print(f'{len(bets)} Bet(s) gefunden:')
for b in bets:
    print(f'  {b[\"tip\"]} @ {b[\"best_odds\"]} | Edge {b[\"edge_pct\"]:.1f}% | Kelly {b[\"kelly_pct\"]:.1f}%')
print('OK')
"
```

Erwartetes Ergebnis: Funktion läuft ohne Fehler, gibt 0 oder mehr Bets zurück (je nach Mock-Werten — kein Assertion-Fehler).

**Step 3: Commit**

```bash
git add sports_scanner.py
git commit -m "feat: add analyze_football_ou() for over/under value bet detection"
```

---

### Task 5: `build_ou_table` — HTML-Tabelle für O/U Bets

**Files:**
- Modify: `sports_scanner.py` — neue Funktion nach `build_football_table` (nach Zeile 660)

**Step 1: Funktion einfügen**

```python
def build_ou_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine Über/Unter Value Bets gefunden.</div>'
    headers = ["Liga", "Spiel", "Tipp", "Anstoß", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "λ Heim", "λ Gast"]
    rows = ""
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        tag = SPORT_LABELS.get(b["sport"], b["sport"])
        ec  = edge_class(b["edge_pct"])
        rows += f"""<tr>
          <td><span class="tag">{tag}</span></td>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b['lam_home']:.2f}</td>
          <td style="color:#8b949e">{b['lam_away']:.2f}</td>
        </tr>"""
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{ths}</tr>{rows}</table>"
```

**Step 2: Schnelltest**

```bash
python3 -c "
import sys; sys.path.insert(0, '/root/sports_scanner')
import sports_scanner as ss

mock_bets = [{
    'type': 'football_ou', 'sport': 'soccer_germany_bundesliga',
    'match': 'Bayern Munich – Dortmund', 'tip': 'Über 2.5',
    'kick_off': '2026-03-15T14:30:00Z', 'model_prob': 0.62,
    'best_odds': 1.85, 'edge_pct': 14.7, 'kelly_pct': 3.1,
    'lam_home': 2.1, 'lam_away': 1.3,
}]
html = ss.build_ou_table(mock_bets)
assert '<table>' in html
assert 'Über 2.5' in html
assert '14.7%' in html
print('build_ou_table OK')

# Leere Liste
html_empty = ss.build_ou_table([])
assert 'empty' in html_empty
print('build_ou_table empty OK')
"
```

**Step 3: Commit**

```bash
git add sports_scanner.py
git commit -m "feat: add build_ou_table() for HTML report section"
```

---

### Task 6: `generate_html` und `main` aktualisieren

**Files:**
- Modify: `sports_scanner.py:687–732` (`generate_html`)
- Modify: `sports_scanner.py:754–798` (`main`, Football-Analyse-Loop)
- Modify: `sports_scanner.py:844–876` (CSV-Export)

**Step 1: `generate_html` um O/U-Parameter und -Sektion erweitern**

Funktionssignatur von:
```python
def generate_html(football_bets: list, tennis_bets: list) -> str:
```
zu:
```python
def generate_html(football_bets: list, ou_bets: list, tennis_bets: list) -> str:
```

In der Funktion die Zusammenfassung-Karte ergänzen. Bestehender Block:
```python
    total     = len(football_bets) + len(tennis_bets)
    all_edges = [b["edge_pct"] for b in football_bets + tennis_bets]
```
ersetzen durch:
```python
    total     = len(football_bets) + len(ou_bets) + len(tennis_bets)
    all_edges = [b["edge_pct"] for b in football_bets + ou_bets + tennis_bets]
```

Im HTML-Body die Summary-Karten — bestehend:
```python
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball</div></div>
  <div class="card"><div class="val">{len(tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
```
ersetzen durch:
```python
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball 1X2</div></div>
  <div class="card"><div class="val">{len(ou_bets)}</div><div class="lbl">⚽ Über/Unter</div></div>
  <div class="card"><div class="val">{len(tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
```

Nach der Fußball-Sektion die neue O/U-Sektion einfügen — bestehend:
```python
<h2>🎾 Tennis Value Bets (Elo-Modell)</h2>
```
davor einfügen:
```python
<h2>⚽ Über/Unter Value Bets (Poisson-Modell)</h2>
{build_ou_table(ou_bets)}

```

**Step 2: `main` — O/U Bets sammeln**

Nach `all_football_bets: list = []` eine neue Liste ergänzen:
```python
    all_ou_bets:       list = []
```

Im Football-Analyse-Loop (nach `bets = analyze_football_match(match, model)`) die O/U-Analyse anfügen:
```python
            ou_bets_match = analyze_football_ou(match, model)
            if ou_bets_match:
                all_ou_bets.extend(ou_bets_match)
                for b in ou_bets_match:
                    print(f"    ✓ O/U VALUE: {b['match']} → {b['tip']} "
                          f"@ {b['best_odds']:.2f} | Edge {b['edge_pct']:.1f}%")
```

Den `generate_html`-Aufruf anpassen:
```python
    html = generate_html(all_football_bets, all_ou_bets, all_tennis_bets)
```

Report-Ausgabe ergänzen:
```python
    print(f"[📊 Report] O/U Bets:       {len(all_ou_bets)}")
```

**Step 3: CSV-Export — O/U Bets hinzufügen**

Nach dem Football-CSV-Block (nach `rows.append({...lam_away...})`), vor dem Tennis-Block, einfügen:
```python
    for b in all_ou_bets:
        rows.append({
            "Typ":        "Fußball O/U",
            "Liga":       SPORT_LABELS.get(b["sport"], b["sport"]),
            "Spiel":      b["match"],
            "Tipp":       b["tip"],
            "Anstoß":     b["kick_off"],
            "Modell-%":   f"{b['model_prob']*100:.1f}",
            "BestOdds":   f"{b['best_odds']:.2f}",
            "Edge-%":     f"{b['edge_pct']:.1f}",
            "Kelly-%":    f"{b['kelly_pct']:.1f}",
            "λ-Heim":     f"{b['lam_home']:.2f}",
            "λ-Gast":     f"{b['lam_away']:.2f}",
        })
```

**Step 4: Syntax-Check**

```bash
cd /root/sports_scanner
python3 -c "import sports_scanner; print('Syntax OK')"
```

Erwartetes Ergebnis: `Syntax OK`

**Step 5: Vollständigen Trockenlauf**

```bash
python3 sports_scanner.py 2>&1 | head -60
```

Erwartetes Ergebnis: Kein Traceback, Ausgabe enthält `O/U VALUE` oder `O/U Bets: 0` am Ende.

**Step 6: Commit**

```bash
git add sports_scanner.py
git commit -m "feat: integrate over/under bets into main loop, HTML report, and CSV export"
```

---

## Fertig!

Nach Task 6 sind alle Änderungen committed. Der Scanner:
- Ruft `totals`-Market in einem API-Call ab
- Berechnet Über/Unter-Wahrscheinlichkeiten per Poisson
- Zeigt O/U Value Bets in eigener Sektion im HTML-Report
- Exportiert O/U Bets ins CSV
