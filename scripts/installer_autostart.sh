#!/usr/bin/env bash
# installer_autostart.sh — Bascule le robot en MODE UNIQUE auto-start.
#
# Après ce script :
#   • main.py démarre tout seul au boot et se relance seul s'il plante (systemd)
#   • l'heure se synchronise automatiquement au boot (NTP) → HTTPS/OpenAI OK
#   • le launcher "Démarrer" (dual-mode) est désactivé → plus de conflit de port
#   • la tablette a TOUJOURS l'interface complète
#
# À lancer une seule fois, sur le robot :
#     bash scripts/installer_autostart.sh
# (demande le mot de passe sudo une fois)
set -e

DIR="/home/unitree/g1_agent_interim"
echo "== 1/5  Synchronisation automatique de l'heure (NTP) =="
# systemd-timesyncd est masqué sur ce robot → on le démasque avant d'activer NTP.
sudo systemctl unmask systemd-timesyncd.service || true
sudo systemctl enable --now systemd-timesyncd.service || true
sudo timedatectl set-ntp true || true
sudo timedatectl set-timezone Europe/Paris || true
echo "   (l'heure se corrigera au boot dès que le Wi-Fi est connecté)"
echo "   État : $(timedatectl show -p NTP -p NTPSynchronized 2>/dev/null | tr '\n' ' ')"

echo "== 2/5  Installation du service g1-main (main.py au boot + auto-restart) =="
sudo cp "$DIR/scripts/g1-main.service" /etc/systemd/system/g1-main.service
sudo systemctl daemon-reload
sudo systemctl enable g1-main.service

echo "== 3/5  Désactivation du launcher (fin du dual-mode) =="
sudo systemctl disable --now g1-launcher.service || true
#   'stop' tue aussi le main.py que le launcher avait lancé → port 8000 libéré.

echo "== 4/5  Démarrage du robot sous systemd =="
sudo systemctl start g1-main.service
echo "   (main.py démarre ; le serveur tablette met ~10-20 s à répondre)"

echo "== 5/5  Vérification =="
sleep 15
if curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/call/end 2>/dev/null | grep -q 200; then
  echo "   ✓ Serveur tablette opérationnel sur le port 8000"
else
  echo "   ⏳ Pas encore prêt — vérifie dans 20 s : systemctl status g1-main"
fi

echo
echo "TERMINÉ. Le robot est maintenant en mode unique auto-start."
echo "  • État        : systemctl status g1-main"
echo "  • Logs        : journalctl -u g1-main -f"
echo "  • Redémarrer  : sudo systemctl restart g1-main"
echo "  • Arrêter     : sudo systemctl stop g1-main"
echo "  • Revenir en arrière : sudo systemctl disable --now g1-main && sudo systemctl enable --now g1-launcher"
