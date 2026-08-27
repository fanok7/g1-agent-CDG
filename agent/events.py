from agent.parler_client import send_emotion
import json, base64, asyncio, os, time, threading, re
import sounddevice as sd
import numpy as np
from robot.audio import play_audio, find_microphone
from robot.hardware import get_audio_client
from robot.led_manager import led
from robot.gestures import execute_gesture
from tools.registry import call as call_tool
from tools.screenshot_tool import (send_image_email, FALL_RECIPIENT as _FALL_RECIPIENT,
                                   FIRE_RECIPIENT as _FIRE_RECIPIENT)

from agent.text_input import input_queue as _text_input_queue
import analytics as _analytics   # statistiques anonymes (questions, langues, zones)

try:
    from tablet_server.server import (push_chat, push_status, push_choices, push_lang,
                                       push_lang_user, push_assist_confirm)
    _TABLET_AVAILABLE = True
except Exception:
    _TABLET_AVAILABLE = False

import config as _config
# Mode interprète (Daneel) : chaque phrase fr/zh est traduite dans l'autre langue.
TRANSLATE_MODE = _config.TRANSLATE_MODE

_RESPONDING_FLAG  = "/tmp/agent_responding"
# Verrou d'appel d'assistance : tant qu'il existe, l'IA n'écoute plus (micro non
# envoyé) et ne parle plus (audio non joué) — un humain gère l'échange.
_AI_PAUSED_FLAG   = "/tmp/ai_paused"
from robot import call_audio as _call_audio   # pont audio de l'appel (micro/HP robot)
_FACE_STATE_FILE  = "/tmp/face_id_state.json"
_FACE_STALE_SECS  = 5.0
_RPS_GO_SIGNAL    = "/tmp/rps_go"
_RPS_GO_LEAD      = 0.1   # avance (s) sur la fin du TTS — compense le mouvement des doigts
_FALL_STATE_FILE  = "/tmp/fall_state.json"
_FALL_STALE_SECS  = 15.0  # au-delà : résidu d'un crash, on ignore
_FIRE_STATE_FILE  = "/tmp/fire_state.json"
_FIRE_STALE_SECS  = 15.0
_QR_STATE_FILE    = "/tmp/qr_state.json"
_QR_STALE_SECS    = 15.0

# Tools qui nécessitent une instruction forcée dans response.create
_TOOL_INSTRUCTIONS = {
    'demarrer_pfc': 'Dis exactement et uniquement ce texte, mot pour mot, avec enthousiasme : "3 ! 2 ! 1 ! Go !"',
}

# Mots interrogatifs EN DÉBUT de phrase = question OUVERTE -> pas de Oui/Non.
# (uniquement en tête : "que" dans "voulez-vous que…" n'est pas interrogatif)
_MOTS_OUVERTS_DEBUT = ('comment', 'quel', 'quelle', 'quels', 'quelles', 'que ', "qu'",
                       'où', 'quand', 'pourquoi', 'combien', 'qui ', 'lequel', 'laquelle',
                       'à quel', 'en quel',
                       # anglais
                       'how', 'what', 'where', 'when', 'why', 'which', 'who', 'whose',
                       # espagnol / allemand / italien (débuts fréquents)
                       'cómo', 'qué', 'dónde', 'cuándo', 'wie', 'was', 'wo', 'come', 'dove')
# Tournures typiques d'une vraie question fermée (oui/non), multilingue.
_TOURNURES_FERMEES = ('voulez-vous', 'souhaitez-vous', 'désirez-vous', 'aimeriez-vous',
                      'est-ce que', 'puis-je', 'avez-vous', 'êtes-vous', 'dois-je',
                      'faut-il', 'confirmez-vous', "d'accord", 'peut-on', 'pouvez-vous',
                      # anglais
                      'do you', 'would you', 'are you', 'can i', 'shall i', 'may i',
                      'have you', 'did you', 'is it', 'should i', 'want to',
                      # espagnol / italien / allemand
                      'quieres', 'desea', 'quiere', 'vuoi', 'möchten', 'wollen sie')


# Le robot enchaîne souvent plusieurs phrases ("Bonjour ... Comment puis-je
# vous aider ?"). Pour juger la question, on ne regarde que la DERNIÈRE phrase,
# pas tout le blabla d'avant.
def _derniere_phrase(text):
    parts = [p.strip() for p in re.split(r'[.!?]+', text or "") if p.strip()]
    return parts[-1].lower() if parts else (text or "").strip().lower()


def _looks_like_yes_no(text):
    """N'affiche des boutons Oui/Non QUE pour une vraie question fermée.
    On analyse la DERNIÈRE phrase : ouverte si elle commence par un mot
    interrogatif (comment/quel/où…) → pas de boutons ; sinon oui/non seulement
    si elle contient une tournure fermée (voulez-vous, avez-vous…)."""
    t = (text or "").strip().lower()
    if not t.endswith('?'):
        return False
    q = _derniere_phrase(t)                  # la vraie question, pas le "Bonjour…" d'avant
    if q.startswith(_MOTS_OUVERTS_DEBUT):    # question ouverte -> pas de boutons
        return False
    return any(tr in q for tr in _TOURNURES_FERMEES)


# Réflexe code : le petit modèle oublie souvent d'appeler proposer_choix pour le
# mode de transport → on détecte la question et on affiche les 4 boutons nous-mêmes.
# Multilingue : on reconnaît une question qui parle de mode de déplacement, dans
# n'importe quelle langue. Deux signaux : une tournure "comment vous déplacez",
# OU la question liste au moins 2 modes (à pied / voiture / transport / vélo).
_TRANSPORT_PHRASES = ('déplac', 'mode de transport', 'moyen de transport',
                      'how will you travel', 'how would you like to travel', 'how do you want to travel',
                      'getting there', 'cómo va a viajar', 'cómo quiere ir',
                      'wie möchten sie reisen', 'come vuoi viaggiare', 'come vuoi andare')
