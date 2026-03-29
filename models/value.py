#!/usr/bin/env python3
"""
Value Betting Berechnungen
──────────────────────────
Edge- und Kelly-Berechnung fuer Value Bets.
"""


def compute_value(model_prob: float, odds: float) -> tuple[float, float]:
    """
    Edge  = model_prob x odds - 1
    Kelly = edge / (odds - 1)
    """
    if odds <= 1.0 or model_prob <= 0:
        return 0.0, 0.0
    edge  = model_prob * odds - 1.0
    kelly = edge / (odds - 1.0)
    return edge, kelly
