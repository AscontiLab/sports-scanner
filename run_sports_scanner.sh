#!/bin/bash
# Daily Sports Value Scanner — Wrapper-Script
# Läuft täglich (inkl. Wochenende) für Bundesliga & Tennis via Cron

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$SCRIPT_DIR/.scanner.lock"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/scanner_$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"

# Verhindere parallele Läufe
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Scanner läuft bereits — Abbruch." >> "$LOG_FILE"
    exit 0
fi

echo "======================================" >> "$LOG_FILE"
echo "Start: $(date '+%Y-%m-%d %H:%M:%S')"   >> "$LOG_FILE"
echo "======================================" >> "$LOG_FILE"

cd "$SCRIPT_DIR" || exit 1

/usr/bin/python3 sports_scanner.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "Ende: $(date '+%Y-%m-%d %H:%M:%S')  (Exit: $EXIT_CODE)" >> "$LOG_FILE"

if [ $EXIT_CODE -eq 0 ]; then
    echo "Sende Sports-Report …" >> "$LOG_FILE"
    /usr/bin/python3 "$SCRIPT_DIR/send_sports_report.py" >> "$LOG_FILE" 2>&1
    if [ $? -eq 0 ]; then
        echo "Sports-Report erfolgreich gesendet." >> "$LOG_FILE"
    else
        echo "FEHLER: Sports-Report konnte nicht gesendet werden." >> "$LOG_FILE"
    fi

    echo "Sende Kicktipp-Report …" >> "$LOG_FILE"
    /usr/bin/python3 "$SCRIPT_DIR/send_kicktipp_report.py" >> "$LOG_FILE" 2>&1
    if [ $? -eq 0 ]; then
        echo "Kicktipp-Report erfolgreich gesendet." >> "$LOG_FILE"
    else
        echo "HINWEIS: Kicktipp-Report nicht gesendet (ggf. keine Spiele)." >> "$LOG_FILE"
    fi
else
    echo "Scanner fehlgeschlagen — kein E-Mail-Versand." >> "$LOG_FILE"
fi

# Logs älter als 30 Tage löschen
find "$LOG_DIR" -name "scanner_*.log" -mtime +30 -delete

exit $EXIT_CODE
