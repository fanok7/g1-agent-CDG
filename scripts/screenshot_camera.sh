#!/bin/bash
# Screenshot rapide de ce que voit une caméra du robot, sans passer par un tool agent.
# Copie la dernière frame partagée en /tmp vers vision/Screenshot/ (horodatée).
#
# Usage : ./scripts/screenshot_camera.sh [ugreen|realsense]
#   ugreen    (défaut) — caméra face_id, /tmp/latest_ugreen.jpg
#   realsense           — caméra vision_server, /tmp/latest_realsense.jpg

set -e

CAM="${1:-ugreen}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vision/Screenshot"
TS="$(date +%Y%m%d_%H%M%S)"

case "$CAM" in
  ugreen)    SRC=/tmp/latest_ugreen.jpg ;;
  realsense) SRC=/tmp/latest_realsense.jpg ;;
  *) echo "Usage: $0 [ugreen|realsense]" >&2; exit 1 ;;
esac

if [ ! -f "$SRC" ]; then
  echo "[ERREUR] $SRC introuvable — le subprocess correspondant tourne-t-il ?" >&2
  exit 1
fi

mkdir -p "$DIR"
DEST="$DIR/${CAM}_${TS}.jpg"
cp "$SRC" "$DEST"
echo "$DEST"
