"""
calendar_tool.py — Tools Google Calendar
=========================================
Accède au Google Calendar via OAuth2 (token stocké dans ~/.google_calendar_token.json).

Quatre tools :
  - agenda_du_jour         : tous les événements d'aujourd'hui
  - prochain_rendez_vous   : les N prochains événements (aujourd'hui + jours suivants)
  - chercher_rdv_personne  : cherche un RDV par nom/prénom (recherche dans les titres)
  - rdv_creneau            : trouve les RDV à un horaire précis (ex: "14h30")
"""

import os
import re
import warnings
from datetime import datetime, timezone, timedelta

import pytz

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from tools.registry import register

_TOKEN_FILE = os.path.expanduser("~/.google_calendar_token.json")
_PARIS      = pytz.timezone("Europe/Paris")
_SCOPES     = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS  = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre"]


def _get_service():
    if not os.path.exists(_TOKEN_FILE):
        raise FileNotFoundError(f"Token absent : {_TOKEN_FILE}")
    # Ne pas valider les scopes ici : on utilise ceux stockés dans le token.
    # Pour obtenir le scope calendar.events (écriture), relancer calendar_setup.py.
    creds = Credentials.from_authorized_user_file(_TOKEN_FILE)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _has_write_scope() -> bool:
    """True si le token courant inclut calendar.events (droits d'écriture)."""
    if not os.path.exists(_TOKEN_FILE):
        return False
    import json as _json
    with open(_TOKEN_FILE) as f:
        data = _json.load(f)
    scopes = data.get("scopes") or data.get("scope", "")
    if isinstance(scopes, list):
        scopes = " ".join(scopes)
    return "calendar.events" in scopes or "calendar/feeds" in scopes


def _fmt_event(event: dict) -> str:
    """Format court pour agenda_du_jour : heure + nom + poste (si disponible)."""
    start = event.get("start", {})
    end   = event.get("end", {})
    title = event.get("summary", "Sans titre")

    info  = _parse_description(event.get("description", ""))
    nom   = info.get("nom") or _extract_name(title)

    if "date" in start:
        d    = datetime.strptime(start["date"], "%Y-%m-%d")
        jour = _JOURS[d.weekday()]
        mois = _MOIS[d.month - 1]
        base = f"Toute la journée ({jour} {d.day} {mois})"
    else:
        dt_s = datetime.fromisoformat(start["dateTime"]).astimezone(_PARIS)
        dt_e = datetime.fromisoformat(end["dateTime"]).astimezone(_PARIS)
        hs   = f"{dt_s.hour}h{dt_s.minute:02d}" if dt_s.minute else f"{dt_s.hour}h"
        he   = f"{dt_e.hour}h{dt_e.minute:02d}" if dt_e.minute else f"{dt_e.hour}h"
        base = f"{hs} – {he}"

    parts = [f"{base} : {nom}"]
    if info.get("poste"):
        parts.append(f"Poste : {info['poste'].strip()}")
    if info.get("tel"):
        parts.append(f"Tél : {info['tel']}")
    if info.get("email"):
        parts.append(f"Email : {info['email']}")
    if info.get("documents"):
        parts.append(f"Documents : {', '.join(info['documents'])}")
    return " | ".join(parts)


def _extract_name(title: str) -> str:
    """Extrait le nom entre parenthèses : 'RDV ... (Jean Dupont)' → 'Jean Dupont'."""
    m = re.search(r'\(([^)]+)\)', title)
    return m.group(1).strip() if m else title


