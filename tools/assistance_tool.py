"""tools/assistance_tool.py — Appel d'assistance humaine (fauteuil roulant,
demande de parler à un responsable…).

Enchaînement déclenché par le tool :
  1. Crée un salon audio Daily.co UNIQUE pour cette demande.
  2. Alerte le responsable via un webhook n8n → notification push ntfy
     (motif, langue, lien cliquable vers le salon).
  3. Met l'IA en SOURDINE via un verrou fichier (/tmp/ai_paused) : tant qu'il
     existe, agent/events.py n'envoie plus le micro et ne joue plus la voix.
  4. Bascule la tablette en mode "Appel en cours" : elle rejoint le salon en
     arrière-plan (daily-js callObject), le son sort par ses haut-parleurs.

Le raccrochage (bouton tablette ou départ du salon) appelle POST /call/end, qui
retire le verrou → l'IA se réveille.

POURQUOI DAILY ET PAS JITSI : le SDK Daily rejoint un salon en `callObject`,
c'est-à-dire SANS iframe. Les serveurs Jitsi gratuits sont inutilisables ici —
soit ils refusent l'intégration en iframe (`X-Frame-Options`, cas de
meet.ffmuc.net), soit ils exigent un compte et parquent le robot dans un lobby
(cas de meet.jit.si depuis 2024). Sans iframe, ces deux contraintes disparaissent.
"""

import os
import threading
import time
import uuid
from datetime import datetime

import requests

from tools.registry import register
from tablet_server.server import (
    push_call, push_call_end, push_assist_confirm, AI_PAUSED_FLAG,
)

# Webhook n8n qui prévient le responsable (→ ntfy dans le workflow fourni).
N8N_ASSIST_WEBHOOK_URL = os.environ.get(
    "N8N_ASSIST_WEBHOOK_URL", "https://n8n.i-interim.com/webhook/assistance"
)

# ── Daily.co ────────────────────────────────────────────────────────────────
# Deux modes, dans cet ordre de préférence :
#   1. DAILY_API_KEY  → un salon NEUF et unique par appel (recommandé : deux
#      visiteurs successifs ne peuvent pas se retrouver dans le même salon, et
#      le salon expire tout seul).
#   2. DAILY_ROOM_URL → un salon fixe créé à la main dans le dashboard Daily
#      (dépannage : aucune clé API nécessaire, mais salon permanent et partagé).
DAILY_API_KEY = os.environ.get("DAILY_API_KEY", "").strip()
DAILY_ROOM_URL = os.environ.get("DAILY_ROOM_URL", "").strip().rstrip("/")
DAILY_API = "https://api.daily.co/v1/rooms"

# Sécurité : si personne ne raccroche, l'IA ne doit pas rester muette pour
# toujours → on lève le verrou automatiquement au bout de ce délai.
_CALL_MAX_SECONDS = int(os.environ.get("ASSIST_CALL_TIMEOUT", "600"))   # 10 min

# Où passe la voix pendant l'appel :
#   "robot"    → micro + haut-parleur DU ROBOT (daily-python sur le Jetson).
#                Meilleure prise de son (SNR mesuré à 34 dB contre le ventilo
#                capté par la tablette), mais half-duplex : pas d'interruption.
#   "tablette" → micro + HP de la tablette (navigateur). Full duplex grâce à
#                l'annulation d'écho du navigateur. C'est le mode de SECOURS :
#                mets ASSIST_AUDIO=tablette dans ~/.env pour y revenir.
ASSIST_AUDIO = os.environ.get("ASSIST_AUDIO", "robot").strip().lower()


