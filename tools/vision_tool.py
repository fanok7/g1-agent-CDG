"""
vision_tool.py — Tool "ce_que_je_vois"
=======================================
Lit /tmp/vision_state.json  (objets YOLO, écrit par vision_server.py)
et /tmp/face_id_state.json  (visages InsightFace, écrit par face_id.py)
et retourne un résumé de ce que le robot voit.
"""

import json
import time

import ipc
from tools.registry import register

VISION_FILE  = ipc.VISION_STATE
FACE_FILE    = ipc.FACE_ID_STATE
VISION_STALE = 15.0
FACE_STALE   = 10.0


def _handler(**_kwargs):
    now = time.time()
    lines = []
    has_known_faces = False

    # ── Personnes reconnues (InsightFace) — annoncées EN PREMIER ────────────────
    try:
        with open(FACE_FILE) as f:
            fs = json.load(f)
        if now - fs.get("ts", 0) < FACE_STALE:
            faces = fs.get("faces", [])
            known   = [f["name"] for f in faces if f.get("name") != "Inconnu"]
            unknown = sum(1 for f in faces if f.get("name") == "Inconnu")
            has_known_faces = bool(known)
            if known:
                noms = ", ".join(known)
                extra = f" (et {unknown} inconnu(s))" if unknown else ""
                lines.append(f"Personnes reconnues, dis bien leur prénom : {noms}{extra}.")
            elif unknown:
                lines.append(f"{unknown} personne(s) non reconnue(s) devant la caméra.")
    except FileNotFoundError:
        pass   # face_id subprocess pas encore lancé, on ne le mentionne pas
    except Exception as e:
        lines.append(f"Erreur lecture face_id : {e}")

    # ── Objets YOLO ────────────────────────────────────────────────────────────
    try:
        with open(VISION_FILE) as f:
            vs = json.load(f)
        if now - vs.get("ts", 0) < VISION_STALE:
            objects = vs.get("objects", [])
            if objects:
                parts = []
                for o in objects:
                    label = o.get("label", "objet")
                    # Ne pas répéter "person" si la personne est déjà identifiée par son prénom.
                    if label == "person" and has_known_faces:
                        continue
                    parts.append(label)
                if parts:
                    lines.append("Objets détectés : " + ", ".join(parts) + ".")
            else:
                lines.append("Aucun objet détecté par la caméra.")
        else:
            lines.append("Données caméra périmées (vision_server inactif ?).")
    except FileNotFoundError:
        lines.append("vision_server non démarré — pas de données YOLO.")
    except Exception as e:
        lines.append(f"Erreur lecture vision : {e}")

    return "\n".join(lines) if lines else "Je ne vois rien pour l'instant."


register(
    {
        "name": "ce_que_je_vois",
        "description": (
            "Retourne ce que la caméra voit à l'instant : objets détectés et personnes reconnues. "
            "Appelle ce tool pour toute question visuelle : 'qu'est-ce que tu vois ?', "
            "'il y a quelqu'un ?', 'tu vois mon sac ?', 'la zone est dégagée ?'. "
            "Appelle-le à chaque question — les données changent en temps réel."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    _handler,
)