def _parse_description(desc: str) -> dict:
    """Parse la description HTML Google Calendar d'un RDV I-Interim.
    Structure attendue :
      <b>Réservé par</b>\\nNOM\\nEMAIL\\nTEL\\n<br>
      <b>Poste souhaité</b>\\nPOSTE\\n<br>
      <b>..DOCUMENTS..</b>...  (boilerplate ignoré)
    Retourne dict avec clés : nom, email, tel, poste (chaînes vides si absent)."""
    if not desc:
        return {}
    # Supprimer les balises HTML, garder les \n comme séparateurs
    text = re.sub(r'<br\s*/?>', '\n', desc, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\xa0', ' ')
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lower().startswith('réservé par'):
            # Les 3 lignes suivantes : nom, email, tel
            if i + 1 < len(lines):
                result['nom']   = lines[i + 1]
            if i + 2 < len(lines) and '@' in lines[i + 2]:
                result['email'] = lines[i + 2]
                if i + 3 < len(lines) and re.match(r'[\d\s\+\-\.]{8,}', lines[i + 3]):
                    result['tel'] = lines[i + 3]
            elif i + 2 < len(lines) and re.match(r'[\d\s\+\-\.]{8,}', lines[i + 2]):
                result['tel'] = lines[i + 2]
            i += 4
        elif line.lower().startswith('poste souhaité'):
            if i + 1 < len(lines):
                result['poste'] = lines[i + 1]
            i += 2
        elif 'documents' in line.lower() and 'apporter' in line.lower():
            # Capture les items de la liste documents (li)
            docs = []
            i += 1
            while i < len(lines):
                l = lines[i]
                if l.lower().startswith('téléphone'):
                    break
                docs.append(l.lstrip('- •').strip())
                i += 1
            if docs:
                result['documents'] = docs
        elif 'téléphone:' in line.lower():
            break
        else:
            i += 1
    return result


def _fmt_event_detail(event: dict) -> str:
    """Format complet d'un événement : horaire + nom candidat + poste + tel."""
    start = event.get("start", {})
    end   = event.get("end", {})
    title = event.get("summary", "Sans titre")

    if "date" in start:
        d    = datetime.strptime(start["date"], "%Y-%m-%d")
        jour = _JOURS[d.weekday()]
        mois = _MOIS[d.month - 1]
        base = f"Toute la journée ({jour} {d.day} {mois})"
    else:
        dt_s = datetime.fromisoformat(start["dateTime"]).astimezone(_PARIS)
        dt_e = datetime.fromisoformat(end["dateTime"]).astimezone(_PARIS)
        hs   = f"{dt_s.hour}h{dt_s.minute:02d}" if dt_s.minute else f"{dt_s.hour}h"
        he   = f"{dt_e.hour}h{dt_e.minute:02d}" if dt_e.minute else f"{dt_e.hour}h"
        jour = _JOURS[dt_s.weekday()]
        mois = _MOIS[dt_s.month - 1]
        base = f"{jour} {dt_s.day} {mois}, {hs} – {he}"

    nom_cal = _extract_name(title)
    info    = _parse_description(event.get("description", ""))

    parts = [base, f"Candidat : {info.get('nom', nom_cal)}"]
    if info.get('poste'):
        parts.append(f"Poste : {info['poste'].strip()}")
    if info.get('tel'):
        parts.append(f"Tél : {info['tel']}")
    if info.get('email'):
        parts.append(f"Email : {info['email']}")
    if info.get('documents'):
        parts.append(f"Documents : {', '.join(info['documents'])}")
    return " | ".join(parts)


def _fetch_range(service, time_min: datetime, time_max: datetime, max_results: int = 50):
    result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


# ── Tool 1 : agenda du jour ───────────────────────────────────────────────────

def _handler_agenda(date: str = "", **_kwargs) -> str:
    try:
        service = _get_service()
    except Exception as e:
        return f"Impossible d'accéder au calendrier : {e}"

    if date and date.strip():
        try:
            ref = _PARIS.localize(datetime.strptime(date.strip(), "%Y-%m-%d"))
        except ValueError:
            return f"Format de date invalide : '{date}'. Utilise YYYY-MM-DD (ex: 2026-06-25)."
    else:
        ref = datetime.now(_PARIS)

    dmin = ref.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    dmax = ref.replace(hour=23, minute=59, second=59, microsecond=0)
    label_jour = f"{_JOURS[dmin.weekday()]} {dmin.day} {_MOIS[dmin.month-1]}"

    try:
        items = _fetch_range(service, dmin, dmax)
    except Exception as e:
        return f"Erreur API Google Calendar : {e}"

    if not items:
        return f"Aucun événement le {label_jour}."

    lines = [f"Agenda du {label_jour} ({len(items)} rendez-vous) :"]
    for ev in items:
        lines.append("• " + _fmt_event(ev))
    return "\n".join(lines)


# ── Tool 2 : prochains rendez-vous ───────────────────────────────────────────

def _handler_prochain(nombre: int = 5, **_kwargs) -> str:
    try:
        nombre = max(1, min(int(nombre), 20))
    except (TypeError, ValueError):
        nombre = 5

    try:
        service = _get_service()
    except Exception as e:
        return f"Impossible d'accéder au calendrier : {e}"

    now_utc = datetime.now(timezone.utc)
    end_utc = now_utc + timedelta(days=30)

    try:
        items = _fetch_range(service, now_utc, end_utc, max_results=nombre)
    except Exception as e:
        return f"Erreur API Google Calendar : {e}"

    if not items:
        return "Aucun événement à venir dans le calendrier."

    lines = [f"Prochains rendez-vous ({len(items)}) :"]
    for ev in items:
        lines.append("• " + _fmt_event(ev))
    return "\n".join(lines)


# ── Tool 3 : chercher par nom / prénom ───────────────────────────────────────

_OWNER_NAMES = {"samy", "i-interim", "iinterim"}  # noms du propriétaire du calendrier — jamais cherchés comme candidat


def _is_phone(s: str) -> bool:
    """True si la chaîne contient exactement 10 chiffres (numéro FR valide)."""
    digits = re.sub(r'[\s\+\-\.]', '', s.strip())
    return len(digits) == 10 and digits.isdigit()


def _handler_chercher(nom: str = "", **_kwargs) -> str:
    """Cherche un RDV par nom/prénom OU numéro de téléphone.
    Utilise un fuzzy matching (rapidfuzz) pour tolérer les erreurs de transcription
    phonétique (ex: 'mary' → 'marie', 'dupond' → 'dupont').
    """
    if not nom or not nom.strip():
        return "Paramètre 'nom' requis."
    query = nom.strip().lower()

    # Bloquer la recherche sur le nom du propriétaire du calendrier
    if query in _OWNER_NAMES:
        return "Merci de préciser le nom du visiteur, pas celui de l'agence."

    is_tel = _is_phone(nom)
    query_digits = re.sub(r'\D', '', nom) if is_tel else ""

    try:
        service = _get_service()
    except Exception as e:
        return f"Impossible d'accéder au calendrier : {e}"

    now  = datetime.now(_PARIS)
    dmin = now.replace(hour=0, minute=0, second=0, microsecond=0)
    dmax = dmin + timedelta(days=30)

    try:
        items = _fetch_range(service, dmin, dmax, max_results=50)
    except Exception as e:
        return f"Erreur API Google Calendar : {e}"

    # ── Recherche par téléphone (exacte) ──────────────────────────────────────
    if is_tel:
        found = []
        for ev in items:
            desc_info  = _parse_description(ev.get("description", ""))
            tel_digits = re.sub(r'\D', '', desc_info.get("tel", ""))
            raw_digits = re.sub(r'\D', '', ev.get("description", "") + ev.get("summary", ""))
            if query_digits and (query_digits in tel_digits or query_digits in raw_digits):
                found.append((ev, 100))
        if not found:
            return "Aucun rendez-vous trouvé pour ce téléphone dans les 30 prochains jours."
        lines = [f"{len(found)} rendez-vous trouvé(s) :"]
        for ev, _ in found:
            lines.append("• " + _fmt_event_detail(ev))
        return "\n".join(lines)

    # ── Recherche par nom/prénom avec fuzzy matching ───────────────────────────
    try:
        from rapidfuzz import fuzz
        USE_FUZZY = True
    except ImportError:
        USE_FUZZY = False

    # Seuil de similarité : 75 = tolère 1-2 caractères différents
    # ex: mary/marie → ~85, dupond/dupont → ~92, jean/john → ~75
    FUZZY_THRESHOLD = 75

    scored = []
    for ev in items:
        desc_info  = _parse_description(ev.get("description", ""))
        nom_event  = _extract_name(ev.get("summary", "")).lower()
        nom_desc   = desc_info.get("nom", "").lower()

        # Tokens : on découpe pour matcher prénom ou nom séparément
        # ex: query="mary" sur "marie dupont" → on teste chaque token
        tokens_event = nom_event.split() + nom_desc.split()
        tokens_query = query.split()

        if USE_FUZZY:
            # Score max entre :
            #   - matching exact partiel (query contenu dans le nom complet)
            #   - token_sort_ratio (insensible à l'ordre prénom/nom)
            #   - max token-to-token (mary vs marie, dupond vs dupont)
            scores = []

            # 1. Matching exact partiel (query substring du nom complet)
            if query in nom_event or query in nom_desc:
                scores.append(100)

            # 2. Ratio global sur le nom complet
            for candidate in [nom_event, nom_desc]:
                if candidate:
                    scores.append(fuzz.token_sort_ratio(query, candidate))

            # 3. Matching token à token (chaque mot du query vs chaque token du nom)
            for qt in tokens_query:
                for nt in tokens_event:
                    if nt:
                        scores.append(fuzz.ratio(qt, nt))

            score = max(scores) if scores else 0
        else:
            # Fallback sans rapidfuzz : matching exact partiel uniquement
            score = 100 if (query in nom_event or query in nom_desc) else 0

        if score >= FUZZY_THRESHOLD:
            scored.append((ev, score))

    # Trier par score décroissant
    scored.sort(key=lambda x: x[1], reverse=True)

    if not scored:
        return "Aucun rendez-vous trouvé pour ce nom dans les 30 prochains jours."

    lines = [f"{len(scored)} rendez-vous trouvé(s) :"]
    for ev, score in scored:
        detail = _fmt_event_detail(ev)
        lines.append("• " + detail)
    return "\n".join(lines)


# ── Tool 4 : RDV à un créneau précis ─────────────────────────────────────────

def _parse_heure(heure_str: str):
    """Parse '14h30', '14:30', '14h', '14' → (heure, minute) ou None."""
    s = heure_str.strip().replace("h", ":").replace("H", ":")
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, IndexError):
        pass
    return None


