"""
robot/spotify_player.py — Transport audio Spotify via librespot.

librespot tourne comme device Spotify Connect ("G1 Robot"). On lit son flux
PCM brut (pipe backend, 44100 Hz stéréo S16) sur stdout, on le convertit en
16 kHz mono, et on l'envoie au haut-parleur du robot via AudioClient.PlayStream.

Le CONTRÔLE (play/pause/volume/titre) ne passe PAS par ici : il se fait via
l'API Web Spotify dans tools/spotify_tool.py, qui pilote le device librespot.
Ce module ne fait QUE transporter l'audio que librespot produit.

Le débit pipe de librespot est temps-réel : la lecture bloquante de stdout
sert d'horloge → pas de flooding du service audio (le bug d'avant).

Pause auto pendant que le robot parle (flag /tmp/agent_responding).
"""

import os
import subprocess
import threading
import time

import numpy as np

_RESPONDING_FLAG = '/tmp/agent_responding'
# Appel d'assistance en cours : on coupe aussi la musique, sinon elle sortirait du
# haut-parleur du robot et polluerait le micro de la tablette pendant l'appel.
_AI_PAUSED_FLAG  = '/tmp/ai_paused'

# Format de sortie du pipe librespot
_SRC_RATE  = 44100
_SRC_CH    = 2
_DST_RATE  = 16000
_CHANNEL   = 'chat'           # même canal que la voix — le seul canal validé par le robot
_READ_SEC  = 0.10             # taille de lecture ≈ 100 ms (auto-pacing temps-réel)
_SILENCE   = 12              # |sample| en dessous → chunk considéré silencieux

_CACHE_DIR = os.path.expanduser('~/.config/librespot')
_CREDS     = os.path.join(_CACHE_DIR, 'credentials.json')

_MAX_GAIN    = 3.0           # boost max à volume 100 (comme la voix) — clippe mais plus fort
_lock        = threading.Lock()
_gain        = 1.0           # gain logiciel local (le vrai volume passe par l'API)
_proc        = None          # subprocess librespot courant
_mgr_thread  = None
_started     = False


def is_authenticated() -> bool:
    """True si librespot a déjà un cache de credentials (auth one-shot faite)."""
    return os.path.exists(_CREDS)


def set_volume(pct: int) -> None:
    """Gain logiciel local (0-100). Mappé sur 0.._MAX_GAIN pour avoir de la marge
    au-dessus de l'unité (sinon "plus fort" ne peut pas dépasser le niveau brut)."""
    global _gain
    with _lock:
        _gain = max(0.0, min(100.0, float(pct))) / 100.0 * _MAX_GAIN


def start() -> None:
    """Lance le thread manager librespot (idempotent)."""
    global _mgr_thread, _started
    if _started:
        return
    _started = True
    _mgr_thread = threading.Thread(target=_manager_loop, daemon=True)
    _mgr_thread.start()


def stop() -> None:
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
        _proc = None


def _build_cmd() -> list:
    # Auth : credentials OAuth en cache (créés une fois via --enable-oauth, voir
    # SPOTIFY_SETUP.md). librespot 0.8 les réutilise → connexion headless.
    name = os.environ.get('SPOTIFY_DEVICE_NAME', 'G1 Robot')
    return [
        'librespot',
        '--name', name,
        '--device-type', 'speaker',
        '--bitrate', '320',
        '--backend', 'pipe',          # PCM brut sur stdout
        '--format', 'S16',            # 16-bit signé, 44100 Hz stéréo
        '--initial-volume', '100',
        '--autoplay', 'on',           # radio Spotify quand la file est vide → musique continue
        '--cache', _CACHE_DIR,        # credentials persistés ici
        '--disable-audio-cache',      # ne garde que les credentials, pas l'audio
    ]


def _log_stderr(proc: subprocess.Popen) -> None:
    """Recopie les logs librespot (auth, connexion device) avec préfixe."""
    for raw in iter(proc.stderr.readline, b''):
        line = raw.decode(errors='replace').rstrip()
        if line:
            print(f'[LIBRESPOT] {line}', flush=True)


