#!/bin/bash
# Setup auto-fetch cron job (every 30 minutes)
# Usage: ./scripts/setup-auto-fetch.sh [enable|disable]

CRON_CMD='*/30 * * * * curl -s -X POST http://localhost:8080/api/fetch/quick -H "Content-Type: application/json" -d '\''{"mode":"recommend"}'\'' > /dev/null 2>&1'
CRON_MARKER="# info-radar-auto-fetch"

case "${1:-enable}" in
  enable)
    # Check if already registered
    if crontab -l 2>/dev/null | grep -q "info-radar-auto-fetch"; then
      echo "✅ Auto-fetch already enabled"
      crontab -l | grep "info-radar-auto-fetch"
    else
      (crontab -l 2>/dev/null; echo "$CRON_CMD $CRON_MARKER") | crontab -
      echo "✅ Auto-fetch enabled (every 30 minutes)"
      echo "   $CRON_CMD"
    fi
    ;;
  disable)
    crontab -l 2>/dev/null | grep -v "info-radar-auto-fetch" | crontab -
    echo "🗑️  Auto-fetch disabled"
    ;;
  *)
    echo "Usage: $0 [enable|disable]"
    ;;
esac