_MODE_WORDS = {
    'pied':      ('à pied', 'a pied', ' foot', 'walk', 'a pie', 'zu fuß', ' fuß', 'a piedi', '徒歩', '歩'),
    'voiture':   ('voiture', ' car', 'coche', ' auto', 'macchina', ' auto ', '車', '汽车'),
    'transport': ('transport', 'transit', 'öffentlich', 'trasporto', 'transporte', '公共交通', '交通機関'),
    'velo':      ('vélo', 'velo', ' bike', 'bici', 'fahrrad', '自転車', '自行车'),
}


def _asks_transport(text):
    t = " " + (text or "").strip().lower()
    if not t.rstrip().endswith('?'):
        return False
    if any(p in t for p in _TRANSPORT_PHRASES):
        return True
    modes = sum(1 for words in _MODE_WORDS.values() if any(w in t for w in words))
    return modes >= 2   # la question liste plusieurs modes → c'est la question transport


# Le robot PROPOSE de contacter un humain. Le modèle "mini" oublie souvent
# d'appeler le tool appeler_assistance (il récite juste la phrase), donc l'écran
# de confirmation ne s'affichait pas. On détecte la proposition DANS ce que le
# robot dit et on affiche l'écran par le CODE — même logique que le fallback
# Oui/Non ci-dessus. Signal robuste : "appeler/contacter" + "responsable/agent/
# quelqu'un", ou l'invitation explicite à toucher l'écran. Multilingue (fr/en/es/
# de/it) car le robot parle dans la langue du visiteur.
_ASSIST_PROPOSE = (
    "oui, appeler", "oui appeler", "appeler un responsable", "contacter un responsable",
    "contacte un responsable", "contacter un agent", "prévenir un responsable",
    "pour confirmer", "sur l'écran", "sur l'ecran",
    "yes, call", "call a staff", "contact a staff", "contact someone", "touch",
    "on the screen", "to confirm",
    "llamar a un responsable", "sí, llamar", "en la pantalla",
    "einen mitarbeiter", "auf dem bildschirm",
    "un responsabile", "sullo schermo",
)


def _proposes_assistance(text):
    t = (text or "").lower()
    return any(p in t for p in _ASSIST_PROPOSE)


# "Fallback" = le robot n'a pas su répondre directement (aveu d'ignorance dans
# sa réponse). Sert au taux de fallback de l'IA dans la télémétrie.
_FALLBACK_PHRASES = (
    "je ne sais pas", "je n'ai pas", "je ne peux pas", "je ne dispose pas",
    "désolé", "je n'ai pas trouvé", "je ne trouve pas",
    "i don't know", "i'm not sure", "i cannot", "i can't", "sorry", "i didn't find",
)


def _looks_fallback(text):
    t = (text or "").lower()
    return any(p in t for p in _FALLBACK_PHRASES)


# Détection de la langue DANS la transcription de l'utilisateur → l'interface
# tablette suit ce que le visiteur PARLE, sans dépendre du robot. Scripts non
# latins = sûrs ; langues latines = score de mots-indices (None si pas assez de
# signal → on ne change rien).
_LANG_HINTS = {
    'en': (' the ', ' you ', ' i ', ' is ', ' are ', ' to ', ' and ', ' want ', ' hello',
           ' please', ' thank', ' where', ' how ', ' need', ' hotel', ' can ', " i'm ", ' would '),
    'es': (' el ', ' la ', ' los ', ' hola', ' gracias', ' quiero', ' dónde', ' por favor',
           ' sí ', ' hotel', ' buenos', ' quiere', ' necesito', ' quisiera'),
    'de': (' der ', ' die ', ' das ', ' ich ', ' und ', ' ein ', ' hallo', ' danke',
           ' möchte', ' wo ', ' hotel', ' brauche', ' bitte', ' ich möchte'),
    'it': (' il ', ' la ', ' sono', ' ciao', ' grazie', ' voglio', ' dove', ' per favore',
           ' hotel', ' vorrei', ' bisogno', ' buongiorno'),
    'fr': (' le ', ' la ', ' les ', ' je ', ' vous ', ' un ', ' une ', ' est ', ' et ',
           ' bonjour', ' merci', ' oui', ' non', ' hôtel', ' où ', ' veux', ' voudrais', ' besoin'),
}


