"""agent_local/vllm_client.py — client HTTP pour vLLM (/v1/chat/completions,
API compatible OpenAI).

Remplace agent_local/ollama_client.py : Ollama produisait une sortie GPU
corrompue sur ce Jetson Thor (bug confirmé, voir changements/) — vLLM tourne
correctement sur ce matériel.

Différence de conception importante par rapport au premier essai (Ollama) :
PAS de format JSON forcé (response_format) combiné aux tools — testé
empiriquement sur ce serveur, les deux sont incompatibles ici : dès que
response_format contraint la sortie, le modèle ne peut plus émettre la
syntaxe d'appel d'outil et ignore les tools. On revient donc à l'approche du
pipeline OpenAI Realtime existant (agent/events.py) : réponse en texte libre
+ tool-calling standard, y compris pour les gestes (executer_geste est un
tool comme un autre, pas un champ d'un schema JSON).

Les tools sont exécutés via tools.registry.call() — mêmes handlers que le
chemin OpenAI Realtime, aucune logique dupliquée.
"""

import json
import time

import requests

import config
from tools.registry import call as call_tool

MAX_TOOL_ITERATIONS = 4
REQUEST_TIMEOUT_S = 60

# Qwen3 a un mode "réflexion" (chaîne de raisonnement visible avant la
# réponse) — désactivé systématiquement : un agent vocal doit répondre vite,
# pas réfléchir à voix haute pendant plusieurs secondes.
_EXTRA_BODY = {'chat_template_kwargs': {'enable_thinking': False}}


def chat(messages, tools=None, model=None):
    """Un aller-retour /v1/chat/completions. Retourne le dict `message` de la
    réponse ({'role','content','tool_calls'?})."""
    body = {
        'model': model or config.VLLM_MODEL,
        'messages': messages,
        **_EXTRA_BODY,
    }
    if tools:
        body['tools'] = tools
    resp = requests.post(f'{config.VLLM_HOST}/v1/chat/completions', json=body,
                          timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']


def run_turn(messages, model=None, tools=None):
    """Boucle d'appel de tools jusqu'à une réponse finale sans tool_calls
    (ou jusqu'à MAX_TOOL_ITERATIONS, garde-fou anti-boucle infinie). Les
    tools sont TOUJOURS proposés au modèle (y compris executer_geste — le
    geste s'exécute alors directement en effet de bord via tools.registry.call,
    comme dans le pipeline OpenAI Realtime, pas besoin de le faire remonter
    séparément à l'appelant).

    Retourne (content: str, tool_trace: list[str], latency_s: float).
    """
    tools = tools if tools is not None else []
    history = list(messages)
    tool_trace = []
    t0 = time.time()
    message = {}
    for _ in range(MAX_TOOL_ITERATIONS):
        message = chat(history, tools=tools, model=model)
        tool_calls = message.get('tool_calls') or []
        if not tool_calls:
            break
        history.append(message)
        for tc in tool_calls:
            fn = tc.get('function') or {}
            name = fn.get('name', '')
            args = fn.get('arguments') or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result = call_tool(name, args)
            tool_trace.append(name)
            history.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', name),
                'content': result,
            })

    return message.get('content') or '', tool_trace, time.time() - t0
