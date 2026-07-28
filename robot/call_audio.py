"""robot/call_audio.py — Appel d'assistance passant par le MICRO et le
HAUT-PARLEUR DU ROBOT (au lieu de ceux de la tablette).

Le Jetson rejoint lui-même le salon Daily via daily-python :

    Responsable ──Daily──► haut-parleur du robot (AudioClient.PlayStream)
                    ▲
    micro Cubilux ──┘  (chunks réutilisés de la capture existante)

POURQUOI RÉUTILISER LA CAPTURE EXISTANTE : agent/events.py tient déjà un flux
sounddevice ouvert sur le micro. En ouvrir un second sur le même périphérique
ALSA provoquerait un conflit. Pendant un appel l'IA est en sourdine, donc ces
chunks partent à la poubelle — on les redirige ici via push_mic().

HALF-DUPLEX (anti-écho) : le haut-parleur et le micro du robot sont à quelques
centimètres l'un de l'autre, sans annulation d'écho matérielle. Sans précaution,
la voix du responsable ressortirait du HP, serait recaptée par le micro et lui
reviendrait en écho. Tant que le responsable parle, on cesse donc d'émettre le
micro (comme le ducking déjà utilisé pour Spotify). Conséquence assumée : on ne
peut pas se couper la parole.

Mode de secours : si ce module échoue, ASSIST_AUDIO=tablette rebascule l'audio
sur le navigateur de la tablette (qui, lui, a l'annulation d'écho du navigateur).
"""

import os
import threading
import time

import numpy as np

# Format du flux : 24 kHz en entrée (ce que produit la capture de events.py),
# 16 kHz en sortie (seul débit accepté par play_pcm_stream du SDK Unitree).
MIC_RATE = 24000
SPK_RATE = 16000
_CHANNEL = "chat"          # même canal que la voix — le seul validé par le robot

# Half-duplex : au-dessus de ce niveau on considère que le responsable parle.
_SEUIL_VOIX = 300          # amplitude moyenne sur 20 ms (bruit de fond ≈ 30)
_HANGOVER = 0.35           # s de silence avant de rouvrir le micro (évite le hachage)

# Gain logiciel appliqué au micro AVANT envoi dans l'appel. Le préampli ALSA du
# Cubilux est réglé bas (45 %, soit -27,5 dB) : la voix ne dépasse pas ~4 % de la
# dynamique, donc le responsable entend faiblement. On amplifie ici, et NON dans
# la capture de events.py, pour ne pas toucher à la chaîne de l'IA qui fonctionne.
# Réglable sans modifier le code : ASSIST_MIC_GAIN=6 dans ~/.env.
# Valeur calée sur un enregistrement réel à 1 m : la voix crêtait à 4 % de la
# dynamique, ×6 la porte à ~24 % — audible et net, tout en gardant assez de marge
# pour quelqu'un qui parle fort ou de près avant d'écrêter.
_MIC_GAIN = float(os.environ.get("ASSIST_MIC_GAIN", "6.0"))   # 6x ≈ +15,6 dB

_lock = threading.Lock()
_client = None             # CallClient daily-python
_mic_dev = None            # micro virtuel alimenté par push_mic()
_spk_dev = None            # haut-parleur virtuel lu par _speaker_loop
_actif = False
_parle_jusqua = 0.0        # instant jusqu'auquel on considère le distant actif


def est_actif():
    return _actif


def _sur_depart(reason=None):
    """Un participant a quitté le salon.

    Indispensable en mode audio=robot : c'est la tablette qui détectait le
    départ du responsable, mais elle ne rejoint plus le salon. Sans ça, un
    responsable qui raccroche laisserait le visiteur devant un robot muet
    jusqu'au watchdog (10 min)."""
    # participants() inclut 'local' (le robot) : s'il ne reste que lui,
    # l'interlocuteur est parti.
    try:
        restants = [k for k in _client.participants().keys() if k != "local"]
    except Exception:
        restants = []
    print("[CALL-AUDIO] Départ ({}) — reste {} interlocuteur(s).".format(
        reason, len(restants)), flush=True)
    if not restants:
        print("[CALL-AUDIO] Le responsable a raccroché → fin d'appel.", flush=True)
        # En thread : on ne bloque pas la callback de daily-python, alors que
        # _fin_distante() va justement appeler leave() sur ce même client.
        threading.Thread(target=_fin_distante, daemon=True).start()


def _fin_distante():
    """Raccrochage venu d'en face : réveille l'IA et remet la tablette au menu.
    Même effet que POST /call/end, mais déclenché côté robot."""
    try:
        from tablet_server.server import AI_PAUSED_FLAG, push_call_end
        salon = ""
        try:
            with open(AI_PAUSED_FLAG) as f:
                salon = f.read().strip()
            os.remove(AI_PAUSED_FLAG)
        except OSError:
            pass
        push_call_end()          # la tablette quitte l'écran d'appel
        arreter()                # le robot quitte le salon, libère le HP
        if salon:
            from tools.assistance_tool import fermer_salon
            fermer_salon(salon)
    except Exception as e:
        print("[CALL-AUDIO] fin distante : {}".format(e), flush=True)


