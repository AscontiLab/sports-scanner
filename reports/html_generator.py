#!/usr/bin/env python3
"""
HTML-Report Generator
─────────────────────
Erzeugt HTML-Reports fuer Value Bets, Kicktipp und BTTS.
"""

from datetime import datetime

from config import (
    SPORT_LABELS, UEFA_LABELS, MIN_EDGE_PCT, MAX_EDGE_PCT, MIN_ODDS,
)
from models.poisson import predict_most_likely_score
from backtesting import get_summary
from bankroll_manager import get_daily_budget, get_peak_and_drawdown


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{--bg:#08080f;--surface:rgba(15,15,35,0.8);--border:rgba(0,240,255,0.15);--cyan:#00f0ff;--gold:#c8aa6e;--pink:#ff006e;--green:#00ff88;--text:#e8e8f0;--dim:#6e6e80}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Inter,sans-serif;background:var(--bg);color:var(--text);padding:20px;min-height:100vh}
h1{color:var(--cyan);font-size:1.6em;padding-bottom:10px;border-bottom:1px solid var(--border);margin-bottom:6px}
h2{color:var(--gold);margin-top:32px;font-size:1.15em;padding-left:10px;border-left:3px solid var(--gold)}
.summary{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
      padding:14px 22px;min-width:130px;backdrop-filter:blur(16px);transition:all .25s}
.card:hover{border-color:var(--cyan);box-shadow:0 0 20px rgba(0,240,255,0.1)}
.card .val{font-family:'JetBrains Mono',monospace;font-size:1.9em;font-weight:700;color:var(--cyan)}
.card .lbl{color:var(--dim);font-size:0.8em;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--surface);
      border:1px solid var(--border);border-radius:12px;overflow:hidden;margin:14px 0}
th{background:rgba(0,240,255,0.06);padding:9px 12px;text-align:left;
   color:var(--gold);font-size:0.78em;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:0.88em;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,240,255,0.04)}
.g{color:var(--green);font-weight:700}
.y{color:var(--gold);font-weight:700}
.o{color:var(--pink);font-weight:700}
.tag{background:var(--cyan);color:var(--bg);border-radius:4px;
     padding:2px 7px;font-size:0.75em;font-weight:600}
.tag2{background:rgba(0,240,255,0.1);color:var(--cyan);border:1px solid var(--border);
      border-radius:4px;padding:2px 7px;font-size:0.75em}
.empty{color:var(--dim);padding:18px;text-align:center;
       background:var(--surface);border:1px solid var(--border);border-radius:12px}
.note{background:rgba(200,170,110,0.08);border-left:3px solid var(--gold);padding:10px 14px;
      color:var(--gold);font-size:0.82em;border-radius:0 8px 8px 0;margin:10px 0}
.tag3{background:rgba(255,0,110,0.15);color:var(--pink);border:1px solid rgba(255,0,110,0.3);
      border-radius:4px;padding:2px 7px;font-size:0.75em}
.league-header{color:var(--text);font-size:0.95em;margin:18px 0 4px 0;padding:0;border:none}
.league-header .tag,.league-header .tag2,.league-header .tag3{font-size:0.85em;padding:3px 10px}
details{margin:10px 0}
details summary{list-style:none}
details summary::-webkit-details-marker{display:none}
details[open] summary{margin-bottom:10px}
.footer{color:var(--dim);font-size:0.78em;margin-top:30px;
        border-top:1px solid var(--border);padding-top:14px}
