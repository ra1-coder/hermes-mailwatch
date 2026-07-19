#!/bin/sh
# Container-safe supervisor (no systemd required).
# Companion to run_mailwatch.sh. Sources the shared ./mailwatch.env, runs the
# Discord capturer, restarts forever on exit. The capturer is INDEPENDENT of
# the gateway (the brain) — it keeps filing the transcript even while the brain
# is down. Stdlib only; no extra deps beyond python3.
cd "$(dirname "$0")" || exit 1
[ -f mailwatch.env ] || { echo "mailwatch.env missing"; exit 1; }
while true; do
  set -a; . ./mailwatch.env; set +a
  python3 hermes_discord_capturer.py >> discord_capturer.log 2>&1
  echo "$(date) capturer exited, restarting in 10s" >> discord_capturer.log
  sleep 10
done
