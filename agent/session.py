import json
import websockets
from config import (REALTIME_URL, OPENAI_API_KEY, VOICE, SYSTEM_PROMPT, TRANSLATE_MODE,
                    ROBOT_MODE)
from tools.registry import get_schemas
from tools.datetime_tool import date_heure_fr


def build_instructions() -> str:
    """Contexte temporel (heure de Paris au démarrage) + SYSTEM_PROMPT actif.
    Partagé entre le chemin OpenAI Realtime (connect() ci-dessous) et le
    pipeline local (agent_local/orchestrator.py) — une seule source pour
    cette logique plutôt qu'une copie par chemin."""
    import pytz
    from datetime import timedelta
    _paris = pytz.timezone("Europe/Paris")
    _now   = __import__("datetime").datetime.now(_paris)
    _demain = _now + timedelta(days=1)
    _apres  = _now + timedelta(days=2)
    return (
        f"CONTEXTE TEMPOREL (heure de Paris au démarrage) :\n"
        f"  Aujourd'hui  : {date_heure_fr()}  — date ISO : {_now.strftime('%Y-%m-%d')}\n"
        f"  Demain       : {_demain.strftime('%A %d/%m/%Y').lower()} — date ISO : {_demain.strftime('%Y-%m-%d')}\n"
        f"  Après-demain : {_apres.strftime('%A %d/%m/%Y').lower()} — date ISO : {_apres.strftime('%Y-%m-%d')}\n"
        f"Pour l'heure exacte EN COURS DE CONVERSATION, appelle le tool date_heure_actuelle.\n"
        f"Pour l'agenda d'un jour précis, passe le paramètre date au format YYYY-MM-DD à agenda_du_jour.\n\n"
        + SYSTEM_PROMPT
    )


async def connect():
    headers = {'Authorization': f'Bearer {OPENAI_API_KEY}'}
    print('[AGENT] Connexion OpenAI Realtime...')
    ws = await websockets.connect(REALTIME_URL, extra_headers=headers)
    print('[AGENT] Connecté.')

    instructions = build_instructions()

    schemas = get_schemas()
    tools = [] if TRANSLATE_MODE else schemas
    await ws.send(json.dumps({
        'type': 'session.update',
        'session': {
            'type': 'realtime',
            'instructions': instructions,
            'output_modalities': ['audio'],
            'audio': {
                'input': {
                    'format': {'type': 'audio/pcm', 'rate': 24000},
                    # Micro actuel (Anker PowerConf, conférence 360°/plusieurs
                    # mètres) au lieu de l'ancien Cubilux plus directionnel :
                    # far_field filtre le bruit ambiant AVANT le VAD et la
                    # transcription (vs near_field pour un micro-casque proche).
                    'noise_reduction': {'type': 'far_field'},
                    'transcription': {
                        'model': 'gpt-4o-mini-transcribe',
                        # Pas de 'language' forcé pour CDG/Daneel : auto-détection →
                        # le robot comprend et répond dans la langue de l'interlocuteur
                        # (FR, EN, ES...). En iinterim, les visiteurs sont quasi
                        # exclusivement francophones (agence à Paris) : forcer 'fr'
                        # évite les erreurs de détection de langue qui dégradaient la
                        # transcription (le modèle n'a plus à deviner la langue).
                        **({'language': 'fr'} if ROBOT_MODE == 'iinterim' else {}),
                        # 'prompt' : indice de vocabulaire (mécanisme Whisper standard) —
                        # aide le modèle à mieux transcrire les noms/prénoms propres,
                        # qu'il a tendance à mal orthographier ou halluciner sur du son
                        # ambigu. Reste général (pas de liste figée de prénoms) : sert à
                        # orienter le style de transcription attendu, pas à deviner.
                        **({'prompt': "Conversation en français dans une agence d'intérim. "
                                      "Transcris fidèlement les prénoms et noms de famille "
                                      "français ou étrangers prononcés par le visiteur, sans "
                                      "les remplacer par un mot proche plus courant."}
                           if ROBOT_MODE == 'iinterim' else {}),
                    },
                    'turn_detection': {
                        'type': 'semantic_vad',
                        # False : le robot doit toujours finir sa phrase avant de
                        # réécouter — pas de barge-in (coupure par un bruit ambiant
                        # ou une reprise de parole pendant que le robot répond).
                        'interrupt_response': False,
                        # En mode interprète (daneel), la réponse n'est PAS auto :
                        # events.py injecte l'instruction de traduction puis envoie
                        # response.create lui-même. Sinon la réponse part avant
                        # l'instruction et le modèle répond dans la mauvaise langue.
                        'create_response': not TRANSLATE_MODE,
                        # 'high' : testé et abandonné le 2026-09-24 (coupe trop tôt, dégrade la
                        # transcription), re-testé et à nouveau problématique le 2026-09-30 sur
                        # ce point précis — remis quand même à la demande explicite du
                        # 2026-09-30. Si la qualité de transcription se dégrade à nouveau,
                        # repasser à 'medium' est le premier réflexe.
                        'eagerness': 'high',
                    },
                },
                'output': {
                    'format': {'type': 'audio/pcm', 'rate': 24000},
                    'voice': VOICE,
                },
            },
            'tools': tools,
            'tool_choice': 'auto',
        }
    }))
    print(f'[AGENT] Session configurée — {len(tools)} tool(s) actif(s)')
    return ws