"""


def format_dt(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        return iso_str[:16]


def edge_class(e: float) -> str:
    if e >= 10: return "g"
    if e >= 5:  return "y"
    return "o"


def _group_by_league(bets: list, label_map: dict, key: str = "sport") -> dict:
    """Gruppiert Bets nach Liga/Turnier, sortiert innerhalb nach Anstoss + Edge."""
    groups = {}
    for b in bets:
        raw_league = (
            b.get(key)
            or b.get("sport")
            or b.get("sport_key")
            or b.get("tournament")
            or b.get("league")
            or "Unbekannt"
        )
        league = label_map.get(raw_league, raw_league) if label_map else raw_league
        groups.setdefault(league, []).append(b)
    for league in groups:
        groups[league].sort(key=lambda x: (x["kick_off"], -x.get("edge_pct", x.get("p_btts_yes", 0))))
    # Sortiere Ligen nach fruehestem Anstoss
    return dict(sorted(groups.items(), key=lambda kv: kv[1][0]["kick_off"]))


def _build_bet_table(bets: list, empty_msg: str) -> str:
    """Gemeinsame Logik fuer Football- und O/U-Tabellen."""
    if not bets:
        return f'<div class="empty">{empty_msg}</div>'
    headers = ["Spiel", "Tipp", "Anstoß", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "λ Heim", "λ Gast"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_bets in _group_by_league(bets, SPORT_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag">{league}</span> ({len(league_bets)})</h3>'
        rows = ""
        for b in league_bets:
            ec = edge_class(b["edge_pct"])
            rows += f"""<tr>
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
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_football_table(bets: list) -> str:
    return _build_bet_table(bets, "Keine Value Bets gefunden.")


def build_ou_table(bets: list) -> str:
    return _build_bet_table(bets, "Keine Over/Under Value Bets gefunden.")