def _detect_lang(text):
    t = " " + (text or "").strip().lower() + " "
    if re.search(r'[぀-ヿ]', t):   # hiragana / katakana → japonais
        return 'ja'
    if re.search(r'[一-鿿]', t):   # idéogrammes (sans kana) → chinois
        return 'zh'
    if re.search(r'[؀-ۿ]', t):   # arabe
        return 'ar'
    scores = {lang: sum(1 for w in hints if w in t) for lang, hints in _LANG_HINTS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 1 else None   # pas assez de signal → ne change rien


# Nom lisible de la langue, pour l'instruction injectée au modèle.
_LANG_NAMES = {'fr': 'français', 'en': 'anglais', 'es': 'espagnol', 'de': 'allemand',
               'it': 'italien', 'ar': 'arabe', 'zh': 'chinois', 'ja': 'japonais'}


def _translation_target(lang):
    """Langue cible de l'interprète (mode daneel) : chinois→français,
    français→chinois. Langue NON détectée → on suppose un locuteur francophone
    → cible chinois (évite la réponse en français par défaut). Autre langue
    (en/es/de...) → None (conversation normale, le modèle s'adapte)."""
    if not TRANSLATE_MODE:
        return None
    if lang == 'zh':
        return 'fr'
    if lang in ('fr', None):
        return 'zh'
    return None


def _in_wrong_lang(out_text, tgt):
    """Garde-fou interprète : la réponse en cours est-elle dans la mauvaise
    langue (celle d'entrée au lieu de la cible) ?
    Robustesse : le chinois = idéogrammes. Cible=fr → tout idéogramme est un
    échec. Cible=zh → aucun idéogramme = pas du chinois : échec dès qu'on a
    assez de texte (ou qu'une vraie langue est détectée) — mais on attend
    l'idéogramme si le début n'est qu'un nom de marque latin (« Monoprix 是的 »)."""
    has_cjk = bool(re.search(r'[一-鿿]', out_text))
    if tgt == 'fr':
        return has_cjk
    if not has_cjk:
        olang = _detect_lang(out_text)
        if olang not in (None, 'zh'):
            return True
        return len(out_text.strip()) >= 12
    return False


async def _interpreter_response(ws, text):
    """Déclenche une réponse interprète pour un texte donné (voix ou clavier) :
    response.create AVEC l'instruction de traduction (éphémère, PAS injectée dans
    l'historique — une injection en français polluerait le contexte et ferait
    répondre le modèle en français). Sans cette instruction (create_response=False),
    le modèle "mini" répondrait dans la langue d'entrée au lieu de traduire.
    Autre langue (ex. anglais) → réponse normale dans sa langue."""
    lang = _detect_lang(text)
    tgt = _translation_target(lang)
    if tgt:
        globals()['_last_detected_lang'] = lang
        globals()['_turn_src'] = lang
        globals()['_turn_tgt'] = tgt
        globals()['_turn_retried'] = 0
        nom_src = _LANG_NAMES.get(lang, lang)
        nom_tgt = _LANG_NAMES.get(tgt, tgt)
        await ws.send(json.dumps({
            'type': 'response.create',
            'response': {'instructions':
                f"INTERPRÈTE — l'interlocuteur vient de parler en {nom_src}. "
                f"Traduis son message en {nom_tgt} UNIQUEMENT : la traduction seule, "
                f"fidèle et complète, sans commentaire, sans question, et JAMAIS en {nom_src}."},
        }))
    else:
        await ws.send(json.dumps({'type': 'response.create'}))


_last_detected_lang = None   # pour n'injecter l'instruction qu'au CHANGEMENT de langue
_turn_src = None             # interprète : langue d'entrée du tour courant
_turn_tgt = None             # interprète : langue cible du tour courant (garde-fou)
_turn_retried = 0            # nombre de regénérations du tour (max 1), interprète ET mode normal
_turn_user_lang = None       # langue détectée du tour courant (garde-fou hors interprète)
_last_user_text = ""         # dernière phrase du visiteur → motif de l'écran d'assistance
_assist_confirm_shown = False  # évite de réafficher l'écran d'assistance en boucle
_q_start = None              # horodatage de la question en cours (mesure de latence)
_fallback_turn = False       # ce tour a-t-il basculé en recherche web (fallback IA) ?

def _response_create(tool_name=None):
    msg = {'type': 'response.create'}
    led.reflechit() 
    if tool_name and tool_name in _TOOL_INSTRUCTIONS:
        msg['response'] = {'instructions': _TOOL_INSTRUCTIONS[tool_name]}
    return msg

def _set_responding(state: bool):
    if state:
        open(_RESPONDING_FLAG, 'w').close()
    else:
        try:
            os.remove(_RESPONDING_FLAG)
        except FileNotFoundError:
            pass


# ── Salut automatique garanti par le CODE ─────────────────────────────────────
# Le modèle (mini) est surchargé de contexte et oublie parfois d'appeler le geste.
# Dès que le robot DIT bonjour/au revoir, on lance le geste 'saluer' nous-mêmes —
# fiable quel que soit le contexte. Anti-doublon partagé avec face_greeting_loop.
_GREETING_WORDS = ('bonjour', 'bonsoir', 'coucou', 'bienvenue', 'salut',
                   'au revoir', 'au plaisir', 'bonne journée', 'à bientôt',
                   'à très vite', 'enchanté')
_CONGRATS_WORDS = ('félicitation', 'bravo', 'bien joué', 'chapeau', 'hourra',
                   'youpi', 'bonne nouvelle', 'quelle réussite', 'super nouvelle')
_last_gesture = {}              # geste → dernier timestamp (anti-doublon par geste)
_gesture_lock = threading.Lock()
_suppress_gestures_until = 0.0  # timestamp : gestes bloqués pendant scan billet


def _fire_gesture(geste: str, min_gap: float = 6.0) -> bool:
    """Lance un geste (thread), au plus une fois toutes les min_gap s par type de geste."""
    if time.time() < _suppress_gestures_until:
        return False
    with _gesture_lock:
        if time.time() - _last_gesture.get(geste, 0.0) < min_gap:
            return False
        _last_gesture[geste] = time.time()
    threading.Thread(target=execute_gesture, args=(geste,), daemon=True).start()
    return True


def _greet_now() -> bool:
    return _fire_gesture('saluer')


def _maybe_reflex_gesture(text: str):
    """Gestes réflexes garantis par le CODE (le modèle surchargé les oublie) :
    salut quand le robot dit bonjour/au revoir, applaudissements quand il félicite."""
    low = (text or '').lower()
    if any(w in low for w in _GREETING_WORDS):
        _fire_gesture('saluer')
    elif any(w in low for w in _CONGRATS_WORDS):
        _fire_gesture('applaudir')

MICRO_INDEX, MICRO_SR = find_microphone()


async def send_audio_loop(ws):
    """Capture micro en continu. Si le micro est indisponible (débranché,
    erreur PortAudio...), NE PLANTE PAS le reste de l'agent : asyncio.gather()
    annulerait toutes les autres boucles (clavier, vision...) si cette
    exception remontait. On tente une seule fois puis on abandonne
    proprement (pas de retry qui spamme les logs) — le clavier
    (text_input_loop) reste utilisable sans micro."""
    loop = asyncio.get_event_loop()
    q = asyncio.Queue()

    def cb(indata, frames, t, status):
        data = indata[::2] if MICRO_SR == 48000 else indata
        pcm = (data * 32767).astype(np.int16).tobytes()
        loop.call_soon_threadsafe(q.put_nowait, pcm)

    try:
        stream = sd.InputStream(samplerate=MICRO_SR, channels=1, dtype='float32',
                                 device=MICRO_INDEX, blocksize=int(MICRO_SR * 0.02),
                                 callback=cb)
        stream.start()
    except Exception as e:
        print(f'[MICRO] Indisponible ({e}) — micro désactivé pour cette session, '
              f'utilise le clavier.')
        return

    print('[MICRO] Flux ouvert.')
    try:
        while True:
            chunk = await q.get()
            if os.path.exists(_RESPONDING_FLAG):
                continue
            if os.path.exists(_AI_PAUSED_FLAG):
                # Appel d'assistance en cours : l'IA n'écoute pas, mais le chunk
                # n'est pas perdu — il alimente l'appel si celui-ci passe par le
                # micro du robot (sans ouvrir un 2e flux sur le même micro ALSA).
                _call_audio.push_mic(chunk)
                continue
            await ws.send(json.dumps({
                'type': 'input_audio_buffer.append',
                'audio': base64.b64encode(chunk).decode()
            }))
    finally:
        stream.stop()
        stream.close()


async def _flush_results(ws, results):
    """Envoie un batch de résultats de tools + un seul response.create.
    Si un tool du batch exige une instruction forcée (ex: compte à rebours PFC),
    elle est appliquée quelle que soit sa position dans le batch."""
    for cid, name, result in results:
        await ws.send(json.dumps({
            'type': 'conversation.item.create',
            'item': {'type': 'function_call_output', 'call_id': cid, 'output': result}
        }))
    forced = next((n for _, n, _ in results if n in _TOOL_INSTRUCTIONS), None)
    await ws.send(json.dumps(_response_create(forced)))
    return forced


async def receive_events_loop(ws):
    loop = asyncio.get_event_loop()

    audio_buf      = bytearray()
    text_buf       = ''
    responding     = False
    rps_go_pending = False
    _audio_playing = False   # True pendant que play_audio tourne dans l'executor
    choix_appele   = False   # True si proposer_choix a été appelé pendant ce tour

    # État multi-tools : keyed par call_id (corrige la corruption scalaire)
    _calls   = {}   # call_id → {'name': str, 'args': str}   — en cours de streaming
    _results = []   # [(call_id, name, result)]               — prêts, batch courant
    _queued  = []   # [(call_id, name, result)]               — différés (robot parlait)

    async for raw in ws:
        e = json.loads(raw)
        t = e.get('type', '')

        if t == 'input_audio_buffer.speech_started':
            print('[Toi] Parle...')
            if responding:
                get_audio_client().PlayStop('chat')
                audio_buf.clear()
                responding = False
                _set_responding(False)
                rps_go_pending = False

        elif t == 'conversation.item.input_audio_transcription.completed':
            transcript = e.get("transcript", "")
            print(f'[Toi] {transcript}')
            if transcript:
                globals()['_last_user_text'] = transcript   # motif de l'écran d'assistance
                # Départ du chrono latence : la question est enregistrée à la FIN
                # de la réponse du robot (pour mesurer latency_ms + is_fallback).
                globals()['_q_start'] = time.time()
                globals()['_fallback_turn'] = False
            if transcript and _TABLET_AVAILABLE:
                push_chat("user", transcript)
                _lang = _detect_lang(transcript)
                globals()['_turn_retried'] = 0
                if _lang:
                    globals()['_turn_user_lang'] = _lang
                if _lang and not TRANSLATE_MODE:
                    # Accueil classique : l'interface suit la langue parlée.
                    push_lang_user(_lang)
                    # Le modèle s'accroche parfois à la langue précédente : on lui
                    # force la bascule par une instruction interne (sans déclencher
                    # de réponse — elle agit sur le tour suivant).
                    if _lang != _last_detected_lang:
                        globals()['_last_detected_lang'] = _lang
                        _nom = _LANG_NAMES.get(_lang, _lang)
                        await ws.send(json.dumps({
                            'type': 'conversation.item.create',
                            'item': {'type': 'message', 'role': 'user',
                                     'content': [{'type': 'input_text',
                                                  'text': f"[SYSTÈME — instruction interne, n'y réponds pas] "
                                                          f"L'interlocuteur parle maintenant en {_nom}. "
                                                          f"Réponds en {_nom} à partir de maintenant."}]}
                        }))
                if not responding:   # pas d'overlay "Réflexion" si le robot parle déjà
                    push_status("reflechit")

            # ── INTERPRÈTE (daneel) : déclenchement manuel de la réponse ──────
            # create_response=False (session.py) : c'est NOUS qui envoyons
            # response.create, une fois la transcription arrivée, avec l'instruction
            # de traduction. Sans ça, le modèle "mini" répondrait dans la langue
            # d'entrée. L'interface tablette reste en français (équipe locale).
            if TRANSLATE_MODE and transcript:
                await _interpreter_response(ws, transcript)

        elif t == 'response.output_audio.delta':
            audio_buf.extend(base64.b64decode(e['delta']))
            if not responding:
                print('[G1] Parle...')
                _set_responding(True)
                send_emotion("parle")
                led.parle()
                if _TABLET_AVAILABLE:
                    push_status("parle")
                choix_appele = False   # nouveau tour de parole
            responding = True

        elif t == 'response.output_audio_transcript.delta':
            text_buf += e.get('delta', '')
            # ── Garde-fou de langue (interprète daneel) ──
            # Le modèle "mini" sort parfois du rôle et répond dans la langue
            # d'entrée (ex. français → français). On le détecte dès que le
            # transcript de sortie est dans la mauvaise langue, on annule la
            # réponse et on régénère avec une instruction plus dure (1 retry
            # max par tour). Détection robuste via _detect_lang (idéogrammes).
            if TRANSLATE_MODE and _turn_tgt and _turn_retried == 0 \
                    and len(text_buf.strip()) >= 3 and _in_wrong_lang(text_buf, _turn_tgt):
                globals()['_turn_retried'] = 1
                _nom_src = _LANG_NAMES.get(_turn_src or 'fr', 'fr')
                _nom_tgt = _LANG_NAMES.get(_turn_tgt, _turn_tgt)
                print(f'[G1] Réponse en langue source ({_nom_src}) — regénération ({_nom_tgt}).')
                responding = False
                _set_responding(False)
                audio_buf.clear()
                text_buf = ''
                await ws.send(json.dumps({'type': 'response.cancel'}))
                await ws.send(json.dumps({
                    'type': 'response.create',
                    'response': {'instructions':
                        f"ERREUR DE LANGUE DÉTECTÉE — tu as commencé à répondre en {_nom_src} "
                        f"au lieu de {_nom_tgt}. Recommence : traduis UNIQUEMENT en {_nom_tgt}, "
                        f"fidèle et complet, sans préambule, sans commentaire, sans question. "
                        f"Ne commence JAMAIS en {_nom_src}."},
                }))

            # ── Même garde-fou, hors mode interprète ──
            # Le modèle "mini" dévie parfois spontanément de langue (ex. répond
            # en espagnol à une question en français) sans que la détection
            # d'entrée n'ait pu le prévoir. On compare la langue du texte en
            # cours de génération à celle du tour utilisateur ; en cas de
            # désaccord net, on annule et on régénère avec une instruction
            # dure (1 retry max par tour, comme l'interprète).
            elif not TRANSLATE_MODE and _turn_user_lang and _turn_retried == 0 \
                    and len(text_buf.strip()) >= 15:
                _out_lang = _detect_lang(text_buf)
                if _out_lang and _out_lang != _turn_user_lang:
                    globals()['_turn_retried'] = 1
                    _nom = _LANG_NAMES.get(_turn_user_lang, _turn_user_lang)
                    print(f'[G1] Réponse en mauvaise langue ({_out_lang}) — regénération ({_nom}).')
                    responding = False
                    _set_responding(False)
                    audio_buf.clear()
                    text_buf = ''
                    await ws.send(json.dumps({'type': 'response.cancel'}))
                    await ws.send(json.dumps({
                        'type': 'response.create',
                        'response': {'instructions':
                            f"ERREUR DE LANGUE DÉTECTÉE — recommence ta réponse UNIQUEMENT en "
                            f"{_nom}, c'est la langue que l'interlocuteur vient de parler."},
                    }))

        elif t == 'response.output_audio.done':
            if os.path.exists(_AI_PAUSED_FLAG):
                # Appel d'assistance en cours : on jette la réponse au lieu de la
                # jouer — l'humain parle, l'IA reste muette.
                audio_buf.clear()
                text_buf = ''
                responding = False
                _set_responding(False)
                continue
            if audio_buf:
                print(f'[G1] {text_buf}')
                # Télémétrie : la question du visiteur est enregistrée MAINTENANT
                # (fin de réponse) pour disposer de la latence réelle et de
                # is_fallback. best-effort — n'interrompt jamais la conversation.
                if _last_user_text and _q_start is not None:
                    try:
                        _lat = int((time.time() - _q_start) * 1000)
                        _fb = _fallback_turn or _looks_fallback(text_buf)
                        _analytics.log_question(
                            _last_user_text, _last_detected_lang or "fr",
                            latency_ms=_lat, is_fallback=_fb)
                    except Exception:
                        pass
                    globals()['_q_start'] = None
                if text_buf and _TABLET_AVAILABLE:
                    push_chat("assistant", text_buf)
                    # Réflexes du mode accueil (assistance / boutons / gestes) —
                    # inutiles et parasites pour un interprète : désactivés en
                    # mode traduction (daneel). Les sous-titres (push_chat) eux
                    # restent actifs : phrase originale + traduction à l'écran.
                    if TRANSLATE_MODE:
                        push_choices([])
                    elif _proposes_assistance(text_buf) and not os.path.exists(_AI_PAUSED_FLAG):
                        if not _assist_confirm_shown:
                            globals()['_assist_confirm_shown'] = True
                            push_assist_confirm({
                                "motif": _last_user_text or "Demande d'assistance",
                                "langue": _last_detected_lang or "fr",
                            })
                            print("[ASSIST] Écran de confirmation affiché (réflexe code).")
                    else:
                        globals()['_assist_confirm_shown'] = False
                    # Le modèle (mini) oublie souvent d'appeler proposer_choix après
                    # une question fermée malgré l'instruction dans le prompt — même
                    # classe de problème que les gestes réflexes ci-dessous : on
                    # garantit un fallback Oui/Non par le CODE s'il ne l'a pas fait.
                    if not choix_appele:
                        if _asks_transport(text_buf):
                            push_choices(["À pied", "Voiture", "Transport", "Vélo"])
                        elif _looks_like_yes_no(text_buf):
                            push_choices(["Oui", "Non"])
                        else:
                            # Réponse qui n'est PAS une question fermée → on
                            # efface les boutons d'un tour précédent pour qu'ils
                            # ne traînent/réapparaissent pas au rechargement.
                            push_choices([])
                if not TRANSLATE_MODE:
                    _maybe_reflex_gesture(text_buf)   # salut/applaudissements auto par le code
                if rps_go_pending:
                    dur = len(audio_buf) / 2 / 24000.0
                    threading.Timer(max(0.0, dur - _RPS_GO_LEAD),
                                    lambda: open(_RPS_GO_SIGNAL, 'w').close()).start()
                    rps_go_pending = False
                pcm = bytes(audio_buf)
                audio_buf.clear()
                # _audio_playing positionné AVANT l'await : response.done peut
                # arriver pendant la lecture et ne touchera pas le flag.
                _audio_playing = True
                await loop.run_in_executor(None, play_audio, pcm)
                _audio_playing = False
                send_emotion("content")
                led.ecoute()
                if _TABLET_AVAILABLE:
                    push_status("ecoute")
                text_buf = ''
                responding = False
                _set_responding(False)
                print('[G1] Écoute...')

        elif t == 'response.done':
            globals()['_turn_tgt'] = None   # garde-fou interprète : tour terminé
            globals()['_turn_retried'] = 0  # garde-fou langue : prêt pour le tour suivant
            # Ne pas retirer le flag si l'audio est encore en lecture :
            # play_audio dans l'executor s'en charge quand il termine.
            if not _audio_playing:
                responding = False
                _set_responding(False)

            to_send = _queued + _results
            _queued.clear()
            _results.clear()
            if to_send:
                forced = await _flush_results(ws, to_send)
                if forced == 'demarrer_pfc':
                    rps_go_pending = True

        elif t == 'response.output_item.added':
            item = e.get('item', {})
            if item.get('type') == 'function_call':
                cid  = item.get('call_id')
                name = item.get('name')
                _calls[cid] = {'name': name, 'args': ''}
                print(f'[TOOL] Appel : {name}')

        elif t == 'response.function_call_arguments.delta':
            cid = e.get('call_id')
            if cid in _calls:
                _calls[cid]['args'] += e.get('delta', '')

        elif t == 'response.function_call_arguments.done':
            cid = e.get('call_id')
            if cid not in _calls:
                continue
            entry = _calls.pop(cid)
            name  = entry['name']
            if name == 'proposer_choix':
                choix_appele = True
            # Indicateur de chargement : un tool "lent" (carte, hôtels, web…)
            # peut prendre plusieurs secondes — on montre "Réfléchit..." pendant
            # ce temps (sauf tools d'affichage instantanés, et pas pendant que
            # le robot parle déjà).
            if _TABLET_AVAILABLE and not responding and name not in ('proposer_choix', 'definir_langue_ecran'):
                push_status("reflechit")
            # recherche_web = l'IA n'a pas la réponse en propre → ce tour est un
            # fallback (utilisé pour le taux de fallback de la télémétrie).
            if name == 'recherche_web':
                globals()['_fallback_turn'] = True
            _tool_status = "ok"
            try:
                # L'event .done porte les arguments complets — plus fiable que
                # l'accumulation des deltas
                args = json.loads(e.get('arguments') or entry['args'] or '{}')
                print(f'[TOOL] Args : {args}')
                # Exécution dans un thread pool : ne bloque plus la boucle asyncio
                result = await loop.run_in_executor(
                    None, lambda n=name, a=args: call_tool(n, a)
                )
            except Exception as ex:
                print(f'[TOOL] Erreur : {ex}')
                result = str(ex)
                _tool_status = "error"
            # Statistique : service sollicité + statut (ok/error). best-effort.
            try:
                _analytics.log_tool(name, _tool_status)
            except Exception:
                pass

            # Si le robot parle encore, différer ; sinon accumuler dans le batch courant.
            # Le flush réel se fait sur response.done, pas ici.
            if responding:
                _queued.append((cid, name, str(result)))
            else:
                _results.append((cid, name, str(result)))

        elif t == 'error':
            print(f'[ERREUR] {e.get("error", {})}')
            try: _analytics.note_api_error()   # compteur de l'event health
            except Exception: pass


async def rps_result_loop(ws):
    """Injecte le résultat RPS dans la conversation dès que la partie se termine."""
    _RPS_RESULT = '/tmp/rps_result.json'
    while True:
        await asyncio.sleep(1)
        if not os.path.exists(_RPS_RESULT):
            continue
        try:
            with open(_RPS_RESULT) as f:
                result = json.load(f)
            os.remove(_RPS_RESULT)
        except Exception:
            continue

        r      = result.get('result', 'rate')
        player = result.get('player') or 'inconnu'
        robot  = result.get('robot', '?')
        sp     = result.get('score_player', 0)
        sr     = result.get('score_robot', 0)

        # Attendre que le robot ait fini de parler avant d'injecter le résultat
        # (sinon response.create est rejeté et le résultat est perdu)
        for _ in range(30):
            if not os.path.exists(_RESPONDING_FLAG):
                break
            await asyncio.sleep(0.5)

        if r == 'rate':
            msg = (f'[RPS] Geste du joueur non détecté. '
                   f'Le robot avait joué {robot}. '
                   f'Annonce que tu n\'as pas vu son geste et propose de rejouer.')
        else:
            verdict = {
                'victoire': 'le joueur gagne, toi (le robot) tu as perdu',
                'defaite':  'toi (le robot) tu gagnes, le joueur a perdu',
                'egalite':  'égalité, personne ne gagne',
            }.get(r, r)
            msg = (f'[RPS] Toi (le robot) tu as joué {robot}, le joueur a joué {player}. '
                   f'Verdict : {verdict}. '
                   f'Score : joueur {sp} — robot {sr}. '
                   f'Annonce le résultat de façon enthousiaste et propose une revanche.')

        await ws.send(json.dumps({
            'type': 'conversation.item.create',
            'item': {
                'type':    'message',
                'role':    'user',
                'content': [{'type': 'input_text', 'text': msg}],
            }
        }))
        await ws.send(json.dumps({'type': 'response.create'}))


async def text_input_loop(ws):
    """Alternative clavier au micro : lit `input_queue` (alimentée par un
    input() terminal dans un thread dédié — voir main.py) et injecte le
    texte comme un vrai message utilisateur dans la conversation Realtime,
    exactement comme si le micro l'avait capté. Permet de discuter avec le
    robot sans micro (ou en complément), utile pour tester ou dans un
    environnement bruyant."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, _text_input_queue.get, True, 0.5)
        except Exception:
            await asyncio.sleep(0.1)
            continue
        if not text:
            continue

        # Attendre que le robot ait fini de parler avant d'injecter (sinon
        # response.create est rejeté et le message est perdu) — même garde
        # que rps_result_loop.
        for _ in range(60):
            if not os.path.exists(_RESPONDING_FLAG):
                break
            await asyncio.sleep(0.5)

        print(f'[TEXTE] {text}')
        await ws.send(json.dumps({
            'type': 'conversation.item.create',
            'item': {
                'type':    'message',
                'role':    'user',
                'content': [{'type': 'input_text', 'text': text}],
            }
        }))
        if TRANSLATE_MODE:
            # Interprète : même déclenchement que pour la voix (traduction fr↔zh).
            await _interpreter_response(ws, text)
        else:
            await ws.send(json.dumps({'type': 'response.create'}))


async def fall_alert_loop(ws):
    """Alerte proactive : détecte une chute via /tmp/fall_state.json (écrit par
    AgentToolHandler du module vision/fall_detection) et fait réagir le robot
    immédiatement. Consomme le fichier (lecture + suppression) — l'anti-spam
    (cooldown) est géré côté détecteur."""
    while True:
        await asyncio.sleep(1)
        if not os.path.exists(_FALL_STATE_FILE):
            continue
        try:
            with open(_FALL_STATE_FILE) as f:
                state = json.load(f)
            os.remove(_FALL_STATE_FILE)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

        # Résidu d'un crash précédent : timestamp périmé → on ignore
        if time.time() - state.get('ts', 0) > _FALL_STALE_SECS:
            continue

        print('[FALL] Chute détectée → alerte proactive', flush=True)
        try: _analytics.log_alert("chute")
        except Exception: pass

        # Envoi de la photo de la chute par email (bloquant réseau → executor).
        # L'image a été sauvegardée par le module fall_detection dans vision/Screenshot.
        image_path = state.get('image')
        loop = asyncio.get_event_loop()
        sent = await loop.run_in_executor(
            None, lambda: send_image_email(
                image_path or '', _FALL_RECIPIENT,
                'ALERTE CHUTE — Homme à terre',
                'Le robot G1 a détecté une personne au sol. Photo en pièce jointe.'))
        print(f'[FALL] Email : {sent}', flush=True)

        # Une chute est urgente : on attend juste que le robot finisse sa phrase
        # en cours (sinon response.create est rejeté), au plus ~3s.
        for _ in range(6):
            if not os.path.exists(_RESPONDING_FLAG):
                break
            await asyncio.sleep(0.5)

        await ws.send(json.dumps({
            'type': 'conversation.item.create',
            'item': {
                'type': 'message',
                'role': 'user',
                'content': [{'type': 'input_text',
                             'text': '[SYSTÈME URGENT] La caméra vient de détecter une '
                                     'personne au sol (chute probable). Une photo a été '
                                     'envoyée par email pour prévenir les secours.'}]
            }
        }))
        # On force le cri d'alerte exact, puis le robot rassure.
        await ws.send(json.dumps({'type': 'response.create', 'response': {
            'instructions': 'Dis fort, avec urgence, exactement ces mots pour commencer : '
                            '"Homme à terre ! Homme à terre !" Puis, calmement, demande si la '
                            'personne va bien et indique que tu as prévenu quelqu\'un par email.'}}))
        await asyncio.sleep(5)


async def fire_alert_loop(ws):
    """Alerte proactive : détecte un feu/fumée via /tmp/fire_state.json (écrit par
    AgentToolHandler du module vision/fire_detection) et fait crier le robot
    immédiatement. Consomme le fichier (lecture + suppression) — l'anti-spam
    (cooldown) est géré côté détecteur. Calqué sur fall_alert_loop."""
    while True:
        await asyncio.sleep(1)
        if not os.path.exists(_FIRE_STATE_FILE):
            continue
        try:
            with open(_FIRE_STATE_FILE) as f:
                state = json.load(f)
            os.remove(_FIRE_STATE_FILE)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

        # Résidu d'un crash précédent : timestamp périmé → on ignore
        if time.time() - state.get('ts', 0) > _FIRE_STALE_SECS:
            continue

        event = state.get('event', 'fire')   # 'fire' ou 'smoke'
        quoi  = 'de la fumée' if event == 'smoke' else 'un départ de feu'
        print(f'[FIRE] {event} détecté → alerte proactive', flush=True)
        try: _analytics.log_alert("fumee" if event == "smoke" else "feu")
        except Exception: pass

        # Envoi de la photo de la scène par email (bloquant réseau → executor).
        image_path = state.get('image')
        loop = asyncio.get_event_loop()
        sent = await loop.run_in_executor(
            None, lambda: send_image_email(
                image_path or '', _FIRE_RECIPIENT,
                'ALERTE FEU — Départ de feu détecté',
                f'Le robot G1 a détecté {quoi}. Photo en pièce jointe.'))
        print(f'[FIRE] Email : {sent}', flush=True)

        # Un feu est urgent : on attend juste que le robot finisse sa phrase
        # en cours (sinon response.create est rejeté), au plus ~3s.
        for _ in range(6):
            if not os.path.exists(_RESPONDING_FLAG):
                break
            await asyncio.sleep(0.5)

        await ws.send(json.dumps({
            'type': 'conversation.item.create',
            'item': {
                'type': 'message',
                'role': 'user',
                'content': [{'type': 'input_text',
                             'text': f'[SYSTÈME URGENT] La caméra vient de détecter {quoi} '
                                     '(incendie probable). Une photo a été envoyée par email '
                                     'pour prévenir les secours.'}]
            }
        }))
        # On force le cri d'alerte exact, puis le robot guide vers la sortie.
        await ws.send(json.dumps({'type': 'response.create', 'response': {
            'instructions': 'Dis fort, avec urgence, exactement ces mots pour commencer : '
                            '"Au feu ! Au feu !" Puis, calmement, demande aux personnes de '
                            's\'éloigner et d\'évacuer vers la sortie, et indique que tu as '
                            'prévenu quelqu\'un par email.'}}))
        await asyncio.sleep(5)


async def qr_alert_loop(ws):
    """Détecte les billets scannés passivement et les injecte dans la conversation."""
    while True:
        await asyncio.sleep(1)
        if not os.path.exists(_QR_STATE_FILE):
            continue
        try:
            with open(_QR_STATE_FILE) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if time.time() - state.get('ts', 0) > _QR_STALE_SECS:
            continue
        try:
            os.remove(_QR_STATE_FILE)
        except FileNotFoundError:
            pass
        for _ in range(30):
            if not os.path.exists(_RESPONDING_FLAG):
                break
            await asyncio.sleep(0.3)

        passager = state.get('passager', '')
        vol      = state.get('vol', '')
        de       = state.get('de', '')
        vers     = state.get('vers', '')
        pnr      = state.get('pnr', '')

        if passager:
            texte = (
                f"[SYSTÈME] Billet détecté automatiquement par la caméra. "
                f"Passager : {passager}. Vol : {vol}. De {de} vers {vers}. PNR : {pnr}. "
                f"Accueille le passager par son nom, confirme son vol et propose de l'aide."
            )
        else:
            texte = f"[SYSTÈME] Code QR scanné : {state.get('raw','')[:120]}"

        print(f'[QR] Billet injecté : {passager} / {vol}', flush=True)
        # Bloquer les gestes réflexes pendant la réponse QR (bras en mouvement = dangereux
        # quand quelqu'un tient son téléphone ou billet devant la caméra)
        global _suppress_gestures_until
        _suppress_gestures_until = time.time() + 15.0
        await ws.send(json.dumps({'type': 'conversation.item.create', 'item': {
            'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': texte}]
        }}))
        await ws.send(json.dumps({'type': 'response.create'}))
        await asyncio.sleep(3)


async def face_greeting_loop(ws):
    greeted              = set()
    # embeddings des inconnus déjà salués : liste de (vecteur np, timestamp)
    _greeted_unknowns: list = []
    # confirmation : inconnu_id → (embedding accumulé, count)
    _pending_unknowns: dict = {}
    UNKNOWN_SIM_THRESHOLD = 0.4   # même seuil qu'InsightFace — même personne si > 0.4
    UNKNOWN_FORGET_SEC    = 6000.0  # oublier un inconnu après 10 min (resaluer s'il revient)
    CONFIRM_FRAMES        = 3      # frames consécutives avant de saluer
    INCONNU_COOLDOWN      = 25.0  # cooldown post-conversation uniquement

    if TRANSLATE_MODE:
        # Interprète (daneel) : pas de salutation proactive pendant la visio —
        # un "Bonjour Jean !" en pleine traduction couperait l'échange.
        return

    for _ in range(30):
        if os.path.exists(_FACE_STATE_FILE):
            break
        await asyncio.sleep(1)
    else:
        await asyncio.sleep(5)

    while True:
        await asyncio.sleep(1)

        # Bloquer si le robot parle ou réfléchit
        if os.path.exists(_RESPONDING_FLAG):
            continue

        # Cooldown post-conversation : pas de salutation pendant INCONNU_COOLDOWN
        # secondes après que le robot a fini de parler
        try:
            with open('/tmp/last_conversation_end') as f:
                _last_conversation_end = float(f.read().strip())
        except (FileNotFoundError, ValueError):
            _last_conversation_end = 0.0

        if time.time() - _last_conversation_end < INCONNU_COOLDOWN:
            continue

        try:
            with open(_FACE_STATE_FILE) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue

        if time.time() - state.get('ts', 0) > _FACE_STALE_SECS:
            continue

        for face in state.get('faces', []):
            name = face.get('name', 'Inconnu')

            # ── Personne connue ───────────────────────────────────────────────
            if name != 'Inconnu':
                if name in greeted:
                    continue
                greeted.add(name)
                led.visage_reconnu()                           # 💜 Violet
                threading.Timer(4.0, led.clear_vision).start()
                print(f'[FACE] Personne reconnue : {name} → salutation')
                _greet_now()
                await ws.send(json.dumps({
                    'type': 'conversation.item.create',
                    'item': {
                        'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text',
                                     'text': f'[SYSTÈME] La caméra vient de détecter {name}. '
                                             f'Salue-le chaleureusement par son prénom.'}]
                    }
                }))
                await ws.send(json.dumps({'type': 'response.create'}))
                await asyncio.sleep(5)

            # ── Inconnu ───────────────────────────────────────────────────────
            else:
                # Ignorer si le score est trop proche d'une personne connue
                score = face.get('score', 1.0)
                if score > 0.15:
                    continue

                # Récupérer l'embedding fourni par face_id.py
                emb_raw = face.get('embedding')
                if not emb_raw:
                    continue
                emb = np.array(emb_raw, dtype=np.float32)

                now = time.time()

                # Purger les inconnus oubliés (> UNKNOWN_FORGET_SEC)
                _greeted_unknowns[:] = [
                    (e, t) for (e, t) in _greeted_unknowns
                    if now - t < UNKNOWN_FORGET_SEC
                ]

                # Vérifier si cet inconnu a déjà été salué
                already_greeted = any(
                    float(np.dot(emb, e)) > UNKNOWN_SIM_THRESHOLD
                    for (e, _) in _greeted_unknowns
                )
                if already_greeted:
                    _pending_unknowns.pop('inconnu', None)
                    continue

                # Confirmation sur CONFIRM_FRAMES frames consécutives
                prev_emb, count = _pending_unknowns.get('inconnu', (emb, 0))
                # Vérifier que c'est bien la même personne entre frames
                if float(np.dot(emb, prev_emb)) > UNKNOWN_SIM_THRESHOLD:
                    count += 1
                else:
                    count = 1  # nouvelle tête différente, recommencer
                _pending_unknowns['inconnu'] = (emb, count)

                if count < CONFIRM_FRAMES:
                    continue

                # Confirmé → saluer + mémoriser + réinitialiser le compteur
                _pending_unknowns.pop('inconnu', None)
                _greeted_unknowns.append((emb, now))
                led.visage_inconnu()                           # 🩷 Magenta
                threading.Timer(4.0, led.clear_vision).start()
                print(f'[FACE] Nouvel inconnu confirmé ({CONFIRM_FRAMES} frames) → salutation')
                _greet_now()
                await ws.send(json.dumps({
                    'type': 'conversation.item.create',
                    'item': {
                        'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text',
                                     'text': '[SYSTÈME] La caméra vient de détecter une '
                                             'nouvelle personne inconnue devant toi. '
                                             'Dis-lui bonjour chaleureusement.'}]
                    }
                }))
                await ws.send(json.dumps({'type': 'response.create'}))
                await asyncio.sleep(5)