def _handler_creneau(heure: str = "", **_kwargs) -> str:
    """Retourne les RDV qui commencent dans la fenêtre [heure-15min, heure+15min] aujourd'hui."""
    if not heure or not heure.strip():
        return "Paramètre 'heure' requis (ex: '14h30' ou '14:30')."

    parsed = _parse_heure(heure)
    if parsed is None:
        return f"Format d'heure non reconnu : « {heure} ». Utilise '14h30' ou '14:30'."
    h, m = parsed

    try:
        service = _get_service()
    except Exception as e:
        return f"Impossible d'accéder au calendrier : {e}"

    now   = datetime.now(_PARIS)
    pivot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    dmin  = pivot - timedelta(minutes=15)
    dmax  = pivot + timedelta(minutes=15)

    try:
        items = _fetch_range(service, dmin, dmax, max_results=10)
    except Exception as e:
        return f"Erreur API Google Calendar : {e}"

    if not items:
        heure_fmt = f"{h}h{m:02d}" if m else f"{h}h"
        return f"Aucun rendez-vous autour de {heure_fmt} aujourd'hui (±15 min)."

    heure_label = f"{h}h{m:02d}" if m else f"{h}h"
    lines = [f"Rendez-vous autour de {heure_label} :"]
    for ev in items:
        lines.append("• " + _fmt_event_detail(ev))
    return "\n".join(lines)


