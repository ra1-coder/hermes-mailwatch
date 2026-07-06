#!/bin/sh
# Container-safe supervisor (no systemd required).
# Sources ./mailwatch.env, runs the watcher, restarts forever on exit.
cd "$(dirname "$0")" || exit 1
[ -f mailwatch.env ] || { echo "mailwatch.env missing"; exit 1; }
while true; do
  set -a; . ./mailwatch.env; set +a
  python3 hermes_mailwatch.py >> mailwatch.log 2>&1
  echo "$(date) watcher exited, restarting in 10s" >> mailwatch.log
  sleep 10
done