def _manager_loop() -> None:
    """Lance librespot, lit son PCM, et le relance s'il meurt (backoff 3s)."""
    global _proc

    if not is_authenticated():
        print('[PLAYER] librespot non authentifié — aucun cache credentials. '
              'Lance la procédure d\'auth one-shot (voir doc).', flush=True)

    try:
        from robot.hardware import get_audio_client
    except Exception as e:
        print(f'[PLAYER] AudioClient indisponible : {e}', flush=True)
        return

    while True:
        try:
            proc = subprocess.Popen(
                _build_cmd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            print('[PLAYER] binaire librespot introuvable dans le PATH.', flush=True)
            return
        with _lock:
            _proc = proc

        threading.Thread(target=_log_stderr, args=(proc,), daemon=True).start()
        print(f'[PLAYER] librespot démarré (PID {proc.pid})', flush=True)

        try:
            _pump_pcm(proc, get_audio_client())
        except Exception as e:
            print(f'[PLAYER] Erreur transport PCM : {e}', flush=True)

        rc = proc.poll()
        print(f'[PLAYER] librespot terminé (code {rc}) — redémarrage dans 3s', flush=True)
        try:
            get_audio_client().PlayStop(_CHANNEL)
        except Exception:
            pass
        time.sleep(3)


def _pump_pcm(proc: subprocess.Popen, client) -> None:
    """Lit le PCM librespot, convertit 44100/stéréo → 16000/mono, envoie au HP."""
    # taille de lecture alignée sur une frame stéréo (2 canaux * 2 octets)
    frame_bytes = _SRC_CH * 2
    read_bytes  = int(_SRC_RATE * _READ_SEC) * frame_bytes
    stream_id   = str(int(time.time() * 1000))
    was_responding = False
    sample_ratio   = _DST_RATE / _SRC_RATE

    while proc.poll() is None:
        with _lock:
            if _proc is not proc:        # stop() appelé
                break

        t0 = time.time()

        # On vide TOUJOURS le pipe, au rythme temps-réel (pacing en fin de boucle).
        # Le backend pipe de librespot écrit aussi vite qu'on lit : si on lit sans
        # cadence on draine tout le titre en quelques secondes ; si on ne lit pas
        # du tout, librespot se bloque sur l'écriture et Spotify fige le device.
        data = proc.stdout.read(read_bytes)
        if not data:
            break

        # Musique coupée quand le robot parle OU pendant un appel d'assistance.
        responding = os.path.exists(_RESPONDING_FLAG) or os.path.exists(_AI_PAUSED_FLAG)
        if responding != was_responding:
            if responding:
                try:
                    client.PlayStop(_CHANNEL)   # coupe la musique, priorité à la voix
                except Exception:
                    pass
                stream_id = str(int(time.time() * 1000))
            was_responding = responding

        # Pendant que le robot parle : on a lu le chunk (pipe vidé au rythme réel)
        # mais on ne l'envoie pas au HP → la musique continue "en sourdine" et
        # redevient audible quand le robot se tait, sans figer librespot.
        if not responding:
            # 44100 stéréo S16 → mono float
            n = len(data) - (len(data) % frame_bytes)
            if n > 0:
                stereo = np.frombuffer(data[:n], dtype=np.int16).reshape(-1, _SRC_CH)
                mono   = stereo.mean(axis=1)
                # silence (device connecté mais rien ne joue) → ne pas occuper le canal
                if np.max(np.abs(mono)) >= _SILENCE:
                    # resample 44100 → 16000
                    n_out = max(1, int(round(len(mono) * sample_ratio)))
                    out = np.interp(
                        np.linspace(0.0, 1.0, n_out, endpoint=False),
                        np.linspace(0.0, 1.0, len(mono), endpoint=False),
                        mono,
                    )
                    with _lock:
                        g = _gain
                    out = np.clip(out * g, -32768, 32767).astype(np.int16)
                    try:
                        ret = client.PlayStream(_CHANNEL, stream_id, out.tobytes())
                        ret_code = ret[0] if isinstance(ret, tuple) else ret
                        if ret_code != 0:
                            print(f'[PLAYER] PlayStream ret={ret_code}', flush=True)
                    except Exception as e:
                        print(f'[PLAYER] Erreur envoi : {e}', flush=True)

        # Pacing temps-réel : caler le débit sur la durée réelle du chunk lu,
        # calculée depuis le nombre d'octets (robuste aux lectures partielles).
        # Sans ça, librespot déverse tout le titre d'un coup.
        chunk_sec = len(data) / float(_SRC_RATE * frame_bytes)
        dt = time.time() - t0
        if dt < chunk_sec:
            time.sleep(chunk_sec - dt)
