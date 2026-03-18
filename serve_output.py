#!/usr/bin/env python3
"""
Minimaler HTTP-Server für Scanner-Output-Dateien.
Läuft auf Port 8099, erreichbar vom n8n-Docker-Container via 172.28.0.1:8099.
Dient nur zum Lesen der Output-Dateien (sports_signals.html, kicktipp_data.json etc.).
"""

import hmac
import json
import os
import sqlite3
import sys
from datetime import date
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8099
# Serve both sports-scanner and stock-scanner output
BASE_DIRS = {
    "/sports/": Path("/home/claude-agent/sports-scanner/output"),
    "/stock/": Path("/home/claude-agent/stock-scanner/output"),
    "/hub/": Path("/home/claude-agent/hub"),
}

# Bearer-Token für GET /api/* Endpoints (optional, abwärtskompatibel)
from config import load_credentials
_creds = load_credentials()
_SPORTS_API_TOKEN = _creds.get("SPORTS_API_TOKEN") or None

if _SPORTS_API_TOKEN:
    print(f"[Auth] SPORTS_API_TOKEN konfiguriert — API-Endpoints geschützt")
else:
    print("[Auth] WARNUNG: Kein SPORTS_API_TOKEN in ~/.stock_scanner_credentials — API-Endpoints ungeschützt")


def _sports_dirs():
    """Sortierte Liste der Output-Verzeichnisse (neueste zuerst)."""
    base = BASE_DIRS["/sports/"]
    if not base.is_dir():
        return []
    return sorted([d for d in os.listdir(base) if (base / d).is_dir()], reverse=True)


