"""
agent/shake_hand_loop.py — Boucle async : événements poignée de main → GPT

Consomme la queue tools.shake_hand_tool.event_queue et injecte
un message dans la session OpenAI Realtime pour que GPT réagisse
verbalement lors d'une poignée de main.

Ajout dans main.py :
    from agent.shake_hand_loop import shake_hand_event_loop
    # dans asyncio.gather() :
    shake_hand_event_loop(ws),
"""

import asyncio
import json
import logging
import os

import ipc

log = logging.getLogger(__name__)

_EVENT_PROMPTS = {
    'handshake_start': (
        '[SYSTÈME] Quelqu\'un vient de poser sa main dans ta paume — '
        'tu es en train de lui serrer la main. '
        'Réagis chaleureusement, dis quelque chose de naturel et bref '
        '(bonjour, ravi de te rencontrer, etc.).'
    ),
    'handshake_end': (
        '[SYSTÈME] La personne vient de retirer sa main — '
        'la poignée de main est terminée. '
        'Dis au revoir ou continue la conversation naturellement.'
    ),
}


async def shake_hand_event_loop(ws):
    """Boucle async à intégrer dans asyncio.gather()."""
    try:
        from tools.shake_hand_tool import event_queue
    except ImportError:
        log.info('[SHAKE_LOOP] shake_hand_tool non chargé — boucle inactive.')
        return

    log.info('[SHAKE_LOOP] Boucle démarrée.')

    while True:
        await asyncio.sleep(0.1)

        try:
            event = event_queue.get_nowait()
        except Exception:
            continue

        prompt = _EVENT_PROMPTS.get(event)
        if not prompt:
            log.warning('[SHAKE_LOOP] Événement inconnu : %s', event)
            continue

        # Même garde que les autres boucles d'injection (events.py) : si le
        # robot parle encore, response.create est rejeté et le message est
        # perdu. On attend la fin de la phrase en cours, au plus ~3 s.
        for _ in range(6):
            if not os.path.exists(ipc.RESPONDING_FLAG):
                break
            await asyncio.sleep(0.5)

        print('[SHAKE_LOOP] %s → injection GPT' % event, flush=True)

        try:
            await ws.send(json.dumps({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'message',
                    'role': 'user',
                    'content': [{'type': 'input_text', 'text': prompt}]
                }
            }))
            await ws.send(json.dumps({'type': 'response.create'}))

        except Exception as exc:
            log.error('[SHAKE_LOOP] Erreur WebSocket : %s', exc)
            await asyncio.sleep(1.0)
