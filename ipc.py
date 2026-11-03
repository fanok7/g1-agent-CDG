"""
ipc.py — Contrat d'échange entre les processus du robot.

L'agent (main.py, python3.8), les sous-processus vision (vision_server.py en
python3.8, face_id.py en miniconda), le jeu RPS et le superviseur dashboard.py
tournent dans des interpréteurs SÉPARÉS : ils ne partagent aucune mémoire et ne
communiquent que par des fichiers dans /tmp.

Ces chemins sont donc une interface publique entre processus. Ils étaient
auparavant réécrits littéralement dans une dizaine de modules — renommer un
fichier demandait dix éditions, et un oubli ne se voyait qu'à l'exécution (un
producteur écrivant là où plus personne ne lit). Ce module est l'unique source
de vérité.

Contrainte : ce fichier est importé par des interpréteurs différents (système
python3.8 ET miniconda). Il ne doit donc JAMAIS importer autre chose que la
bibliothèque standard, ni rien exécuter à l'import.

Usage :
    import ipc
    if os.path.exists(ipc.RESPONDING_FLAG): ...

    # depuis un sous-processus vision (pas lancé depuis la racine du repo) :
    import sys; sys.path.insert(0, '/home/unitree/g1_agent_interim')
    import ipc
"""

import os

# Racine du dépôt — permet aux sous-processus de retrouver le repo sans le
# coder en dur chez eux.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


# ── Drapeaux (présence du fichier = état actif, contenu ignoré) ──────────────

RESPONDING_FLAG = "/tmp/agent_responding"
"""Le robot parle ou réfléchit. Créé/supprimé par agent.events._set_responding.
Lu par : send_audio_loop (coupe le micro), spotify_player (ducking de la
musique), les boucles d'alerte (attendent la fin de la phrase). Résidu de crash
→ robot muet : `rm /tmp/agent_responding`."""

AI_PAUSED_FLAG = "/tmp/ai_paused"
"""Appel d'assistance humaine en cours : l'IA n'écoute ni ne parle. Créé par
tablet_server.server au décroché, supprimé au raccroché. Résidu de crash →
micro ET haut-parleur coupés en permanence : `rm /tmp/ai_paused`."""

VISION_PAUSE_FLAG = "/tmp/vision_pause"
"""Demande aux processus vision de lâcher la caméra (jeu RPS). Créé/supprimé
par rps_game_runner ; respecté par face_id.py et vision_server.py."""


# ── État publié par la vision (JSON horodaté, champ "ts") ───────────────────

VISION_STATE = "/tmp/vision_state.json"
"""{"objects": [{label, conf, x, y, cam}], "ts": ...} — écrit par
vision_server.py, lu par le tool ce_que_je_vois (péremption 15 s)."""

FACE_ID_STATE = "/tmp/face_id_state.json"
"""{"faces": [{name, score, embedding?}], "ts": ...} — écrit par face_id.py, lu
par identifier_personne, ce_que_je_vois et face_greeting_loop (péremption 10 s).
"""

QR_STATE = "/tmp/qr_state.json"
"""Billet d'avion scanné passivement — écrit par vision_server.py, consommé
(lu puis supprimé) par qr_alert_loop."""

FALL_STATE = "/tmp/fall_state.json"
"""Chute détectée — écrit par vision/fall_detection, consommé par
fall_alert_loop. ⚠ producteur ET consommateur commentés dans main.py."""

FIRE_STATE = "/tmp/fire_state.json"
"""Feu/fumée détecté — écrit par vision/fire_detection, consommé par
fire_alert_loop. ⚠ producteur ET consommateur commentés dans main.py."""


# ── Frames brutes partagées entre processus ─────────────────────────────────

UGREEN_FRAME = "/tmp/latest_ugreen.jpg"
"""Dernière frame de la caméra UGREEN. face_id.py possède /dev/video0 en
exclusivité et republie chaque frame ici ; vision_server.py (YOLO UGREEN),
screenshot_tool et fall/fire_detection la relisent. C'est ce relais qui évite
que deux processus ouvrent la même caméra."""

REALSENSE_FRAME = "/tmp/latest_realsense.jpg"
"""Dernière frame RealSense, écrite par vision_server.py, lue par qr_tool
(péremption 3 s)."""


# ── Jeu Pierre-Feuille-Ciseaux ──────────────────────────────────────────────

RPS_GO = "/tmp/rps_go"
"""Top de révélation, écrit par agent.events à la fin exacte du TTS
« 3 ! 2 ! 1 ! Go ! », attendu par rps_game_runner."""

RPS_RESULT = "/tmp/rps_result.json"
"""Résultat de la manche — écrit par rps_game_runner, consommé par
rps_result_loop."""

GESTURE_CMD = "/tmp/gesture_cmd"
"""Nom d'un geste à exécuter (texte brut). Écrit par rps_game_runner pour les
réactions de fin de manche, consommé par main._gesture_cmd_loop."""


# ── Divers ──────────────────────────────────────────────────────────────────

LAST_CONVERSATION_END = "/tmp/last_conversation_end"
"""Horodatage de fin de la dernière conversation, lu par face_greeting_loop
pour ne pas saluer un inconnu juste après un départ (INCONNU_COOLDOWN).
Écrit par agent/events.py::_set_responding(False) — à chaque fois que le
robot arrête de parler (fin de tour), pas seulement en toute fin de
conversation : ça prolonge naturellement le cooldown tant qu'un échange est
en cours, et le fait expirer 25s après la dernière phrase du robot."""

TABLET_CLIENT_LOG = "/tmp/tablet_client.log"
"""Journal des erreurs JS renvoyées par la tablette (POST /clientlog)."""

ASSIST_DEBUG_LOG = "/tmp/assist_debug.log"
"""Trace de diagnostic des écrans de confirmation d'assistance."""


# ── Nettoyage au démarrage ──────────────────────────────────────────────────

CLEANUP_AT_STARTUP = (
    VISION_STATE,
    FACE_ID_STATE,
    RESPONDING_FLAG,
    VISION_PAUSE_FLAG,
    RPS_GO,
    RPS_RESULT,
    GESTURE_CMD,
    FALL_STATE,
    FIRE_STATE,
    QR_STATE,
    AI_PAUSED_FLAG,
    LAST_CONVERSATION_END,
)
"""Fichiers supprimés par main.py au lancement. Un résidu de crash bloquerait
sinon le micro (RESPONDING_FLAG), le micro ET le haut-parleur (AI_PAUSED_FLAG),
la caméra (VISION_PAUSE_FLAG), ou rejouerait un geste (GESTURE_CMD)."""


def cleanup():
    """Supprime les fichiers IPC résiduels. Idempotent."""
    for path in CLEANUP_AT_STARTUP:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