def _creer_salon():
    """Retourne (nom_du_salon, url). Salon Daily neuf si une clé API est
    configurée, sinon repli sur le salon fixe DAILY_ROOM_URL.

    Le nom est non devinable (uuid) : un salon Daily est public par son URL, un
    nom prévisible laisserait un tiers écouter la conversation."""
    nom = "g1-assist-{}".format(uuid.uuid4().hex[:12])

    if DAILY_API_KEY:
        try:
            r = requests.post(
                DAILY_API,
                headers={"Authorization": "Bearer {}".format(DAILY_API_KEY)},
                json={
                    "name": nom,
                    "privacy": "public",   # le responsable rejoint par simple lien
                    "properties": {
                        # Le salon s'auto-détruit : pas de salon orphelin qui
                        # traîne, et la facturation reste bornée.
                        "exp": int(time.time()) + _CALL_MAX_SECONDS + 300,
                        "eject_at_room_exp": True,
                        "start_video_off": True,    # appel AUDIO uniquement
                        "start_audio_off": False,
                        "enable_prejoin_ui": False,  # personne ne peut cliquer côté robot
                        "enable_chat": False,
                        "enable_screenshare": False,
                    },
                },
                timeout=8,
            )
            if r.status_code in (200, 201):
                return nom, r.json().get("url", "")
            print("[ASSIST] Création salon Daily refusée ({}) : {}".format(
                r.status_code, r.text[:200]))
        except Exception as e:
            print("[ASSIST] API Daily injoignable : {}".format(e))

    # Repli : salon fixe. Mieux qu'un appel qui n'a pas lieu du tout.
    if DAILY_ROOM_URL:
        print("[ASSIST] Repli sur le salon fixe DAILY_ROOM_URL.")
        return DAILY_ROOM_URL.rsplit("/", 1)[-1], DAILY_ROOM_URL

    print("[ASSIST] ⚠ Ni DAILY_API_KEY ni DAILY_ROOM_URL — aucun salon possible.")
    return nom, ""


def fermer_salon(room):
    """Supprime le salon Daily → tout participant encore connecté est éjecté.

    C'est ce qui rend le raccrochage SYMÉTRIQUE : sans ça, quand le visiteur
    raccroche depuis la tablette, le responsable resterait seul dans un salon
    toujours ouvert sans savoir que l'échange est terminé.

    On ne supprime que les salons créés par le robot : le salon fixe de secours
    (DAILY_ROOM_URL) est réutilisé d'un appel à l'autre, le détruire casserait
    tous les appels suivants."""
    # Le robot quitte le salon (rend le micro et le haut-parleur) — à faire même
    # si la suppression du salon échoue ensuite.
    try:
        from robot import call_audio
        call_audio.arreter()
    except Exception as e:
        print("[ASSIST] Arrêt audio robot : {}".format(e))

    if not (DAILY_API_KEY and room and room.startswith("g1-assist-")):
        return
    try:
        r = requests.delete(
            "{}/{}".format(DAILY_API, room),
            headers={"Authorization": "Bearer {}".format(DAILY_API_KEY)},
            timeout=8,
        )
        # 404 = déjà expiré (exp) ou déjà supprimé : rien d'anormal.
        if r.status_code in (200, 204, 404):
            print("[ASSIST] Salon {} fermé.".format(room))
        else:
            print("[ASSIST] Fermeture salon refusée ({}) : {}".format(
                r.status_code, r.text[:150]))
    except Exception as e:
        print("[ASSIST] Fermeture salon impossible : {}".format(e))


def _watchdog(room):
    """Lève la sourdine si l'appel n'a pas été raccroché à temps."""
    time.sleep(_CALL_MAX_SECONDS)
    try:
        # On ne coupe que si c'est TOUJOURS le même appel qui tourne : sinon un
        # appel expiré couperait la sourdine d'un appel plus récent.
        if os.path.exists(AI_PAUSED_FLAG):
            with open(AI_PAUSED_FLAG) as f:
                if f.read().strip() != room:
                    return
            os.remove(AI_PAUSED_FLAG)
            push_call_end()
            fermer_salon(room)
            print("[ASSIST] Appel expiré ({}s) — IA réactivée.".format(_CALL_MAX_SECONDS))
    except Exception as e:
        print("[ASSIST] watchdog: {}".format(e))