def push_mic(pcm24k):
    """Chunk micro (PCM 16-bit mono 24 kHz) venu de agent/events.py.

    Ignoré tant que le responsable parle : c'est le half-duplex anti-écho."""
    if not _actif or _mic_dev is None:
        return
    if time.time() < _parle_jusqua:
        return
    try:
        if _MIC_GAIN != 1.0:
            # int32 avant multiplication : en int16 le produit déborderait et
            # les crêtes repartiraient en négatif (grésillement violent).
            x = np.frombuffer(pcm24k, dtype=np.int16).astype(np.int32) * _MIC_GAIN
            pcm24k = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        _mic_dev.write_frames(pcm24k)
    except Exception:
        pass       # un chunk perdu ne doit jamais interrompre l'appel


def _speaker_loop():
    """Lit l'audio du salon et le joue sur le haut-parleur du robot."""
    try:
        from robot.hardware import get_audio_client
        audio = get_audio_client()
    except Exception as e:
        print("[CALL-AUDIO] AudioClient indisponible : {}".format(e), flush=True)
        return

    global _parle_jusqua
    stream_id = str(int(time.time() * 1000))
    n = int(SPK_RATE * 0.02)          # tranches de 20 ms

    while _actif:
        try:
            data = _spk_dev.read_frames(n)
        except Exception:
            break
        if not data:
            time.sleep(0.005)
            continue

        # Le seuil ne sert QU'À décider du half-duplex, jamais à filtrer ce qu'on
        # joue : conditionner la lecture au niveau hacherait les passages doux et
        # les fins de mots.
        niveau = float(np.mean(np.abs(np.frombuffer(data, dtype=np.int16))))
        if niveau > _SEUIL_VOIX:
            _parle_jusqua = time.time() + _HANGOVER

        try:
            audio.PlayStream(_CHANNEL, stream_id, data)
        except Exception as e:
            print("[CALL-AUDIO] PlayStream : {}".format(e), flush=True)

    try:
        audio.PlayStop(_CHANNEL)
    except Exception:
        pass
    print("[CALL-AUDIO] Sortie audio arrêtée.", flush=True)


def demarrer(url):
    """Rejoint le salon Daily. Retourne True si l'appel est bien établi —
    False laisse l'appelant rebasculer sur l'audio de la tablette."""
    global _client, _mic_dev, _spk_dev, _actif, _parle_jusqua
    if _actif:
        arreter()

    try:
        from daily import Daily, CallClient, EventHandler
    except Exception as e:
        print("[CALL-AUDIO] daily-python absent : {}".format(e), flush=True)
        return False

    # Défini ici : EventHandler n'est importable qu'une fois daily disponible.
    class _Handler(EventHandler):
        def on_participant_joined(self, participant):
            print("[CALL-AUDIO] Le responsable a rejoint l'appel.", flush=True)

        def on_participant_left(self, participant, reason=None):
            _sur_depart(reason)

    try:
        Daily.init()
        # Périphériques VIRTUELS : daily-python ne touche pas au matériel, c'est
        # nous qui poussons/tirons les échantillons.
        _mic_dev = Daily.create_microphone_device(
            "g1-mic", sample_rate=MIC_RATE, channels=1, non_blocking=True)
        _spk_dev = Daily.create_speaker_device(
            "g1-spk", sample_rate=SPK_RATE, channels=1)
        Daily.select_speaker_device("g1-spk")

        _client = CallClient(event_handler=_Handler())
        _client.update_inputs({
            "camera": False,
            "microphone": {"isEnabled": True,
                           "settings": {"deviceId": "g1-mic"}},
        })
        _client.join(url)

        _parle_jusqua = 0.0
        _actif = True
        threading.Thread(target=_speaker_loop, daemon=True).start()
        print("[CALL-AUDIO] Robot connecté au salon (micro + HP du robot).", flush=True)
        return True
    except Exception as e:
        print("[CALL-AUDIO] Connexion impossible : {}".format(e), flush=True)
        _actif = False
        return False


def arreter():
    """Quitte le salon et libère le haut-parleur."""
    global _client, _mic_dev, _spk_dev, _actif
    if not _actif and _client is None:
        return
    _actif = False              # arrête _speaker_loop
    time.sleep(0.1)
    try:
        if _client is not None:
            _client.leave()
            _client.release()
    except Exception as e:
        print("[CALL-AUDIO] leave : {}".format(e), flush=True)
    _client = None
    _mic_dev = None
    _spk_dev = None
    print("[CALL-AUDIO] Appel terminé côté robot.", flush=True)
