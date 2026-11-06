#!/usr/bin/env bash
# famboard backup — crea un archivio con tutto lo stato "prezioso": il database
# (incluso l'indice domini→categoria), .env, la chiave di sessione e il
# censimento dispositivi. NON include venv, codice o unit systemd (si ricreano).
#
# Il DB viene copiato con lo snapshot online di SQLite: consistente anche con il
# servizio in esecuzione, nessun fermo necessario.
#
#   scripts/backup.sh                       # -> famboard-backup-<data>.tar.gz
#   scripts/backup.sh -o /path/mio.tar.gz   # nome file a scelta
#   scripts/backup.sh --with-categories     # include anche il tarball UT1 sorgente
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

OUT=""; WITH_CAT=0
while [ $# -gt 0 ]; do
    case "$1" in
        -o) OUT="${2:?manca il nome file dopo -o}"; shift 2;;
        --with-categories) WITH_CAT=1; shift;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) echo "argomento non riconosciuto: $1" >&2; exit 1;;
    esac
done
OUT="${OUT:-famboard-backup-$(date +%Y%m%d-%H%M).tar.gz}"

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
DBP="$("$PY" -c 'from famboard.config import settings;print(settings.db_path)' 2>/dev/null || echo data/famboard.db)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STAGE="$TMP/famboard-backup"; mkdir -p "$STAGE/data"

if [ -f "$DBP" ]; then
    echo "==> Snapshot consistente del database..."
    "$PY" - "$DBP" "$STAGE/data/famboard.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)
s.close(); d.close()
PY
else
    echo "[!] Database non trovato in $DBP (backup senza DB)." >&2
fi

[ -f data/.secret_key ] && cp data/.secret_key "$STAGE/data/.secret_key"
[ -f .env ]             && cp .env "$STAGE/.env"
[ -d censimentoMAC ]    && cp -r censimentoMAC "$STAGE/censimentoMAC"
if [ "$WITH_CAT" = 1 ] && [ -f data/categories/blacklists.tar.gz ]; then
    mkdir -p "$STAGE/data/categories"
    cp data/categories/blacklists.tar.gz "$STAGE/data/categories/"
fi

cat > "$STAGE/MANIFEST" <<EOF
famboard backup
creato:  $(date -Iseconds)
host:    $(hostname 2>/dev/null || echo ?)
commit:  $(git rev-parse --short HEAD 2>/dev/null || echo n/d)
db:      $DBP
EOF

tar -C "$TMP" -czf "$OUT" famboard-backup
echo "==> Backup creato: $OUT ($(du -h "$OUT" | cut -f1))"
echo "    Ripristina sul nuovo container con: scripts/restore.sh $OUT"
