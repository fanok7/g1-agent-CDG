"""
tablet_server/server.py — "Tablette virtuelle" du robot G1 (production).

Adapté depuis g1_virtual_tablet (banc de test) pour tourner en Python 3.8
(SDK Unitree oblige) — annotations de type via `typing` au lieu de `list[...]`
/ `X | None` (syntaxe Python 3.9+ uniquement).

Sert une page HTML unique qui se met à jour en temps réel via Server-Sent
Events (SSE), sans jamais recharger la page. Le backend pousse trois choses
indépendamment, appelées depuis agent/events.py au fil de la vraie
conversation (micro + haut-parleur du robot, API Realtime OpenAI) :
  - push_display() : le contenu de l'écran (texte/qr/plan/idle), déclenché
    par les tools tablette (tools/tablet_tools.py).
  - push_status()  : l'indicateur d'état du robot (écoute/réfléchit/parle).
  - push_chat()    : bulle de conversation (sous-titre utilisateur / réponse
    du robot), pour l'affichage façon chat en plus de la voix.
  - push_choices() : boutons tactiles (alternative à une réponse vocale).

Le serveur tourne dans un thread séparé (uvicorn) démarré par main.py — la
communication avec le reste de l'agent (boucle asyncio de events.py) se fait
via loop.call_soon_threadsafe.
"""

import asyncio
import json
import os
import queue as _queue_mod
import secrets
import threading
import time as _time_module
from datetime import datetime
from typing import List, Optional

import choices
import ipc

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Avis (notation de la dernière réponse) ──────────────────────────────────
# L'avis reste 100 % LOCAL, et passe par un SEUL chemin : analytics.log_rating,
# qui alimente le dashboard (note moyenne, satisfaction par zone). Il a existé
# en plus un statistiques_robot.csv écrit en parallèle : personne ne le lisait
# (le dashboard ne lit que le .jsonl), il a été retiré.

app = FastAPI(title="G1 Tablette")
app.mount("/static", StaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))

# ── État courant + abonnés SSE ──────────────────────────────────────────────
_current_state = {"type": "idle"}
_current_status = "ecoute"
_current_lang = "fr"   # langue de l'interface (pilotée par l'agent)
_current_call = None   # appel d'assistance en cours : {room, url, motif} ou None
_pending_confirm = None  # avertissement d'assistance vocal en attente : {motif, langue} ou None
AI_PAUSED_FLAG = ipc.AI_PAUSED_FLAG   # verrou : l'IA n'écoute ni ne parle tant qu'il existe

# ── Authentification des actions sensibles ──────────────────────────────────
# Ce serveur écoute sur 0.0.0.0 (accès wifi voulu depuis un laptop de debug,
# cf. main.py::_start_tablet_server) — sans ça, n'importe qui sur le même
# réseau pourrait injecter des messages dans la conversation (/respond) ou
# déclencher un vrai appel d'assistance (/call/start) via une simple requête.
# La page tablette elle-même reçoit le token au chargement (voir index()) et
# le renvoie sur ses propres appels — un visiteur normal ne voit jamais rien
# de différent. Persistant si G1_ADMIN_TOKEN est dans .env, sinon régénéré à
# chaque démarrage (affiché en log).
ADMIN_TOKEN = os.environ.get("G1_ADMIN_TOKEN") or secrets.token_urlsafe(16)
if not os.environ.get("G1_ADMIN_TOKEN"):
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print("[SERVER] G1_ADMIN_TOKEN non défini — token généré pour cette session : {}".format(ADMIN_TOKEN))


def _authorized(request: Request) -> bool:
    tok = request.headers.get("x-admin-token") or request.query_params.get("token")
    return tok == ADMIN_TOKEN


def _unauthorized():
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


# Écran de Samy (ESP32, voir Projet/écran interactif) — domaine de confiance
# séparé de ADMIN_TOKEN : ce token n'autorise que /api/samy-ready, rien
# d'autre sur ce serveur.
SAMY_SCREEN_TOKEN = os.environ.get("SAMY_SCREEN_TOKEN", "")


def _samy_authorized(request: Request) -> bool:
    tok = request.headers.get("x-auth-token") or request.query_params.get("token")
    return bool(SAMY_SCREEN_TOKEN) and tok == SAMY_SCREEN_TOKEN


