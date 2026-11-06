<img src="famboard/web/static/logo.svg" alt="famboard" width="200">

# famboard

Dashboard di **monitoraggio familiare** costruita sopra Pi-hole. Raccoglie le
richieste DNS dei dispositivi di casa, le classifica per categoria (Social,
Intrattenimento, Educazione, Contenuti per adulti, ...) e permette di censire
le persone, attribuire i dispositivi e tenere d'occhio le categorie sensibili,
soprattutto per i minori.

Il Pi-hole resta la sorgente dati: famboard lo raggiunge **direttamente via API**
(nessun accesso a Proxmox), ci aggiunge l'anagrafica persone/dispositivi e la
categorizzazione dei domini.

## Architettura

```
Pi-hole (192.168.1.46)  --REST API-->  famboard
  /api/queries           (log DNS)       |-- poller: raccoglie + classifica
  /api/network/devices   (MAC, vendor)   |-- SQLite: persone, dispositivi, eventi
                                         |-- FastAPI: dashboard web
```

- **Identità dispositivo = MAC** (dall'inventario di rete). Le query, loggate per
  IP, vengono agganciate al dispositivo tramite una mappa IP→MAC aggiornata a ogni ciclo.
- **Classificazione**: indice domini→categoria costruito dalle blacklist
  categorizzate dell'Università di Tolosa (UT1), più un overlay curato dei
  principali servizi consumer (social, streaming, ricerca, ...).

## Installazione (da zero)

```bash
sudo ./setup.sh
```

L'installer: crea il virtualenv e installa le dipendenze; **cerca il Pi-hole**
(1. scansione della rete locale; 2. se assente, verifica se Pi-hole è installato
su questo stesso server; 3. altrimenti chiede l'indirizzo IP); chiede e **verifica
la password** del Pi-hole (Pi-hole v6 usa solo una password, nessuno userid);
scrive `.env` (generando una password per la dashboard); inizializza il DB,
scarica le liste categorie, e installa/avvia il servizio systemd + il timer dei
report. Variabili `PIHOLE_URL`/`PIHOLE_PASSWORD`/`APP_PASSWORD` preimpostate
nell'ambiente rendono l'installazione non interattiva.

**Nome di rete:** il setup registra sul Pi-hole il record DNS locale
**`famboard.home`** → IP del server e attiva un proxy systemd sulla porta 80
(`famboard-80.socket`, inoltro alla porta dell'app): la dashboard si apre
digitando `http://famboard.home`, senza IP né porta. Personalizzabile con
`FAMBOARD_HOST=nome.che.vuoi` (vuoto per saltare il passaggio); meglio evitare
nomi `.local`, che i dispositivi Apple risolvono solo via mDNS ignorando il
Pi-hole. `teardown.sh` rimuove sia il record DNS che il proxy.

**Disinstallazione pulita:** `sudo ./teardown.sh` (aggiungi `--purge` per
cancellare anche il database). Ferma e rimuove i servizi, **elimina dal Pi-hole
tutti i gruppi/client/regex creati da famboard**, e rimuove venv/`.env`.

## Configurazione manuale

```bash
cp .env.example .env
# modifica .env: PIHOLE_URL, PIHOLE_PASSWORD (consigliata una "application
# password" dedicata creata nel pannello Pi-hole, non quella admin)
```

## Avvio manuale

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m famboard.cli init                 # crea DB + categorie
# scarica le liste UT1 e costruisce l'indice (~5M domini):
curl -o data/categories/blacklists.tar.gz \
  https://dsi.ut-capitole.fr/blacklists/download/blacklists.tar.gz
.venv/bin/python -m famboard.cli build-categories data/categories/blacklists.tar.gz

# avvia dashboard + poller:
.venv/bin/uvicorn famboard.web.app:app --host 0.0.0.0 --port 8080
```

Dashboard: `http://<host>:8080` (o `http://famboard.home` se installato con
`setup.sh`). Il poller gira dentro lo stesso processo e
aggiorna i dati dal Pi-hole ogni `POLL_INTERVAL` secondi.

### Avvio persistente (systemd)

```ini
# /etc/systemd/system/famboard.service
[Unit]
Description=famboard
After=network.target
[Service]
WorkingDirectory=/opt/famBoard
EnvironmentFile=/opt/famBoard/.env
ExecStart=/opt/famBoard/.venv/bin/uvicorn famboard.web.app:app --host 0.0.0.0 --port 8080
Restart=always
[Install]
WantedBy=multi-user.target
```

Le unit sono già scritte in `/etc/systemd/system/` (`famboard.service`,
`famboard-report.service`, `famboard-report.timer`). Per attivarle:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now famboard.service        # dashboard + poller
sudo systemctl enable --now famboard-report.timer   # report settimanale (lun 08:00)
```

Il report settimanale usa `WEBHOOK_URL` e/o SMTP (`.env`); senza questi, genera
il testo ma non lo recapita.

## Comandi CLI

| comando | descrizione |
|---|---|
| `famboard.cli init` | crea il DB e semina le categorie |
| `famboard.cli build-categories <tar>` | carica le liste UT1 nell'indice |
| `famboard.cli poll-once` | un singolo ciclo di raccolta |
| `famboard.cli poll` | ciclo di raccolta in loop |
| `famboard.cli reclassify` | ricalcola le categorie sugli eventi salvati |
| `famboard.cli backfill --days N` | importa lo storico recente (max 24h) via API |
| `famboard.cli import-ftl` | importa lo storico profondo dal DB FTL (stdin) |
| `famboard.cli report --days N [--send]` | report attività per persona |
| `famboard.cli census-sync [--csv F]` | sincronizza censimento CSV ↔ DB (vedi sotto) |
| `famboard.cli prune --days N [--yes]` | rimuove i dispositivi non visti da N giorni |

### Censimento dispositivi (backup CSV)

Il CSV indicato da `CENSUS_CSV` (default `censimentoMAC/census.csv`) è il
**backup leggibile delle etichette** dei dispositivi. `census-sync` prima
importa dal CSV ciò che manca nel DB (etichetta i dispositivi senza nome,
registra MAC mai visti; **non sovrascrive mai** un'etichetta già presente nel
DB, i conflitti vengono solo segnalati), poi riesporta il CSV dal DB — il DB è
la fonte di verità. L'installer lo importa automaticamente se il file esiste;
la disinstallazione non lo tocca.

`prune` elimina i dispositivi non visti da N giorni (default 30) **sia dal DB
che dalla network table del Pi-hole** — necessario, altrimenti il poller li
reimporta al ciclo successivo. Un dispositivo unito (MAC multipli) si conserva
se almeno un membro è recente; lo storico query e gli avvisi restano. Senza
`--yes` è una prova a vuoto (mostra cosa verrebbe rimosso). Al termine il CSV
viene riallineato.

## Funzionalità della dashboard

- **Panoramica** con statistiche, grafico categorie, andamento nel tempo,
  attività per fascia oraria (ore notturne evidenziate) e top domini.
- **Persone**: censimento con flag maggiorenne/minorenne.
- **Dispositivi**: rilevati via MAC, con tipo indovinato dal produttore,
  **filtrabili per tipo** (phone, iot, server…); assegnabili a una persona o a
  "uso familiare".
- **Dettaglio persona / dispositivo** con le stesse analisi filtrate.
- **Unione dispositivi (MAC randomizzati)**: più MAC dello stesso telefono si
  uniscono in un dispositivo logico; storico, assegnazione e regole valgono su
  tutti. La lista mostra possibili "stessi dispositivi" da unire.
- **Controllo accesso a internet**, per **tutto il dispositivo o per singola
  categoria**, in quattro modi: blocco immediato, a tempo (durata o fino a un
  orario, con avvio ritardato), pianificato ricorrente ("blocca negli orari" /
  "consenti SOLO negli orari", es. Netflix 16–17), e **quota giornaliera** (es.
  "Facebook 1h/giorno", "Videogiochi 1h/giorno" — stima dal tempo di utilizzo,
  azzerata a mezzanotte). Più blocco/sblocco rapido per persona.
- **Blocklist per minori**: nella scheda di una persona **minorenne** si possono
  selezionare blocklist curate (contenuti per adulti, azzardo, disinformazione,
  social — liste StevenBlack). Vengono sottoscritte come adlist del Pi-hole in un
  gruppo dedicato `famboard-kids-<id>` e applicate **solo ai dispositivi di quel
  minore**. Richiedono l'applicazione del controllo attiva; la prima attivazione
  ricostruisce la gravity del Pi-hole.
- **Avvisi**: generati quando una categoria sensibile viene *effettivamente
  raggiunta* (non bloccata dal Pi-hole), con acknowledge e notifiche webhook.
- **Profilazione del disagio**: categorie "critiche" (segnali di
  autolesionismo/crisi, disturbi alimentari, estremismo) che allertano **per
  chiunque**, non solo minori, con un pannello dedicato in home. *Il DNS vede
  solo i domini contattati, non i termini di ricerca.*
- **Attenzione**: una pagina che classifica persone e dispositivi con un
  *punteggio di attenzione* calcolato dai domini sensibili/critici contattati —
  pesando ricorrenza, varietà di domini, ore notturne e tendenza in crescita, e
  dando molto più peso alle categorie critiche. Ogni soggetto segnalato riporta
  i *motivi* leggibili e i **dispositivi** coinvolti (nome, **MAC**, possessore,
  tipo). Nessuna cattura di pacchetti: il DNS mostra i domini, non i contenuti.
  Da qui si può **scaricare il report in PDF** (stile dashboard, tema scuro) o
  forzarne l'invio via notifica.
- **Report «da attenzionare» event-driven**: inviato via webhook/email **solo
  quando la situazione cambia** rispetto all'ultimo invio (deduplica per firma)
  e non prima di `ATTENTION_MIN_INTERVAL`: niente report identici a raffica. In
  cima, un riassunto di contesto opzionale generato da un **LLM locale**
  (`AI_SUMMARY_URL`, endpoint OpenAI-compatibile; disattivato se non impostato —
  riceve solo i fatti già calcolati, mai contenuti di navigazione). Sia il PDF
  sia le notifiche push riportano il dettaglio dispositivo (nome, MAC, possessore).
- **Stato**: connessione Pi-hole, salute del poller, copertura categorie, e
  l'interruttore per l'applicazione dei blocchi.

## Note

- **Autenticazione**: imposta `APP_PASSWORD` in `.env` per attivare il login.
  Se vuota, la dashboard è aperta (solo LAN fidata). La password del Pi-hole e
  quella della dashboard vivono solo in `.env` (gitignored).
- **Notifiche**: imposta `WEBHOOK_URL` (es. un topic ntfy) per ricevere un
  avviso quando un minore raggiunge una categoria sensibile.
- **Storico**: l'API del Pi-hole serve **solo le ultime 24h** (il limite
  `webserver.api.maxHistory` è fissato a 86400 e non è aumentabile). Per lo
  storico profondo (fino a `maxDBdays`, ~90 giorni) si legge direttamente il DB
  FTL del Pi-hole e lo si importa con `import-ftl`, es.:
  ```bash
  ssh root@<host-pihole-o-proxmox> \
    "pct exec <ctid> -- pihole-FTL sqlite3 /etc/pihole/pihole-FTL.db \
     'SELECT id,timestamp,type,status,domain,client FROM queries WHERE timestamp>=<cutoff>'" \
    | .venv/bin/python -m famboard.cli import-ftl
  ```
- **Copertura categorie**: viene classificato ~il 30% del traffico; il resto è
  CDN/telemetria/infrastruttura (non "navigazione"). L'overlay curato in
  `famboard/taxonomy.py` si allarga facilmente aggiungendo domini.
- **Applicazione dei blocchi**: disattivata di default. Si attiva da **Stato** →
  "Attiva applicazione". Quando attiva, famboard impone i blocchi creando sul
  Pi-hole un gruppo `famboard-blockall` (deny regex che blocca tutto) e gruppi
  `famboard-cat-<categoria>`, e assegnando i **client per IP corrente** ai
  gruppi, con un **riavvio del resolver** a ogni modifica (ricarica le
  assegnazioni e svuota la cache, così il blocco è immediato). La regex blocca-tutto
  è confinata al solo gruppo blockall (mai sul Default), così la rete non viene
  mai oscurata per errore. Il blocco è a livello DNS (come ogni parental control
  basato su Pi-hole): efficace sui dispositivi comuni, aggirabile da chi usa DNS
  privato/DoH/VPN.

## Test

Suite `pytest` (funzionali, di regressione e di sicurezza) in `tests/`:

```bash
.venv/bin/pytest
```

I test girano in isolamento (DB temporaneo, nessun accesso al Pi-hole reale).
Coprono classificazione, logica di controllo (orari/quota), enforcement
(inclusa la verifica di sicurezza che la regex blocca-tutto non tocchi mai il
gruppo Default), unione dispositivi, ingest/alert, analisi, report e le rotte
web (autenticazione richiesta, resistenza a SQL injection, nessuna fuga di
password).

**Esecuzione automatica a ogni modifica del codice:**
- *Claude Code*: aggiungi questo hook da `/hooks` (o in `.claude/settings.json`)
  — esegue `scripts/run_tests_hook.sh` dopo ogni Edit/Write di file `.py`:
  ```json
  { "hooks": { "PostToolUse": [ { "matcher": "Write|Edit|MultiEdit",
    "hooks": [ { "type": "command", "command": "bash /opt/famBoard/scripts/run_tests_hook.sh", "timeout": 120 } ] } ] } }
  ```
- *Git*: `ln -sf ../../scripts/pre-commit.sh .git/hooks/pre-commit` — blocca il
  commit se i test falliscono.