def _stock_dirs():
    base = BASE_DIRS["/stock/"]
    if not base.is_dir():
        return []
    return sorted([d for d in os.listdir(base) if (base / d).is_dir()], reverse=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _count_csv_rows(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        return max(0, len(lines) - 1)  # minus header
    except Exception:
        return 0


def _filter_records(records, qs):
    if not isinstance(records, list):
        return []
    filtered = records
    system = qs.get("system", [""])[0]
    status = qs.get("status", [""])[0]
    limit = qs.get("limit", [""])[0]
    if system:
        filtered = [r for r in filtered if r.get("system") == system]
    if status:
        filtered = [r for r in filtered if r.get("status") == status]
    if limit:
        try:
            filtered = filtered[:max(0, int(limit))]
        except ValueError:
            pass
    return filtered


def _sort_signals(signals):
    if not isinstance(signals, list):
        return []
    return sorted(
        signals,
        key=lambda r: (
            int(r.get("priority", 0) or 0),
            r.get("timing", {}).get("event_time", "") or "",
            r.get("title", "") or "",
        ),
        reverse=True,
    )


def _sports_bets_payload(date_str: str | None = None):
    """Liefert empfohlene und platzierte Sports-Bets für das Dashboard."""
    if date_str is None:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    db_path = Path("/home/claude-agent/sports-scanner/sports_backtesting.db")
    if not db_path.exists():
        return {"date": date_str, "recommended": [], "placed": []}

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, sport_key, bet_type, home_team, away_team, commence_time,
                tip, best_odds, edge_pct, stake_eur, actual_stake_eur, tier,
                confidence_score, placed, placed_at, bet_won, actual_pnl_eur
            FROM predictions
            WHERE selected = 1
              AND SUBSTR(commence_time, 1, 10) = ?
            ORDER BY commence_time ASC, confidence_score DESC, id DESC
            """,
            (date_str,),
        ).fetchall()

    recommended = []
    for r in rows:
        recommended.append({
            "prediction_id": r["id"],
            "sport_key": r["sport_key"],
            "bet_type": r["bet_type"],
            "match": f"{r['home_team']} – {r['away_team']}",
            "tip": r["tip"],
            "odds": r["best_odds"],
            "edge_pct": r["edge_pct"],
            "stake_eur": r["stake_eur"],
            "actual_stake_eur": r["actual_stake_eur"],
            "tier": r["tier"],
            "confidence_score": r["confidence_score"],
            "placed": bool(r["placed"]),
            "placed_at": r["placed_at"],
            "bet_won": r["bet_won"],
            "actual_pnl_eur": r["actual_pnl_eur"],
            "commence_time": r["commence_time"],
        })

    return {
        "date": date_str,
        "recommended": recommended,
        "placed": [row for row in recommended if row["placed"]],
    }


class OutputHandler(SimpleHTTPRequestHandler):

    def _check_api_auth(self) -> bool:
        """Prüft Bearer-Token für GET /api/* Endpoints.
        Gibt True zurück wenn Zugriff erlaubt, False wenn abgelehnt (Response bereits gesendet)."""
        if _SPORTS_API_TOKEN is None:
            return True  # kein Token konfiguriert → abwärtskompatibel
        auth_header = self.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {_SPORTS_API_TOKEN}"):
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "Unauthorized"}')
        return False

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _handle_api(self):
        """Convenience-API-Endpoints fuer n8n Workflows."""
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # --- /api/hub-summary ---
        # Liefert Counts fuer Hub-Dashboard in einem Call
        if path == "/api/hub-summary":
            sports = _sports_dirs()
            # Kicktipp count (latest)
            kicktipp_count = 0
            for d in sports:
                kd = _read_json(BASE_DIRS["/sports/"] / d / "kicktipp_data.json")
                if isinstance(kd, list) and len(kd) > 0:
                    kicktipp_count = len(kd)
                    break
            # Sports signals count (latest)
            sports_count = 0
            for d in sports:
                p = BASE_DIRS["/sports/"] / d / "sports_signals.csv"
                if p.is_file():
                    sports_count = _count_csv_rows(p)
                    break
            # CFD count (latest)
            cfd_count = 0
            for d in _stock_dirs():
                p = BASE_DIRS["/stock/"] / d / "cfd_setups.csv"
                if p.is_file():
                    cfd_count = _count_csv_rows(p)
                    break
            # KI-Tipps — tagesbasierte Rotation
            tips = _read_json(BASE_DIRS["/hub/"] / "ki_tips.json")
            if not isinstance(tips, list):
                tips = []
            if len(tips) > 5:
                offset = date.today().toordinal() % len(tips)
                tips = (tips[offset:] + tips[:offset])
            # Code Review
            review = _read_json(BASE_DIRS["/hub/"] / "code_review.json")
            if not isinstance(review, dict):
                review = {}
            self._json_response({
                "kicktippCount": kicktipp_count,
                "sportsCount": sports_count,
                "cfdCount": cfd_count,
                "tips": tips[:5],
                "codeReview": review,
            })
            return True

        # --- /api/hub-runs ---
        if path == "/api/hub-runs":
            runs = _read_json(BASE_DIRS["/hub/"] / "latest_runs.json")
            self._json_response(_filter_records(runs, qs))
            return True

        # --- /api/hub-signals ---
        if path == "/api/hub-signals":
            signals = _read_json(BASE_DIRS["/hub/"] / "latest_signals.json")
            self._json_response(_filter_records(signals, qs))
            return True

        # --- /api/hub-top-signals ---
        if path == "/api/hub-top-signals":
            signals = _read_json(BASE_DIRS["/hub/"] / "latest_signals.json")
            filtered = _filter_records(signals, qs)
            selected = [s for s in filtered if s.get("status") in ("selected", "watch", "candidate")]
            self._json_response(_sort_signals(selected))
            return True

        # --- /api/kicktipp-latest ---
        # Liefert neueste Kicktipp-Daten + verfuegbare Daten
        if path == "/api/kicktipp-latest":
            sports = _sports_dirs()
            date_str = ""
            data = []
            avail = []
            for d in sports:
                kd = _read_json(BASE_DIRS["/sports/"] / d / "kicktipp_data.json")
                if isinstance(kd, list) and len(kd) > 0:
                    avail.append(d)
                    if not date_str:
                        date_str = d
                        data = kd
            self._json_response({
                "date": date_str,
                "matches": data,
                "availableDates": avail,
            })
            return True

        # --- /api/kicktipp-for-date?date=YYYY-MM-DD ---
        if path == "/api/kicktipp-for-date":
            date_str = qs.get("date", [""])[0]
            if not date_str:
                self._json_response({"error": "date parameter required"}, 400)
                return True
            kd = _read_json(BASE_DIRS["/sports/"] / date_str / "kicktipp_data.json")
            if not isinstance(kd, list):
                kd = []
            self._json_response({"date": date_str, "matches": kd})
            return True

        # --- /api/kicktipp-stats ---
        # Aggregierte Stats ueber alle verfuegbaren Tage
        if path == "/api/kicktipp-stats":
            sports = _sports_dirs()
            all_preds = []
            avail = []
            for d in sports[:30]:
                kd = _read_json(BASE_DIRS["/sports/"] / d / "kicktipp_data.json")
                if isinstance(kd, list) and len(kd) > 0:
                    for m in kd:
                        m["_date"] = d
                    all_preds.extend(kd)
                    avail.append(d)
            total = len(all_preds)
            with_tendency = sum(1 for m in all_preds if m.get("tendency") and m["tendency"] != "?")
            by_league = {}
            for m in all_preds:
                lg = m.get("league", "?")
                if lg not in by_league:
                    by_league[lg] = {"total": 0, "withModel": 0}
                by_league[lg]["total"] += 1
                if m.get("tendency") and m["tendency"] != "?":
                    by_league[lg]["withModel"] += 1
            from datetime import datetime, timedelta
            now = datetime.now()
            last7 = [d for d in avail if (now - datetime.strptime(d, "%Y-%m-%d")).days <= 7]
            last7count = sum(1 for m in all_preds if m.get("_date") in last7)
            self._json_response({
                "totalPredictions": total,
                "withModel": with_tendency,
                "coveragePercent": round(with_tendency / total * 100) if total > 0 else 0,
                "availableDates": len(avail),
                "last7daysPredictions": last7count,
                "byLeague": by_league,
            })
            return True

        # --- /api/sports-bankroll ---
        if path == "/api/sports-bankroll":
            data = _read_json(BASE_DIRS["/sports/"] / ".." / "output" / "sports_bankroll.json")
            if not data:
                # Fallback: direkt im sports output-Verzeichnis
                data = _read_json(Path(__file__).parent / "output" / "sports_bankroll.json")
            self._json_response(data or {})
            return True

        # --- /api/sports-tuning ---
        if path == "/api/sports-tuning":
            data = _read_json(Path(__file__).parent / "output" / "sports_tuning.json")
            self._json_response(data or {})
            return True

        if path == "/api/sports-bets":
            date_str = qs.get("date", [""])[0] or None
            self._json_response(_sports_bets_payload(date_str))
            return True

        return False

    def _check_path_traversal(self, file_path: Path, base_dir: Path) -> bool:
        """Prüft auf Path Traversal. Gibt True zurück wenn sicher."""
        resolved = file_path.resolve()
        if not resolved.is_relative_to(base_dir.resolve()):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Forbidden: path traversal detected")
            return False
        return True

    def _check_put_allowed(self) -> bool:
        """Prüft ob PUT von erlaubter IP kommt (localhost/Docker-Netz)."""
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1") and not client_ip.startswith("172.28."):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Forbidden: PUT only allowed from localhost/Docker")
            return False
        return True

    def do_GET(self):
        # API endpoints first
        if self.path.startswith("/api/"):
            if not self._check_api_auth():
                return
            if self._handle_api():
                return

        # Route to correct base dir
        for prefix, base_dir in BASE_DIRS.items():
            if self.path.startswith(prefix):
                rel_path = self.path[len(prefix):]
                file_path = base_dir / rel_path
                if not self._check_path_traversal(file_path, base_dir):
                    return
                if file_path.is_file():
                    self.send_response(200)
                    if file_path.suffix == ".html":
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                    elif file_path.suffix == ".json":
                        self.send_header("Content-Type", "application/json")
                    elif file_path.suffix == ".csv":
                        self.send_header("Content-Type", "text/csv; charset=utf-8")
                    else:
                        self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(file_path.read_bytes())
                    return
                elif file_path.is_dir():
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    entries = sorted(os.listdir(file_path), reverse=True)
                    self.wfile.write(json.dumps(entries).encode())
                    return

        if self.path == "/" or self.path == "":
            self._json_response({"routes": list(BASE_DIRS.keys())})
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not found")

    def do_PUT(self):
        """Erlaubt Schreiben in /hub/ (für KI-News-Aggregator). Nur localhost/Docker."""
        if not self._check_put_allowed():
            return
        if self.path.startswith("/hub/"):
            rel_path = self.path[len("/hub/"):]
            base_dir = BASE_DIRS["/hub/"]
            file_path = base_dir / rel_path
            if not self._check_path_traversal(file_path, base_dir):
                return
            content_length = int(self.headers.get("Content-Length", 0))
            max_size = 1_048_576  # 1 MB
            if content_length > max_size:
                self.send_response(413)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Payload too large (max 1 MB)")
                return
            body = self.rfile.read(content_length)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"Forbidden")

    def do_POST(self):
        """Erlaubt operative Aktionen für Sports-Bets (nur localhost/Docker)."""
        if not self._check_put_allowed():
            return

        if self.path == "/api/sports-bets/place":
            try:
                from backtesting import set_prediction_placed
                from write_sports_dashboard_data import main as refresh_sports_dashboard_data
            except Exception:
                self._json_response({"ok": False, "error": "Backtesting-Modul nicht ladbar"}, 500)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._json_response({"ok": False, "error": "Ungueltiges JSON"}, 400)
                return

            prediction_id = payload.get("prediction_id")
            if not isinstance(prediction_id, int):
                self._json_response({"ok": False, "error": "prediction_id muss int sein"}, 400)
                return

            placed = 1 if payload.get("placed", True) else 0
            actual_stake_eur = payload.get("actual_stake_eur")

            try:
                result = set_prediction_placed(prediction_id, placed, actual_stake_eur)
            except ValueError as exc:
                self._json_response({"ok": False, "error": str(exc)}, 400)
                return
            except Exception:
                self._json_response({"ok": False, "error": "Bet konnte nicht aktualisiert werden"}, 500)
                return

            try:
                refresh_sports_dashboard_data()
            except Exception:
                pass

            self._json_response({"ok": True, "placement": result})
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, format, *args):
        pass  # Kein Log-Spam


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), OutputHandler)
    print(f"Output-Server auf Port {PORT} gestartet")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
