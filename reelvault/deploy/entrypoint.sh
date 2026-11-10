#!/bin/sh
# ReelVault container entrypoint (runs under tini). Normally the container runs as user reelvault (uid 10001) and
# this only starts the command. Some platforms mount a new volume owned by root (Fly.io, Railway, Render). Run the
# container as root there (see deploy/hosts/paas-with-volume.md): this gives /data to uid 10001 once, then drops to
# it, so the app itself never runs as root.
set -e
DATA="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  if [ "$(stat -c %u "$DATA")" != "10001" ]; then
    echo "Giving $DATA to the reelvault user (uid 10001). This runs once."
    chown -R 10001:10001 "$DATA"
  else
    # Files a root shell made later (a restore or backup run from `fly ssh console` or a Railway/Render shell)
    # would stop the app from writing. Give back only what uid 10001 doesn't own: cheap when there is nothing.
    find "$DATA" ! -uid 10001 -exec chown 10001:10001 {} + 2>/dev/null || true
  fi
  exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all -- "$@"
fi
exec "$@"
