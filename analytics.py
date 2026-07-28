"""analytics.py — Télémétrie & analytique "production-ready" du robot d'accueil.

Conçu pour un backend FastAPI (Python 3.8) où les événements arrivent DEPUIS
PLUSIEURS CONTEXTES : la boucle asyncio de l'agent (agent/events.py), le thread
uvicorn (routes FastAPI) et des threads d'exécution de tools. Le logger est donc
bâti sur une file thread-safe + un thread d'écriture unique — l'appelant ne fait
qu'un `put_nowait()` instantané (écriture RÉELLEMENT asynchrone, ne bloque jamais
une requête LLM).

RGPD / anonymat :
  - aucune identité (pas de nom, pas de face-id) ;
  - les suites de chiffres des questions sont masquées (badge/téléphone → #) ;
  - un `session_id` ÉPHÉMÈRE et non-nominatif regroupe les événements d'une même
    visite ; il expire après 2 min d'inactivité (nouvelle visite = nouvel ID) ;
    on ne peut donc pas relier deux visites ni identifier une personne.

6 types d'événements STRICTS (schémas figés) :
  question : {ts, session_id, type, category, lang, q_anonymized, latency_ms, is_fallback}
  tool     : {ts, session_id, type, category, tool_name, status}
  rating   : {ts, session_id, type, category, note}
  call     : {ts, session_id, type, motif, answered, duration_s}
  alert    : {ts, session_id: null, type, kind}            (événement système)
  health   : {ts, type, cpu_percent, ram_percent, usb_connected, api_errors_count}
"""

import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime

try:
    import psutil
except Exception:                     # health dégradé si psutil absent
    psutil = None

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_BASE_DIR, "tablet_server", "analytics_events.jsonl")

# ── Catégorisation (zone/sujet) ─────────────────────────────────────────────
_TOOL_ZONE = {
    "chercher_hotel": "Hôtels",
    "get_departures": "Vols", "get_arrivals": "Vols", "get_flight_status": "Vols",
    "get_delayed_flights": "Vols", "get_live_flights_for_airport": "Vols",
    "get_airport_info": "Vols", "get_airline_info": "Vols",
    "prochains_departs_arret": "Transport", "calculer_itineraire": "Transport",
    "etat_trafic_idf": "Transport", "velib_a_proximite": "Transport",
    "itineraire_velo": "Transport",
    "search_nearby_places": "Lieux & itinéraires", "search_places_text": "Lieux & itinéraires",
    "get_place_details": "Lieux & itinéraires", "geocode_address": "Lieux & itinéraires",
    "reverse_geocode": "Lieux & itinéraires", "compute_route": "Lieux & itinéraires",
    "compute_route_matrix": "Lieux & itinéraires",
    "appeler_assistance": "Assistance humaine",
    "spotify_jouer": "Divertissement", "spotify_en_cours": "Divertissement",
    "spotify_controle": "Divertissement", "spotify_volume": "Divertissement",
    "jouer_pfc": "Divertissement", "demarrer_pfc": "Divertissement",
    "recherche_web": "Recherche web",
    "chercher_formation": "Emploi / Formation", "chercher_badge": "Emploi / Formation",
}

_KEYWORD_ZONE = [
    ("Vols", ["vol ", "vols", "porte", "embarq", "terminal", "avion", "flight", "gate",
              "boarding", "departure", "arrival"]),
    ("Hôtels", ["hotel", "hôtel", "dormir", "chambre", "nuit", "sleep", "room"]),
    ("Transport", ["métro", "metro", "rer", "bus", "train", "taxi", "navette", "tram",
                   "transport", "subway", "shuttle"]),
    ("Bagages", ["bagage", "valise", "consigne", "luggage", "baggage", "suitcase"]),
    ("Restauration", ["manger", "restaurant", "café", "cafe", "boire", "eat", "drink",
                      "coffee", "food"]),
    ("Services pratiques", ["toilette", "wc", "pharmacie", "eau", "distributeur", "atm",
                            "toilet", "restroom", "bathroom", "water"]),
    ("Assistance humaine", ["fauteuil", "aide", "handicap", "urgence", "perdu", "responsable",
                            "wheelchair", "help", "emergency", "lost", "medical"]),
    ("Lieux & itinéraires", ["où est", "ou est", "comment aller", "direction", "chemin",
                             "sortie", "where is", "how to get", "exit", "way to"]),
    ("Wi-Fi & services", ["wifi", "wi-fi", "internet", "recharge", "prise", "charge"]),
]

_DIGITS = re.compile(r"\d{2,}")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def categoriser(question):
    q = (question or "").lower()
    for zone, mots in _KEYWORD_ZONE:
        if any(m in q for m in mots):
            return zone
    return "Autre"


def anonymiser(question):
    """Question minusculisée, chiffres masqués (#), ponctuation retirée, espaces
    compactés — sert de clé de regroupement ET garantit l'absence de PII."""
    q = (question or "").lower().strip()
    q = _DIGITS.sub("#", q)
    q = _PUNCT.sub(" ", q)
    return _SPACES.sub(" ", q).strip()


