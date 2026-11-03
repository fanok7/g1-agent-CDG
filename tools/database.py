import os
from config import SUPABASE_URL, SUPABASE_KEY
from tools.registry import register

# Les deux recherches ci-dessous encadrent déjà le terme dans '%...%' — on ne
# veut PAS que le LLM (ou un texte injecté via la tablette, voir § sécurité de
# l'audit) puisse lui-même fournir des méta-caractères ILIKE ('%'/'_') : un nom
# ou numéro de badge réduit à '%' correspondrait alors à "n'importe quoi" et
# renverrait des dossiers arbitraires (date de naissance, email...) au lieu
# d'"aucun résultat". Une longueur minimale évite aussi qu'une seule lettre
# renvoie une liste large de dossiers d'inconnus.
_MIN_LEN = 2


def _terme_valide(valeur) -> bool:
    v = (valeur or '').strip()
    return len(v) >= _MIN_LEN and '%' not in v and '_' not in v

try:
    from supabase import create_client
    _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    test = _supabase.table('formation').select('id').limit(1).execute()
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print(f'[SUPABASE] Connexion OK — {len(test.data)} ligne(s) de test')
except Exception as e:
    print(f'[SUPABASE] ERREUR connexion : {e}')
    _supabase = None


def _handler(nom: str) -> str:
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print(f'[SUPABASE] Recherche nom="{nom}"')
    if _supabase is None:
        return 'Erreur : base de données non connectée.'
    if not _terme_valide(nom):
        return 'Veuillez fournir un nom de famille valide.'
    try:
        result = _supabase.table('formation') \
            .select('nom, prenom, fpi, fphi, typo, certif, carte_pro, badge_date_expiration') \
            .ilike('nom', f'%{nom}%') \
            .limit(3) \
            .execute()
        if not result.data:
            return f'Aucune personne trouvée pour le nom "{nom}".'
        lignes = []
        for c in result.data:
            lignes.append(
                f"{c.get('prenom','')} {c.get('nom','')} — "
                f"Type: {c.get('typo') or 'non renseigné'} — "
                f"FPI: {c.get('fpi') or 'non planifié'} — "
                f"FPHI: {c.get('fphi') or 'non planifié'} — "
                f"Certif: {c.get('certif') or 'non planifié'} — "
                f"Badge expire: {c.get('badge_date_expiration') or 'aucun'}"
            )
        return '\n'.join(lignes)
    except Exception as e:
        print(f'[SUPABASE] ERREUR : {e}')
        return f'Erreur base de données : {e}'


register(
    schema={
        'name': 'chercher_formation',
        'description': 'Recherche les infos de formation d\'une personne par son nom de famille.',
        'parameters': {
            'type': 'object',
            'properties': {'nom': {'type': 'string'}},
            'required': ['nom']
        }
    },
    handler=_handler
)


def _handler_badge(nom: str = None, numero_badge: str = None) -> str:
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print(f'[SUPABASE] Recherche badge nom="{nom}" numero="{numero_badge}"')
    if _supabase is None:
        return 'Erreur : base de données non connectée.'
    if not numero_badge and not _terme_valide(nom):
        return 'Veuillez fournir un nom de famille valide ou un numéro de badge.'
    if numero_badge and not _terme_valide(numero_badge):
        return 'Veuillez fournir un numéro de badge valide.'
    try:
        query = _supabase.table('badges_corsur') \
            .select('nom, prenom, "dateNaissance", email, "numeroDemande", "numeroBadge", etat, "dateSoumission", "dateFinAccordee", "numAA", entreprise')
        if numero_badge:
            query = query.ilike('"numeroBadge"', f'%{numero_badge}%')
        else:
            query = query.ilike('nom', f'%{nom}%')
        result = query.limit(3).execute()
        if not result.data:
            critere = numero_badge if numero_badge else nom
            return f'Aucun badge trouvé pour "{critere}".'
        lignes = []
        for c in result.data:
            lignes.append(
                f"{c.get('prenom', '')} {c.get('nom', '')} — "
                f"Entreprise: {c.get('entreprise') or 'non renseignée'} — "
                f"N° badge: {c.get('numeroBadge') or 'non attribué'} — "
                f"N° demande: {c.get('numeroDemande') or 'non renseigné'} — "
                f"État: {c.get('etat') or 'inconnu'} — "
                f"Fin accordée: {c.get('dateFinAccordee') or 'non renseignée'} — "
                f"N° AA: {c.get('numAA') or 'non renseigné'}"
            )
        return '\n'.join(lignes)
    except Exception as e:
        print(f'[SUPABASE] ERREUR badge : {e}')
        return f'Erreur base de données : {e}'


register(
    schema={
        'name': 'chercher_badge',
        'description': (
            'Recherche le dossier de badge aéroportuaire (CORSUR) d\'une personne par son nom de famille. '
            'Retourne le numéro de badge, l\'état de la demande, la date de fin accordée, l\'entreprise et le numéro AA.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'nom': {'type': 'string', 'description': 'Nom de famille (ex: Dupont)'},
                'numero_badge': {'type': 'string', 'description': 'Numéro de badge CORSUR (ex: 123456)'}
            },
            'required': []
        }
    },
    handler=_handler_badge
)