# ── Enregistrement ────────────────────────────────────────────────────────────

register(
    {
        "name": "agenda_du_jour",
        "description": (
            "Retourne tous les rendez-vous du calendrier Google pour un jour donné. "
            "Sans paramètre = aujourd'hui. "
            "Pour demain, après-demain ou n'importe quelle date : passe le paramètre date au format YYYY-MM-DD. "
            "À appeler pour : 'mon planning', 'mes RDV aujourd'hui', 'agenda de demain', "
            "'qui vient jeudi ?', 'qu'est-ce que j'ai le 25 ?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date au format YYYY-MM-DD. Omis = aujourd'hui. Ex: '2026-06-25' pour demain.",
                },
            },
            "required": [],
        },
    },
    _handler_agenda,
)

register(
    {
        "name": "prochain_rendez_vous",
        "description": (
            "Retourne les prochains rendez-vous du calendrier (aujourd'hui et jours suivants). "
            "À appeler pour : 'mon prochain RDV', 'cette semaine', 'mes prochaines réunions'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nombre": {"type": "integer",
                           "description": "Nombre d'événements à retourner (défaut 5, max 20)."},
            },
            "required": [],
        },
    },
    _handler_prochain,
)

register(
    {
        "name": "chercher_rdv_personne",
        "description": (
            "Cherche un rendez-vous dans le calendrier par nom/prénom OU numéro de téléphone du candidat. "
            "À appeler dès qu'un visiteur dit 'j'ai rendez-vous', 'je suis [prénom]', 'j'ai un RDV ici'. "
            "Ne jamais passer 'Samy' comme nom — c'est le propriétaire du calendrier, pas un candidat. "
            "Si la recherche par nom échoue (prénom mal entendu), rappeler ce tool avec le numéro de téléphone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nom": {"type": "string",
                        "description": "Nom, prénom (partiel ok) OU numéro de téléphone du candidat."},
            },
            "required": ["nom"],
        },
    },
    _handler_chercher,
)

