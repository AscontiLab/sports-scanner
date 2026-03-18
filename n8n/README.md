# n8n Webhook Proxy fuer Sports-Bets

Der Sports-Report laeuft unter:

- `https://agents.umzwei.de/webhook/sports-report`

Damit Klicks auf `In Bankroll` im Browser funktionieren, darf das Frontend nicht direkt auf `http://...:8099` zugreifen. Stattdessen braucht es gleich-origin Webhooks unter derselben Domain:

- `GET /webhook/sports-bets`
- `POST /webhook/sports-bets-place`

## Zweck

Diese Webhooks leiten Browser-Requests serverseitig an den lokalen Sports-Scanner-Server weiter:

- `http://172.28.0.1:8099/api/sports-bets`
- `http://172.28.0.1:8099/api/sports-bets/place`

## Datei

- `sports-bets-proxy-workflows.json`

Die Datei enthaelt zwei importierbare Workflow-Definitionen:

1. `Sports Bets List Proxy`
2. `Sports Bets Place Proxy`

## Zielrouten

Nach dem Import und Aktivieren muessen diese Routen erreichbar sein:

- `https://agents.umzwei.de/webhook/sports-bets?date=YYYY-MM-DD`
- `https://agents.umzwei.de/webhook/sports-bets-place`

## Hinweise

- Der lokale `serve_output.py` muss auf Port `8099` laufen.
- Im Docker-/n8n-Kontext zeigt `172.28.0.1` auf den Host.
- Die Workflows geben JSON direkt an den Browser zurueck.
- CORS ist im Sports-Scanner-Server bereits ergaenzt, bleibt hier aber ebenfalls unkritisch.
