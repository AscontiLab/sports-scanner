#!/bin/bash
# Exportiert die SQLite-Datenbank als SQL-Dump nach backup/
# Wird nach jedem Scanner-Lauf aufgerufen.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_FILE="$SCRIPT_DIR/sports_backtesting.db"
BACKUP_DIR="$SCRIPT_DIR/backup"
DUMP_FILE="$BACKUP_DIR/sports_backtesting.sql"

if [ ! -f "$DB_FILE" ]; then
    echo "[Backup] Keine DB gefunden: $DB_FILE"
    exit 0
fi

mkdir -p "$BACKUP_DIR"

# SQL-Dump via Python (sqlite3 CLI nicht immer verfuegbar)
/usr/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('$DB_FILE')
with open('$DUMP_FILE', 'w') as f:
    for line in conn.iterdump():
        f.write(line + '\n')
conn.close()
" 2>/dev/null

if [ $? -eq 0 ]; then
    SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
    echo "[Backup] SQL-Dump: $DUMP_FILE ($SIZE)"
else
    echo "[Backup] FEHLER beim Dump von $DB_FILE"
    exit 1
fi

# Git add + commit (nur wenn sich was geaendert hat)
cd "$SCRIPT_DIR"
if git diff --quiet "$DUMP_FILE" 2>/dev/null && git diff --cached --quiet "$DUMP_FILE" 2>/dev/null; then
    echo "[Backup] Keine Aenderungen — kein Commit noetig."
else
    git add "$DUMP_FILE"
    git commit -m "backup: SQL-Dump sports_backtesting $(date +%Y-%m-%d)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
    if git push origin master; then
        echo "[Backup] Dump committed und gepusht."
    else
        echo "[Backup] FEHLER beim Push nach origin/master (Exit-Code: $?)"
        exit 1
    fi
fi
