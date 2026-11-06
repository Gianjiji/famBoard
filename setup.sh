#!/usr/bin/env bash
# famboard installer — sets up the app from scratch.
#
# Pi-hole: the installer first asks whether you already have Pi-hole or want to
# install it now on this server.
#   - install now  -> runs the official Pi-hole installer, then uses 127.0.0.1;
#   - already have  -> discovery: 1) scan the local network for a Pi-hole (v6
#     API); 2) check whether Pi-hole is installed on THIS server; 3) otherwise
#     ask for the Pi-hole IP.
#
# Non-interactive use: preset PIHOLE_URL / PIHOLE_PASSWORD (and optionally
# APP_PASSWORD) in the environment and the prompts are skipped. Force the
# install path non-interactively with INSTALL_PIHOLE=1.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
PORT="${FAMBOARD_PORT:-8080}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; }

need() { command -v "$1" >/dev/null 2>&1; }

# --- a Pi-hole v6 answers /api with a JSON carrying FTL's "took" field -------
probe_pihole() {  # $1 = ip ; echoes base URL on success
    local ip=$1 url
    for url in "https://$ip" "http://$ip"; do
        if curl -sk --max-time 3 -X POST "$url/api/auth" \
             -H 'Content-Type: application/json' -d '{"password":""}' 2>/dev/null \
             | grep -q '"took"'; then
            echo "$url"; return 0
        fi
    done
    return 1
}

local_subnet_base() {  # echoes e.g. 192.168.1
    ip -o -4 addr show scope global 2>/dev/null \
        | awk '{print $4}' | head -1 | cut -d/ -f1 | awk -F. '{print $1"."$2"."$3}'
}

scan_network() {  # echoes the first Pi-hole base URL found, or nothing
    local base ip url
    # fast path: DNS servers in resolv.conf and the default gateway
    local candidates
    candidates="$(awk '/^nameserver/{print $2}' /etc/resolv.conf 2>/dev/null) \
                 $(ip route 2>/dev/null | awk '/default/{print $3}')"
    for ip in $candidates; do
        [ -n "$ip" ] || continue
        url=$(probe_pihole "$ip") && { echo "$url"; return 0; }
    done
    # full sweep of the /24 (parallel, short timeout)
    base="$(local_subnet_base)"; [ -n "$base" ] || return 1
    say "Scansione della rete ${base}.0/24 ..." >&2
    for n in $(seq 1 254); do
        ( probe_pihole "$base.$n" >"/tmp/fb_scan_$n" 2>/dev/null || true ) &
        if (( n % 50 == 0 )); then wait; fi
    done
    wait
    for n in $(seq 1 254); do
        if [ -s "/tmp/fb_scan_$n" ]; then url=$(cat "/tmp/fb_scan_$n"); rm -f /tmp/fb_scan_*; echo "$url"; return 0; fi
    done
    rm -f /tmp/fb_scan_* 2>/dev/null || true
    return 1
}

pihole_installed_locally() {
    command -v pihole-FTL >/dev/null 2>&1 || [ -f /etc/pihole/pihole.toml ] || [ -d /etc/pihole ]
}

verify_password() {  # $1 url, $2 password -> 0 if valid
    curl -sk --max-time 6 -X POST "$1/api/auth" -H 'Content-Type: application/json' \
        -d "{\"password\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$2")}" 2>/dev/null \
        | grep -q '"valid":true'
}

