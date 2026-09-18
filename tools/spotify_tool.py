"""
tools/spotify_tool.py — Contrôle Spotify via l'API Web.

Toutes les actions ciblent le device Spotify Connect "G1 Robot" exposé par
librespot (voir robot/spotify_player.py). L'API Web ne transporte PAS l'audio :
elle dit à Spotify quoi jouer sur ce device, et librespot reçoit le flux et
l'envoie au haut-parleur. Cohérent : ce qui joue == ce que le robot diffuse.

Prérequis : compte Premium, token OAuth (tools/spotify_setup.py), et librespot
authentifié sur LE MÊME compte que le token.
"""

import json
import os
import time

import requests

from tools.registry import register

try:
    from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_DEVICE_NAME
except ImportError:
    SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
    SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
    SPOTIFY_DEVICE_NAME = os.environ.get('SPOTIFY_DEVICE_NAME', 'G1 Robot')

TOKEN_PATH = os.path.expanduser('~/.spotify_token.json')
_BASE      = 'https://api.spotify.com/v1'
_MARKET    = 'FR'


def _get_token() -> str:
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            'Token Spotify manquant. Lancez d\'abord : python3.8 tools/spotify_setup.py'
        )
    with open(TOKEN_PATH) as f:
        data = json.load(f)

    if time.time() > data.get('expires_at', 0) - 60:
        r = requests.post('https://accounts.spotify.com/api/token', data={
            'grant_type':    'refresh_token',
            'refresh_token': data['refresh_token'],
        }, auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET), timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f'Refresh token Spotify échoué : {r.status_code} {r.text}')
        new = r.json()
        data['access_token'] = new['access_token']
        data['expires_at']   = time.time() + new['expires_in']
        if 'refresh_token' in new:
            data['refresh_token'] = new['refresh_token']
        with open(TOKEN_PATH, 'w') as f:
            json.dump(data, f)

    return data['access_token']


def _api(method: str, path: str, **kwargs):
    token = _get_token()
    return requests.request(
        method, f'{_BASE}{path}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=10, **kwargs
    )


def _device_id():
    """Retourne l'id du device librespot 'G1 Robot', ou None s'il n'est pas là."""
    r = _api('GET', '/me/player/devices')
    if r.status_code != 200:
        return None
    for d in r.json().get('devices', []):
        if d.get('name') == SPOTIFY_DEVICE_NAME:
            return d.get('id')
    return None


def _no_device_msg() -> str:
    return (f'Le device "{SPOTIFY_DEVICE_NAME}" n\'est pas connecté. '
            'Vérifie que librespot tourne et est authentifié sur le bon compte.')


# ── Handlers ──────────────────────────────────────────────────────────────────

def _jouer_handler(query: str) -> str:
    try:
        dev = _device_id()
        if not dev:
            return _no_device_msg()

        # On cherche artiste + playlist + titre, et on joue TOUJOURS dans un
        # contexte (artiste, playlist ou album), jamais un titre nu. Un titre nu
        # (uris:[...]) crée une file d'attente d'un seul élément → "suivant" ne
        # marche pas et la radio automatique échoue (400 "context not available").
        # Un contexte donne une vraie file + l'enchaînement autoplay.
        r = _api('GET', '/search', params={
            'q': query, 'type': 'artist,playlist,track', 'limit': 3, 'market': _MARKET,
        })
        if r.status_code != 200:
            return f'Erreur recherche Spotify ({r.status_code}).'
        res = r.json()
        q = query.lower().strip()
        artists   = [a for a in res.get('artists', {}).get('items', []) if a]
        playlists = [p for p in res.get('playlists', {}).get('items', []) if p]
        tracks    = [t for t in res.get('tracks', {}).get('items', []) if t]

        body  = None
        label = query

        def _track_body(t):
            album = t.get('album', {})
            arts  = ', '.join(a['name'] for a in t.get('artists', []))
            name  = t.get('name', query)
            if album.get('uri'):   # contexte album → file d'attente + autoplay
                b = {'context_uri': album['uri'], 'offset': {'uri': t['uri']}}
            else:
                b = {'uris': [t['uri']]}
            return b, (f'{name} de {arts}' if arts else name)

        # 1. Un artiste dont le nom correspond → top titres + radio de l'artiste
        for a in artists:
            name = a.get('name', '')
            if name and (name.lower() in q or q in name.lower()):
                body, label = {'context_uri': a['uri']}, name
                break

        # 2. Un titre dont le nom correspond précisément (demande d'un morceau précis)
        if body is None:
            for t in tracks:
                tname = t.get('name', '')
                if tname and (tname.lower() in q or q in tname.lower()):
                    body, label = _track_body(t)
                    break

        # 3. Sinon une playlist (idéal pour un genre / une ambiance / générique)
        if body is None and playlists:
            p = playlists[0]
            body, label = {'context_uri': p['uri']}, p.get('name', query)

        # 4. Dernier recours : le premier titre, dans le contexte de son album
        if body is None and tracks:
            body, label = _track_body(tracks[0])

        if body is None:
            return f'Rien trouvé sur Spotify pour : {query}'

        # Joue sur le device librespot (transfère la lecture au robot)
        pr = _api('PUT', '/me/player/play', params={'device_id': dev}, json=body)
        if pr.status_code in (200, 202, 204):
            return f'Je joue {label} sur le haut-parleur.'
        if pr.status_code == 404:
            return _no_device_msg()
        return f'Erreur lecture Spotify ({pr.status_code}).'
    except Exception as e:
        return f'Erreur Spotify : {e}'


