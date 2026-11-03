"""agent_local/asr.py — micro -> VAD (Silero) -> transcription (faster-whisper).

Remplace le VAD serveur d'OpenAI Realtime (semantic_vad, agent/session.py) —
qui n'existe plus une fois qu'on ne parle plus à l'API OpenAI. Segmentation du
tour de parole en local : on accumule tant que Silero VAD détecte de la
parole, on coupe après un silence continu suffisant, puis on transcrit le
segment accumulé (pas de streaming ASR — plus simple, suffisant pour ce
squelette).

Dépendances : faster-whisper (CPU, modèle `base` int8 — suffisant sur Orin NX,
contrairement à onnxruntime-GPU qui a motivé le déport vision sur la Thor,
voir THOR.md) et Silero VAD via torch.hub (le premier lancement télécharge le
modèle — nécessite une connexion internet une fois, wlan0 ; ensuite mis en
cache localement, fonctionne hors-ligne). Si l'empreinte de torch s'avère trop
lourde sur l'Orin NX, le paquet pip `silero-vad` (backend ONNX, sans torch)
est un remplacement direct de `_load_vad()` ci-dessous.
"""

import math
import queue

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

import ipc
from robot.audio import find_microphone

SAMPLE_RATE = 16000          # requis par Silero VAD et faster-whisper
_VAD_MIN_SAMPLES = 512       # Silero VAD refuse ("chunk too short") en dessous, à 16kHz
_FRAME_MS = 32               # 32ms x 16000Hz = 512 échantillons pile (30ms en donnait 480, insuffisant)
_SILENCE_END_MS = 700        # silence continu after lequel on coupe le tour
_MIN_SPEECH_MS = 250         # segment plus court = bruit, on l'ignore
_SPEECH_PROB_THRESHOLD = 0.5
_WHISPER_MODEL_SIZE = 'base'

_RESPONDING_FLAG = ipc.RESPONDING_FLAG


class Listener:
    """Un seul flux micro ouvert pour toute la session (comme
    agent/events.py::send_audio_loop). listen_for_utterance() BLOQUE jusqu'à
    un tour de parole complet — à appeler via run_in_executor, jamais
    directement depuis la boucle asyncio."""

    def __init__(self):
        mic_index, mic_sr = find_microphone()
        self._mic_sr = mic_sr
        g = math.gcd(mic_sr, SAMPLE_RATE)
        self._resample_up, self._resample_down = SAMPLE_RATE // g, mic_sr // g
        self._queue = queue.Queue()

        print('[ASR] Chargement faster-whisper...')
        from faster_whisper import WhisperModel
        # cpu_threads mesuré empiriquement sur cette Jetson (8 cœurs) : la
        # valeur par défaut de CTranslate2 est ~3-4x plus lente que
        # cpu_threads=4 (contention probable avec les autres threads du
        # process — VAD, callback micro, boucle asyncio). Pas de valeur
        # universelle documentée par CTranslate2 pour ce cas ; à réajuster
        # si le nombre de cœurs ou la charge du process change.
        self._whisper = WhisperModel(_WHISPER_MODEL_SIZE, compute_type='int8', cpu_threads=4)

        self._vad_model = self._load_vad()

        def _cb(indata, frames, t, status):
            self._queue.put(indata[:, 0].copy())

        self._stream = sd.InputStream(
            samplerate=mic_sr, channels=1, dtype='float32', device=mic_index,
            blocksize=int(mic_sr * _FRAME_MS / 1000), callback=_cb,
        )
        self._stream.start()
        print('[ASR] Micro ouvert.')

    @staticmethod
    def _load_vad():
        import torch
        print('[ASR] Chargement Silero VAD...')
        model, _utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad', model='silero_vad', trust_repo=True)

        def _prob(frame: np.ndarray) -> float:
            if len(frame) < _VAD_MIN_SAMPLES:
                # Filet de sécurité : selon le taux d'échantillonnage réel du
                # micro, l'arrondi du resampling peut tomber sous le minimum
                # Silero (512 échantillons à 16kHz) — on complète avec du
                # silence plutôt que de laisser torch.jit lever une exception.
                frame = np.pad(frame, (0, _VAD_MIN_SAMPLES - len(frame)))
            return model(torch.from_numpy(frame), SAMPLE_RATE).item()

        return _prob

    def _resample(self, chunk: np.ndarray) -> np.ndarray:
        """resample_poly (filtre FIR anti-repliement) plutôt qu'une
        interpolation linéaire (np.interp) : le micro capture en 48kHz, qui
        contient de l'énergie au-delà de 8kHz (Nyquist de la cible 16kHz) —
        sans filtre anti-repliement avant la décimation, cette énergie se
        replie en bruit dans la bande utile et dégrade nettement la
        transcription Whisper (confirmé empiriquement : sorties incohérentes
        avec l'interpolation linéaire, correctes avec resample_poly)."""
        if self._mic_sr == SAMPLE_RATE:
            return chunk
        if len(chunk) == 0:
            return chunk
        return resample_poly(chunk, self._resample_up, self._resample_down).astype(np.float32)

    def listen_for_utterance(self) -> str:
        """Bloque jusqu'à un tour de parole complet, retourne la
        transcription (chaîne vide si rien d'exploitable — bruit trop
        court, ou robot en train de parler)."""
        import os
        import time

        t_start = time.monotonic()
        speech_frames = []
        silence_ms = 0.0
        speech_ms = 0.0
        in_speech = False

        while True:
            chunk = self._queue.get()
            if os.path.exists(_RESPONDING_FLAG):
                # Le robot parle/réfléchit : on vide sans accumuler, pour ne
                # jamais capturer sa propre voix (pas d'écho annulé
                # matériellement — même garde que send_audio_loop).
                speech_frames = []
                silence_ms = 0.0
                speech_ms = 0.0
                in_speech = False
                continue

            frame = self._resample(chunk)
            if len(frame) == 0:
                continue
            frame_ms = len(frame) / SAMPLE_RATE * 1000
            prob = self._vad_model(frame)

            if prob >= _SPEECH_PROB_THRESHOLD:
                in_speech = True
                silence_ms = 0.0
                speech_ms += frame_ms
                speech_frames.append(frame)
            elif in_speech:
                silence_ms += frame_ms
                speech_frames.append(frame)  # garde un peu de silence de fin
                if silence_ms >= _SILENCE_END_MS:
                    break
            # sinon : silence avant toute parole détectée -> on ignore

        if speech_ms < _MIN_SPEECH_MS or not speech_frames:
            return ''

        t_speech_end = time.monotonic()
        audio = np.concatenate(speech_frames)
        # condition_on_previous_text=False + no_repeat_ngram_size : atténue le
        # bouclage classique de Whisper en décodage glouton (beam_size=1) sur
        # les segments plus longs — observé en conditions réelles ("Bonne
        # soirée. Bonne soirée. Merci beaucoup. Merci beaucoup...").
        segments, _info = self._whisper.transcribe(
            audio, language='fr', beam_size=1,
            condition_on_previous_text=False, no_repeat_ngram_size=3)
        text = ' '.join(seg.text.strip() for seg in segments).strip()
        t_done = time.monotonic()
        print(f'[ASR] écoute+VAD: {t_speech_end - t_start:.2f}s, whisper: '
              f'{t_done - t_speech_end:.2f}s (audio={speech_ms/1000:.2f}s)')
        return text

    def close(self):
        self._stream.stop()
        self._stream.close()