# Install Pi-hole from the official installer and set a known API password.
# On success sets PH_URL (and usually PH_PASS). Returns non-zero on failure.
install_pihole() {
    if [ "$(id -u)" != 0 ]; then
        err "L'installazione di Pi-hole richiede privilegi root (sudo)."; return 1
    fi
    need curl || { err "curl non trovato: impossibile scaricare l'installer Pi-hole."; return 1; }
    say "Installazione di Pi-hole tramite l'installer ufficiale (install.pi-hole.net)..."
    if ! (curl -sSL https://install.pi-hole.net | bash); then
        err "Installer di Pi-hole non riuscito."; return 1
    fi
    need pihole || { err "Pi-hole non risulta installato dopo l'installer."; return 1; }
    PH_URL="https://127.0.0.1"
    local newpw="${PIHOLE_PASSWORD:-}" generated=0
    if [ -z "$newpw" ]; then
        read -rsp "Imposta una password per Pi-hole (vuoto = generane una automaticamente): " newpw; echo
        if [ -z "$newpw" ]; then
            newpw="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)))')"
            generated=1
        fi
    fi
    # Set it as the Pi-hole admin/API password, then hand it to the rest of the
    # installer so it lands in .env (PIHOLE_PASSWORD) for the famboard API client.
    if pihole setpassword "$newpw" >/dev/null 2>&1; then
        PH_PASS="$newpw"
        say "Pi-hole installato. Password amministratore impostata e salvata in .env."
        if [ "$generated" = 1 ]; then
            say "Password Pi-hole generata (serve anche per il pannello admin): $newpw"
        fi
    else
        warn "Pi-hole installato, ma non ho impostato la password: te la chiederò ora."
    fi
    return 0
}

# =============================================================================
say "Installazione famboard in $APP_DIR"

# 1) Python environment ------------------------------------------------------
if ! need python3; then err "python3 non trovato. Installalo e riprova."; exit 1; fi
if [ ! -d .venv ]; then say "Creazione virtualenv..."; python3 -m venv .venv; fi
say "Installazione dipendenze..."; .venv/bin/pip install --quiet --upgrade pip >/dev/null
.venv/bin/pip install --quiet -r requirements.txt

# 2) Pi-hole: already installed, or install it now? --------------------------
PH_URL="${PIHOLE_URL:-}"
PH_PASS="${PIHOLE_PASSWORD:-}"
if [ -z "$PH_URL" ]; then
    WANT_INSTALL=0
    if [ "${INSTALL_PIHOLE:-0}" = 1 ]; then
        WANT_INSTALL=1
    elif pihole_installed_locally; then
        say "Pi-hole risulta già installato su questo server."
    else
        printf '\n'
        say "Pi-hole è il motore DNS su cui si appoggia famboard."
        read -rp "Hai già Pi-hole, o vuoi installarlo ora su questo server? [G=ho già Pi-hole / i=installa] " ANS
        case "$ANS" in i|I|install*) WANT_INSTALL=1;; esac
    fi
    if [ "$WANT_INSTALL" = 1 ]; then
        install_pihole || warn "Installazione saltata: cerco un Pi-hole esistente."
    fi
fi

# Discovery (only if we still don't have a Pi-hole URL).
if [ -z "$PH_URL" ]; then
    say "Ricerca del Pi-hole nella rete..."
    if PH_URL="$(scan_network)"; then
        say "Pi-hole trovato: $PH_URL"
    elif pihole_installed_locally; then
        say "Pi-hole risulta installato su questo stesso server: uso https://127.0.0.1"
        PH_URL="https://127.0.0.1"
    else
        warn "Nessun Pi-hole trovato automaticamente."
        read -rp "Indirizzo IP (o URL) del Pi-hole: " ANS
        case "$ANS" in http*://*) PH_URL="$ANS";; *) PH_URL="https://$ANS";; esac
    fi
fi

# 3) Password (Pi-hole v6 uses a password only; no userid) -------------------
if [ -n "${PIHOLE_USER:-}" ]; then
    warn "Pi-hole non usa uno userid per l'API: ignoro PIHOLE_USER='$PIHOLE_USER'."
fi
if [ -z "$PH_PASS" ]; then
    while :; do
        read -rsp "Password del Pi-hole per $PH_URL: " PH_PASS; echo
        if verify_password "$PH_URL" "$PH_PASS"; then say "Password verificata."; break
        else err "Password non valida (o Pi-hole non raggiungibile). Riprova."; fi
    done
else
    verify_password "$PH_URL" "$PH_PASS" || warn "Attenzione: la password preimpostata non è stata verificata."
fi

# 4) Write .env --------------------------------------------------------------
APP_PASS="${APP_PASSWORD:-$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(18)))')}"
say "Scrittura di .env"
umask 177
cat > .env <<EOF
PIHOLE_URL=$PH_URL
PIHOLE_PASSWORD=$PH_PASS
PIHOLE_VERIFY_TLS=false
DB_PATH=data/famboard.db
POLL_INTERVAL=60
APP_PASSWORD=$APP_PASS
SECRET_KEY=
WEBHOOK_URL=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
SMTP_TO=
SMTP_TLS=true
EOF
umask 022

