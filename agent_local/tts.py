"""agent_local/tts.py — synthèse vocale locale (Piper, CPU).

Piper piloté via son binaire CLI plutôt que son API Python interne (moins
stable d'une version à l'autre) — même logique que `librespot` dans
robot/spotify_player.py : un sous-processus, entrée/sortie par pipe, pas de
fichier temporaire. Piper choisi pour la latence (voix ONNX légère, CPU
temps-réel) plutôt que Kokoro (meilleure qualité mais plus lourd, GPU
recommandé) — Kokoro reste une piste de qualité une fois la latence
validée.

Piper produit du PCM 16-bit mono à SON propre sample rate (dépend de la voix
— souvent 22050 Hz) : on resample vers 24000 Hz avant robot.audio.play_audio,
qui attend déjà ce format côté chemin OpenAI Realtime et resample lui-même en
interne vers 16 kHz pour le haut-parleur (robot/audio.py) — aucune
modification de ce module.
"""

import json
import os
import shutil
import subprocess

import numpy as np

import config

def _find_piper_bin() -> str:
    # shutil.which('piper') dépend du PATH du shell courant — absent dans un
    # terminal ouvert avant l'ajout à ~/.bashrc, ou dans un service systemd
    # (qui ne source jamais .bashrc). Repli sur le chemin d'installation
    # connu (~/piper/piper, voir changements/2026-09-24-vllm-migration.md)
    # pour que le TTS marche indépendamment du shell qui a lancé main.py.
    found = shutil.which('piper')
    if found:
        return found
    fallback = os.path.expanduser('~/piper/piper')
    if os.path.isfile(fallback):
        return fallback
    return 'piper'


PIPER_BIN = _find_piper_bin()
_TARGET_RATE = 24000  # attendu par robot.audio.play_audio


def _voice_sample_rate(model_path: str) -> int:
    """Piper publie <model>.onnx.json à côté du .onnx avec la config de la
    voix (dont le sample_rate) — évite de coder le taux en dur."""
    try:
        with open(model_path + '.json') as f:
            return int(json.load(f)['audio']['sample_rate'])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return 22050  # repli : sample rate le plus courant des voix Piper


_VOICE_RATE = _voice_sample_rate(config.PIPER_VOICE_MODEL)


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(pcm) == 0:
        return pcm
    n_out = int(round(len(pcm) * dst_rate / src_rate))
    return np.interp(
        np.linspace(0.0, 1.0, n_out),
        np.linspace(0.0, 1.0, len(pcm)),
        pcm,
    )


def synthesize(text: str) -> bytes:
    """Texte -> PCM 16-bit mono 24kHz (format attendu par
    robot.audio.play_audio). Retourne b'' si le texte est vide ou si la
    synthèse échoue (voix manquante, binaire introuvable...)."""
    text = (text or '').strip()
    if not text:
        return b''
    if not os.path.isfile(config.PIPER_VOICE_MODEL):
        print(f'[TTS] Voix Piper introuvable : {config.PIPER_VOICE_MODEL}')
        return b''
    proc = subprocess.run(
        [PIPER_BIN, '--model', config.PIPER_VOICE_MODEL, '--output-raw'],
        input=text.encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout:
        print(f'[TTS] Erreur Piper : {proc.stderr.decode(errors="replace")}')
        return b''
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    pcm = _resample(pcm, _VOICE_RATE, _TARGET_RATE)
    return pcm.astype(np.int16).tobytes()
