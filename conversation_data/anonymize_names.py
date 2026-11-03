"""anonymize_names.py — Produit une copie anonymisée (noms) de l'archive de
conversations, pour l'usage fine-tuning/évaluation décrit dans README.md.

Ne touche JAMAIS transcripts_archive.jsonl (le brut) : lit ce fichier, écrit
transcripts_archive.anon.jsonl à côté. Relançable à tout moment — reconstruit
la sortie en entier à chaque exécution (pas d'écriture incrémentale, le
fichier source reste la seule source de vérité).

Usage :
    python3.8 conversation_data/anonymize_names.py

Ce que ça anonymise : uniquement les NOMS PROPRES DE PERSONNES dans le texte
des tours de conversation (turns[].text), remplacés par [NOM]. Ne touche PAS
les numéros (badge, téléphone) ni les adresses — hors périmètre de cette
demande, à traiter séparément si besoin (voir README.md § Limites).

Méthode : un appel gpt-4o-mini par session (même pattern que le résumé de
mail dans tools/gmail.py). Nécessite OPENAI_API_KEY — donc envoie le texte à
OpenAI. Accepté pour l'instant car les conversations transitent de toute
façon par l'API Realtime d'OpenAI (moteur "openai", actif aujourd'hui). Si le
pipeline local (--engine local, agent_local/) devient la voie de production,
cette étape devra être remplacée par un modèle tournant sur la Thor (Ollama)
pour respecter la promesse "aucune donnée ne sort du robot" — voir
changements/2026-09-23-pipeline-local-pourquoi.md.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from config import OPENAI_API_KEY

_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(_DIR, "transcripts_archive.jsonl")
ANON_PATH = os.path.join(_DIR, "transcripts_archive.anon.jsonl")

_PROMPT = (
    "Tu reçois une conversation entre un visiteur et un robot d'accueil, sous "
    "forme de tours JSON (role/text). Remplace UNIQUEMENT les noms propres de "
    "personnes (prénom, nom de famille, nom complet — visiteur, tiers évoqué, "
    "membre de l'agence) par [NOM], dans le champ \"text\" de chaque tour. Ne "
    "change RIEN d'autre : ni la ponctuation, ni les autres mots, ni les "
    "numéros, ni les noms de lieux/villes/aéroports/entreprises. Si un tour ne "
    "contient aucun nom de personne, renvoie-le identique. Renvoie UNIQUEMENT "
    "le JSON de la liste \"turns\" modifiée, rien d'autre, pas de markdown."
)


def _anonymize_turns(turns):
    if not turns:
        return turns
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer {}".format(OPENAI_API_KEY)},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": json.dumps(turns, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=20,
        )
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        # tolère {"turns": [...]} ou directement [...]
        result = parsed.get("turns", parsed) if isinstance(parsed, dict) else parsed
        if isinstance(result, list) and len(result) == len(turns):
            return result
    except Exception as e:
        print("[ANON] échec sur une session, turns laissés tels quels : {}".format(e))
    return turns


def main():
    if not os.path.exists(RAW_PATH):
        print("[ANON] Rien à anonymiser : {} n'existe pas encore.".format(RAW_PATH))
        return
    n = 0
    with open(RAW_PATH, "r", encoding="utf-8") as src, \
         open(ANON_PATH, "w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            evt["turns"] = _anonymize_turns(evt.get("turns", []))
            dst.write(json.dumps(evt, ensure_ascii=False) + "\n")
            n += 1
    print("[ANON] {} session(s) anonymisée(s) -> {}".format(n, ANON_PATH))


if __name__ == "__main__":
    main()
