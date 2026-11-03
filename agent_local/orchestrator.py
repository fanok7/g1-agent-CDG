"""agent_local/orchestrator.py — boucle agent du pipeline 100% local.

Équivalent minimal du run() de main.py pour `--engine local` : portée v1
validée avec l'utilisateur — micro + haut-parleur + boucle agent seulement,
pas de tablette, pas de supervision vision/Thor, pas de mains idle, pas de
Spotify. Boucle : écoute (VAD) -> ASR -> vLLM (texte + tool-calling, gestes
inclus) -> validation -> TTS -> haut-parleur, sur le personnage actif
(config.SYSTEM_PROMPT, sélectionné par --mode comme aujourd'hui).

Les gestes ne sont PAS extraits séparément ici : executer_geste est un tool
comme date_heure_actuelle, exécuté en effet de bord par
agent_local.vllm_client.run_turn() (via tools.registry.call), exactement
comme le fait déjà le chemin OpenAI Realtime (agent/events.py). Ce module n'a
donc qu'à gérer le texte de la réponse.
"""

import asyncio
import os

import config
import ipc
from robot import hardware
from robot.audio import play_audio
from agent.session import build_instructions
from agent_local import asr, tts, validator
from agent_local.vllm_client import run_turn
from agent_local.schema_adapter import get_tool_schemas

_RESPONDING_FLAG = ipc.RESPONDING_FLAG

_RETRY_INSTRUCTION = (
    "Ta réponse précédente contenait une didascalie entre parenthèses ou du "
    "jargon technique interdit. Recommence, en respectant strictement les "
    "règles (pas de parenthèses, pas de jargon)."
)


def _set_responding(state: bool):
    if state:
        open(_RESPONDING_FLAG, 'w').close()
    else:
        try:
            os.remove(_RESPONDING_FLAG)
        except FileNotFoundError:
            pass


async def run_local():
    ipc.cleanup()
    hardware.init()

    loop = asyncio.get_event_loop()
    print('[LOCAL] Chargement ASR (faster-whisper + Silero VAD)...')
    listener = await loop.run_in_executor(None, asr.Listener)

    messages = [{'role': 'system', 'content': build_instructions()}]
    tools = get_tool_schemas()

    print(f'[LOCAL] Prêt — modèle {config.VLLM_MODEL} ({config.VLLM_HOST}), '
          f'mode {config.ROBOT_MODE}.')
    try:
        while True:
            print('[LOCAL] Écoute...')
            text = await loop.run_in_executor(None, listener.listen_for_utterance)
            if not text:
                continue
            print(f'[Toi] {text}')
            messages.append({'role': 'user', 'content': text})

            reponse = await _generate(loop, messages, tools)
            messages.append({'role': 'assistant', 'content': reponse})

            print(f'[G1] {reponse}')

            _set_responding(True)
            try:
                t0 = loop.time()
                pcm = await loop.run_in_executor(None, tts.synthesize, reponse)
                t1 = loop.time()
                print(f'[LOCAL] Latence TTS (synthèse) : {t1 - t0:.2f}s')
                if pcm:
                    await loop.run_in_executor(None, play_audio, pcm)
            finally:
                _set_responding(False)
    finally:
        listener.close()


async def _generate(loop, messages, tools) -> str:
    """Un tour complet : appel vLLM (+ boucle tools), validation, et UN
    retry si la sortie contient une didascalie/du jargon interdit."""
    content, tool_trace, latency = await loop.run_in_executor(
        None, lambda: run_turn(messages, model=config.VLLM_MODEL, tools=tools))
    if tool_trace:
        print(f'[LOCAL] Tools appelés : {tool_trace}')
    print(f'[LOCAL] Latence LLM : {latency:.2f}s')

    reponse = validator.validate(content)
    if reponse is not None:
        return reponse

    print('[LOCAL] Sortie invalide — retry.')
    retry_messages = messages + [{'role': 'system', 'content': _RETRY_INSTRUCTION}]
    content, _tool_trace, _latency = await loop.run_in_executor(
        None, lambda: run_turn(retry_messages, model=config.VLLM_MODEL, tools=tools))
    return validator.validate(content) or validator.FALLBACK_REPLY
