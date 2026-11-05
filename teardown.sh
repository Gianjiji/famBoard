#!/usr/bin/env bash
# famboard uninstaller — clean removal.
#   - stops & removes the systemd service + report timer
#   - removes every famboard artifact from the Pi-hole (groups/clients/regex)
#   - deletes the virtualenv, local database, secret key and .env
# The source files are left in place; delete the folder yourself if you want.
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

PURGE_DATA=0
[ "${1:-}" = "--purge" ] && PURGE_DATA=1

# 1) stop & remove systemd units ---------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
    say "Arresto e rimozione servizi systemd..."
    for unit in famboard.service famboard-report.timer famboard-report.service \
                famboard-80.socket famboard-80.service; do
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload 2>/dev/null || true
else
    warn "systemd non disponibile o non root: salto la rimozione del servizio."
fi

# 2) remove famboard's Pi-hole artifacts -------------------------------------
if [ -d .venv ] && [ -f .env ]; then
    say "Rimozione gruppi/client/regex creati da famboard sul Pi-hole..."
    FAMBOARD_HOST="${FAMBOARD_HOST:-famboard.home}" .venv/bin/python - <<'PY' \
        || warn "Pulizia Pi-hole saltata (irraggiungibile o credenziali assenti)."
import os
from famboard.config import settings
from famboard.pihole import PiholeClient
from famboard import enforce
host = os.environ.get("FAMBOARD_HOST", "famboard.home")
with PiholeClient(settings.pihole_url, settings.pihole_password,
                  verify_tls=settings.pihole_verify_tls) as c:
    print("  rimossi:", enforce.teardown(c))
    for e in c.dns_hosts():
        parts = e.split()
        if len(parts) == 2 and parts[1] == host:
            c.delete_dns_host(e)
            print("  rimosso record DNS:", e)
PY
else
    warn "venv/.env assenti: salto la pulizia del Pi-hole."
fi

# 3) remove venv + local state -----------------------------------------------
say "Rimozione virtualenv..."
rm -rf .venv

if [ "$PURGE_DATA" = 1 ]; then
    REPLY=s
else
    read -rp "Eliminare anche il database e i dati raccolti (data/)? [s/N] " REPLY || REPLY=n
fi
case "$REPLY" in
    s|S|y|Y) say "Rimozione data/ e .env..."; rm -rf data .env ;;
    *) warn "Dati conservati. Rimuovo solo .env."; rm -f .env ;;
esac

say "Disinstallazione completata. (I file sorgente restano in $APP_DIR)"
