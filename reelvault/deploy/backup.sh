#!/usr/bin/env bash
# ReelVault backups by hand (DESIGN-SAAS.md 11.7). The worker already makes one every day by itself:
#   /data/backups/db-YYYY-MM-DD.sqlite (keep 7) and users-YYYY-WW.tar.zst (member files without videos, keep 4).
#
#   ./deploy/backup.sh                    make a backup now
#   ./deploy/backup.sh list               show the backups on this server
#   ./deploy/backup.sh copy-out DIR       copy every backup from the container into DIR on this server
#   ./deploy/backup.sh restore db-2026-11-09.sqlite         put that database back (stops and restarts the app)
#   ./deploy/backup.sh restore-files users-2026-45.tar.zst  put member files back (frames, sheets, PDFs)
#
# Restore keeps the current database as before-restore-<time>.sqlite, so you can undo it the same way.
# Postgres: use pg_dump / pg_restore instead (DEPLOY.md, "Backups").
# Off-site copy every night (cron on this server, as root):
#   15 4 * * *  cd /path/to/reelvault && ./deploy/backup.sh copy-out /root/rv-backups && rclone copy /root/rv-backups remote:rv
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose() { (cd "$DEPLOY_DIR" && docker compose "$@"); }
die() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }
confirm() { local a=""; read -r -p "$1 [y/N]: " a || true; [ "$a" = y ] || [ "$a" = Y ] || [ "$a" = yes ]; }

[ -f "$DEPLOY_DIR/.env" ] || die "ReelVault is not set up here (deploy/.env is missing)."

safe_name() {  # only plain backup file names, never paths
  [[ "$1" =~ ^(db-[0-9]{4}-[0-9]{2}-[0-9]{2}|before-restore-[0-9T-]+)\.sqlite$ ]] && return 0
  [[ "$1" =~ ^users-[0-9]{4}-[0-9]{2}\.tar\.(zst|gz)$ ]] && return 0
  return 1
}

stop_app() { compose stop app $(compose config --services 2>/dev/null | grep -x worker || true); }
start_app() { compose start app $(compose config --services 2>/dev/null | grep -x worker || true); }

wait_healthy() {
  local i=0
  while [ "$i" -lt 180 ]; do
    curl -fsS --max-time 5 http://127.0.0.1:8000/healthz 2>/dev/null | grep -q '"ok": *true' && return 0
    sleep 3; i=$((i + 3))
  done
  return 1
}

cmd="${1:-backup}"
case "$cmd" in
  backup)
    compose exec -T app python -m reelvault.server.storage backup
    echo "Backup done. See them with: ./deploy/backup.sh list"
    ;;
  list)
    compose exec -T app python -m reelvault.server.storage list
    ;;
  copy-out)
    dest="${2:-}"; [ -n "$dest" ] || die "Say where to copy to, e.g. ./deploy/backup.sh copy-out /root/rv-backups"
    mkdir -p "$dest"
    cid="$(compose ps -q app)"; [ -n "$cid" ] || die "The app is not running."
    docker cp "$cid:/data/backups/." "$dest/"
    echo "Copied the backups to $dest"
    ;;
  restore|restore-files)
    name="${2:-}"
    safe_name "$name" || die "Give a backup file name from './deploy/backup.sh list', e.g. db-2026-11-09.sqlite"
    what="database"; sub="restore"
    if [ "$cmd" = restore-files ]; then what="member files"; sub="restore-files"; fi
    confirm "Restore the $what from $name? The app stops for a minute." || die "Nothing was changed."
    stop_app
    if compose run --rm --no-deps -T app python -m reelvault.server.storage "$sub" "/data/backups/$name"; then
      echo "Restored. Starting the app again..."
    else
      echo "The restore failed. Starting the app again with the old data..."
    fi
    start_app
    wait_healthy && echo "The app is running." || die "The app did not come back. Run ./deploy/setup.sh logs"
    ;;
  *)
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
