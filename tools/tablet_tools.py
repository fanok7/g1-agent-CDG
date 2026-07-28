"""
tools/tablet_tools.py — Outils d'affichage sur la tablette embarquée du robot.

La tablette n'a pas de cerveau à elle : le micro, le haut-parleur et l'IA de
conversation restent entièrement dans agent/events.py (API Realtime OpenAI).
Ces 4 tools ne font que piloter l'ÉCRAN annexe (texte/QR/plan/boutons), en
poussant vers tablet_server via Server-Sent Events.

Réutilise googlemaps_tools.py (compute_route, geocode_address) déjà présent
dans ce projet pour le calcul d'itinéraire — pas de hack sys.path nécessaire
ici (contrairement au banc de test g1_virtual_tablet qui vit dans un projet
séparé).
"""

import base64
import os
import socket
import time

import qrcode
import requests

from tools.registry import register
from tools.googlemaps_tools import compute_route, geocode_address
from tablet_server.server import push_display, push_choices, push_lang

_BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR = os.path.join(_BASE_DIR, "tablet_server", "static")
_QR_DIR    = os.path.join(_STATIC_DIR, "qrcodes")
_PLANS_DIR = os.path.join(_STATIC_DIR, "plans")
_NOTES_DIR = os.path.join(_STATIC_DIR, "notes")

_TABLET_PORT = 8000


def _detect_lan_ip():
    """IP locale utilisée pour sortir vers Internet (le wifi/eth partagé) —
    sert à construire un lien LAN accessible depuis un téléphone sur le même
    réseau (fallback QR texte trop long pour tenir dans un seul QR direct)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _tablet_lan_url():
    return "http://{}:{}".format(_detect_lan_ip(), _TABLET_PORT)


def _save_qr(data, out_dir, prefix, error_correction=qrcode.constants.ERROR_CORRECT_M):
    os.makedirs(out_dir, exist_ok=True)
    filename = "{}_{}.png".format(prefix, int(time.time() * 1000))
    filepath = os.path.join(out_dir, filename)
    qr = qrcode.QRCode(error_correction=error_correction)
    qr.add_data(data)
    qr.make(fit=True)
    qr.make_image().save(filepath)
    rel_dir = os.path.relpath(out_dir, _STATIC_DIR)
    return "/static/{}/{}".format(rel_dir, filename)


# ── proposer_choix ───────────────────────────────────────────────────────────
def _proposer_choix_handler(options):
    push_choices(options)
    return "Boutons tactiles affichés : {}.".format(", ".join(options))


register(
    {
        "name": "proposer_choix",
        "description": (
            "Affiche des boutons tactiles sur la tablette correspondant EXACTEMENT "
            "aux options de la question fermée que tu viens de poser à l'oral — "
            "alternative tactile à une réponse vocale. Appelle TOUJOURS cet outil "
            "juste après avoir posé une question à choix fermé (oui/non, ou un choix "
            "parmi plusieurs options nommées), quel que soit le nombre d'options. "
            "Exemples : question oui/non -> options=['Oui','Non'] ; question sur le "
            "mode de transport -> options=['À pied','En voiture','En transport en "
            "commun','À vélo']. Libellés courts (1-3 mots), correspondant exactement "
            "à ce que l'utilisateur pourrait dire à l'oral."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 4,
                    "description": "2 à 4 libellés courts, un par bouton, dans l'ordre logique.",
                },
            },
            "required": ["options"],
        },
    },
    _proposer_choix_handler,
)


# ── definir_langue_ecran ──────────────────────────────────────────────────────
# Langues traduites côté interface (index.html). Repli 'en' sinon.
_LANG_CODES = {"fr", "en", "es", "de", "it", "ar", "zh", "ja"}
# Quelques alias courants (nom de langue → code) pour être tolérant.
_LANG_ALIASES = {
    "francais": "fr", "français": "fr", "french": "fr",
    "anglais": "en", "english": "en", "inglés": "en",
    "espagnol": "es", "spanish": "es", "español": "es", "espanol": "es",
    "allemand": "de", "german": "de", "deutsch": "de",
    "italien": "it", "italian": "it", "italiano": "it",
    "arabe": "ar", "arabic": "ar",
    "chinois": "zh", "chinese": "zh", "mandarin": "zh",
    "japonais": "ja", "japanese": "ja", "japon": "ja",
}


def _normalize_lang(langue):
    l = (langue or "").strip().lower()
    if l in _LANG_CODES:
        return l
    if l in _LANG_ALIASES:
        return _LANG_ALIASES[l]
    return l[:2] if l[:2] in _LANG_CODES else "en"


def _definir_langue_ecran_handler(langue):
    code = _normalize_lang(langue)
    push_lang(code)
    return "Langue de l'interface réglée sur '{}'.".format(code)


register(
    {
        "name": "definir_langue_ecran",
        "description": (
            "Adapte la langue de l'interface de la tablette à celle du visiteur. "
            "Appelle ce tool dès que tu détectes la langue parlée par l'utilisateur "
            "(ou quand il change de langue), en plus de lui répondre dans cette langue. "
            "Langues gérées : fr, en, es, de, it, ar, zh, ja (repli anglais sinon)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "langue": {
                    "type": "string",
                    "description": "Code de langue (fr, en, es, de, it, ar, zh) ou nom (français, anglais...).",
                },
            },
            "required": ["langue"],
        },
    },
    _definir_langue_ecran_handler,
)


# ── afficher_texte_ecran ──────────────────────────────────────────────────────
_NOTE_PAGE_TEMPLATE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titre}</title>
<style>
  body {{ margin:0; padding:24px; background:#f4f2fb; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1a1a2e; }}
  h1 {{ font-size:1.4em; color:#6c2bd9; margin:0 0 16px; }}
  pre {{ white-space:pre-wrap; word-wrap:break-word; font-family:inherit; font-size:1.05em; line-height:1.5; margin:0; }}
</style></head>
<body><h1>{titre}</h1><pre>{contenu}</pre></body></html>"""


