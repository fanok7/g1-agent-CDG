#!/usr/bin/env bash
# lancer_dashboard.sh — Dashboard de télémétrie du robot G1, EN UNE COMMANDE.
#
# À exécuter sur TON ORDINATEUR (Linux / macOS / WSL / Git Bash), pas sur le robot.
# Installe les dépendances si besoin, puis lance le dashboard Streamlit qui va
# chercher les données directement sur le robot.
#
#   bash scripts/lancer_dashboard.sh
#
# Robot sur une autre IP (Wi-Fi) ? :
#   G1_ROBOT_URL="http://192.168.0.128:8000" bash scripts/lancer_dashboard.sh
set -e

ICI="$(cd "$(dirname "$0")/.." && pwd)"     # racine du projet
export G1_ROBOT_URL="${G1_ROBOT_URL:-http://192.168.123.164:8000}"

echo "→ Robot ciblé : $G1_ROBOT_URL"

# Dépendances (installées une seule fois).
if ! python3 -c "import streamlit, pandas" 2>/dev/null; then
  echo "→ Installation de streamlit + pandas…"
  python3 -m pip install --quiet --upgrade streamlit pandas
fi

# Sans écran (ex: sur le robot Jetson en SSH) → serveur headless accessible sur
# le réseau. Avec écran (ton PC) → ouverture normale du navigateur.
if [ -z "$DISPLAY" ]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "→ Mode headless (pas d'écran). Ouvre dans TON navigateur :"
  echo "     http://${IP:-<IP-du-robot>}:8501"
  exec python3 -m streamlit run "$ICI/dashboard_stats.py" \
       --server.address 0.0.0.0 --server.headless true --server.port 8501
else
  echo "→ Ouverture du dashboard dans ton navigateur…"
  exec python3 -m streamlit run "$ICI/dashboard_stats.py"
fi
