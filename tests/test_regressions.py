import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtesting
import bankroll_manager
import bet_selector


def test_record_daily_snapshot_recomputes_same_day_bankroll(tmp_path):
    db_path = tmp_path / "sports_backtesting.db"
    backtesting.init_db(db_path)
    bankroll_manager._DB_PATH = db_path
    bankroll_manager.init_bankroll(100.0)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                run_id, odds_api_match_id, sport_key, bet_type,
                home_team, away_team, commence_time,
                tip, outcome_side, ou_line,
                model_prob, model_source, lam_home, lam_away, elo_home, elo_away,
                best_odds, best_odds_bookie, consensus_prob, overround,
                edge_pct, kelly_pct, stake_units, selected, bet_won, pnl_eur
            ) VALUES (
                1, NULL, 'soccer_epl', '1x2',
                'A', 'B', '2026-03-14T12:00:00Z',
                'A', 'home', NULL,
                0.55, 'Poisson', NULL, NULL, NULL, NULL,
                2.0, 'bm1', 0.5, 0.06,
                5.0, 1.0, 1.0, 1, 1, 10.0
            )
            """
        )

    bankroll_manager.record_daily_snapshot("2026-03-14")
    with sqlite3.connect(db_path) as conn:
        bankroll = conn.execute(
            "SELECT bankroll FROM bankroll_snapshots WHERE date = '2026-03-14'"
        ).fetchone()[0]
    assert bankroll == 110.0

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE predictions
            SET pnl_eur = 20.0
            WHERE commence_time = '2026-03-14T12:00:00Z'
            """
        )

    bankroll_manager.record_daily_snapshot("2026-03-14")
    with sqlite3.connect(db_path) as conn:
        bankroll = conn.execute(
            "SELECT bankroll FROM bankroll_snapshots WHERE date = '2026-03-14'"
        ).fetchone()[0]
    assert bankroll == 120.0


def test_log_prediction_stores_outcome_specific_consensus(tmp_path):
    db_path = tmp_path / "sports_backtesting.db"
    backtesting.init_db(db_path)
    run_id = backtesting.log_scan_run("2026-03-14T10:00:00Z", model_version="test")

    match_raw = {
        "id": "match-1",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": "bm1",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Home", "price": 2.0},
                            {"name": "Draw", "price": 3.5},
                            {"name": "Away", "price": 4.0},
                        ],
                    }
                ],
            },
            {
                "key": "bm2",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Home", "price": 2.2},
                            {"name": "Draw", "price": 3.4},
                            {"name": "Away", "price": 3.8},
                        ],
                    }
                ],
            },
        ],
    }
    bet = {
        "sport": "soccer_epl",
        "match": "Home – Away",
        "tip": "Away",
        "kick_off": "2026-03-14T18:00:00Z",
        "model_prob": 0.31,
        "best_odds": 4.0,
        "edge_pct": 5.0,
        "kelly_pct": 1.2,
    }

    prediction_id = backtesting.log_prediction(run_id, bet, match_raw=match_raw)

    with sqlite3.connect(db_path) as conn:
        consensus_prob, best_bookie = conn.execute(
            "SELECT consensus_prob, best_odds_bookie FROM predictions WHERE id = ?",
            (prediction_id,),
        ).fetchone()

    expected_away_consensus = ((1 / 4.0) / (1 / 2.0 + 1 / 3.5 + 1 / 4.0) +
                               (1 / 3.8) / (1 / 2.2 + 1 / 3.4 + 1 / 3.8)) / 2
    assert round(consensus_prob, 6) == round(expected_away_consensus, 6)
    assert best_bookie == "bm1"


def test_confidence_score_uses_bet_specific_training_matches():
    base_bet = {
        "type": "tennis",
        "model_source": "Elo",
        "model_prob": 0.58,
        "consensus_prob": 0.54,
        "best_odds": 2.1,
        "edge_pct": 8.0,
        "overround": 0.06,
    }
    model_stats = {"Elo": {"resolved": 0, "won": 0, "roi_pct": 0.0}}

    low_depth = bet_selector.compute_confidence_score(
        {**base_bet, "training_matches": 50},
        model_stats,
    )
    high_depth = bet_selector.compute_confidence_score(
        {**base_bet, "training_matches": 5000},
        model_stats,
    )

    assert high_depth > low_depth
