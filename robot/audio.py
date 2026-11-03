import sys, os, time
sys.path.insert(0, '/home/unitree/unitree_sdk2_python/example/g1/audio')

import numpy as np
import sounddevice as sd
from wav import play_pcm_stream
from config import VOLUME_BOOST
import robot.hardware as hardware


def find_microphone():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0 and 'MIC' in d['name'].upper():
            sr = int(d['default_samplerate'])
            if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
                print(f'[MICRO] {d["name"]} index={i} sr={sr}')
            return i, sr
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0 and 'USB' in d['name'].upper():
            sr = int(d['default_samplerate'])
            if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
                print(f'[MICRO] fallback {d["name"]} index={i} sr={sr}')
            return i, sr
    sr = int(sd.query_devices(0)['default_samplerate'])
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print(f'[MICRO] fallback index=0 sr={sr}')
    return 0, sr


def _resample(pcm_24k: bytes) -> list:
    """PCM 24kHz 16-bit mono → PCM 16kHz 16-bit mono + volume boost.
    Retourne list[int] (octets bruts) attendu par play_pcm_stream.
    Traitement in-process : pas de subprocess ffmpeg, pas de fichiers temp."""
    samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return []
    n_out = int(round(len(samples) * 16000 / 24000))
    resampled = np.interp(
        np.linspace(0.0, 1.0, n_out),
        np.linspace(0.0, 1.0, len(samples)),
        samples,
    )
    resampled = np.clip(resampled * VOLUME_BOOST, -32768, 32767).astype(np.int16)
    return list(resampled.tobytes())


def play_audio(pcm_bytes: bytes):
    audio_client = hardware.get_audio_client()
    if not pcm_bytes:
        return

    pcm_list = _resample(pcm_bytes)
    if not pcm_list:
        return

    # Durée réelle de l'audio converti (16kHz mono 16-bit = 2 bytes/sample)
    dur = len(pcm_list) / (16000 * 2)

    t0 = time.time()
    # Pacing temps-réel : chunk_size/sleep_time doivent correspondre au débit
    # réel (16kHz, 16-bit = 32000 octets/s), sinon les chunks arrivent plus
    # vite que le temps de lecture et débordent le buffer audio du robot côté
    # SDK — ça coupait les réponses longues en plein milieu de la phrase.
    # Le micro reste libéré au bon moment via le time.sleep(remaining) plus bas.
    _chunk_size = 32000  # 1s d'audio par chunk
    play_pcm_stream(audio_client, pcm_list, 'chat',
                     chunk_size=_chunk_size, sleep_time=_chunk_size / (16000 * 2))

    # Attendre uniquement le temps restant pour que l'audio finisse de jouer,
    # en tenant compte du temps déjà passé dans play_pcm_stream.
    elapsed = time.time() - t0
    remaining = max(0.0, dur - elapsed + 0.5)
    time.sleep(remaining)

    audio_client.PlayStop('chat')
    time.sleep(0.1)
