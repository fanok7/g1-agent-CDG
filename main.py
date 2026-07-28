"""
G1 Agent Interim — point d'entrée.

python3.8 main.py
"""

import sys
sys.path.insert(0, '/home/unitree/g1_agent_interim')

import asyncio
import os
import threading
import subprocess
import time
import robot.hardware as hardware
import robot.spotify_player as spotify_player
from robot import hand_idle
from robot.gestures import execute_gesture

# Chargement des tools — l'import suffit à les enregistrer dans le registry
import tools.web_search       # noqa: F401
import tools.database         # noqa: F401
import tools.gesture_tool     # noqa: F401
import tools.gmail            # noqa: F401
import tools.airlabs_tools    # noqa: F401
import tools.transport_tools  # noqa: F401
import tools.googlemaps_tools # noqa: F401
import tools.face_id_tool     # noqa: F401
import tools.shake_hand_tool
import tools.vision_tool      # noqa: F401
import tools.rps_tool         # noqa: F401
import tools.spotify_tool     # noqa: F401
import tools.datetime_tool    # noqa: F401
import tools.screenshot_tool  # noqa: F401
import tools.calendar_tool    # noqa: F401
import tools.qr_tool          # noqa: F401
import tools.hotel_tools      # noqa: F401 — chercher_hotel (géocodage OSM + webhook n8n)
import tools.tablet_tools     # noqa: F401 — proposer_choix / afficher_texte_ecran / afficher_qr_ecran / afficher_plan_ecran
import tools.assistance_tool  # noqa: F401 — appeler_assistance (alerte n8n + salon Jitsi + sourdine IA)
from tools.screenshot_tool import SCREENSHOT_DIR
from agent.text_input import input_queue as _text_input_queue

import uvicorn
from tablet_server.server import app as tablet_app

from agent.parler_client import send_emotion
from agent.session import connect
from agent.events import (send_audio_loop, receive_events_loop, face_greeting_loop,
                          rps_result_loop, qr_alert_loop, text_input_loop)



MINICONDA_PYTHON    = "/home/unitree/miniconda3/bin/python3"
PYTHON38            = "/usr/bin/python3.8"
FACE_ID_SCRIPT      = "/home/unitree/g1_agent_interim/vision/face_id/face_id.py"
VISION_SRV_SCRIPT   = "/home/unitree/g1_agent_interim/vision/vision_server.py"
GESTURE_POSE_SCRIPT = "/home/unitree/g1_agent_interim/vision/gesture_pose.py"
VISION_FALL_SCRIPT  = "/home/unitree/g1_agent_interim/vision/fall_detection/main.py"
VISION_FALL_CONFIG  = "/home/unitree/g1_agent_interim/vision/fall_detection/config/g1.yaml"
VISION_FIRE_SCRIPT  = "/home/unitree/g1_agent_interim/vision/fire_detection/main.py"
VISION_FIRE_CONFIG  = "/home/unitree/g1_agent_interim/vision/fire_detection/config/g1.yaml"
GESTURE_CMD_FILE    = "/tmp/gesture_cmd"
TABLET_PORT         = 8000


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _start_tablet_server():
    """Serveur FastAPI/SSE de la tablette annexe — thread daemon séparé
    (uvicorn bloquant), écoute sur toutes les interfaces pour être joignable
    depuis le wifi (192.168.0.x) comme depuis eth0 (192.168.123.x)."""
    uvicorn.run(tablet_app, host="0.0.0.0", port=TABLET_PORT, log_level="warning")


_TABLET_BROWSERS = ["chromium-browser", "chromium", "google-chrome"]


