#!/bin/sh
# Startet als root, stellt die Rechte am Datenordner sicher und läuft dann als UID/GID 1000.
# So funktionieren auch Bind-Mounts (./data), die Docker beim ersten Start als root anlegt.
set -e
DATA_DIR="${DATA_DIR:-/data}"
export APP_UID="${APP_UID:-1000}"
export APP_GID="${APP_GID:-1000}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  if ! chown -R "$APP_UID:$APP_GID" "$DATA_DIR" 2>/dev/null; then
    echo "WARNUNG: Konnte die Rechte von $DATA_DIR nicht anpassen (z.B. NFS/CIFS-Freigabe)." >&2
    echo "         Der Ordner muss für UID $APP_UID beschreibbar sein." >&2
  fi
  export HOME=/home/companion
  exec python -c 'import os, sys
gid, uid = int(os.environ["APP_GID"]), int(os.environ["APP_UID"])
os.setgroups([]); os.setgid(gid); os.setuid(uid)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"
