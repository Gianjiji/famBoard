#!/usr/bin/env bash
# famboard restore — ripristina un backup in QUESTA directory, poi lancia
# setup.sh che ricrea il virtualenv e (ri)installa il servizio riusando il .env
# ripristinato (nessuna riconfigurazione, nessun ri-download delle categorie).
#
# Migrazione su un container dedicato:
#   1) sul nuovo container:  git clone <repo> /opt/famBoard && cd /opt/famBoard
#   2) copia qui l'archivio prodotto da backup.sh
#   3) sudo ./scripts/restore.sh famboard-backup-*.tar.gz
#
#   --no-service   ripristina i dati e crea il venv, ma non tocca systemd
set -euo pipefail

ARCHIVE="${1:-}"
case "$ARCHIVE" in
    ""|-h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
esac
[ -f "$ARCHIVE" ] || { echo "archivio non trovato: $ARCHIVE" >&2; exit 1; }
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
shift || true

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

# ferma un eventuale servizio in esecuzione, così i file non sono in uso
if command -v systemctl >/dev/null 2>&1; then
    systemctl stop famboard.service 2>/dev/null || true
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$ARCHIVE"
SRC="$TMP/famboard-backup"
[ -d "$SRC" ] || { echo "archivio non valido (manca famboard-backup/)" >&2; exit 1; }

echo "==> Ripristino dei dati..."
mkdir -p data
if [ -f "$SRC/data/famboard.db" ]; then
    cp "$SRC/data/famboard.db" data/famboard.db
    rm -f data/famboard.db-wal data/famboard.db-shm   # lo snapshot è autoconsistente
fi
[ -f "$SRC/data/.secret_key" ] && { cp "$SRC/data/.secret_key" data/.secret_key; chmod 600 data/.secret_key; }
[ -f "$SRC/.env" ]             && { cp "$SRC/.env" .env; chmod 600 .env; }
[ -d "$SRC/censimentoMAC" ]    && cp -r "$SRC/censimentoMAC" .
if [ -f "$SRC/data/categories/blacklists.tar.gz" ]; then
    mkdir -p data/categories
    cp "$SRC/data/categories/blacklists.tar.gz" data/categories/
fi
[ -f "$SRC/MANIFEST" ] && { echo "--- MANIFEST del backup ---"; cat "$SRC/MANIFEST"; echo "---------------------------"; }

NO_SERVICE=0
for a in "$@"; do [ "$a" = "--no-service" ] && NO_SERVICE=1; done

echo "==> Configurazione (venv + servizio) tramite setup.sh, riusando il .env ripristinato..."
if [ "$NO_SERVICE" = 1 ]; then
    FAMBOARD_NO_SERVICE=1 exec ./setup.sh
else
    exec ./setup.sh
fi
