#!/usr/bin/env bash
# Deploy to a Linux server (any box with Python 3.10+, ssh and Docker) and install the cron job.
# Run from the repo root (Git Bash on Windows works):
#   scripts/deploy.sh               # code + config + cron + web dashboard
#   scripts/deploy.sh --env         # also copy .env (Gmail app password etc.), first time / after rotating
#   scripts/deploy.sh --no-docker   # cron scan only, no web dashboard containers
# The server keeps its own data/tracker.db; this script never overwrites it.
set -euo pipefail
cd "$(dirname "$0")/.."

envval() { grep -E "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }

HOST="${DEPLOY_HOST:-$(envval DEPLOY_HOST)}"
[[ -n "$HOST" ]] || { echo "set DEPLOY_HOST in .env (user@host)"; exit 1; }
[[ -f config.json ]] || { echo "config.json missing: copy config.example.json and edit it (docs/SETUP.md)"; exit 1; }
DIR="car-deal-tracker"
ARGS=" $* "

# When to scan, in the server's clock. Default: every 3 hours from 07:00 to 22:00.
SCHEDULE="${SCAN_CRON:-$(envval SCAN_CRON)}"
SCHEDULE="${SCHEDULE:-0 7,10,13,16,19,22 * * *}"
CRON="$SCHEDULE cd \$HOME/$DIR && python3 -m tracker scan >> data/scan.log 2>&1"

ssh "$HOST" "mkdir -p ~/$DIR/data"
FILES=(tracker config.json requirements.txt docker-compose.yml deploy)
[[ -f data/seed.json ]] && FILES+=(data/seed.json)
tar czf - --exclude='__pycache__' "${FILES[@]}" | ssh "$HOST" "tar xzf - -C ~/$DIR"

if [[ "$ARGS" == *" --env "* ]]; then
  [[ -f .env ]] || { echo ".env missing (copy .env.example)"; exit 1; }
  scp -q .env "$HOST:~/$DIR/.env"
  ssh "$HOST" "chmod 600 ~/$DIR/.env"
  echo "copied .env"
fi

# Seed once, on first deploy only (no-op without data/seed.json).
ssh "$HOST" "cd ~/$DIR && [ -f data/tracker.db ] || python3 -m tracker seed"

# Idempotent crontab entry.
ssh "$HOST" "(crontab -l 2>/dev/null | grep -v 'python3 -m tracker scan'; echo '$CRON') | crontab -"

# Fix rows stored by older parser builds (idempotent).
ssh "$HOST" "cd ~/$DIR && python3 -m tracker cleanup"

# Web dashboard, plus the Tailscale sidecar when TS_AUTHKEY is set. Recreate web so it picks up new code.
if [[ "$ARGS" != *" --no-docker "* ]]; then
  UP="docker compose up -d --force-recreate web"
  [[ -n "$(envval TS_AUTHKEY)" ]] && UP="$UP && docker compose up -d tailscale"
  ssh "$HOST" "cd ~/$DIR && export HOST_UID=\$(id -u) HOST_GID=\$(id -g) && $UP"
fi

ssh "$HOST" "cd ~/$DIR && python3 -m tracker report --top 5"
echo "deployed to $HOST:~/$DIR; cron: $CRON"
