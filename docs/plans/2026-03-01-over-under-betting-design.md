# Design: Über/Unter Value Bets (Over/Under)

**Datum:** 2026-03-01
**Scope:** Nur Fußball (1. / 2. / 3. Bundesliga)

---

## Ziel

Das bestehende Poisson-Modell liefert bereits erwartete Tore pro Team (`lam_home`, `lam_away`).
Daraus lassen sich direkt Über/Unter-Wahrscheinlichkeiten für alle verfügbaren Tor-Linien
berechnen und mit den Bookie-Quoten auf Value prüfen.

---

## Mathematische Grundlage

Die Summe zweier unabhängiger Poisson-Variablen ist Poisson mit Rate:

```
λ_total = lam_home + lam_away
P(Unter X.5) = Poisson.CDF(floor(X), λ_total)
P(Über X.5)  = 1 - P(Unter X.5)
```

---

## Änderungen

### 1. `get_odds()` — API-Call

- `markets` von `"h2h"` auf `"h2h,totals"` erweitern
- Ein API-Call liefert beide Märkte (kein zusätzlicher Credit-Verbrauch)

### 2. Neue Funktion `predict_ou(lam_home, lam_away, line)`

```
Eingabe:  lam_home, lam_away (float), line (float z.B. 2.5)
Ausgabe:  (p_over, p_under) als Wahrscheinlichkeiten
Logik:    λ_total = lam_home + lam_away
          p_under = poisson.cdf(int(line), λ_total)
          p_over  = 1 - p_under
```

### 3. Neue Funktion `best_ou_odds_from_match(match)`

- Liest `totals` Market aus Bookmaker-Daten
- Liefert Liste: `[{line: 2.5, over_odds: 1.85, under_odds: 1.95}, ...]`
- Beste Quote pro Linie über alle Bookies

### 4. Neue Funktion `analyze_football_ou(match, model)`

- Berechnet `lam_home`, `lam_away` via `predict_football`
- Für jede verfügbare Linie: `predict_ou` → Edge/Kelly berechnen
- Schwellwerte: Edge ≥ 3%, Quote ≥ 1.25 (identisch zu 1X2)
- Gibt Liste von Bet-Dicts zurück

### 5. Neue Funktion `build_ou_table(bets)`

Spalten: Liga | Spiel | Linie | Tipp | Anstoß | Modell-% | Beste Quote | Edge-% | Kelly-%

### 6. `generate_html()` — Neue Sektion

- Sektion `⚽ Über/Unter Value Bets` zwischen Fußball-1X2 und Tennis
- Summary-Card `O/U` mit Anzahl gefundener Bets

### 7. CSV-Export

- O/U Bets als separate Zeilen mit Typ `"Fußball O/U"`

---

## Datenfluss

```
get_odds(markets="h2h,totals")
    → match enthält beide Märkte
    → analyze_football_match()      → 1X2 Bets (wie bisher)
    → analyze_football_ou()         → O/U Bets (neu)
        → predict_football()        → lam_home, lam_away
        → best_ou_odds_from_match() → Linien + Quoten
        → predict_ou()              → p_over, p_under
        → compute_value()           → edge, kelly
```

---

## Schwellwerte (wie 1X2)

- MIN_EDGE_PCT = 3.0%
- MIN_ODDS = 1.25
- MAX_KELLY = 5%