def _open_tablet_display():
    """Ouvre automatiquement l'affichage de la tablette en plein écran (kiosk)
    sur l'écran branché directement au Jetson (localhost — pas besoin du wifi,
    donc pas affecté par l'isolation client du wifi campus). Best-effort :
    si aucun écran/navigateur n'est disponible (robot headless en SSH), ça
    échoue silencieusement sans jamais bloquer le reste de l'agent."""
    if not os.environ.get("DISPLAY"):
        return
    url = f"http://127.0.0.1:{TABLET_PORT}"
    for _ in range(20):
        try:
            import urllib.request
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.5)
    else:
        return
    for browser in _TABLET_BROWSERS:
        try:
            subprocess.Popen(
                [browser, "--kiosk", "--noerrdialogs", "--disable-infobars", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError:
            continue
    print("[TABLETTE] Aucun navigateur trouvé (chromium-browser/chromium/google-chrome) "
          "— ouvre le lien manuellement.")


def _terminal_input_loop():
    """Alternative clavier au micro : lit le terminal en continu dans un
    thread séparé (input() est bloquant) et pousse chaque ligne dans
    `input_queue` — lue par agent.events.text_input_loop qui l'injecte dans
    la conversation Realtime exactement comme si le micro l'avait captée.
    Utile pour discuter avec le robot sans micro (ou environnement bruyant)."""
    while True:
        try:
            text = input("\nVous : ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if text:
            _text_input_queue.put(text)

send_emotion("content")

def _start_subprocess(script: str, tag: str, python: str = MINICONDA_PYTHON,
                      args=None) -> subprocess.Popen:
    proc = subprocess.Popen(
        [python, script, *(args or [])],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f"[G1] {tag} démarré (PID {proc.pid})")
    return proc


async def _pipe_logs(proc: subprocess.Popen) -> None:
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, proc.stdout.readline)
        if not line:
            break
        print(line.decode(errors="replace").rstrip())


async def _supervise(script: str, tag: str, python: str, args=None) -> None:
    """Lance, pipe les logs, et redémarre automatiquement si le subprocess crash."""
    while True:
        proc = _start_subprocess(script, tag, python, args)
        await _pipe_logs(proc)
        proc.poll()
        rc = proc.returncode
        print(f"[G1] {tag} terminé (code {rc}) — redémarrage dans 3s")
        await asyncio.sleep(3)


async def _gesture_cmd_loop() -> None:
    """Surveille /tmp/gesture_cmd et exécute les gestes détectés par gesture_pose."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(0.1)
        if not os.path.exists(GESTURE_CMD_FILE):
            continue
        try:
            with open(GESTURE_CMD_FILE) as f:
                geste = f.read().strip()
            os.remove(GESTURE_CMD_FILE)
            if geste:
                threading.Thread(target=execute_gesture, args=(geste,), daemon=True).start()
        except Exception as e:
            print(f"[GESTURE] Erreur lecture cmd : {e}")


async def run():
    # Nettoyage de tous les fichiers IPC — un résidu de crash bloque sinon le
    # micro (agent_responding), la caméra (vision_pause) ou rejoue un geste.
    for f in ['/tmp/vision_state.json', '/tmp/face_id_state.json',
              '/tmp/agent_responding', '/tmp/vision_pause', '/tmp/rps_go',
              '/tmp/rps_result.json', '/tmp/gesture_cmd', '/tmp/fall_state.json',
              '/tmp/fire_state.json', '/tmp/qr_state.json',
              ]:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    # Vide le dossier des screenshots à chaque lancement : on repart d'une session
    # propre. Les photos de feu/chute sont conservées PENDANT la session (preuve +
    # email), puis effacées au prochain démarrage de main.py.
    if os.path.isdir(SCREENSHOT_DIR):
        for name in os.listdir(SCREENSHOT_DIR):
            path = os.path.join(SCREENSHOT_DIR, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    hardware.init()
    from robot.led_manager import led
    from robot.hardware import get_audio_client
    led.init(get_audio_client())   
    led.idle()
    spotify_player.start()
    threading.Thread(target=hand_idle.start, daemon=True).start()
    threading.Thread(target=_terminal_input_loop, daemon=True).start()
    threading.Thread(target=_start_tablet_server, daemon=True).start()
    threading.Thread(target=_open_tablet_display, daemon=True).start()
    # ── Infrastructure PERSISTANTE (indépendante de la connexion OpenAI) ──────
    # Vision + gestes tournent en continu comme tâches de fond : une reconnexion
    # de l'agent ne les coupe JAMAIS (pas de coupure caméra à chaque hoquet
    # réseau). Créées une seule fois pour toute la session.
    infra = [
        asyncio.ensure_future(_supervise(VISION_SRV_SCRIPT, "vision_server", PYTHON38)),
        asyncio.ensure_future(_supervise(FACE_ID_SCRIPT,    "face_id",       MINICONDA_PYTHON)),
        #asyncio.ensure_future(_supervise(VISION_FALL_SCRIPT, "fall_detection", PYTHON38, ["-c", VISION_FALL_CONFIG])),
        #asyncio.ensure_future(_supervise(VISION_FIRE_SCRIPT, "fire_detection", PYTHON38, ["-c", VISION_FIRE_CONFIG])),
        asyncio.ensure_future(_gesture_cmd_loop()),
    ]

    print('=' * 60)
    print(f'[TABLETTE] Ouvre ce lien pour voir l\'écran : http://{_lan_ip()}:{TABLET_PORT}')
    print('=' * 60)
    print('[G1] Prêt. Parle pour commencer, ou tape ton message + Entrée (Ctrl+C pour quitter)')

    # ── Boucle de RECONNEXION de l'agent ─────────────────────────────────────
    # Si la WebSocket OpenAI tombe (coupure réseau, timeout keepalive, fermeture
    # serveur…), on se reconnecte automatiquement au lieu de crasher. Essentiel
    # pour un robot d'accueil autonome qui tourne toute la journée. Backoff
    # exponentiel (2→4→8…→30s max) pour ne pas marteler l'API en cas de panne.
    backoff = 2
    try:
        while True:
            ws = None
            try:
                ws = await connect()
                print('[G1] Connecté à OpenAI — agent actif.')
                backoff = 2   # remise à zéro après une connexion réussie
                await asyncio.gather(
                    send_audio_loop(ws),
                    receive_events_loop(ws),
                    face_greeting_loop(ws),
                    rps_result_loop(ws),
                    text_input_loop(ws),
                    #fall_alert_loop(ws),
                    #fire_alert_loop(ws),
                    qr_alert_loop(ws),
                )
            except asyncio.CancelledError:
                raise   # arrêt volontaire (Ctrl+C) → surtout ne pas reconnecter
            except Exception as e:
                print(f'[G1] Connexion agent perdue ({type(e).__name__}: {e}) — '
                      f'reconnexion dans {backoff}s…')
            finally:
                # Nettoyage à chaque coupure : ferme la WS, coupe la parole en
                # cours, retire le flag qui bloquerait le micro à la reconnexion.
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                try:
                    get_audio_client().PlayStop('chat')
                except Exception:
                    pass
                try:
                    os.remove('/tmp/agent_responding')
                except FileNotFoundError:
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    finally:
        for t in infra:
            t.cancel()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print('\n[G1] Au revoir !')
