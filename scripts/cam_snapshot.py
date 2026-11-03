#!/usr/bin/env python3
"""cam_snapshot.py — Pose une image annotée de la caméra dans le dépôt.

Sert quand le flux MJPEG du port 8080 n'est pas joignable depuis le poste de
travail : l'Orin NX est sur un autre sous-réseau que le PC et seul le SSH est
routé (voir § Réseau de CLAUDE.md). VSCode affiche les .jpg dans l'éditeur, donc
un fichier dans le dépôt suffit à voir ce que regarde le robot — sans
navigateur, sans tunnel, sans port à ouvrir.

    python3 scripts/cam_snapshot.py [fichier.jpg]   # défaut cam_snapshot.jpg

Lit une image COMPLÈTE du flux (jusqu'au marqueur de fin JPEG) : un découpage
approximatif donne un fichier tronqué que certains visualiseurs refusent.
"""
import os
import sys
import time
import urllib.request

URL  = os.environ.get("VISION_STREAM", "http://127.0.0.1:8080/stream")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out  = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "cam_snapshot.jpg")

try:
    flux = urllib.request.urlopen(URL, timeout=10)
except Exception as e:
    sys.exit(f"Flux injoignable ({e}) — vision_server tourne-t-il ? "
             "Vérifier : ss -ltn | grep 8080")

buf, debut, fin = b"", time.time(), None
try:
    while time.time() - debut < 15:
        morceau = flux.read(8192)
        if not morceau:
            break
        buf += morceau
        i = buf.find(b"\xff\xd8")                      # début d'image
        if i >= 0:
            j = buf.find(b"\xff\xd9", i)               # fin d'image
            if j >= 0:
                fin = buf[i:j + 2]
                break
finally:
    flux.close()

if not fin:
    sys.exit("Aucune image complète reçue en 15 s.")

with open(out, "wb") as f:
    f.write(fin)
print(f"Image écrite : {out} ({len(fin)} octets, {time.strftime('%H:%M:%S')})\n"
      f"→ ouvre-la dans VSCode (clic dans l'explorateur). Relance ce script "
      f"pour rafraîchir.")
