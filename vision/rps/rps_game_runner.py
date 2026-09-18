"""
rps_game_runner.py — Moteur Pierre Feuille Ciseaux pour l'agent G1

Deux phases :
  1. prepare()   → robot choisit son coup (secret), prêt à jouer
  2. countdown() → synchronisé avec le compte à rebours vocal du robot :
                   révèle la main quand events.py signale la fin du TTS
                   (/tmp/rps_go), puis détecte le geste du joueur

IPC :
  /tmp/rps_go           → écrit par events.py à la fin du TTS "3! 2! 1! Go!"
  /tmp/rps_result.json  → lu par rps_result_loop dans events.py
  /tmp/gesture_cmd      → lu par gesture_cmd_loop dans main.py
  /tmp/vision_pause     → mis en pause vision_server pour libérer la caméra
"""

import os
import sys
import json
import time
import random
import threading

_RPS_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(os.path.dirname(_RPS_DIR))
sys.path.insert(0, _RPS_DIR)
sys.path.insert(0, _ROOT_DIR)

import ipc
from rps_game   import RPSGame, GESTES
from rps_vision import RPSVision
from rps_hand   import RPSHand

try:
    import robot.arm_sdk as arm_sdk
    _ARM_AVAILABLE = True
except Exception:
    _ARM_AVAILABLE = False

try:
    from robot import hand_idle
    _HAND_IDLE_AVAILABLE = True
except Exception:
    _HAND_IDLE_AVAILABLE = False

MODEL_PATH     = os.path.join(_RPS_DIR, 'rps_v2.engine')
GESTURES_YAML  = os.path.join(_RPS_DIR, 'hand_gestures.yaml')
RESULT_FILE    = ipc.RPS_RESULT
GESTURE_CMD    = ipc.GESTURE_CMD
VISION_PAUSE   = ipc.VISION_PAUSE_FLAG

# La révélation est synchronisée sur /tmp/rps_go, écrit par events.py à la fin
# exacte de la lecture TTS "3 ! 2 ! 1 ! Go !". Le timeout couvre le cas où le
# compte à rebours est interrompu (latence API + TTS ≈ 4-6s en temps normal).
GO_SIGNAL      = ipc.RPS_GO
GO_TIMEOUT     = 9.0   # fallback si le signal n'arrive jamais
DETECT_TIMEOUT = 6.0   # fenêtre de détection après la révélation

REACTIONS = {
    'victoire': 'saluer',
    'defaite':  'refus',
    'egalite':  'applaudir',
}

# ── État global ───────────────────────────────────────────────────────────────
_state_lock       = threading.Lock()
_robot_gesture    = None
_game             = RPSGame()   # singleton persistant — score conservé entre rounds
_countdown_active = False
_hand             = None
_vision           = None


def is_ready():
    """True si prepare() a été appelé et que le coup est prêt."""
    with _state_lock:
        return _robot_gesture is not None and not _countdown_active


def is_busy():
    with _state_lock:
        return _countdown_active


def reset_score():
    """Réinitialise le score (nouvelle partie depuis zéro)."""
    global _game
    with _state_lock:
        _game = RPSGame()


def prepare():
    """
    Phase 1 : robot choisit son coup en secret.
    Pré-charge le modèle YOLO et ouvre la connexion Modbus en background.
    Le score est conservé entre les rounds — appeler reset_score() pour repartir de zéro.
    """
    global _robot_gesture, _hand, _vision
    with _state_lock:
        if _countdown_active:
            return None
        _robot_gesture = random.choice(GESTES)

    # Le mouvement naturel des mains (hand_idle) et RPSHand écrivent tous les
    # deux sur le même registre Modbus de la main droite — il faut suspendre
    # l'un avant que l'autre ne prenne la main, sous peine de conflit.
    if _HAND_IDLE_AVAILABLE:
        hand_idle.stop()

    _patch_yaml()
    _hand   = RPSHand(side='r')
    _vision = RPSVision(MODEL_PATH, source=0)
    threading.Thread(target=_vision.preload, daemon=True).start()

    print(f'[RPS] Coup secret choisi : {_robot_gesture}', flush=True)
    if _ARM_AVAILABLE:
        arm_sdk.hold_pose('rps', right_only=True)
    return _robot_gesture


