"""
visitor_notify_tool.py — Prévient Samy par SMS (arrivée d'un visiteur, ou message libre)
================================================================================
Un seul tool, `signaler_visiteur` : sans `message`, envoie l'annonce standard
"visiteur à l'accueil : {nom}". Avec `message`, envoie ce texte à la place
(ex: demande spécifique du visiteur à transmettre). Pas de recherche d'agenda
ici (voir tools/calendar_tool.py pour ça — deux besoins différents).
Best-effort, dans un thread : un échec réseau ne doit jamais faire échouer
la réponse au visiteur.
"""

import json
import os
import threading
import time
from datetime import datetime

import pytz
import requests

import ipc
from tablet_server.server import mark_visitor_waiting
from tools.registry import register

_PARIS = pytz.timezone("Europe/Paris")

# Lus à l'import (comme les autres webhooks du projet) : modifier .env n'a
# d'effet qu'après relance de main.py.
_SMS_GATEWAY_URL       = os.environ.get("SMS_GATEWAY_URL", "")
_SMS_GATEWAY_USER      = os.environ.get("SMS_GATEWAY_USER", "")
_SMS_GATEWAY_PASS      = os.environ.get("SMS_GATEWAY_PASS", "")
_SMS_GATEWAY_DEVICE_ID = os.environ.get("SMS_GATEWAY_DEVICE_ID", "")
_SMS_SAMY_PHONE        = os.environ.get("SMS_SAMY_PHONE", "")

# Écran de notification de Samy (ESP32, voir Projet/écran interactif) — best-effort
# en plus du SMS, pas un remplacement : on garde le SMS tant que l'écran n'a pas
# fait ses preuves. URL typique : http://<ip-lan-esp32>/notify.
_SAMY_SCREEN_URL   = os.environ.get("SAMY_SCREEN_URL", "")
_SAMY_SCREEN_TOKEN = os.environ.get("SAMY_SCREEN_TOKEN", "")

# Anti-spam : le modèle "mini" rappelle parfois un tool plusieurs fois pour le
# même événement (oubli/répétition, cf. autres réflexes du projet). Sans
# `message` (annonce standard d'arrivée), on ne renvoie jamais deux fois pour
# le même nom dans la session. Avec `message` (contenu libre), on autorise
# plusieurs envois différents — seul le cooldown ci-dessous s'applique, pour
# ne pas bloquer un visiteur qui a vraiment plusieurs choses à transmettre.
_notified_names = set()
_last_sent_ts   = 0.0
_MIN_INTERVAL_S = 20
_FACE_STALE_S   = 5.0
_lock = threading.Lock()


def _visage_reconnu() -> str:
    try:
        with open(ipc.FACE_ID_STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    if time.time() - state.get("ts", 0) > _FACE_STALE_S:
        return ""
    connus = [f["name"] for f in state.get("faces", []) if f.get("name") not in (None, "", "Inconnu")]
    return connus[0] if len(connus) == 1 else ""


def _handler(nom: str = "", message: str = "", **_kwargs) -> str:
    if not nom or not nom.strip():
        nom = _visage_reconnu()
    if not nom or not nom.strip():
        return "Nom du visiteur inconnu : demande-le-lui avant de prévenir Samy."

    if not (_SMS_GATEWAY_URL and _SMS_GATEWAY_USER and _SMS_GATEWAY_PASS and _SMS_SAMY_PHONE):
        return "Notification SMS non configurée (variables SMS_* absentes du .env)."

    key = nom.strip().lower()
    global _last_sent_ts
    with _lock:
        now = time.monotonic()
        if not message.strip() and key in _notified_names:
            return "Samy a déjà été prévenu pour ce visiteur — pas de nouveau SMS."
        if now - _last_sent_ts < _MIN_INTERVAL_S:
            return "Samy vient déjà d'être prévenu — pas de nouveau SMS pour l'instant."
        if not message.strip():
            _notified_names.add(key)
        _last_sent_ts = now

    horodatage = datetime.now(_PARIS).strftime("%H:%M")
    if message.strip():
        texte = f"[G1] Message de {nom.strip()} ({horodatage}) : {message.strip()}"
    else:
        texte = f"[G1] Visiteur à l'accueil ({horodatage}) : {nom.strip()}"

    def _send():
        try:
            requests.post(
                _SMS_GATEWAY_URL,
                auth=(_SMS_GATEWAY_USER, _SMS_GATEWAY_PASS),
                json={
                    "phoneNumbers": [_SMS_SAMY_PHONE],
                    "textMessage": {"text": texte},
                    "deviceId": _SMS_GATEWAY_DEVICE_ID,
                    "priority": 100,
                    "ttl": 86400,
                },
                timeout=6,
            )
        except Exception as e:
            print(f"[SMS] Échec notification Samy : {e}", flush=True)

    def _send_screen():
        if not (_SAMY_SCREEN_URL and _SAMY_SCREEN_TOKEN):
            return
        try:
            requests.post(
                _SAMY_SCREEN_URL,
                headers={"X-Auth-Token": _SAMY_SCREEN_TOKEN},
                json={"texte": texte},
                timeout=4,
            )
        except Exception as e:
            print(f"[SAMY_SCREEN] Échec affichage écran : {e}", flush=True)

    threading.Thread(target=_send, daemon=True).start()
    threading.Thread(target=_send_screen, daemon=True).start()

    # Le visiteur reste présent en attendant Samy — si Samy appuie sur
    # "Candidat Suivant" sur son écran pendant ce temps, le robot pourra le
    # lui annoncer directement (voir tablet_server.server /api/samy-ready).
    try:
        mark_visitor_waiting()
    except Exception:
        pass

    return "Samy a été prévenu par SMS."


register(
    {
        "type": "function",
        "name": "signaler_visiteur",
        "description": (
            "Prévient Samy par SMS. Sans 'message' : annonce standard qu'un visiteur "
            "est arrivé. Avec 'message' : transmet ce texte précis à la place (ex: le "
            "visiteur demande de transmettre une information particulière à Samy). "
            "RÈGLE STRICTE sur le timing : pose D'ABORD la question de confirmation "
            "('Je peux prévenir Samy... ?') SEUL, comme TOUTE ta réponse de ce tour — "
            "n'appelle PAS ce tool dans le même tour que la question, même si tu es "
            "confiant que le visiteur va dire oui. Appelle-le UNIQUEMENT au tour SUIVANT, "
            "après avoir reçu sa réponse orale explicite ('oui'). N'interroge PAS "
            "l'agenda — pour chercher un rendez-vous existant, utiliser "
            "chercher_rdv_personne séparément."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nom": {"type": "string", "description": "Nom (et prénom si donné) du visiteur. Laisse vide si une personne connue est reconnue devant la caméra ; sinon, demande le nom d'abord."},
                "message": {
                    "type": "string",
                    "description": (
                        "Optionnel. Contenu précis à transmettre à Samy, si le visiteur "
                        "a une demande particulière plutôt qu'une simple annonce d'arrivée."
                    ),
                },
            },
            "required": [],
        },
    },
    _handler,
)
