#!/usr/bin/env python3
"""
samy_agenda_server.py — Service autonome pour l'écran ESP32 de Samy.

Lit directement Google Calendar via tools/calendar_tool.py (token déjà
autorisé : ~/.google_calendar_token.json). Volontairement INDÉPENDANT de
main.py : pas d'import de config.py, de robot.hardware ni du reste de
l'agent vocal — juste le calendrier, pour tourner en permanence (systemd)
sans dépendre du lancement manuel de l'agent.

Deux rôles :
  - PUSH (thread de fond) : POST périodique vers AGENDA_PUSH_URL (l'écran),
    même mécanisme que tools/visitor_notify_tool.py pour les notifications —
    contourne le problème réseau robot<->écran en passant par la même règle
    de serveur virtuel du routeur que /notify.
  - GET /api/samy-agenda : conservé pour du débogage local sur le robot
    (curl http://127.0.0.1:8090/...), mais l'écran ne l'appelle plus.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

# Ce script vit dans g1_agent_interim/Ecran_de_samy/ — remonter d'un niveau
# pour retrouver le paquet tools/ et le .env de g1_agent_interim.
_G1_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _G1_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_G1_ROOT, ".env"))

from tools.calendar_tool import _get_service, _fetch_range, _extract_name, _parse_description, _PARIS

PORT = int(os.environ.get("SAMY_AGENDA_PORT", "8090"))
TOKEN = os.environ.get("SAMY_SCREEN_TOKEN", "")
AGENDA_PUSH_URL = os.environ.get("AGENDA_PUSH_URL", "")
PUSH_INTERVAL_S = 3 * 60

# Sondage rapide du bouton de l'écran (GET /status) — dérivé de AGENDA_PUSH_URL
# (même hôte:port, qui passe déjà par la règle de serveur virtuel du routeur),
# juste un chemin différent. Rapide (pas 3 min comme l'agenda) : Samy appuie
# sur "Candidat Suivant" pour dire au visiteur présent d'y aller MAINTENANT,
# pas dans 3 minutes.
STATUS_POLL_URL = AGENDA_PUSH_URL.replace("/agenda", "/status") if AGENDA_PUSH_URL else ""
STATUS_POLL_INTERVAL_S = 2
TABLET_SERVER_READY_URL = "http://127.0.0.1:8000/api/samy-ready"

# Disponibilité de Samy, pilotée par les boutons "Candidat Suivant" /
# "Indisponible" de l'écran (POST), lue par un futur tool vocal du robot
# (GET) pour savoir s'il peut annoncer "Samy va vous recevoir". En mémoire
# seulement : repart à "disponible" si le service redémarre, par choix —
# pas d'état "indisponible" silencieux qui traîne après un redémarrage.
_status_lock = threading.Lock()
samy_available = True


def build_agenda(max_results=5):
    service = _get_service()
    now_utc = datetime.now(timezone.utc)
    items = _fetch_range(service, now_utc, now_utc + timedelta(days=14), max_results=max_results)

    events = []
    for ev in items:
        start = ev.get("start", {})
        end = ev.get("end", {})
        info = _parse_description(ev.get("description", ""))
        nom = info.get("nom") or _extract_name(ev.get("summary", "Sans titre"))

        if "dateTime" in start:
            dt_s = datetime.fromisoformat(start["dateTime"]).astimezone(_PARIS)
            dt_e = datetime.fromisoformat(end["dateTime"]).astimezone(_PARIS)
            events.append({
                "jour": dt_s.strftime("%d/%m"),
                "heure_debut": dt_s.strftime("%Hh%M"),
                "heure_fin": dt_e.strftime("%Hh%M"),
                "nom": nom,
                "poste": info.get("poste", ""),
            })
        else:
            d = datetime.strptime(start.get("date", ""), "%Y-%m-%d")
            events.append({
                "jour": d.strftime("%d/%m"), "heure_debut": "Journee", "heure_fin": "",
                "nom": nom, "poste": info.get("poste", ""),
            })
    return events


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # le journal systemd garde déjà stdout/stderr si besoin de débugger

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if not TOKEN or self.headers.get("X-Auth-Token", "") != TOKEN:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/api/samy-agenda":
            try:
                self._send_json(200, {"ok": True, "events": build_agenda()})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/samy-status":
            with _status_lock:
                self._send_json(200, {"ok": True, "disponible": samy_available})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if not TOKEN or self.headers.get("X-Auth-Token", "") != TOKEN:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path != "/api/samy-status":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send_json(400, {"ok": False, "error": "json invalide"})
            return

        if "disponible" not in body:
            self._send_json(400, {"ok": False, "error": "champ 'disponible' requis"})
            return

        global samy_available
        with _status_lock:
            samy_available = bool(body["disponible"])
        print(f"[samy_agenda_server] disponibilite -> {samy_available}", flush=True)
        self._send_json(200, {"ok": True, "disponible": samy_available})


def push_loop():
    if not AGENDA_PUSH_URL:
        print("[samy_agenda_server] AGENDA_PUSH_URL absent du .env — push désactivé", flush=True)
        return
    while True:
        try:
            events = build_agenda()
            requests.post(
                AGENDA_PUSH_URL,
                headers={"X-Auth-Token": TOKEN},
                json={"events": events},
                timeout=5,
            )
        except Exception as e:
            print(f"[samy_agenda_server] échec push agenda : {e}", flush=True)
        time.sleep(PUSH_INTERVAL_S)


def poll_status_loop():
    """Interroge l'écran régulièrement pour savoir si Samy vient d'appuyer sur
    "Candidat Suivant". Sur un passage False -> True (front montant), relaie
    localement vers tablet_server.py (même machine, pas de réseau) pour que
    le robot l'annonce s'il y a un visiteur qui attend — voir /api/samy-ready
    dans tablet_server/server.py. Ne fait rien si main.py ne tourne pas (la
    requête locale échoue simplement, best-effort)."""
    if not STATUS_POLL_URL:
        print("[samy_agenda_server] STATUS_POLL_URL indisponible — sondage bouton désactivé", flush=True)
        return

    global samy_available
    last_known = None
    while True:
        try:
            r = requests.get(STATUS_POLL_URL, headers={"X-Auth-Token": TOKEN}, timeout=3)
            if r.ok:
                disponible = bool(r.json().get("disponible"))
                with _status_lock:
                    samy_available = disponible
                if disponible and last_known is False:
                    try:
                        resp = requests.post(
                            TABLET_SERVER_READY_URL,
                            headers={"X-Auth-Token": TOKEN},
                            timeout=3,
                        )
                        print(f"[samy_agenda_server] Candidat Suivant -> relai local : {resp.status_code} {resp.text}", flush=True)
                    except Exception as e:
                        print(f"[samy_agenda_server] relai local impossible (main.py eteint ?) : {e}", flush=True)
                last_known = disponible
        except Exception:
            pass  # best-effort : l'ecran peut etre temporairement injoignable
        time.sleep(STATUS_POLL_INTERVAL_S)


if __name__ == "__main__":
    threading.Thread(target=push_loop, daemon=True).start()
    threading.Thread(target=poll_status_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[samy_agenda_server] listening on :{PORT}", flush=True)
    server.serve_forever()