def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Au-delà de cette longueur, l'URL (texte encodé en base64) rend le QR trop
# dense pour être scanné de façon fiable — on tronque proprement (la liste
# complète reste lisible à l'écran du robot de toute façon).
_QR_TEXT_MAX = 900

# Pages publiques n8n : /note-save (POST texte → URL courte) et /note (GET la page).
_N8N_NOTE_URL      = os.environ.get("N8N_NOTE_URL",      "https://n8n.i-interim.com/webhook/note")
_N8N_NOTE_SAVE_URL = os.environ.get("N8N_NOTE_SAVE_URL", "https://n8n.i-interim.com/webhook/note-save")


def _save_text_retrieval_qr(titre, contenu_texte):
    """QR de récupération. Stratégie en 2 temps :
    1) On envoie le texte à n8n qui le STOCKE et renvoie une URL COURTE
       (…/note?id=abcd) → le QR reste petit et scannable quelle que soit la
       longueur de la liste.
    2) Repli si n8n indisponible : on encode le texte en base64 dans l'URL
       (…/note?d=…) — QR plus dense mais fonctionne quand même.
    Au scan, la page n8n propose un bouton natif "Enregistrer dans mes notes"."""
    payload = "{}\n\n{}".format(titre, contenu_texte).strip()

    note_url = None
    try:
        r = requests.post(_N8N_NOTE_SAVE_URL, json={"text": payload}, timeout=6)
        if r.status_code == 200:
            note_url = (r.json() or {}).get("url")
    except Exception:
        note_url = None

    if not note_url:   # repli base64-dans-l'URL (tronqué pour rester scannable)
        p = payload
        if len(p) > _QR_TEXT_MAX:
            p = p[:_QR_TEXT_MAX].rstrip() + "\n[...] (liste complète sur l'écran du robot)"
        b64 = base64.urlsafe_b64encode(p.encode("utf-8")).decode("ascii")
        note_url = "{}?d={}".format(_N8N_NOTE_URL, b64)

    url = _save_qr(note_url, _QR_DIR, "qr", error_correction=qrcode.constants.ERROR_CORRECT_L)
    return url, "link"


