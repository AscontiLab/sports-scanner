#!/usr/bin/env python3
"""
Poisson-Modell (Dixon-Coles-Stil)
─────────────────────────────────
Attack/Defense-Parameter per Maximum-Likelihood mit Time-Decay.
"""

import math
import numpy as np
import pandas as pd
from scipy.stats import poisson
from scipy.optimize import minimize


def fit_poisson_model(df: pd.DataFrame, decay_rate: float = 0.005) -> dict:
    """
    Passt Attack/Defense-Parameter per Maximum-Likelihood an.
    log(lambda_heim) = home_adv + attack[heim] - defense[gast]
    log(lambda_gast) = attack[gast]            - defense[heim]

    Time-Decay: weight = exp(-decay_rate * days_ago)
    Halbwertszeit bei decay_rate=0.005 ~ 140 Tage.
    """
    teams    = sorted(set(df["HomeTeam"]) | set(df["AwayTeam"]))
    n_teams  = len(teams)
    idx      = {t: i for i, t in enumerate(teams)}

    ht = df["HomeTeam"].map(idx).values
    at = df["AwayTeam"].map(idx).values
    hg = df["FTHG"].values.astype(float)
    ag = df["FTAG"].values.astype(float)

    # Time-Decay Gewichte berechnen
    weights = np.ones(len(df))
    if "Date" in df.columns and decay_rate > 0:
        today = pd.Timestamp.now()
        dates = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
        days_ago = (today - dates).dt.days.fillna(180).values.astype(float)
        weights = np.exp(-decay_rate * days_ago)

    n_params = 2 * n_teams + 1
    x0       = np.zeros(n_params)
    x0[-1]   = 0.25  # Home-Vorteil

    def neg_ll(x):
        att = x[:n_teams]
        dfs = x[n_teams:2*n_teams]
        ha  = x[-1]
        lh  = np.exp(ha  + att[ht] - dfs[at])
        la  = np.exp(att[at] - dfs[ht])
        ll  = (hg * np.log(lh + 1e-10) - lh
             + ag * np.log(la + 1e-10) - la)
        return -np.sum(weights * ll)

    constraints = [{"type": "eq", "fun": lambda x: x[0]}]
    res = minimize(neg_ll, x0, method="SLSQP",
                   constraints=constraints,
                   options={"maxiter": 2000, "ftol": 1e-9})
    if not res.success:
        raise RuntimeError(f"Poisson-Fit fehlgeschlagen: {res.message}")

    x   = res.x
    return {
        "attack":   {t: x[i]           for t, i in idx.items()},
        "defense":  {t: x[n_teams + i] for t, i in idx.items()},
        "home_adv": float(x[-1]),
        "teams":    teams,
        "training_matches": int(len(df)),
    }


def predict_football(home: str, away: str, model: dict, max_goals: int = 8):
    """Liefert P(Heim-Sieg), P(Unentschieden), P(Auswaertssieg) via Poisson."""
    attack  = model["attack"]
    defense = model["defense"]
    ha      = model["home_adv"]
    if home not in attack or away not in attack:
        return None
    lh = math.exp(ha  + attack[home] - defense[away])
    la = math.exp(attack[away] - defense[home])
    hp = [poisson.pmf(g, lh) for g in range(max_goals + 1)]
    ap = [poisson.pmf(g, la) for g in range(max_goals + 1)]
    p_h = p_d = p_a = 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = hp[hg] * ap[ag]
            if   hg > ag: p_h += p
            elif hg == ag: p_d += p
            else:          p_a += p
    total = p_h + p_d + p_a
    return {
        "home": p_h / total,
        "draw": p_d / total,
        "away": p_a / total,
        "lam_home": lh,
        "lam_away": la,
    }


def predict_ou(lam_home: float, lam_away: float, line: float) -> tuple[float, float]:
    """
    Berechnet P(Ueber line) und P(Unter line) via Poisson.
    Korrekt fuer .5-Linien (2.5, 3.5) und ganzzahlige Linien (2.0, 3.0).
    Gibt (p_over, p_under) zurueck, die sich zu 1.0 summieren.
    """
    lam_total = lam_home + lam_away
    p_under = float(poisson.cdf(math.floor(line - 1e-9), lam_total))
    p_over  = 1.0 - p_under
    return p_over, p_under


def predict_btts(lam_home: float, lam_away: float) -> float:
    """
    Berechnet P(Beide Teams treffen) via unabhaengige Poisson-Verteilungen.
    P(BTTS) = P(Heim >= 1) x P(Gast >= 1)
    """
    p_home_scores = 1.0 - float(poisson.pmf(0, lam_home))
    p_away_scores = 1.0 - float(poisson.pmf(0, lam_away))
    return p_home_scores * p_away_scores


def predict_most_likely_score(lam_home: float, lam_away: float,
                              max_goals: int = 6,
                              tendency: str | None = None) -> tuple[int, int]:
    """Gibt das wahrscheinlichste (Heim-Tore, Gast-Tore) zurueck.
    Wenn tendency angegeben, wird nur innerhalb der Tendenz gesucht:
      Heimsieg -> home > away, Unentschieden -> home == away,
      Auswärtssieg -> away > home.
    """
    best_p, best_h, best_a = 0.0, 1, 1
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            if tendency == "Heimsieg" and h <= a:
                continue
            if tendency == "Unentschieden" and h != a:
                continue
            if tendency == "Auswärtssieg" and a <= h:
                continue
            p = poisson.pmf(h, lam_home) * poisson.pmf(a, lam_away)
            if p > best_p:
                best_p, best_h, best_a = p, h, a
    return best_h, best_a


def elo_to_football_1x2(elo_home: float, elo_away: float,
                         home_adv: float = 65.0) -> tuple[float, float, float]:
    """
    Konvertiert Club-Elo-Ratings in 1X2-Wahrscheinlichkeiten fuer Fussball.
    home_adv: Heimvorteil in Elo-Punkten (Standard: 65 fuer UEFA-Heimspiele).
    Gibt (p_home, p_draw, p_away) zurueck.
    """
    dr      = elo_home + home_adv - elo_away
    e_home  = 1.0 / (1.0 + 10.0 ** (-dr / 400.0))
    # Unentschieden: max ~28% bei ausgeglichenem Spiel, sinkt bei Favoriten
    p_draw  = 0.28 * math.exp(-2.0 * (e_home - 0.5) ** 2)
    remaining = 1.0 - p_draw
    p_home  = e_home * remaining
    p_away  = (1.0 - e_home) * remaining
    return p_home, p_draw, p_away