# 5) DB + category lists -----------------------------------------------------
say "Inizializzazione database e categorie..."
.venv/bin/python -m famboard.cli init
# se esiste un censimento CSV, precarica le etichette dei dispositivi
if [ -f "${CENSUS_CSV:-censimentoMAC/census.csv}" ]; then
    say "Importazione censimento dispositivi..."
    .venv/bin/python -m famboard.cli census-sync || warn "Censimento non importato."
fi
if [ ! -f data/categories/blacklists.tar.gz ]; then
    say "Scaricamento liste categorie (UT1)..."
    mkdir -p data/categories
    curl -s --max-time 120 -o data/categories/blacklists.tar.gz \
        "https://dsi.ut-capitole.fr/blacklists/download/blacklists.tar.gz" \
        && .venv/bin/python -m famboard.cli build-categories data/categories/blacklists.tar.gz \
        || warn "Liste categorie non scaricate: potrai farlo dopo con build-categories."
fi

# 6) systemd service + weekly report timer -----------------------------------
if need systemctl && [ "$(id -u)" = 0 ]; then
    say "Installazione servizio systemd..."
    cat > /etc/systemd/system/famboard.service <<EOF
[Unit]
Description=famboard - household network dashboard
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn famboard.web.app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/famboard-report.service <<EOF
[Unit]
Description=famboard - weekly report
[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m famboard.cli report --days 7 --send
EOF
    cat > /etc/systemd/system/famboard-report.timer <<EOF
[Unit]
Description=famboard - weekly report timer (Mondays 08:00)
[Timer]
OnCalendar=Mon *-*-* 08:00:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
    cat > /etc/systemd/system/famboard-80.socket <<EOF
[Unit]
Description=famboard - ascolto su porta 80 (inoltro a $PORT)
[Socket]
ListenStream=80
[Install]
WantedBy=sockets.target
EOF
    cat > /etc/systemd/system/famboard-80.service <<EOF
[Unit]
Description=famboard - proxy porta 80 -> $PORT
Requires=famboard.service
After=famboard.service
[Service]
ExecStart=/lib/systemd/systemd-socket-proxyd 127.0.0.1:$PORT
EOF
    systemctl daemon-reload
    systemctl enable --now famboard.service
    systemctl enable --now famboard-report.timer
    systemctl enable --now famboard-80.socket 2>/dev/null \
        || warn "Porta 80 non attivata (occupata?): la dashboard resta su :$PORT."
    say "Servizio avviato."
else
    warn "systemd non disponibile o non root: avvia manualmente con"
    warn "  .venv/bin/uvicorn famboard.web.app:app --host 0.0.0.0 --port $PORT"
fi

# 7) nome di rete (es. http://famboard.home) ---------------------------------
FAMBOARD_HOST="${FAMBOARD_HOST:-famboard.home}"
IP_LOCAL="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$FAMBOARD_HOST" ] && [ -n "$IP_LOCAL" ]; then
    say "Registrazione DNS locale '$FAMBOARD_HOST' sul Pi-hole..."
    FAMBOARD_HOST="$FAMBOARD_HOST" IP_LOCAL="$IP_LOCAL" .venv/bin/python - <<'PY' \
        || warn "Record DNS non registrato: aggiungilo dal pannello Pi-hole (Local DNS)."
import os
from famboard.config import settings
from famboard.pihole import PiholeClient
host, ip = os.environ["FAMBOARD_HOST"], os.environ["IP_LOCAL"]
with PiholeClient(settings.pihole_url, settings.pihole_password,
                  verify_tls=settings.pihole_verify_tls) as c:
    for e in c.dns_hosts():
        parts = e.split()
        if len(parts) == 2 and parts[1] == host and parts[0] != ip:
            c.delete_dns_host(e)          # stale IP for our hostname
    if f"{ip} {host}" not in c.dns_hosts():
        c.add_dns_host(ip, host)
print(f"  {host} -> {ip}")
PY
fi

say "Fatto! Dashboard: http://${FAMBOARD_HOST:-$IP_LOCAL} (oppure http://${IP_LOCAL:-<questo-host>}:$PORT)"
say "Password dashboard: $APP_PASS   (modificabile in .env, voce APP_PASSWORD)"