def _afficher_texte_ecran_handler(titre, contenu_texte):
    qr_url, qr_mode = _save_text_retrieval_qr(titre, contenu_texte)
    push_display({
        "type": "text", "titre": titre, "contenu": contenu_texte,
        "qr_url": qr_url, "qr_mode": qr_mode,
    })
    return "Texte affiché à l'écran : '{}' (QR de récupération : {}).".format(titre, qr_mode)


register(
    {
        "name": "afficher_texte_ecran",
        "description": (
            "Affiche un texte formaté (titre + contenu) en plein écran sur la "
            "tablette du robot, accompagné d'un QR code permettant de récupérer ce "
            "texte directement sur le téléphone de l'utilisateur. À utiliser "
            "uniquement après confirmation explicite de l'utilisateur, pour une "
            "information textuelle dense (listes, horaires, résultats multiples)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "titre": {"type": "string", "description": "Titre court affiché en haut de l'écran."},
                "contenu_texte": {
                    "type": "string",
                    "description": "Contenu à afficher, déjà mis en forme (retours à la ligne explicites si besoin).",
                },
            },
            "required": ["titre", "contenu_texte"],
        },
    },
    _afficher_texte_ecran_handler,
)


# ── afficher_qr_ecran ─────────────────────────────────────────────────────────
def _afficher_qr_ecran_handler(titre, donnee_a_encoder):
    image_url = _save_qr(donnee_a_encoder, _QR_DIR, "qr")
    push_display({"type": "qr", "titre": titre, "image_url": image_url})
    return "QR code affiché à l'écran : '{}'.".format(titre)


register(
    {
        "name": "afficher_qr_ecran",
        "description": (
            "Génère et affiche un QR code générique en plein écran sur la tablette "
            "du robot (un lien, un texte à récupérer, etc.). À utiliser uniquement "
            "après confirmation explicite de l'utilisateur. Pour un itinéraire entre "
            "deux lieux nommés, préfère afficher_plan_ecran qui montre une vraie carte. "
            "Pour une info qui est fondamentalement du texte à lire, préfère "
            "afficher_texte_ecran."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "titre": {"type": "string", "description": "Titre court affiché au-dessus du QR code."},
                "donnee_a_encoder": {"type": "string", "description": "Donnée à encoder dans le QR code (URL, texte, etc.)."},
            },
            "required": ["titre", "donnee_a_encoder"],
        },
    },
    _afficher_qr_ecran_handler,
)


# ── afficher_plan_ecran ───────────────────────────────────────────────────────
_VALID_MODES = {"driving", "walking", "transit", "bicycling"}
_MODE_TO_ROUTES_API = {"driving": "DRIVE", "walking": "WALK", "transit": "TRANSIT", "bicycling": "BICYCLE"}