# "Un visiteur attend Samy" : mis à vrai par tools/visitor_notify_tool.py
# juste après avoir prévenu Samy (le robot vient de dire "asseyez-vous, Samy
# va venir vous chercher"), effacé au prochain /reset (visiteur reparti) ou
# une fois l'annonce faite. Permet à /api/samy-ready de savoir s'il y a
# vraiment quelqu'un à qui parler quand Samy appuie sur "Candidat Suivant".
_visitor_waiting = False


def mark_visitor_waiting():
    global _visitor_waiting
    _visitor_waiting = True


def clear_visitor_waiting():
    global _visitor_waiting
    _visitor_waiting = False


_current_choices = []  # type: List[str]
_chat_history = []     # type: List[dict]   [{"role": "user"|"assistant", "text": "..."}, ...]
_CHAT_HISTORY_MAX = 50
_subscribers = []       # type: List[asyncio.Queue]
_loop = None            # type: Optional[asyncio.AbstractEventLoop]

# File d'attente locale conservée pour compat (ancien nom importé ailleurs) —
# la vraie destination des réponses tactiles est agent.text_input.input_queue
# (celle que agent.events.text_input_loop consomme réellement pour injecter
# le texte dans la conversation Realtime, exactement comme le clavier).
input_queue = _queue_mod.Queue()

try:
    from agent.text_input import input_queue as _agent_input_queue
except Exception:
    _agent_input_queue = None


@app.on_event("startup")
async def _on_startup():
    global _loop
    _loop = asyncio.get_running_loop()
    # Démarre la télémétrie : thread d'écriture asynchrone + event health (15 min).
    try:
        import analytics
        # 'usb_connected' de l'event health = la tablette a-t-elle un flux SSE
        # ouvert (proxy fiable du tunnel USB adb reverse).
        analytics.logger.set_usb_check(lambda: len(_subscribers) > 0)
        analytics.start()
    except Exception as e:
        print("[SERVER] télémétrie non démarrée : {}".format(e))


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {
            "initial_state": json.dumps(_current_state),
            "initial_status": json.dumps(_current_status),
            "initial_choices": json.dumps(_current_choices),
            "initial_chat": json.dumps(_chat_history),
            # Table de traduction des boutons réflexes — source unique
            # dans choices.py, partagée avec le prompt et le tool.
            "choice_labels": json.dumps(choices.TRANSLATIONS, ensure_ascii=False),
            "admin_token": json.dumps(ADMIN_TOKEN),
        },
    )


@app.get("/ping")
async def ping():
    """Sonde de disponibilité, appelée en boucle par la tablette. Si main.py
    s'arrête, ce ping échoue (connexion refusée) → la tablette recharge et
    affiche « page introuvable » au lieu d'une interface figée."""
    return {"ok": True}