def _en_cours_handler() -> str:
    try:
        r = _api('GET', '/me/player/currently-playing', params={'market': _MARKET})
        if r.status_code == 204 or not r.content:
            return 'Aucune lecture en cours.'
        data = r.json()
        if not data or data.get('item') is None:
            return 'Aucune lecture en cours.'
        item    = data['item']
        title   = item.get('name', '?')
        artists = ', '.join(a['name'] for a in item.get('artists', []))
        album   = item.get('album', {}).get('name', '')
        playing = 'en cours' if data.get('is_playing') else 'en pause'
        return f'{title} — {artists} ({album}), {playing}.'
    except Exception as e:
        return f'Erreur Spotify : {e}'


def _controle_handler(action: str) -> str:
    try:
        dev = _device_id()
        if not dev:
            return _no_device_msg()
        params = {'device_id': dev}
        if action == 'pause':
            r = _api('PUT', '/me/player/pause', params=params)
            return 'Lecture en pause.' if r.status_code in (200, 204) else f'Erreur ({r.status_code}).'
        if action == 'reprendre':
            r = _api('PUT', '/me/player/play', params=params)
            return 'Lecture reprise.' if r.status_code in (200, 204) else f'Erreur ({r.status_code}).'
        if action == 'suivant':
            r = _api('POST', '/me/player/next', params=params)
            return 'Titre suivant.' if r.status_code in (200, 204) else f'Erreur ({r.status_code}).'
        if action == 'precedent':
            r = _api('POST', '/me/player/previous', params=params)
            return 'Titre précédent.' if r.status_code in (200, 204) else f'Erreur ({r.status_code}).'
        return f'Action inconnue : {action}'
    except Exception as e:
        return f'Erreur Spotify : {e}'


def _volume_handler(volume: int) -> str:
    try:
        volume = max(0, min(100, int(volume)))
        # Seule et unique source de vérité pour le volume réel : le gain logiciel
        # local (spotify_player.py, appliqué aux échantillons juste avant le HP).
        # Le volume du device Spotify (API) reste fixé à 100 en permanence —
        # sinon librespot applique AUSSI sa propre atténuation sur le PCM, et les
        # deux effets se cumulent en cascade (quadratique au lieu de linéaire) :
        # demander 50% donnait ~25% du volume, et une simple baisse pouvait
        # sonner comme une coupure quasi totale.
        try:
            from robot import spotify_player
            spotify_player.set_volume(volume)
        except Exception:
            pass
        dev = _device_id()
        params = {'volume_percent': 100}
        if dev:
            params['device_id'] = dev
        r = _api('PUT', '/me/player/volume', params=params)
        if r.status_code in (200, 204):
            return f'Volume réglé à {volume}%.'
        if r.status_code == 404:
            return _no_device_msg()
        return f'Erreur Spotify ({r.status_code}).'
    except Exception as e:
        return f'Erreur Spotify : {e}'


# ── Enregistrement ────────────────────────────────────────────────────────────

register(
    schema={
        'name': 'spotify_jouer',
        'description': 'Recherche un titre, artiste, genre ou ambiance sur Spotify et lance la lecture sur le robot.',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Ce à jouer : artiste, titre, genre, ambiance (ex: "Daft Punk", "jazz", "musique calme")'},
            },
            'required': ['query'],
        },
    },
    handler=_jouer_handler
)

register(
    schema={
        'name': 'spotify_en_cours',
        'description': 'Retourne le titre et l\'artiste en cours de lecture sur Spotify.',
        'parameters': {'type': 'object', 'properties': {}, 'required': []},
    },
    handler=_en_cours_handler
)

register(
    schema={
        'name': 'spotify_controle',
        'description': 'Contrôle la lecture Spotify : pause, reprendre, titre suivant ou précédent.',
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['pause', 'reprendre', 'suivant', 'precedent'],
                    'description': 'Action à effectuer',
                },
            },
            'required': ['action'],
        },
    },
    handler=_controle_handler
)

register(
    schema={
        'name': 'spotify_volume',
        'description': 'Règle le volume de la musique Spotify (0 à 100).',
        'parameters': {
            'type': 'object',
            'properties': {
                'volume': {'type': 'integer', 'description': 'Volume en pourcentage (0-100)'},
            },
            'required': ['volume'],
        },
    },
    handler=_volume_handler
)