def _afficher_plan_ecran_handler(titre, origine, destination, mode):
    if mode not in _VALID_MODES:
        return "[ERREUR] mode invalide '{}' — attendu : {}.".format(mode, ", ".join(sorted(_VALID_MODES)))

    geo_origine = geocode_address(origine)
    if "error" in geo_origine:
        return (
            "[ERREUR] Lieu de départ introuvable : '{}' ({}). "
            "Redemande à l'utilisateur un lieu plus précis (ville, quartier).".format(origine, geo_origine['error'])
        )
    geo_dest = geocode_address(destination)
    if "error" in geo_dest:
        return (
            "[ERREUR] Lieu d'arrivée introuvable : '{}' ({}). "
            "Redemande à l'utilisateur un lieu plus précis (ville, quartier).".format(destination, geo_dest['error'])
        )

    route = compute_route(
        origin_lat=geo_origine["location"]["lat"], origin_lng=geo_origine["location"]["lng"],
        dest_lat=geo_dest["location"]["lat"], dest_lng=geo_dest["location"]["lng"],
        travel_mode=_MODE_TO_ROUTES_API[mode],
    )
    if "error" in route:
        return (
            "[ERREUR] Itinéraire introuvable entre '{}' et '{}' en mode {} ({}). "
            "Redemande un autre mode de transport ou des lieux plus précis.".format(
                origine, destination, mode, route['error'])
        )

    import requests
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    lat_o, lng_o = geo_origine["location"]["lat"], geo_origine["location"]["lng"]
    lat_d, lng_d = geo_dest["location"]["lat"], geo_dest["location"]["lng"]
    static_map = requests.get(
        "https://maps.googleapis.com/maps/api/staticmap",
        params={
            "size": "640x640",
            "markers": ["color:green|label:A|{},{}".format(lat_o, lng_o),
                        "color:red|label:B|{},{}".format(lat_d, lng_d)],
            "key": api_key,
        },
        timeout=8,
    )
    if static_map.status_code != 200:
        return "[ERREUR] Génération de la carte échouée (HTTP {}).".format(static_map.status_code)

    os.makedirs(_PLANS_DIR, exist_ok=True)
    filename = "plan_{}.png".format(int(time.time() * 1000))
    with open(os.path.join(_PLANS_DIR, filename), "wb") as f:
        f.write(static_map.content)

    image_url = "/static/plans/{}".format(filename)
    gmaps_link = (
        "https://www.google.com/maps/dir/?api=1"
        "&origin={},{}&destination={},{}&travelmode={}".format(lat_o, lng_o, lat_d, lng_d, mode)
    )
    qr_url = _save_qr(gmaps_link, _QR_DIR, "qr")

    # Carte INTERACTIVE (zoom/déplacement, tracé de l'itinéraire) via l'API
    # Maps Embed — la tablette a Internet, elle charge l'iframe directement.
    # L'image statique reste envoyée en repli si l'iframe ne charge pas.
    embed_url = (
        "https://www.google.com/maps/embed/v1/directions"
        "?key={}&origin={},{}&destination={},{}&mode={}".format(
            api_key, lat_o, lng_o, lat_d, lng_d, mode)
    )

    push_display({
        "type": "plan", "titre": titre,
        "image_url": image_url, "qr_url": qr_url, "embed_url": embed_url,
    })

    duration_s = route.get("duration", "").rstrip("s") or "?"
    distance_km = round(route.get("distance_meters", 0) / 1000, 1)
    return (
        "Carte affichée à l'écran : '{}' (itinéraire {} → {}, mode={}, {} km, ~{}s).".format(
            titre, origine, destination, mode, distance_km, duration_s)
    )


register(
    {
        "name": "afficher_plan_ecran",
        "description": (
            "Affiche une vraie carte avec deux marqueurs (départ/arrivée), accompagnée "
            "d'un QR code Google Maps navigable. À utiliser uniquement après confirmation "
            "explicite de l'utilisateur, pour un itinéraire entre deux lieux nommés."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "titre": {"type": "string", "description": "Titre court affiché au-dessus de la carte."},
                "origine": {
                    "type": "string",
                    "description": "Point de départ (adresse ou nom de lieu). Si non précisé, utiliser la position connue du robot.",
                },
                "destination": {"type": "string", "description": "Point d'arrivée (adresse ou nom de lieu)."},
                "mode": {
                    "type": "string",
                    "enum": ["driving", "walking", "transit", "bicycling"],
                    "description": (
                        "Mode de transport. Doit TOUJOURS être demandé à l'utilisateur avant "
                        "l'appel si non précisé — ne jamais deviner (driving=voiture, "
                        "walking=à pied, transit=transport en commun, bicycling=vélo)."
                    ),
                },
            },
            "required": ["titre", "origine", "destination", "mode"],
        },
    },
    _afficher_plan_ecran_handler,
)
