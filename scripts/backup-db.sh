#!/bin/bash
# Daily backup for feed.db and user_feedback.db
# Usage: ./scripts/backup-db.sh
# Cron:  0 3 * * * cd /path/to/info2action && ./scripts/backup-db.sh

BASE="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$BASE/data/backups"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"

# Backup feed.db via sqlite3 .dump (safe even if DB is in use)
if [ -f "$BASE/data/feed.db" ]; then
  sqlite3 "$BASE/data/feed.db" ".backup '$BACKUP_DIR/feed-$DATE.db'" 2>/dev/null
  echo "✅ feed.db → backups/feed-$DATE.db"
fi

# Backup user_feedback.db
if [ -f "$BASE/data/user_feedback.db" ]; then
  sqlite3 "$BASE/data/user_feedback.db" ".backup '$BACKUP_DIR/user_feedback-$DATE.db'" 2>/dev/null
  echo "✅ user_feedback.db → backups/user_feedback-$DATE.db"
fi

# Clean up backups older than 7 days
find "$BACKUP_DIR" -name "*.db" -mtime +7 -delete 2>/dev/null
echo "🗑️  Cleaned backups older than 7 days"
