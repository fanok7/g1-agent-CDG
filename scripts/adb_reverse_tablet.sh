#!/usr/bin/env bash
# Maintient le tunnel `adb reverse tcp:8000 tcp:8000` vers la tablette.
# Relance automatiquement le tunnel dès que la tablette est branchée/autorisée.
# Utilisé par le service systemd adb-reverse-tablet.service.

set -u
ADB=/usr/bin/adb
PORT=8000

export ADB_VENDOR_KEYS="/home/unitree/.android/adbkey"
export HOME=/home/unitree

log() { echo "[adb-reverse] $*"; }

# S'assure que le serveur adb tourne
"$ADB" start-server >/dev/null 2>&1

while true; do
    # Y a-t-il une tablette autorisée ?
    state=$("$ADB" get-state 2>/dev/null)
    if [ "$state" = "device" ]; then
        # Le reverse est-il déjà en place ?
        if ! "$ADB" reverse --list 2>/dev/null | grep -q "tcp:$PORT tcp:$PORT"; then
            if "$ADB" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
                log "tunnel tcp:$PORT rétabli"
            else
                log "échec création tunnel, nouvelle tentative bientôt"
            fi
        fi
    else
        log "tablette absente ou non autorisée (state=$state), attente"
    fi
    sleep 5
done