@app.get("/events")
async def events():
    """Flux SSE : chaque nouvel état (affichage, statut, choix, chat) est
    envoyé immédiatement à tous les clients connectés. Chaque message est une
    enveloppe {"display": {...}} / {"status": "..."} / {"choices": [...]} /
    {"chat": {...}} — le JS distingue selon la clé présente."""
    queue = asyncio.Queue()  # type: asyncio.Queue
    _subscribers.append(queue)

    async def stream():
        try:
            yield f"data: {json.dumps({'display': _current_state})}\n\n"
            yield f"data: {json.dumps({'status': _current_status})}\n\n"
            yield f"data: {json.dumps({'lang': _current_lang})}\n\n"
            yield f"data: {json.dumps({'choices': _current_choices})}\n\n"
            if _current_call:   # appel en cours → la tablette le rejoint au (re)chargement
                yield f"data: {json.dumps({'call': _current_call})}\n\n"
                if _current_call_status:   # + son état (attente / communication établie)
                    yield f"data: {json.dumps({'call_status': _current_call_status})}\n\n"
            elif _pending_confirm:   # avertissement vocal en attente → réaffiché si la page recharge
                yield f"data: {json.dumps({'assist_confirm': _pending_confirm})}\n\n"
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        finally:
            _subscribers.remove(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/respond")
async def respond(request: Request):
    """Réponse tactile (bouton cliqué sur la tablette) — injectée dans la
    conversation Realtime exactement comme un message clavier/micro, via
    agent.text_input.input_queue (consommée par agent.events.text_input_loop)."""
    if not _authorized(request):
        return _unauthorized()
    body = await request.json()
    text = (body.get("text") or "").strip()
    if text:
        input_queue.put(text)
        if _agent_input_queue is not None:
            _agent_input_queue.put(text)
    return {"ok": True}


@app.post("/api/samy-ready")
async def samy_ready(request: Request):
    """Samy vient d'appuyer sur « Candidat Suivant » sur son écran. S'il y a
    un visiteur qui attend encore (voir _visitor_waiting), annonce-le dans la
    conversation en cours — même mécanisme que /respond (injection dans
    agent.text_input.input_queue), mais comme une instruction système plutôt
    qu'une réponse du visiteur, pour que le modèle l'annonce directement sans
    y répondre comme à une question."""
    if not _samy_authorized(request):
        return _unauthorized()

    global _visitor_waiting
    if not _visitor_waiting:
        return {"ok": True, "annonce": False}

    _visitor_waiting = False
    texte = "[SYSTÈME] Samy est disponible. Informe le visiteur présent qu'il peut y aller maintenant, en une phrase."
    input_queue.put(texte)
    if _agent_input_queue is not None:
        _agent_input_queue.put(texte)
    return {"ok": True, "annonce": True}


@app.post("/call/start")
async def call_start(request: Request):
    """Lance RÉELLEMENT l'appel — appelé quand le visiteur touche « Oui, appeler »
    sur l'écran d'avertissement, que celui-ci vienne du bouton 🆘 (tactile) ou de
    la demande vocale (le tool a d'abord affiché la confirmation). C'est le SEUL
    point qui déclenche la mise en relation ; la confirmation le précède toujours."""
    if not _authorized(request):
        return _unauthorized()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    motif = (body.get("motif") or "Demande d'assistance (bouton tablette)")
    langue = body.get("langue") or _current_lang

    global _pending_confirm
    _pending_confirm = None   # confirmé → plus d'avertissement en attente

    # Import tardif : évite l'import circulaire (assistance_tool importe ce module).
    from tools.assistance_tool import _lancer_appel
    # En thread : l'appel HTTP vers n8n ne doit pas bloquer la réponse au navigateur.
    threading.Thread(target=_lancer_appel,
                     kwargs={"motif": motif, "langue": langue}, daemon=True).start()
    return {"ok": True}


@app.post("/call/end")
async def call_end(request: Request):
    """Raccrochage : appelé par la tablette quand l'appel d'assistance se termine
    (bouton "Raccrocher" ou départ du salon). Retire le verrou → l'IA se remet à
    écouter et parler, et ferme le salon → le responsable est éjecté lui aussi."""
    if not _authorized(request):
        return _unauthorized()
    salon = (_current_call or {}).get("room")
    try:
        os.remove(AI_PAUSED_FLAG)
    except FileNotFoundError:
        pass
    push_call_end()

    if salon:
        # En thread : l'appel HTTP vers Daily ne doit pas retarder la réponse à la
        # tablette (le visiteur doit revoir le menu immédiatement).
        from tools.assistance_tool import fermer_salon   # import tardif (circulaire)
        threading.Thread(target=fermer_salon, args=(salon,), daemon=True).start()
    return {"ok": True}


@app.post("/clientlog")
async def clientlog(request: Request):
    """Journal du navigateur de la tablette → fichier lisible côté robot.
    Sans ça, une erreur JS (permission micro refusée, Jitsi injoignable…) reste
    invisible : la tablette est en kiosque, sans console accessible."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    ligne = "{} {}\n".format(datetime.now().strftime("%H:%M:%S"), body.get("msg", ""))
    try:
        with open(ipc.TABLET_CLIENT_LOG, "a", encoding="utf-8") as f:
            f.write(ligne)
    except Exception:
        pass
    print("[TABLETTE] {}".format(ligne.rstrip()))
    return {"ok": True}


@app.post("/assist/cancel")
async def assist_cancel():
    """Le visiteur a touché « Annuler » sur l'avertissement (ou l'a fermé) : on
    oublie la demande vocale en attente pour qu'elle ne réapparaisse pas si la
    tablette recharge."""
    global _pending_confirm
    _pending_confirm = None
    return {"ok": True}


@app.get("/api/rdv-slots")
async def rdv_slots(date: str):
    """Créneaux libres pour UN jour donné — appelé directement par les flèches
    du calendrier tactile (afficher_calendrier_rdv), SANS repasser par le LLM :
    la navigation jour par jour doit être instantanée, pas au prix d'un aller-
    retour Realtime API. Import tardif (comme /call/start) pour éviter tout
    souci d'ordre d'import avec le paquet tools au démarrage de main.py."""
    from fastapi.responses import JSONResponse
    from datetime import datetime as _dt
    from tools.calendar_tool import _get_service, _free_slots, _SAMY_HOURS, _PARIS, _JOURS, _MOIS

    try:
        ref = _PARIS.localize(_dt.strptime(date, "%Y-%m-%d"))
    except ValueError:
        return JSONResponse({"error": "date invalide"}, status_code=400)

    label = "{} {} {}".format(_JOURS[ref.weekday()], ref.day, _MOIS[ref.month - 1])

    if ref.weekday() not in _SAMY_HOURS:
        return {"date_iso": date, "date_label": label, "slots": [], "closed": True}

    try:
        service = _get_service()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    slots = _free_slots(service, date, n=32)
    return {"date_iso": date, "date_label": label, "slots": slots, "closed": False}


@app.post("/reset")
async def reset(request: Request):
    """Nouvelle session : efface l'historique de conversation et remet l'écran
    à zéro. Appelée par la tablette après une longue inactivité (le visiteur est
    parti) pour ne pas exposer la session précédente au visiteur suivant."""
    if not _authorized(request):
        return _unauthorized()
    global _current_state, _current_choices, _current_lang, _pending_confirm
    clear_visitor_waiting()  # visiteur reparti : plus personne à qui annoncer Samy
    if _chat_history:
        try:
            import analytics
            analytics.log_transcript("session_end", list(_chat_history))
        except Exception:
            pass
    _current_state = {"type": "idle"}
    _current_choices = []
    _current_lang = "fr"
    _pending_confirm = None   # une demande vocale non confirmée ne survit pas à la session
    del _chat_history[:]   # vide la liste en place (référence conservée par push_chat)
    return {"ok": True}


@app.post("/api/rating")
async def rating(request: Request):
    """Notation (1-5 étoiles) de la dernière réponse du robot par le visiteur.
    Enregistrée en local dans la télémétrie anonyme (analytics), lue par le
    dashboard Streamlit. Best-effort : un échec ne bloque jamais la tablette."""
    body = await request.json()
    note = body.get("note")
    question = body.get("question", "")

    # Alimente le dashboard : note + zone (déduite de la question notée), anonyme.
    try:
        import analytics
        analytics.log_rating(note, question)
        # Note basse : archive le texte intégral de la conversation pour relecture
        # (optimiser prompt/tools) — le fichier anonyme, lui, ne garde que la note.
        try:
            if int(note) <= 2:
                analytics.log_transcript("low_rating", list(_chat_history),
                                          {"note": note, "question": question})
        except (TypeError, ValueError):
            pass
    except Exception:
        pass
    return {"status": "success"}


@app.get("/api/export-logs")
async def export_logs(request: Request):
    """Télécharge le journal brut analytics_events.jsonl (télémétrie anonyme).
    Alimente le dashboard Streamlit (dashboard_stats.py) — voir DASHBOARD.md."""
    if not _authorized(request):
        return _unauthorized()
    import analytics
    from fastapi.responses import FileResponse
    if not os.path.exists(analytics.LOG_PATH):
        return JSONResponse({"erreur": "aucune donnée"}, status_code=404)
    horod = datetime.now().strftime("%Y%m%d_%H%M")
    return FileResponse(
        analytics.LOG_PATH, media_type="application/x-ndjson",
        filename="analytics_events_{}.jsonl".format(horod))


def _broadcast(envelope):
    data = json.dumps(envelope)
    if _loop is None:
        return
    for queue in list(_subscribers):
        _loop.call_soon_threadsafe(queue.put_nowait, data)


def push_display(payload):
    """Appelée par les tools tablette, potentiellement depuis un thread
    différent de celui du serveur uvicorn."""
    global _current_state
    _current_state = payload
    _broadcast({"display": payload})


def push_status(status):
    """Met à jour l'indicateur d'état du robot : 'ecoute', 'reflechit' ou
    'parle'. Appelée par agent/events.py au fil des events Realtime API."""
    global _current_status
    _current_status = status
    _broadcast({"status": status})


def push_choices(options):
    """Affiche des boutons tactiles sur la tablette, un par option — appelée
    par le tool proposer_choix() quand le LLM pose une question fermée."""
    global _current_choices
    _current_choices = list(options)
    _broadcast({"choices": _current_choices})


def push_lang(code):
    """Change la langue de l'interface tablette (ex: 'fr', 'en', 'es'). Appelée
    par le tool definir_langue_ecran quand l'agent détecte la langue du visiteur.
    Côté tablette, un retour au 'fr' est ignoré si l'interface est déjà dans une
    autre langue (protège de la dérive du modèle)."""
    global _current_lang
    _current_lang = code
    _broadcast({"lang": code})


def push_assist_confirm(info):
    """Demande à la tablette d'afficher l'écran d'AVERTISSEMENT avant un appel
    d'assistance déclenché À LA VOIX. `info` = {motif, langue}. L'appel réel n'est
    lancé que si le visiteur touche « Oui, appeler » (→ POST /call/start).
    Mémorisé (_pending_confirm) pour être réaffiché si la tablette recharge."""
    global _pending_confirm
    _pending_confirm = info
    # Trace de diagnostic (lisible depuis /tmp) : confirme que le tool a bien été
    # appelé ET combien de tablettes sont connectées au moment de l'envoi.
    try:
        with open(ipc.ASSIST_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("{} push_assist_confirm motif={!r} abonnes_SSE={} loop={}\n".format(
                datetime.now().strftime("%H:%M:%S"), info.get("motif"),
                len(_subscribers), _loop is not None))
    except Exception:
        pass
    _broadcast({"assist_confirm": info})


_call_started_at = None   # horodatage du début de l'appel courant (durée télémétrie)
_current_call_status = None  # 'waiting' | 'connected' | None — état affiché sur la tablette


def push_call(info):
    """Bascule la tablette en mode APPEL : elle affiche l'écran "Appel en cours"
    et rejoint le salon Jitsi en audio. `info` = {room, url, motif}."""
    global _current_call, _call_started_at, _current_call_status
    _current_call = info
    _call_started_at = _time_module.time()
    _current_call_status = "waiting"   # au départ : en attente du responsable
    _broadcast({"call": info})


def push_call_status(status):
    """Met à jour l'état de l'appel affiché : 'waiting' (personne n'a décroché) ou
    'connected' (le responsable a rejoint). Appelée par robot/call_audio.py quand
    un participant arrive dans le salon."""
    global _current_call_status
    _current_call_status = status
    _broadcast({"call_status": status})


def push_call_end():
    """Fin d'appel : la tablette quitte le salon et revient à l'écran normal.
    Enregistre l'appel dans la télémétrie (durée + 'answered' heuristique)."""
    global _current_call, _call_started_at
    info = _current_call or {}
    if _call_started_at is not None:
        duree = _time_module.time() - _call_started_at
        try:
            import analytics
            # answered : un échange d'au moins ~5 s → un humain a réellement répondu
            # (en-dessous : décroché immédiat / personne). Heuristique honnête.
            analytics.log_call(info.get("motif", ""),
                               answered=bool(duree >= 5), duration_s=duree)
            # Appel d'assistance = le robot n'a pas suffi seul : archive le texte
            # intégral pour comprendre pourquoi (prompt/tool manquant, incompréhension…).
            analytics.log_transcript("assistance_call", list(_chat_history),
                                      {"motif": info.get("motif", ""), "duration_s": duree})
        except Exception:
            pass
    global _current_call_status
    _call_started_at = None
    _current_call = None
    _current_call_status = None
    _broadcast({"call_end": True})


def push_lang_user(code):
    """Langue détectée DANS LA VOIX du visiteur (canal PRIORITAIRE). L'interface
    l'applique TOUJOURS — y compris un retour au français — car c'est la langue
    réellement parlée, pas une dérive du robot."""
    global _current_lang
    _current_lang = code
    _broadcast({"lang_user": code})


def push_chat(role, text):
    """Ajoute une bulle à la conversation affichée sur la tablette (rôle
    'user' ou 'assistant'). Appelée par agent/events.py avec exactement le
    texte transcrit du micro / le texte réellement prononcé par le robot."""
    entry = {"role": role, "text": text}
    _chat_history.append(entry)
    del _chat_history[:-_CHAT_HISTORY_MAX]
    _broadcast({"chat": entry})
