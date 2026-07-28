"""
tools/hotel_tools.py — Recherche d'hôtels près d'un lieu, via un workflow n8n.

Géocodage du lieu via Nominatim (OpenStreetMap, gratuit, pas de clé API),
puis appel du webhook n8n qui fait la vraie recherche d'hôtels (scraping/API
tierce côté n8n, hors de ce projet).
"""

import os

import requests

from tools.registry import register

# TODO: vérifier/mettre à jour cette URL de production n8n si elle change.
N8N_HOTEL_WEBHOOK_URL = os.environ.get(
    "N8N_HOTEL_WEBHOOK_URL", "https://n8n.i-interim.com/webhook/recherche-hotel"
)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "Robot-G1-I-Interim/1.0"

# Ajouté aux réponses "aucun résultat" : la base OpenData IDF est incomplète
# (tous les hôtels d'IDF n'y sont pas). Ce signal indique explicitement au LLM
# d'enchaîner sur recherche_web plutôt que de rester sans réponse.
_WEB_FALLBACK_HINT = (
    " [INFO pour l'assistant : la base hôtels est incomplète — utilise "
    "maintenant recherche_web pour compléter, et ne reste pas sans réponse.]"
)


def _format_hotel(h):
    """Le webhook n8n ne renvoie pas toujours les mêmes champs (ex: 'etoiles'
    peut être une liste ['3 étoiles'] plutôt qu'une chaîne, 'distance'/'prix'
    absents selon la source) — on affiche ce qui est disponible plutôt que
    de planter sur un champ manquant."""
    # Le webhook renvoie parfois un item déjà formaté en simple chaîne
    # (ou une liste de chaînes) plutôt qu'un dict — on l'affiche tel quel.
    if isinstance(h, str):
        return f"- {h}"
    nom = h.get("nom", "Hôtel")
    etoiles = h.get("etoiles")
    if isinstance(etoiles, list):
        etoiles = ", ".join(str(e) for e in etoiles)
    # Champs OpenData IDF affichés : nom, étoiles, ville, adresse, téléphone,
    # site. Le nombre de chambres est volontairement EXCLU (le dataset donne le
    # total physique, pas la dispo — non pertinent pour un visiteur). On affiche
    # tout ce qui est renseigné (ignore "-" et les champs absents).
    details = [
        str(v) for v in (
            etoiles, h.get("ville"), h.get("adresse"),
            h.get("telephone"), h.get("site"),
        ) if v and str(v) != "-"
    ]
    return f"- {nom}" + (f" ({', '.join(details)})" if details else "")


def _geocode(query):
    resp = requests.get(
        _NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": _USER_AGENT},
        timeout=5,
    )
    return resp.json()


def chercher_hotel(localisation, etoiles=None):
    """Géocode `localisation` puis interroge le workflow n8n de recherche
    d'hôtels avec les coordonnées GPS obtenues.

    ⚠️ La source est l'OpenData Île-de-France : seuls les hôtels d'IDF
    (Paris + petite/grande couronne) sont couverts. Ce jeu de données ne
    contient NI prix NI disponibilité de chambres — d'où les 2 seuls filtres
    fiables : la localisation et le nombre d'étoiles."""
    try:
        geo_data = _geocode(localisation)
        if not geo_data:
            geo_data = _geocode(f"{localisation} Ile-de-France")
    except Exception as e:
        return f"Erreur de géocodage : {e}"

    if not geo_data:
        return f"Je ne trouve pas '{localisation}' sur la carte." + _WEB_FALLBACK_HINT

    lat, lon = geo_data[0]["lat"], geo_data[0]["lon"]

    payload = {"lat": lat, "lon": lon}
    if etoiles is not None:
        payload["etoiles"] = etoiles

    try:
        response = requests.post(N8N_HOTEL_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        return f"Erreur technique de recherche : {e}"

    if response.status_code != 200:
        return f"Erreur n8n : {response.status_code}."

    try:
        donnees = response.json()
    except ValueError:
        # Le workflow n8n peut planter silencieusement sur certains filtres
        # (ex: nb_chambres) et renvoyer un corps vide en 200 — sans ce garde,
        # une exception non gérée remontait au LLM qui l'interprétait comme
        # un échec du tool et partait chercher ailleurs (recherche_web).
        return ("Le service de recherche d'hôtels n'a pas pu traiter cette demande "
                "(filtre non supporté ?). Réessaie avec moins de critères, par "
                "exemple sans préciser le nombre de chambres.")
    hotels = donnees.get("hotels")
    if hotels is None:
        return "Réponse vide du service d'hôtels."
    # n8n renvoie une CHAÎNE (message déjà rédigé, ex: "Aucun hôtel trouvé
    # avec ces critères.") quand il n'y a pas de résultat, et une LISTE
    # d'hôtels sinon. Sans ce garde, itérer sur la chaîne parcourait ses
    # lettres et _format_hotel plantait (AttributeError) — le LLM prenait ce
    # crash pour un échec du tool et partait chercher sur le web.
    if isinstance(hotels, str):
        return (hotels.strip() or "Aucun hôtel trouvé dans ce rayon.") + _WEB_FALLBACK_HINT
    if not hotels:
        return "Aucun hôtel trouvé dans ce rayon." + _WEB_FALLBACK_HINT

    lignes = [_format_hotel(h) for h in hotels]
    return "Voici les hôtels trouvés :\n" + "\n".join(lignes)


def _handler(**kwargs):
    return chercher_hotel(**kwargs)


register(
    {
        "name": "chercher_hotel",
        "description": (
            "Recherche des hôtels près d'un lieu donné, avec filtre optionnel par "
            "étoiles. Couvre UNIQUEMENT l'Île-de-France (source OpenData IDF) — ni "
            "prix ni disponibilité de chambres. Appelle ce tool pour toute demande "
            "d'hébergement/hôtel — n'invente jamais un nom d'hôtel ni un prix."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "localisation": {
                    "type": "string",
                    "description": "Lieu de recherche en Île-de-France (ville, quartier, adresse). Obligatoire.",
                },
                "etoiles": {"type": "integer", "description": "Nombre d'étoiles souhaité (ex: 3, 4, 5)."},
            },
            "required": ["localisation"],
        },
    },
    _handler,
)
