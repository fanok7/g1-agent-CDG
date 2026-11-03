#!/usr/bin/env bash
# deploy_thor.sh — Synchronise la vision déportée vers la Jetson Thor.
#
# face_id.py tourne sur la Thor depuis sa PROPRE copie (~/g1_face_id/), pas
# depuis ce dépôt : une correction ici reste sans effet sur le robot jusqu'à ce
# qu'elle soit poussée là-bas. Ce script fait ce va-et-vient en une commande,
# code ET photos de référence (ajouter une photo dans le dépôt ne la rend pas
# visible par le robot non plus).
#
#   ./scripts/deploy_thor.sh            # synchronise + redémarre le service
#   ./scripts/deploy_thor.sh --dry-run  # montre ce qui changerait, sans rien faire
#   ./scripts/deploy_thor.sh --no-restart
#
# Voir THOR.md pour l'architecture, et le § Vision de CLAUDE.md.
set -euo pipefail

THOR="${THOR_HOST:-unitree@192.168.123.166}"
DEST="${THOR_DEST:-/home/unitree/g1_face_id}"
SERVICE="g1-face-id.service"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DRY=""
RESTART=1
for a in "$@"; do
    case "$a" in
        --dry-run)    DRY="--dry-run"; RESTART=0 ;;
        --no-restart) RESTART=0 ;;
        -h|--help)    sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Option inconnue : $a" >&2; exit 2 ;;
    esac
done

# Les fichiers réellement déployés là-bas. yolo26n.onnx et le venv sont gérés à
# la main (gros, et absents du dépôt) : pas touchés ici.
FILES=(
    "ipc.py"
    "vision/face_id/face_id.py"
    "vision/realsense_remote_push.py"
)

echo "→ Thor : $THOR:$DEST${DRY:+  (simulation)}"

for f in "${FILES[@]}"; do
    [[ -f "$REPO/$f" ]] || { echo "  ! absent du dépôt, ignoré : $f"; continue; }
    ssh "$THOR" "mkdir -p '$DEST/$(dirname "$f")'"
    rsync -ci $DRY "$REPO/$f" "$THOR:$DEST/$f" | sed 's/^/  /'
done

# --delete : une photo retirée du dépôt doit disparaître du robot, sinon la
# personne reste reconnue indéfiniment.
echo "→ photos de référence (--delete)"
rsync -rci --delete $DRY \
    --include='*.jpg' --include='*.jpeg' --include='*.png' --exclude='*' \
    "$REPO/vision/face_id/photos/" "$THOR:$DEST/vision/face_id/photos/" | sed 's/^/  /'

if [[ -n "$DRY" ]]; then
    echo "→ simulation terminée (rien n'a été modifié, service non redémarré)"
    exit 0
fi

if (( RESTART )); then
    echo "→ redémarrage de $SERVICE"
    ssh "$THOR" "export XDG_RUNTIME_DIR=/run/user/\$(id -u); systemctl --user restart $SERVICE"
    sleep 6
    ssh "$THOR" "export XDG_RUNTIME_DIR=/run/user/\$(id -u);
        journalctl --user-unit=$SERVICE --since '-1 min' --no-pager |
        grep -aiE 'Base :|2e essai|Aucun visage|Caméra reconnue|Traceback' | tail -15" | sed 's/^/  /'
else
    echo "→ service NON redémarré : le robot tourne encore sur l'ancien code."
    echo "  ssh $THOR \"export XDG_RUNTIME_DIR=/run/user/\\\$(id -u); systemctl --user restart $SERVICE\""
fi
