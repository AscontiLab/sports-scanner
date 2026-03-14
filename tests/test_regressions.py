import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtesting
import bankroll_manager
import bet_selector
import serve_output
import sports_scanner


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


def test_normalize_hub_signal_builds_explainability_payload():
    bet = {
        "sport": "soccer_epl",
        "match": "Arsenal – Chelsea",
        "tip": "Chelsea",
        "kick_off": "2026-03-14T18:00:00Z",
        "type": "football",
        "outcome_side": "away",
        "model_prob": 0.54,
        "consensus_prob": 0.48,
        "edge_pct": 8.2,
        "best_odds": 3.4,
        "overround": 0.06,
        "stake_eur": 7.5,
        "training_matches": 4234,
        "confidence_score": 78,
        "tier": "Strong Pick",
        "selected": 1,
    }

    signal = sports_scanner._normalize_hub_signal(bet, "sports-2026-03-14T09:30:00+00:00")

    assert signal["system"] == "sports-scanner"
    assert signal["status"] == "selected"
    assert signal["priority"] == 78
    assert signal["entity"]["market"] == "1x2"
    assert signal["entity"]["side"] == "away"
    assert signal["metrics"]["training_matches"] == 4234
    assert signal["explainability"]["version"] == "v1"
    assert signal["explainability"]["why_now"]
    assert signal["explainability"]["drivers"]


def test_write_hub_exports_preserves_other_systems(tmp_path):
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    (hub_dir / "latest_runs.json").write_text(
        '[{"run_id":"stock-1","system":"stock-scanner","generated_at":"2026-03-14T06:00:00Z"}]',
        encoding="utf-8",
    )
    (hub_dir / "latest_signals.json").write_text(
        '[{"signal_id":"stock:a","system":"stock-scanner","title":"NVDA"}]',
        encoding="utf-8",
    )

    run_payload = {
        "run_id": "sports-1",
        "system": "sports-scanner",
        "generated_at": "2026-03-14T09:00:00Z",
        "status": "ok",
        "summary": {"total_candidates": 1, "selected_count": 1, "watch_count": 0, "warnings_count": 0},
    }
    signals = [
        {
            "signal_id": "sports:a",
            "run_id": "sports-1",
            "system": "sports-scanner",
            "category": "bet",
            "status": "selected",
            "priority": 80,
            "title": "Chelsea @ 3.40",
            "entity": {},
            "timing": {},
            "metrics": {},
            "explainability": {
                "summary": "x",
                "why_now": ["x"],
                "model_basis": ["x"],
                "confidence_reason": ["x"],
                "risk_flags": ["x"],
                "invalidators": ["x"],
                "version": "v1",
            },
        }
    ]

    sports_scanner._write_hub_exports(run_payload, signals, hub_dir=hub_dir)

    runs = json.loads((hub_dir / "latest_runs.json").read_text(encoding="utf-8"))
    exported_signals = json.loads((hub_dir / "latest_signals.json").read_text(encoding="utf-8"))

    assert {item["system"] for item in runs} == {"sports-scanner", "stock-scanner"}
    assert {item["system"] for item in exported_signals} == {"sports-scanner", "stock-scanner"}


def test_sort_signals_orders_by_priority_desc():
    signals = [
        {"title": "B", "priority": 55, "timing": {"event_time": "2026-03-14T10:00:00Z"}},
        {"title": "A", "priority": 80, "timing": {"event_time": "2026-03-14T09:00:00Z"}},
        {"title": "C", "priority": 80, "timing": {"event_time": "2026-03-14T11:00:00Z"}},
    ]

    sorted_signals = serve_output._sort_signals(signals)

    assert [s["title"] for s in sorted_signals] == ["C", "A", "B"]
