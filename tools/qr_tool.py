"""
qr_tool.py — Tool "scanner_billet"
====================================
Lit /tmp/latest_realsense.jpg (frame partagée par vision_server.py)
et décode QR, PDF417 ET Aztec via le repo QR_scan_tool (uv + zxingcpp).

La RealSense est possédée par vision_server — pas de conflit d'accès caméra.
Dépendances : /home/unitree/QR_scan_tool (uv sync déjà fait).
"""

import os
import sys
import time
import datetime
import subprocess
import json as _json

import ipc
from tools.registry import register

RS_FRAME      = ipc.REALSENSE_FRAME
QR_REPO       = "/home/unitree/QR_scan_tool"
UV_BIN        = os.path.expanduser("~/.local/bin/uv")
FRAME_STALE   = 3.0    # secondes
SCAN_TIMEOUT  = 10.0   # secondes max
SCAN_FPS      = 6      # tentatives par seconde

# Script inline exécuté dans le venv uv du repo
_DECODE_SCRIPT = """
import sys
sys.path.insert(0, '{repo}')
from scanner import scan_image
result = scan_image(sys.argv[1])
print(result if result else '__NONE__')
"""


def _decode_file(path: str):
    """Lance scan_image dans le venv uv du repo. Retourne le texte décodé ou None."""
    script = _DECODE_SCRIPT.format(repo=QR_REPO)
    try:
        out = subprocess.check_output(
            [UV_BIN, "run", "--project", QR_REPO, "python", "-c", script, path],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        return None if out == "__NONE__" else out
    except Exception:
        return None


def _parse_bcbp(raw: str) -> dict:
    """Parse IATA BCBP → dict passager/vol. Retourne {"raw": raw} si non BCBP."""
    if not raw or not raw.startswith("M"):
        return {"raw": raw}
    try:
        # pos 0=M, 1=nb_legs, 2-21=nom(20), 22=ticket_indicator, 23-29=pnr(7)
        # 30-32=from(3), 33-35=to(3), 36-38=carrier(3), 39-43=flight(5)
        # 44-46=date_julian(3), 47=class(1), 48-51=seat(4), 52-56=seq(5), 57=status(1)
        nom_brut  = raw[2:22].strip()
        pnr       = raw[23:30].strip()
        depart    = raw[30:33].strip()
        arrivee   = raw[33:36].strip()
        compagnie = raw[36:39].strip()
        vol       = raw[39:44].strip()
        date_j    = raw[44:47].strip()
        classe    = raw[47:48].strip()
        siege     = raw[48:52].strip()
        sequence  = raw[52:57].strip()

        if "/" in nom_brut:
            nom, prenom = nom_brut.split("/", 1)
            nom_affiche = f"{prenom.strip().title()} {nom.strip().title()}"
        else:
            nom_affiche = nom_brut.title()

        date_str = None
        if date_j.isdigit():
            try:
                d = datetime.date(datetime.date.today().year, 1, 1) + datetime.timedelta(days=int(date_j) - 1)
                date_str = d.strftime("%d/%m/%Y")
            except Exception:
                pass

        return {
            "passager":  nom_affiche,
            "pnr":       pnr,
            "vol":       f"{compagnie.strip()}{vol.strip()}",
            "de":        depart,
            "vers":      arrivee,
            "date":      date_str or date_j,
            "classe":    classe,
            "siege":     siege.strip(),
            "sequence":  sequence.strip(),
        }
    except Exception:
        return {"raw": raw}


def _handler(**_kwargs) -> str:
    if not os.path.exists(RS_FRAME):
        return "RealSense non disponible — vision_server.py n'est pas démarré."

    age = time.time() - os.path.getmtime(RS_FRAME)
    if age > FRAME_STALE:
        return f"RealSense inactive — vision_server semble arrêté (frame vieille de {age:.0f}s)."

    print(f"[QR] Scan en cours ({SCAN_TIMEOUT:.0f}s max, QR+PDF417+Aztec)...", flush=True)

    deadline   = time.time() + SCAN_TIMEOUT
    interval   = 1.0 / SCAN_FPS
    last_mtime = None

    while time.time() < deadline:
        try:
            mtime = os.path.getmtime(RS_FRAME)
        except OSError:
            time.sleep(interval)
            continue

        if mtime == last_mtime:
            time.sleep(interval)
            continue
        last_mtime = mtime

        raw = _decode_file(RS_FRAME)
        if raw:
            print(f"[QR] Code trouvé : {raw[:60]}", flush=True)
            info = _parse_bcbp(raw)
            return _json.dumps(info, ensure_ascii=False)

        time.sleep(interval)

    return f"Aucun code-barres détecté en {SCAN_TIMEOUT:.0f}s. Demande au passager de présenter son billet face à la caméra."


register(
    {
        "name": "scanner_billet",
        "description": (
            "Scanne le billet d'avion ou carte d'embarquement présenté devant la caméra RealSense "
            "(QR code, PDF417 papier, Aztec). Retourne le nom du passager, numéro de vol, "
            "aéroports de départ/arrivée, date, siège et PNR. "
            "À appeler quand un passager demande à vérifier/lire son billet ou montre un QR code."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    _handler,
)