register(
    {
        "name": "rdv_creneau",
        "description": (
            "Retourne le ou les rendez-vous prévus à un horaire précis aujourd'hui (±15 min). "
            "À appeler pour : 'qui est prévu à 14h30 ?', 'quel RDV à 11h ?', "
            "'qui vient à cette heure-là ?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "heure": {"type": "string",
                          "description": "Heure du créneau, format '14h30', '14:30' ou '14h'."},
            },
            "required": ["heure"],
        },
    },
    _handler_creneau,
)


# ── Tool 5 : créneaux disponibles ────────────────────────────────────────────

_DOCS_BOILERPLATE = (
    "<b><u><i>DOCUMENTS À APPORTER POUR CANDIDATURE :</i></u></b>"
    "<br><ol>"
    "<li>\xa0Se munir d'un CV actualisé à la date du jour</li>"
    "<li>\xa0Certifications professionnelles / Diplômes ou tout autre documents "
    "mettant en valeur votre candidature</li>"
    "<li>Certificat de Travail</li>"
    "<li>Extrait de casier judiciaire de moins de 3 mois</li>"
    "</ol><br>Téléphone: 01 84 25 44 90"
)


# Planning de disponibilité de Samy par jour de la semaine (0=lundi … 6=dimanche).
# Chaque entrée est une liste de plages (h_debut, h_fin) autorisées.
_SAMY_HOURS = {
    0: [(9, 12), (14, 18)],   # lundi
    1: [(14, 18)],             # mardi  — après-midi uniquement
    2: [(9, 12), (14, 18)],   # mercredi
    3: [(9, 12)],              # jeudi  — matin uniquement
    4: [(9, 12)],              # vendredi — matin uniquement
    # samedi (5) et dimanche (6) absents = fermé
}


