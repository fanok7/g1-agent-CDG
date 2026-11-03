import os
import base64
import requests
from email.mime.text import MIMEText
from tools.registry import register
from config import OPENAI_API_KEY

_SUMMARIZE_THRESHOLD = 500  # chars : en dessous → texte entier, au dessus → résumé


def _summarize(text: str) -> str:
    try:
        r = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENAI_API_KEY}'},
            json={
                'model': 'gpt-4o-mini',
                'messages': [
                    {'role': 'system', 'content': 'Résume ce mail en 2-3 phrases courtes en français.'},
                    {'role': 'user', 'content': text[:4000]}
                ],
                'max_tokens': 150,
            },
            timeout=8
        )
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f'(résumé indisponible : {e})'

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    _GOOGLE_AVAILABLE = True
except ImportError:
    _GOOGLE_AVAILABLE = False
    print('[GMAIL] google-api-python-client non installé')

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CREDENTIALS_PATH = os.path.expanduser('~/.gmail_credentials.json')
TOKEN_PATH = os.path.expanduser('~/.gmail_token.json')


def _get_service():
    if not _GOOGLE_AVAILABLE:
        return None
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                'Token Gmail manquant. Lancez d\'abord : python3.8 tools/gmail_setup.py'
            )
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)


def _extract_body(payload):
    """Cherche récursivement la première partie text/plain d'un payload Gmail
    (multipart). Retourne None si aucune n'existe — le sentinel doit rester
    distinct d'une chaîne "trouvée mais vide", sinon une pièce jointe ou une
    partie non-texte rencontrée AVANT la vraie partie texte dans `parts`
    court-circuiterait la boucle avec un texte placeholder au lieu de
    continuer à chercher."""
    mime = payload.get('mimeType', '')
    if mime == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace').strip()
    if 'parts' in payload:
        for part in payload['parts']:
            text = _extract_body(part)
            if text:
                return text
    return None


def _lire_handler(n: int = 5, non_lus: bool = True) -> str:
    try:
        svc = _get_service()
        if not svc:
            return 'Erreur : Gmail non disponible.'
        labels = ['INBOX', 'UNREAD'] if non_lus else ['INBOX']
        results = svc.users().messages().list(
            userId='me', labelIds=labels, maxResults=n
        ).execute()
        messages = results.get('messages', [])
        if not messages:
            return 'Aucun mail non lu.' if non_lus else 'Aucun mail trouvé.'
        lignes = []
        for m in messages:
            msg = svc.users().messages().get(
                userId='me', id=m['id'], format='full'
            ).execute()
            headers = {h['name']: h['value'] for h in msg['payload']['headers']}
            body = _extract_body(msg['payload']) or '(pas de contenu texte)'
            if len(body) > _SUMMARIZE_THRESHOLD:
                contenu = f'[résumé] {_summarize(body)}'
            else:
                contenu = body
            lignes.append(
                f"De: {headers.get('From', '?')} | "
                f"Sujet: {headers.get('Subject', '?')} | "
                f"Date: {headers.get('Date', '?')}\n"
                f"{contenu}"
            )
            svc.users().messages().modify(
                userId='me', id=m['id'],
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
        return '\n'.join(lignes)
    except Exception as e:
        return f'Erreur Gmail : {e}'


def _envoyer_handler(to: str, subject: str, body: str) -> str:
    try:
        svc = _get_service()
        if not svc:
            return 'Erreur : Gmail non disponible.'
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        svc.users().messages().send(userId='me', body={'raw': raw}).execute()
        return f'Mail envoyé à {to}.'
    except Exception as e:
        return f'Erreur envoi Gmail : {e}'


register(
    schema={
        'name': 'lire_emails_gmail',
        'description': 'Lit les derniers mails non lus depuis Gmail.',
        'parameters': {
            'type': 'object',
            'properties': {
                'n':        {'type': 'integer', 'description': 'Nombre de mails à lire (défaut 5)'},
                'non_lus':  {'type': 'boolean', 'description': 'true = non lus seulement (défaut), false = tous les mails de la boîte de réception'}
            },
            'required': []
        }
    },
    handler=_lire_handler
)

register(
    schema={
        'name': 'envoyer_email_gmail',
        'description': 'Envoie un email via Gmail.',
        'parameters': {
            'type': 'object',
            'properties': {
                'to':      {'type': 'string', 'description': 'Adresse destinataire'},
                'subject': {'type': 'string', 'description': 'Objet du mail'},
                'body':    {'type': 'string', 'description': 'Corps du mail'}
            },
            'required': ['to', 'subject', 'body']
        }
    },
    handler=_envoyer_handler
)