def _lancer_appel(motif, langue="fr"):
    """Ouvre RÉELLEMENT l'appel : salon, alerte, sourdine, écran. Appelé APRÈS
    la confirmation du visiteur (bouton "Oui, appeler" de la tablette, via
    POST /call/start) — jamais directement par le tool vocal."""
    room, url = _creer_salon()

    # 1) Alerte du responsable via n8n (best-effort : un échec réseau ne doit pas
    #    empêcher la mise en relation — le salon existe de toute façon).
    alerte_ok = False
    try:
        r = requests.post(
            N8N_ASSIST_WEBHOOK_URL,
            json={
                "motif": motif,
                "langue": langue,
                "salon": room,
                "url": url,
                "horodatage": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "robot-G1",
            },
            timeout=6,
        )
        alerte_ok = (r.status_code == 200)
    except Exception as e:
        print("[ASSIST] Webhook n8n injoignable : {}".format(e))

    # 2) SOURDINE de l'IA (verrou fichier lu par agent/events.py).
    try:
        with open(AI_PAUSED_FLAG, "w") as f:
            f.write(room)
    except Exception as e:
        print("[ASSIST] Impossible de poser le verrou : {}".format(e))

    # 3) Le robot rejoint lui-même le salon (micro + HP du robot). En cas
    #    d'échec on ne laisse PAS le visiteur sans voix : on repasse
    #    automatiquement l'audio à la tablette pour cet appel.
    audio = "tablette"
    if ASSIST_AUDIO == "robot" and url:
        try:
            from robot import call_audio
            if call_audio.demarrer(url):
                audio = "robot"
            else:
                print("[ASSIST] Audio robot indisponible → repli sur la tablette.")
        except Exception as e:
            print("[ASSIST] Audio robot en erreur ({}) → repli tablette.".format(e))

    # 4) La tablette bascule en écran d'appel. `audio` lui dit si elle doit
    #    rejoindre le salon elle-même ou seulement afficher l'écran.
    push_call({"room": room, "url": url, "motif": motif, "audio": audio})

    # 5) Filet de sécurité anti-blocage.
    threading.Thread(target=_watchdog, args=(room,), daemon=True).start()

    # Note : la statistique de l'appel (durée + answered) est enregistrée au
    # RACCROCHAGE, dans tablet_server.server.push_call_end() — c'est là qu'on
    # connaît la durée réelle. Ne pas logguer ici (on n'aurait pas la durée).

    print("=" * 70)
    print("[ASSIST] APPEL OUVERT — motif={!r} | langue={} | audio={} | alerte n8n: {}".format(
        motif, langue, audio, "OK" if alerte_ok else "ÉCHEC"))
    print("[ASSIST] 👉 REJOINDRE L'APPEL (ouvre ce lien pour parler au visiteur) :")
    print("[ASSIST]    {}".format(url or "(AUCUN SALON — configure DAILY_API_KEY)"))
    print("=" * 70)

    # Message renvoyé au LLM. Il ne parlera plus ensuite (sourdine), donc cette
    # phrase sert surtout de trace ; l'écran prend le relais.
    return ("Mise en relation lancée avec un responsable ({}). "
            "L'écran affiche l'appel en cours ; tu es en sourdine jusqu'au "
            "raccrochage.".format("alerte envoyée" if alerte_ok else "alerte non confirmée"))


def appeler_assistance(motif, langue="fr"):
    """Handler du TOOL vocal. Ne lance PLUS l'appel directement : il affiche
    d'abord l'écran d'avertissement sur la tablette (à quoi sert l'assistance,
    à n'utiliser qu'en cas de besoin). L'appel ne part que si le visiteur touche
    "Oui, appeler" — exactement comme le bouton 🆘. Le robot reste donc à
    l'écoute (pas de sourdine) tant que rien n'est confirmé."""
    push_assist_confirm({"motif": motif, "langue": langue})
    print("[ASSIST] Demande vocale — écran de confirmation affiché (motif={!r}).".format(motif))
    return ("Écran de confirmation affiché. Dis au visiteur de toucher "
            "« Oui, appeler » sur l'écran pour que tu contactes un responsable. "
            "N'appelle pas ce tool à nouveau ; attends sa confirmation.")


def _handler(**kwargs):
    return appeler_assistance(**kwargs)


register(
    {
        "name": "appeler_assistance",
        "description": (
            "Met le visiteur en relation VOCALE avec un responsable humain de "
            "l'aéroport. Appelle ce tool dès qu'il demande de l'aide humaine ou une "
            "assistance que tu ne peux pas fournir toi-même : fauteuil roulant, "
            "mobilité réduite, parler à un agent/humain/responsable, problème "
            "médical, bagage perdu, situation urgente. Avant d'appeler, préviens "
            "brièvement le visiteur que tu contactes un responsable. Après l'appel "
            "du tool tu passes en SOURDINE : l'humain prend le relais."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "motif": {
                    "type": "string",
                    "description": "Motif court et clair de la demande, pour le responsable "
                                   "(ex: 'fauteuil roulant porte K30', 'veut parler à un agent').",
                },
                "langue": {
                    "type": "string",
                    "description": "Langue du visiteur (fr, en, es, de, it, ar, zh, ja) — "
                                   "pour que le responsable sache dans quelle langue répondre.",
                },
            },
            "required": ["motif"],
        },
    },
    _handler,
)
