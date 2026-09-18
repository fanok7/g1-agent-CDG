"""
face_id_tool.py — Tool "identifier_personne"
=============================================
Lit /tmp/face_id_state.json écrit par le subprocess face_id.py
et retourne qui est actuellement visible devant la caméra.
"""

import json
import time

import ipc
from tools.registry import register

STATE_FILE   = ipc.FACE_ID_STATE
STALE_SECS   = 10.0


def _handler(**_kwargs):
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "La reconnaissance faciale n'est pas encore disponible (subprocess non démarré)."

    age = time.time() - state.get("ts", 0)
    if age > STALE_SECS:
        return "Aucune donnée récente de la caméra (flux interrompu)."

    faces = state.get("faces", [])
    if not faces:
        return "Personne n'est visible devant la caméra en ce moment."

    known   = [f["name"] for f in faces if f["name"] != "Inconnu"]
    unknown = sum(1 for f in faces if f["name"] == "Inconnu")

    parts = []
    if known:
        parts.append(", ".join(known))
    if unknown:
        parts.append(f"{unknown} personne(s) inconnue(s)")

    return "Devant la caméra : " + " et ".join(parts) + "."


register(
    {
        "name": "identifier_personne",
        "description": (
            "Identifie les personnes visibles devant la caméra par reconnaissance faciale. "
            "Retourne les prénoms des personnes connues et signale les inconnus. "
            "Appelle ce tool dès qu'on te demande qui est là, quel est le nom/prénom de la personne, "
            "si tu reconnais quelqu'un, ou pour accueillir quelqu'un par son prénom. "
            "C'est le seul tool capable de reconnaître et nommer des personnes."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    _handler,
)