def build_btts_table(signals: list) -> str:
    """Baut die BTTS-Signaltabelle (Beide Teams treffen)."""
    if not signals:
        return '<div class="empty">Keine BTTS-Signale vorhanden.</div>'
    headers = ["Spiel", "Anstoß", "BTTS Ja", "BTTS Nein", "Signal", "λ Heim", "λ Gast"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_sigs in _group_by_league(signals, SPORT_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag2">{league}</span> ({len(league_sigs)})</h3>'
        rows = ""
        for s in sorted(league_sigs, key=lambda x: (x["kick_off"], -x["p_btts_yes"])):
            yes_pct = s["p_btts_yes"]
            if yes_pct >= 65:
                clr = "var(--green, #00ff88)"
                icon = "🟢"
            elif yes_pct >= 55:
                clr = "var(--gold, #c8aa6e)"
                icon = "🟡"
            else:
                clr = "var(--dim, #6e6e80)"
                icon = "⚪"
            rows += f"""<tr>
          <td><strong>{s['match']}</strong></td>
          <td>{format_dt(s['kick_off'])}</td>
          <td style="color:{clr};font-weight:700">{yes_pct:.1f}%</td>
          <td style="color:#8b949e">{s['p_btts_no']:.1f}%</td>
          <td>{icon} {s['signal']}</td>
          <td style="color:#8b949e">{s['lam_home']:.2f}</td>
          <td style="color:#8b949e">{s['lam_away']:.2f}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_uefa_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine UEFA Value Bets gefunden.</div>'
    headers = ["Spiel", "Typ", "Tipp", "Anstoß",
               "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Modell", "Elo"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for league, league_bets in _group_by_league(bets, UEFA_LABELS).items():
        html += f'<h3 class="league-header"><span class="tag3">{league}</span> ({len(league_bets)})</h3>'
        rows = ""
        for b in league_bets:
            ec = edge_class(b["edge_pct"])
            typ_label = "1X2" if b.get("type", "") == "1x2" else "O/U"
            elo_h = b.get("elo_home")
            elo_a = b.get("elo_away")
            elo_str = f"{int(elo_h)}/{int(elo_a)}" if elo_h and elo_a else "–"
            rows += f"""<tr>
          <td><strong>{b['match']}</strong></td>
          <td>{typ_label}</td>
          <td>{b['tip']}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{b.get("model_source", "")}</td>
          <td style="color:#8b949e">{elo_str}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_tennis_table(bets: list) -> str:
    if not bets:
        return '<div class="empty">Keine aktiven Tennis-Turniere mit ausreichend Odds gefunden.</div>'
    headers = ["Spiel", "Tipp", "Zeitpunkt", "Modell-%", "Beste Quote", "Edge-%", "Kelly-%", "Elo", "Modell"]
    ths = "".join(f"<th>{h}</th>" for h in headers)
    html = ""
    for tournament, t_bets in _group_by_league(bets, None, key="tournament").items():
        html += f'<h3 class="league-header"><span class="tag2">{tournament}</span> ({len(t_bets)})</h3>'
        rows = ""
        for b in t_bets:
            ec        = edge_class(b["edge_pct"])
            elo       = str(b["elo"]) if b["elo"] else "–"
            is_konsens = b.get("model_source") == "Konsens"
            warn      = " ⚠️" if is_konsens else ""
            row_style = ' style="opacity:0.65"' if is_konsens else ""
            rows += f"""<tr{row_style}>
          <td><strong>{b['match']}</strong></td>
          <td>{b['tip']}{warn}</td>
          <td>{format_dt(b['kick_off'])}</td>
          <td>{b['model_prob']*100:.1f}%</td>
          <td>{b['best_odds']:.2f}</td>
          <td class="{ec}">{b['edge_pct']:.1f}%</td>
          <td style="color:#58a6ff">{b['kelly_pct']:.1f}%</td>
          <td style="color:#8b949e">{elo}</td>
          <td style="color:#8b949e">{b['model_source']}{warn}</td>
        </tr>"""
        html += f"<table><tr>{ths}</tr>{rows}</table>"
    return html


def build_backtesting_section() -> str:
    """Erzeugt HTML-Sektion mit Backtesting-Feedback."""
    try:
        summary = get_summary()
    except Exception:
        return '<div class="empty">Backtesting-Daten nicht verfügbar.</div>'

    ov = summary.get("overall", {})
    if not ov or not ov.get("total_bets"):
        return '<div class="empty">Noch keine abgeschlossenen Bets im Backtesting.</div>'

    rolling = summary.get("rolling", {})
    r7  = rolling.get("7d", {})
    r30 = rolling.get("30d", {})
    streak = rolling.get("streak", {})

    total = ov.get("total_bets", 0)
    won   = ov.get("won", 0)
    roi   = ov.get("roi_pct", 0) or 0
    pnl   = ov.get("total_pnl", 0) or 0
    hit_rate = f"{won/total*100:.1f}" if total > 0 else "0"

    r7_roi  = r7.get("roi_pct", 0) or 0
    r7_bets = r7.get("bets", 0) or 0
    r30_roi = r30.get("roi_pct", 0) or 0
    r30_bets = r30.get("bets", 0) or 0

    streak_str = f"{streak.get('count', 0)}× {streak.get('type', '—')}"

    roi_class = "g" if roi > 0 else ("o" if roi > -5 else "y")
    r7_class  = "g" if r7_roi > 0 else ("o" if r7_roi > -5 else "y")
    r30_class = "g" if r30_roi > 0 else ("o" if r30_roi > -5 else "y")

    html = f"""
<div class="summary">
  <div class="card"><div class="val {roi_class}">{roi:+.1f}%</div><div class="lbl">Gesamt-ROI ({total} Bets)</div></div>
  <div class="card"><div class="val {r7_class}">{r7_roi:+.1f}%</div><div class="lbl">7-Tage ROI ({r7_bets})</div></div>
  <div class="card"><div class="val {r30_class}">{r30_roi:+.1f}%</div><div class="lbl">30-Tage ROI ({r30_bets})</div></div>
  <div class="card"><div class="val">{hit_rate}%</div><div class="lbl">Trefferquote ({won}/{total})</div></div>
  <div class="card"><div class="val">{pnl:+.2f}</div><div class="lbl">PnL (Units)</div></div>
  <div class="card"><div class="val">{streak_str}</div><div class="lbl">Aktuelle Serie</div></div>
</div>"""

    by_model = summary.get("by_model", [])
    if by_model:
        html += '<table><tr><th>Modell</th><th>Bets</th><th>Won</th><th>PnL</th><th>ROI</th></tr>'
        for m in by_model:
            m_roi = m.get("roi_pct", 0) or 0
            mc = "g" if m_roi > 0 else "y"
            html += f'<tr><td>{m["model_source"]}</td><td>{m["bets"]}</td><td>{m["won"]}</td>'
            html += f'<td class="{mc}">{m["pnl"]:+.2f}</td><td class="{mc}">{m_roi:+.1f}%</td></tr>'
        html += '</table>'

    return html


def build_wettplan_section(selected_bets: list, report_date_iso: str) -> str:
    """Erzeugt die Wettplan-Sektion mit Bankroll-Status und selektierten Bets."""
    if not selected_bets:
        return '<div class="empty">Kein Wettplan für heute — keine Bets selektiert.</div>'

    budget = get_daily_budget()
    dd = get_peak_and_drawdown()
    total_stake = sum(b.get("stake_eur", 0) for b in selected_bets)
    n_strong = sum(1 for b in selected_bets if b.get("tier") == "Strong Pick")
    n_value = sum(1 for b in selected_bets if b.get("tier") == "Value Bet")

    bankroll = budget["bankroll"]
    risk_pct = (total_stake / bankroll * 100) if bankroll > 0 else 0
    placed_count = sum(1 for b in selected_bets if b.get("placed"))

    dd_class = "g" if dd["drawdown_pct"] < 5 else ("y" if dd["drawdown_pct"] < 15 else "o")

    html = f"""
<div class="wettplan-toolbar" data-report-date="{report_date_iso}">
  <div class="wettplan-note">Klicke auf <strong>In Bankroll</strong>, um eine empfohlene Wette als tatsächlich gespielt zu markieren.</div>
  <button class="wettplan-refresh" type="button" onclick="refreshSportsBetState()">Status neu laden</button>
</div>
<div class="summary">
  <div class="card"><div class="val" style="color:var(--green)">{bankroll:.2f} €</div><div class="lbl">Bankroll</div></div>
  <div class="card"><div class="val">{len(selected_bets)}</div><div class="lbl">Bets heute</div></div>
  <div class="card"><div class="val">{total_stake:.2f} €</div><div class="lbl">Tagesrisiko ({risk_pct:.1f}%)</div></div>
  <div class="card"><div class="val">{n_strong} / {n_value}</div><div class="lbl">Strong / Value</div></div>
  <div class="card"><div class="val">{placed_count}</div><div class="lbl">In Bankroll</div></div>
  <div class="card"><div class="val {dd_class}">{dd['drawdown_pct']:.1f}%</div><div class="lbl">Drawdown (Peak: {dd['peak']:.0f} €)</div></div>
</div>

<table>
<tr>
  <th>Tier</th><th>Spiel</th><th>Tipp</th><th>Anstoß</th>
  <th>Score</th><th>Modell-%</th><th>Beste Quote</th>
  <th>Edge-%</th><th>Stake</th><th>Aktion</th>
</tr>"""

    for b in selected_bets:
        tier = b.get("tier", "Value Bet")
        if tier == "Strong Pick":
            tier_badge = '<span style="background:var(--green);color:var(--bg);border-radius:4px;padding:2px 7px;font-size:0.75em;font-weight:600">Strong Pick</span>'
        else:
            tier_badge = '<span style="background:rgba(0,240,255,0.1);color:var(--cyan);border:1px solid var(--border);border-radius:4px;padding:2px 7px;font-size:0.75em">Value Bet</span>'

        ec = edge_class(b.get("edge_pct", 0))
        score = b.get("confidence_score", 0)
        score_color = "var(--green)" if score >= 70 else ("var(--cyan)" if score >= 45 else "var(--dim)")
        pred_id = b.get("_pred_id", "")
        button_label = "In Bankroll" if not b.get("placed") else "Im Bankroll"
        button_class = "bet-btn placed" if b.get("placed") else "bet-btn"
        status_label = "Noch nicht gesetzt" if not b.get("placed") else f"Gesetzt: {b.get('actual_stake_eur', b.get('stake_eur', 0)):.2f} €"

        html += f"""<tr>
  <td>{tier_badge}</td>
  <td><strong>{b.get('match', '?')}</strong></td>
  <td>{b.get('tip', '?')}</td>
  <td>{format_dt(b.get('kick_off', ''))}</td>
  <td style="color:{score_color};font-weight:700">{score:.0f}</td>
  <td>{b.get('model_prob', 0)*100:.1f}%</td>
  <td>{b.get('best_odds', 0):.2f}</td>
  <td class="{ec}">{b.get('edge_pct', 0):.1f}%</td>
  <td style="color:var(--green);font-weight:700">{b.get('stake_eur', 0):.2f} €</td>
  <td>
    <div class="bet-action" data-prediction-id="{pred_id}" data-default-stake="{b.get('stake_eur', 0):.2f}">
      <button type="button" class="{button_class}" onclick="toggleSportsBetPlacement(this)">{button_label}</button>
      <div class="bet-status">{status_label}</div>
    </div>
  </td>
</tr>"""

    html += "</table>"
    return html


def _determine_tendency_and_score(p_home, p_draw, p_away, lam_h, lam_a):
    """Bestimmt Tendenz und wahrscheinlichstes Score basierend auf Wahrscheinlichkeiten."""
    if p_home is not None:
        if p_home >= p_draw and p_home >= p_away:
            tendency = "Heimsieg"
        elif p_draw >= p_home and p_draw >= p_away:
            tendency = "Unentschieden"
        else:
            tendency = "Auswärtssieg"
    else:
        tendency = "?"

    score_home = score_away = None
    if lam_h is not None and lam_a is not None:
        score_home, score_away = predict_most_likely_score(
            lam_h, lam_a, tendency=tendency if tendency != "?" else None
        )
    return tendency, score_home, score_away


def generate_kicktipp_html(matches: list) -> str:
    """Generiert HTML-Report fuer Kicktipp-Tipps."""
    date_str  = datetime.now().strftime("%d.%m.%Y")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    n_matches = len(matches)

    league_order = [
        "1. Bundesliga", "2. Bundesliga", "Premier League",
        "La Liga", "Serie A", "Ligue 1",
        "Champions League", "Europa League",
    ]
    leagues_present = []
    for lg in league_order:
        if any(m["league"] == lg for m in matches):
            leagues_present.append(lg)
    for m in matches:
        if m["league"] not in leagues_present:
            leagues_present.append(m["league"])

    def prob_cell(val, is_max):
        if val is None:
            return "<td>–</td>"
        pct = f"{val*100:.1f}%"
        if is_max:
            return f'<td style="color:#1a7a30;font-weight:700">{pct}</td>'
        return f"<td>{pct}</td>"

    sections = ""
    for league in leagues_present:
        league_matches = [m for m in matches if m["league"] == league]
        if not league_matches:
            continue

        rows = ""
        for m in league_matches:
            ph = m["p_home"]
            pd_ = m["p_draw"]
            pa = m["p_away"]

            vals = [v for v in [ph, pd_, pa] if v is not None]
            max_p = max(vals) if vals else None

            score = (
                f"{m['score_home']}:{m['score_away']}"
                if m["score_home"] is not None else "–"
            )

            tendency = m["tendency"]
            if tendency == "Heimsieg":
                tend_label = f'<strong style="color:#1a56a0">{m["home_team"]}</strong>'
            elif tendency == "Auswärtssieg":
                tend_label = f'<strong style="color:#c05a00">{m["away_team"]}</strong>'
            elif tendency == "Unentschieden":
                tend_label = '<strong style="color:#555577">Unentschieden</strong>'
            else:
                tend_label = "?"

            src_tag = f' <span class="tag2">{m.get("model_source", "?")}</span>' if m.get("model_source") else ""

            rows += f"""<tr>
              <td><strong>{m['home_team']} – {m['away_team']}</strong>{src_tag}</td>
              <td>{format_dt(m['kick_off'])}</td>
              <td>{tend_label}</td>
              <td style="font-weight:700;color:#333">{score}</td>
              {prob_cell(ph,  ph is not None and max_p is not None and ph == max_p)}
              {prob_cell(pd_, pd_ is not None and max_p is not None and pd_ == max_p)}
              {prob_cell(pa,  pa is not None and max_p is not None and pa == max_p)}
            </tr>"""

        icon = "🏆" if league in ("Champions League", "Europa League") else "⚽"
        sections += f"""<h2>{icon} {league}</h2>
<table>
<tr>
  <th>Spiel</th><th>Anstoß</th><th>Tipp (Tendenz)</th><th>Score</th>
  <th>P(Heim)</th><th>P(X)</th><th>P(Ausw.)</th>
</tr>
{rows}
</table>
"""

    if not sections:
        sections = '<div class="empty">Keine Kicktipp-Spiele gefunden.</div>'

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>🎯 Kicktipp-Tipps {date_str}</title>
<style>{CSS}
.kt-note {{ background:#f0fff4; border-left:3px solid #1a7a30; padding:10px 14px;
            color:#1a4a20; font-size:0.82em; border-radius:0 6px 6px 0; margin:10px 0; }}
</style>
</head>
<body>
<h1>🎯 Kicktipp-Tipps — {date_str}</h1>

<div class="summary">
  <div class="card"><div class="val">{n_matches}</div><div class="lbl">Spiele gesamt</div></div>
</div>

<div class="kt-note">
  📌 <strong>Hinweis:</strong> Tendenz und Score basieren auf dem Poisson-Modell (BL1/BL2/PL/LaLiga/SerieA/L1)
  bzw. Club-Elo-Modell (CL/EL). Grün hervorgehoben = höchste Wahrscheinlichkeit.
  Kein Ersatz für eigene Einschätzung!
</div>

{sections}

<div class="footer">
  Generiert: {timestamp} &nbsp;|&nbsp;
  Fußball: Poisson MLE (football-data.co.uk) &nbsp;|&nbsp;
  CL/EL: Club-Elo + Poisson &nbsp;|&nbsp;
  Matches: The Odds API<br>
  ⚠️ Diese Tipps dienen ausschließlich zu Unterhaltungszwecken.
</div>
</body>
</html>"""


def generate_html(football_bets: list, ou_bets: list,
                  tennis_bets: list, uefa_bets: list,
                  selected_bets: list | None = None,
                  btts_signals: list | None = None,
                  odds_api_remaining: int | None = None) -> str:
    date_str  = datetime.now().strftime("%d.%m.%Y")
    report_date_iso = datetime.now().strftime("%Y-%m-%d")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    real_tennis_bets = [b for b in tennis_bets if b.get("model_source") != "Konsens"]
    total     = len(football_bets) + len(ou_bets) + len(real_tennis_bets) + len(uefa_bets)
    all_edges = [b["edge_pct"] for b in football_bets + ou_bets + tennis_bets + uefa_bets]
    max_edge  = max(all_edges) if all_edges else 0.0

    quota_str = f"API-Quota: {odds_api_remaining}" if odds_api_remaining is not None else "API-Quota: ?"

    if selected_bets is None:
        selected_bets = []

    wettplan_html = build_wettplan_section(selected_bets, report_date_iso)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Sports Value Scanner {date_str}</title>
<style>{CSS}</style>
<style>
.wettplan-toolbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 0 14px;flex-wrap:wrap}}
.wettplan-note{{color:var(--dim);font-size:.95em}}
.wettplan-refresh{{background:rgba(0,240,255,0.08);border:1px solid var(--border);color:var(--cyan);border-radius:8px;padding:8px 12px;cursor:pointer;font:inherit}}
.bet-action{{display:grid;gap:6px;min-width:140px}}
.bet-btn{{background:rgba(110,200,124,0.12);border:1px solid rgba(110,200,124,0.35);color:var(--green);border-radius:8px;padding:8px 10px;cursor:pointer;font:inherit;font-weight:600}}
.bet-btn.placed{{background:rgba(200,170,110,0.12);border-color:rgba(200,170,110,0.35);color:var(--gold)}}
.bet-btn.loading{{opacity:.65;cursor:wait}}
.bet-status{{font-size:.8em;color:var(--dim);line-height:1.35}}
</style>
</head>
<body>
<h1>📊 Sports Value Scanner — {date_str}</h1>

<h2>🎯 Wettplan</h2>
{wettplan_html}

<div class="summary">
  <div class="card"><div class="val">{total}</div><div class="lbl">Value Bets gesamt</div></div>
  <div class="card"><div class="val">{len(football_bets)}</div><div class="lbl">⚽ Fußball 1X2</div></div>
  <div class="card"><div class="val">{len(ou_bets)}</div><div class="lbl">⚽ Über/Unter</div></div>
  <div class="card"><div class="val">{len(uefa_bets)}</div><div class="lbl">🏆 UEFA/Pokal</div></div>
  <div class="card"><div class="val">{len(real_tennis_bets)}</div><div class="lbl">🎾 Tennis</div></div>
  <div class="card"><div class="val">{max_edge:.1f}%</div><div class="lbl">Max. Edge</div></div>
</div>

<div class="note">
  📌 <strong>Hinweis:</strong> Edge = (Modell-Wahrscheinlichkeit × Beste Quote) – 1.
  Nur Bets mit Edge ≥ {MIN_EDGE_PCT}% und ≤ {MAX_EDGE_PCT:.0f}% sowie Quote ≥ {MIN_ODDS} werden angezeigt.
  Quarter-Kelly Bankroll-Management aktiv.
</div>

<h2>📈 Backtesting-Performance</h2>
{build_backtesting_section()}

<details>
<summary style="cursor:pointer;color:var(--gold);font-size:1.1em;font-weight:600;margin:20px 0 10px">
  📋 Alle Signale ({total}) — zum Aufklappen klicken
</summary>

<h2>⚽ Fußball Value Bets (Poisson-Modell, Time-Decay)</h2>
{build_football_table(football_bets)}

<h2>⚽ Über/Unter Value Bets (Poisson-Modell)</h2>
{build_ou_table(ou_bets)}

<h2>🏆 UEFA / DFB-Pokal Value Bets</h2>
{build_uefa_table(uefa_bets)}

<h2>🎾 Tennis Value Bets (Elo-Modell, ATP + WTA, Surface-Bias)</h2>
{build_tennis_table(tennis_bets)}

</details>

<details>
<summary style="cursor:pointer;color:var(--gold);font-size:1.1em;font-weight:600;margin:20px 0 10px">
  ⚽ Beide Teams treffen — BTTS-Signale ({len(btts_signals or [])}) — zum Aufklappen klicken
</summary>
<div class="note">
  📌 <strong>Nur Analyse-Signal</strong> — keine Odds verfügbar (The Odds API bietet keinen BTTS-Markt).
  Wahrscheinlichkeiten basieren auf dem Poisson-Modell (λ Heim × λ Gast).
</div>
{build_btts_table(btts_signals or [])}
</details>

<div class="footer">
  Generiert: {timestamp} &nbsp;|&nbsp;
  Fußball: Poisson MLE + Time-Decay (football-data.co.uk) &nbsp;|&nbsp;
  Tennis: Elo ATP+WTA + Surface-Bias (Jeff Sackmann) &nbsp;|&nbsp;
  Odds: The Odds API &nbsp;|&nbsp;
  UEFA/Pokal: Club-Elo + Poisson &nbsp;|&nbsp;
  {quota_str}<br>
  ⚠️ Diese Analyse dient ausschließlich zu Informationszwecken.
  Sportwetten sind mit erheblichen Verlustrisiken verbunden.
</div>
<script>
function getSportsApiBase(){{
  const host=window.location.hostname || '127.0.0.1';
  const protocol=window.location.protocol === 'https:' ? 'https:' : 'http:';
  const port=window.location.port || '';

  if (host === 'agents.umzwei.de') {{
    return `${{window.location.origin}}/webhook`;
  }}

  if (window.location.origin && port === '8099') {{
    return `${{protocol}}//${{host}}:8099`;
  }}

  return `${{protocol}}//${{host}}:8099`;
}}

function getSportsApiUrl(kind, query=''){{
  const base=getSportsApiBase();
  if (base.endsWith('/webhook')) {{
    const route=kind === 'place' ? 'sports-bets-place' : 'sports-bets';
    return `${{base}}/${{route}}${{query}}`;
  }}
  const route=kind === 'place' ? '/api/sports-bets/place' : '/api/sports-bets';
  return `${{base}}${{route}}${{query}}`;
}}

async function refreshSportsBetState(){{
  const toolbar=document.querySelector('.wettplan-toolbar');
  if(!toolbar) return;
  const date=toolbar.dataset.reportDate;
  try {{
    const resp=await fetch(getSportsApiUrl('list', `?date=${{encodeURIComponent(date)}}`));
    if(!resp.ok) throw new Error('State-Load fehlgeschlagen');
    const data=await resp.json();
    const byId=new Map((data.recommended||[]).map(row=>[String(row.prediction_id),row]));
    document.querySelectorAll('.bet-action').forEach((wrap)=>{{
      const row=byId.get(wrap.dataset.predictionId);
      if(!row) return;
      const btn=wrap.querySelector('.bet-btn');
      const status=wrap.querySelector('.bet-status');
      btn.classList.toggle('placed', !!row.placed);
      btn.classList.remove('loading');
      btn.textContent=row.placed ? 'Im Bankroll' : 'In Bankroll';
      status.textContent=row.placed
        ? `Gesetzt: ${{(row.actual_stake_eur ?? row.stake_eur ?? 0).toFixed(2)}} €`
        : 'Noch nicht gesetzt';
    }});
  }} catch(err) {{
    console.error(err);
  }}
}}

async function toggleSportsBetPlacement(button){{
  const wrap=button.closest('.bet-action');
  if(!wrap) return;
  const predictionId=Number(wrap.dataset.predictionId);
  const defaultStake=Number(wrap.dataset.defaultStake || '0');
  const isPlaced=button.classList.contains('placed');
  let payload={{prediction_id: predictionId, placed: !isPlaced}};

  if(!isPlaced){{
    const raw=window.prompt('Einsatz in EUR', defaultStake.toFixed(2));
    if(raw===null) return;
    const parsed=Number(String(raw).replace(',', '.'));
    if(!Number.isFinite(parsed) || parsed <= 0){{
      window.alert('Bitte einen gueltigen Einsatz eingeben.');
      return;
    }}
    payload.actual_stake_eur=parsed;
  }}

  button.classList.add('loading');
  try {{
    const resp=await fetch(getSportsApiUrl('place'), {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(payload)
    }});
    const data=await resp.json();
    if(!resp.ok || !data.ok) {{
      throw new Error(data.error || 'Aktualisierung fehlgeschlagen');
    }}
    await refreshSportsBetState();
  }} catch(err) {{
    button.classList.remove('loading');
    window.alert(err.message || 'Bet konnte nicht aktualisiert werden.');
  }}
}}

document.addEventListener('DOMContentLoaded', () => {{
  refreshSportsBetState();
}});
</script>
</body>
</html>"""