def _allowed_slots(ref) -> list:
    """Retourne tous les quarts d'heure autorisés selon _SAMY_HOURS pour ce jour."""
    plages = _SAMY_HOURS.get(ref.weekday(), [])
    slots = []
    for h_start, h_end in plages:
        t = ref.replace(hour=h_start, minute=0, second=0, microsecond=0)
        end = ref.replace(hour=h_end,  minute=0, second=0, microsecond=0)
        while t < end:
            slots.append((t.hour, t.minute))
            t += timedelta(minutes=15)
    return slots


def _free_slots(service, date_iso: str, n: int = 8) -> list:
    """Retourne les n premiers créneaux libres respectant le planning de Samy."""
    try:
        ref = _PARIS.localize(datetime.strptime(date_iso, "%Y-%m-%d"))
    except ValueError:
        return []

    allowed = _allowed_slots(ref)
    if not allowed:
        return []  # jour fermé (week-end)

    # Fenêtre de requête = première plage debut → dernière plage fin
    h_first, m_first = allowed[0]
    h_last,  m_last  = allowed[-1]
    dmin = ref.replace(hour=h_first, minute=m_first, second=0, microsecond=0)
    dmax = ref.replace(hour=h_last,  minute=m_last,  second=0, microsecond=0) + timedelta(minutes=15)

    # Si c'est aujourd'hui, ignorer les créneaux déjà passés (+ 30 min de marge)
    now = datetime.now(_PARIS)
    if ref.date() == now.date():
        minutes_ahead = (now.minute // 15 + 3) * 15
        cutoff = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes_ahead)
        allowed = [(h, m) for (h, m) in allowed
                   if ref.replace(hour=h, minute=m, second=0, microsecond=0) >= cutoff]

    items = _fetch_range(service, dmin, dmax)
    busy = set()
    for ev in items:
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end",   {}).get("dateTime")
        if s and e:
            dt_s = datetime.fromisoformat(s).astimezone(_PARIS)
            dt_e = datetime.fromisoformat(e).astimezone(_PARIS)
            t = dt_s.replace(second=0, microsecond=0)
            t = t.replace(minute=(t.minute // 15) * 15)
            while t < dt_e:
                busy.add((t.hour, t.minute))
                t += timedelta(minutes=15)
        elif ev.get("start", {}).get("date"):
            busy.update(allowed)  # événement toute la journée = tout bloqué

    free = []
    for (h, m) in allowed:
        if len(free) >= n:
            break
        if (h, m) not in busy:
            free.append(f"{h}h{m:02d}" if m else f"{h}h")
    return free


def _handler_creneaux(date: str = "", **_kwargs) -> str:
    try:
        service = _get_service()
    except Exception as e:
        return f"Impossible d'accéder au calendrier : {e}"

    now = datetime.now(_PARIS)
    if date and date.strip():
        date_iso = date.strip()
        try:
            ref = _PARIS.localize(datetime.strptime(date_iso, "%Y-%m-%d"))
        except ValueError:
            return f"Format de date invalide : '{date}'. Utilise YYYY-MM-DD."
    else:
        # Cherche le prochain jour ouvré à partir de demain
        ref = now + timedelta(days=1)
        while ref.weekday() not in _SAMY_HOURS:
            ref += timedelta(days=1)
        date_iso = ref.strftime("%Y-%m-%d")

    label = f"{_JOURS[ref.weekday()]} {ref.day} {_MOIS[ref.month - 1]}"

    if ref.weekday() not in _SAMY_HOURS:
        return f"Samy n'est pas disponible le {label} (week-end ou jour fermé)."

    slots = _free_slots(service, date_iso)
    if not slots:
        return f"Aucun créneau disponible le {label} (agenda complet ou hors horaires)."
    return f"Créneaux disponibles le {label} : {', '.join(slots)}."


register(
    {
        "name": "creneaux_disponibles",
        "description": (
            "Retourne les créneaux libres de 15 minutes pour une date donnée (défaut : demain). "
            "À appeler pour proposer des horaires à un candidat qui souhaite prendre rendez-vous."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date au format YYYY-MM-DD. Omis = demain.",
                },
            },
            "required": [],
        },
    },
    _handler_creneaux,
)