# ═══════════════════════════════════════════════════════════════════════════
class AnalyticsLogger:
    """Logger de télémétrie thread-safe à écriture asynchrone."""

    def __init__(self, path=LOG_PATH, session_timeout=120, health_interval=900):
        self.path = path
        self._timeout = session_timeout          # 2 min d'inactivité → nouvelle session
        self._health_interval = health_interval  # 15 min entre deux events health
        self._q = queue.Queue()                  # file thread-safe (producteurs multiples)
        self._sess_lock = threading.Lock()
        self._session_id = None
        self._last_activity = 0.0
        self._api_errors = 0
        self._usb_check = None                   # callable injecté par le serveur
        self._started = False

    # ── cycle de vie ────────────────────────────────────────────────────────
    def start(self):
        """Démarre le thread d'écriture + le thread health (idempotent)."""
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._writer_loop, daemon=True).start()
        threading.Thread(target=self._health_loop, daemon=True).start()
        print("[ANALYTICS] Logger démarré → {}".format(self.path), flush=True)

    def set_usb_check(self, fn):
        """Le serveur fournit une fonction booléenne 'la tablette est-elle
        connectée ?' (ex: y a-t-il un abonné SSE). Utilisée par l'event health."""
        self._usb_check = fn

    def note_api_error(self):
        """À appeler sur une erreur de l'API LLM (compteur de l'event health)."""
        self._api_errors += 1

    # ── session éphémère ────────────────────────────────────────────────────
    def _session(self):
        """Renvoie l'ID de session courant, en le régénérant après 2 min sans
        interaction. Non-nominatif : 'sess_' + 5 hex aléatoires."""
        with self._sess_lock:
            now = time.time()
            if self._session_id is None or (now - self._last_activity) > self._timeout:
                self._session_id = "sess_" + uuid.uuid4().hex[:5]
            self._last_activity = now
            return self._session_id

    # ── écriture asynchrone ─────────────────────────────────────────────────
    def _emit(self, evt):
        """Non bloquant : dépose l'événement dans la file (le thread écrit)."""
        try:
            self._q.put_nowait(evt)
        except Exception:
            pass

    def _writer_loop(self):
        while True:
            evt = self._q.get()
            if evt is None:
                break
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            except Exception as e:
                print("[ANALYTICS] écriture impossible : {}".format(e), flush=True)

    def _health_loop(self):
        while True:
            time.sleep(self._health_interval)
            self._emit_health()

    def _emit_health(self):
        cpu = ram = None
        if psutil is not None:
            try:
                cpu = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory().percent
            except Exception:
                pass
        usb = None
        if self._usb_check is not None:
            try:
                usb = bool(self._usb_check())
            except Exception:
                usb = None
        self._emit({
            "ts": _now(), "type": "health",
            "cpu_percent": cpu, "ram_percent": ram,
            "usb_connected": usb, "api_errors_count": self._api_errors,
        })

    # ── API publique : les 6 événements ────────────────────────────────────
    def log_question(self, texte, lang="fr", latency_ms=None, is_fallback=False):
        q = anonymiser(texte)
        if len(q) < 3:                 # « oui », « ok »… : pas une vraie question
            return
        self._emit({
            "ts": _now(), "session_id": self._session(), "type": "question",
            "category": categoriser(texte), "lang": lang or "fr",
            "q_anonymized": q[:120],
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
            "is_fallback": bool(is_fallback),
        })

    def log_tool(self, tool_name, status="ok"):
        cat = _TOOL_ZONE.get(tool_name)
        if not cat:                    # tools internes (vision, geste, date…) : ignorés
            return
        self._emit({
            "ts": _now(), "session_id": self._session(), "type": "tool",
            "category": cat, "tool_name": tool_name, "status": status,
        })

    def log_rating(self, note, question=""):
        try:
            n = int(note)
        except (TypeError, ValueError):
            return
        if not 1 <= n <= 5:
            return
        self._emit({
            "ts": _now(), "session_id": self._session(), "type": "rating",
            "category": categoriser(question), "note": n,
        })

    def log_call(self, motif="", answered=None, duration_s=None):
        self._emit({
            "ts": _now(), "session_id": self._session(), "type": "call",
            "motif": (motif or "")[:120], "answered": answered,
            "duration_s": round(duration_s, 1) if duration_s is not None else None,
        })

    def log_alert(self, kind):
        """Alerte système (chute/feu/fumée) : session_id null — non liée à une
        visite (détectée par la vision, indépendamment de toute interaction)."""
        k = {"chute": "Chute", "feu": "Feu", "fumee": "Fumée",
             "fumée": "Fumée"}.get((kind or "").lower(), kind)
        self._emit({
            "ts": _now(), "session_id": None, "type": "alert", "kind": k,
        })


def _now():
    # Précision seconde : suffisante pour ordonner la télémétrie, sans permettre
    # un pistage fin (et pas de PII).
    return datetime.now().isoformat(timespec="seconds")


# Singleton partagé par tout le backend.
logger = AnalyticsLogger()

# Raccourcis module-level (compat avec les points d'appel existants).
start = logger.start
log_question = logger.log_question
log_tool = logger.log_tool
log_rating = logger.log_rating
log_call = logger.log_call
log_alert = logger.log_alert
note_api_error = logger.note_api_error

# L'agrégation/visualisation est désormais 100 % côté dashboard Streamlit
# (dashboard_stats.py, voir DASHBOARD.md). Ce module ne fait plus QUE collecter.