def countdown():
    """
    Phase 2 : lance le compte à rebours en background.
    Le robot révèle sa main au signal /tmp/rps_go (fin du TTS, fallback GO_TIMEOUT),
    puis détecte le geste du joueur.
    """
    global _countdown_active
    with _state_lock:
        if _robot_gesture is None or _countdown_active:
            return False
        _countdown_active = True
    threading.Thread(target=_reveal_and_detect, daemon=True).start()
    return True


# ── Pipeline interne ──────────────────────────────────────────────────────────
def _reveal_and_detect():
    global _robot_gesture, _countdown_active, _hand, _vision

    try:
        t_start = time.time()
        try:
            os.remove(GO_SIGNAL)
        except FileNotFoundError:
            pass

        # Pauser vision_server pour libérer /dev/video0
        open(VISION_PAUSE, 'w').close()
        time.sleep(0.8)

        # Caméra démarrée pendant le compte à rebours : ouverture V4L2, réglage
        # exposition et 1re inférence CUDA se font avant le "Go !" — la fenêtre
        # de détection n'est plus amputée par le warm-up.
        _vision.start()

        with _state_lock:
            gesture = _robot_gesture

        # Attendre la fin réelle du TTS "3 ! 2 ! 1 ! Go !" (signal de events.py)
        print('[RPS] Attente du signal Go...', flush=True)
        while time.time() - t_start < GO_TIMEOUT:
            if os.path.exists(GO_SIGNAL):
                break
            time.sleep(0.05)
        else:
            print(f'[RPS] WARN signal Go absent — révélation au timeout ({GO_TIMEOUT}s)',
                  flush=True)
        try:
            os.remove(GO_SIGNAL)
        except FileNotFoundError:
            pass

        # Purger les détections du compte à rebours (un poing qui pompe = pierre)
        _vision.reset()

        # ── RÉVÉLATION ── main du robot
        print(f'[RPS] Révèle : {gesture}', flush=True)
        _hand.play(gesture)   # 1.0s (doigts + pouce)

        # Détecter le geste du joueur
        print(f'[RPS] Détection joueur ({DETECT_TIMEOUT}s)...', flush=True)
        player_gesture = _vision.capture_gesture(timeout=DETECT_TIMEOUT)
        print(f'[RPS] Joueur → {player_gesture}', flush=True)

        # Arbitre
        with _state_lock:
            result = _game.play_round(player_gesture, gesture)
        print(f'[RPS] {result["message"]}', flush=True)

        # Résultat → rps_result_loop : l'annonce vocale part tout de suite,
        # pendant que le robot range sa main et relâche le bras.
        with open(RESULT_FILE, 'w') as f:
            json.dump(result, f, ensure_ascii=False)

        # Nettoyage mains + bras AVANT le geste de réaction : ExecuteAction et
        # arm_sdk ne doivent jamais commander les bras en même temps.
        time.sleep(1.5)
        _hand.open()
        _vision.stop()
        _hand.disconnect()
        if _ARM_AVAILABLE:
            arm_sdk.release_pose(wait=True)

        # Réaction gestuelle → gesture_cmd_loop (bras maintenant libres)
        reaction = REACTIONS.get(result['result'])
        if reaction:
            with open(GESTURE_CMD, 'w') as f:
                f.write(reaction)

    finally:
        # Cleanup défensif : même après une exception en pleine partie,
        # libérer caméra + main + bras pour que face_id récupère /dev/video0
        try:
            if _vision is not None:
                _vision.stop()
        except Exception:
            pass
        try:
            if _hand is not None:
                _hand.disconnect()
        except Exception:
            pass
        _hand   = None
        _vision = None
        _cleanup()
        with _state_lock:
            _robot_gesture    = None
            _countdown_active = False
        print('[RPS] Partie terminée', flush=True)


def _patch_yaml():
    import rps_hand as m
    m.GESTURES_FILE = GESTURES_YAML

def _cleanup():
    if _ARM_AVAILABLE:
        arm_sdk.release_pose()
    try:
        os.remove(VISION_PAUSE)
    except FileNotFoundError:
        pass
    if _HAND_IDLE_AVAILABLE:
        hand_idle.start()
