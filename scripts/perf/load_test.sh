#!/bin/bash
# Concurrent load test against /api/feed and /api/stats
# Simulates N parallel anonymous users, prints p50/p95/p99 + error count

URL_FEED="http://127.0.0.1:8080/api/feed?limit=20"
URL_STATS="http://127.0.0.1:8080/api/stats"
URL_SECTIONS="http://127.0.0.1:8080/api/feed/sections"

run_concurrent() {
  local concurrency=$1
  local url=$2
  local label=$3
  local tmp=$(mktemp)

  # Fire $concurrency curls in parallel
  for i in $(seq 1 $concurrency); do
    (curl -s -o /dev/null -w "%{http_code} %{time_total}\n" "$url" >> "$tmp") &
  done
  wait

  # Sort by time and compute percentiles
  local total=$(wc -l < "$tmp")
  local errors=$(grep -cv "^200" "$tmp")
  local times=$(awk '{print $2}' "$tmp" | sort -g)
  local p50=$(echo "$times" | awk -v n=$total 'NR==int(n*0.5+0.5){print; exit}')
  local p95=$(echo "$times" | awk -v n=$total 'NR==int(n*0.95+0.5){print; exit}')
  local p99=$(echo "$times" | awk -v n=$total 'NR==int(n*0.99+0.5){print; exit}')
  local pmax=$(echo "$times" | tail -1)
  local pmin=$(echo "$times" | head -1)

  printf "  %-30s c=%-3d  ok=%d/%d  min=%.2fs  p50=%.2fs  p95=%.2fs  p99=%.2fs  max=%.2fs\n" \
    "$label" "$concurrency" "$((total-errors))" "$total" "$pmin" "$p50" "$p95" "$p99" "$pmax"
  rm -f "$tmp"
}

echo "=== /api/feed?limit=20 ==="
run_concurrent 1 "$URL_FEED" "/api/feed"
run_concurrent 5 "$URL_FEED" "/api/feed"
run_concurrent 10 "$URL_FEED" "/api/feed"
run_concurrent 20 "$URL_FEED" "/api/feed"

echo
echo "=== /api/stats ==="
run_concurrent 1 "$URL_STATS" "/api/stats"
run_concurrent 5 "$URL_STATS" "/api/stats"
run_concurrent 10 "$URL_STATS" "/api/stats"
run_concurrent 20 "$URL_STATS" "/api/stats"

echo
echo "=== /api/feed/sections (cached) ==="
run_concurrent 1 "$URL_SECTIONS" "sections"
run_concurrent 10 "$URL_SECTIONS" "sections"
run_concurrent 20 "$URL_SECTIONS" "sections"

echo
echo "=== Mixed traffic (5 users × 3 endpoints in parallel) ==="
tmp=$(mktemp)
for i in 1 2 3 4 5; do
  (curl -s -o /dev/null -w "feed %{http_code} %{time_total}\n" "$URL_FEED" >> "$tmp") &
  (curl -s -o /dev/null -w "stats %{http_code} %{time_total}\n" "$URL_STATS" >> "$tmp") &
  (curl -s -o /dev/null -w "sect %{http_code} %{time_total}\n" "$URL_SECTIONS" >> "$tmp") &
done
wait
sort -k3 -g "$tmp" | tail -8
rm -f "$tmp"