# ── Tool 6 : créer un rendez-vous ─────────────────────────────────────────────

def _handler_creer_rdv(nom: str = "", tel: str = "", email: str = "",
                       poste: str = "", date: str = "", heure: str = "",
                       **_kwargs) -> str:
    if not _has_write_scope():
        return (
            "Le token Google Calendar ne permet pas encore la création d'événements. "
            "Relance scripts/calendar_setup.py sur le robot pour obtenir les droits d'écriture."
        )
    for field, val in [("nom", nom), ("date", date), ("heure", heure)]:
        if not val or not str(val).strip():
            return f"Paramètre '{field}' manquant."

    parsed = _parse_heure(heure)
    if parsed is None:
        return f"Format d'heure invalide : '{heure}'. Ex: '10h', '10h30', '10:30'."
    h, m = parsed

    try:
        ref = _PARIS.localize(datetime.strptime(date.strip(), "%Y-%m-%d"))
    except ValueError:
        return f"Format de date invalide : '{date}'. Utilise YYYY-MM-DD."

    debut  = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    fin    = debut + timedelta(minutes=15)
    nom_c  = nom.strip()

    description = (
        f"<b>Réservé par</b>\n{nom_c}\n{email.strip()}\n{tel.strip()}\n"
        f"<br><b>Poste souhaité</b>\n{poste.strip()}\n<br>"
        + _DOCS_BOILERPLATE
    )
    summary = f"Rendez Vous Samy I-Interim 15 rue des immeubles industriels ({nom_c})"

    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": debut.isoformat(), "timeZone": "Europe/Paris"},
        "end":   {"dateTime": fin.isoformat(),   "timeZone": "Europe/Paris"},
        "location": "15 Rue des immeubles Industriels, 75011 Paris, France",
    }

    try:
        service = _get_service()
        created = service.events().insert(calendarId="primary", body=event_body).execute()
    except Exception as e:
        return f"Erreur lors de la création du RDV : {e}"

    hs = f"{debut.hour}h{debut.minute:02d}" if debut.minute else f"{debut.hour}h"
    jour_label = f"{_JOURS[debut.weekday()]} {debut.day} {_MOIS[debut.month - 1]}"
    return (
        f"Rendez-vous créé : {nom_c} le {jour_label} à {hs}. "
        f"Poste : {poste.strip() or 'non précisé'}. "
        f"ID événement : {created.get('id', '?')}."
    )


# ARCHIVÉ (2026-08-13) : la prise de RDV se fait désormais en self-service par
# le candidat via le QR Google Calendar (tools/tablet_tools.py::afficher_qr_rdv_ecran)
# plutôt que par le robot qui écrivait directement dans l'agenda de Samy.
# _handler_creer_rdv reste défini plus haut (code intact) — décommenter ce
# register() pour réactiver le tool si le besoin revient.
# register(
#     {
#         "name": "creer_rdv",
#         "description": (
#             "Crée un rendez-vous de 15 minutes dans le Google Agenda de Samy. "
#             "À appeler après avoir obtenu le consentement du candidat sur le créneau choisi. "
#             "Requiert : nom, date (YYYY-MM-DD), heure (ex: '10h30'). "
#             "Optionnel : tel, email, poste."
#         ),
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "nom":   {"type": "string", "description": "Nom complet du candidat."},
#                 "tel":   {"type": "string", "description": "Numéro de téléphone."},
#                 "email": {"type": "string", "description": "Adresse email."},
#                 "poste": {"type": "string", "description": "Poste souhaité (ex: Agent d'accueil aéroportuaire)."},
#                 "date":  {"type": "string", "description": "Date au format YYYY-MM-DD."},
#                 "heure": {"type": "string", "description": "Heure de début, ex: '10h', '10h30', '10:30'."},
#             },
#             "required": ["nom", "date", "heure"],
#         },
#     },
#     _handler_creer_rdv,
# )
