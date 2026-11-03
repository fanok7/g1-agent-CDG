"""scripts/test_vllm_pipeline.py — test texte du pipeline local (vLLM).

Test TEXTE uniquement (pas d'audio — indépendant de l'ASR/VAD/TTS) : envoie
des échanges scriptés au serveur vLLM via agent_local.vllm_client.run_turn
(code réutilisé tel quel, aucune logique dupliquée) et affiche latence,
conformité de contenu (agent_local.validator) et si le tool attendu a bien
été appelé.

Chaque cas est un mini-échange à plusieurs tours (pas un prompt isolé) pour
exercer les branches de l'arbre de décision visiteur (config.py, prompt
IINTERIM) qui dépendent des réponses précédentes.

Usage (depuis la racine du repo) :
    python3.8 scripts/test_vllm_pipeline.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Doit s'exécuter AVANT le premier `import config` (comme dans main.py) :
# config.py lit ROBOT_MODE à l'import pour choisir SYSTEM_PROMPT, et
# build_instructions() (agent/session.py) s'appuie dessus.
os.environ.setdefault('ROBOT_MODE', 'iinterim')

import config
from agent.session import build_instructions
from agent_local import validator
from agent_local.vllm_client import run_turn
from agent_local.schema_adapter import get_tool_schemas

CASES = [
    {'name': 'salutation', 'turns': ["Bonjour"], 'expect_tool': None},
    {'name': 'heure', 'turns': ["Quelle heure est-il ?"],
     'expect_tool': 'date_heure_actuelle'},
    {'name': 'poignee_de_main', 'turns': ["Est-ce que tu peux me serrer la main ?"],
     'expect_tool': 'executer_geste'},
    {'name': 'rdv_sans_documents',
     'turns': ["J'ai rendez-vous", "Non, je n'ai pas mes documents sur moi"],
     'expect_tool': None},
    {'name': 'filiere_hors_perimetre',
     'turns': ["Non, je n'ai pas rendez-vous", "Non, je ne suis pas inscrit",
               "Je cherche un poste dans le BTP"],
     'expect_tool': None},
    {'name': 'filiere_valide_diplomes_ok',
     'turns': ["Non", "Non", "Je cherche une mission en sûreté aéroportuaire",
               "Oui, ma carte pro est à jour"],
     'expect_tool': None},
]


def _run_case(turns: list) -> dict:
    tools = get_tool_schemas()
    messages = [{'role': 'system', 'content': build_instructions()}]
    all_tool_calls = []
    reponse = None
    content = ''
    t0 = time.time()
    for turn in turns:
        messages.append({'role': 'user', 'content': turn})
        content, tool_trace, _lat = run_turn(messages, model=config.VLLM_MODEL, tools=tools)
        all_tool_calls += tool_trace
        reponse = validator.validate(content)
        messages.append({'role': 'assistant', 'content': reponse or content})
    return {
        'ok': reponse is not None,
        'reponse': reponse or f'<INVALIDE: {content[:120]}>',
        'tools_called': all_tool_calls,
        'latency_s': time.time() - t0,
    }


def main():
    rows = []
    for case in CASES:
        try:
            result = _run_case(case['turns'])
        except Exception as e:
            rows.append((case['name'], 'ERREUR', str(e), '', ''))
            continue
        expect = case['expect_tool']
        tool_ok = (expect is None) or (expect in result['tools_called'])
        rows.append((
            case['name'],
            f"{result['latency_s']:.2f}s",
            'oui' if result['ok'] else 'NON',
            'oui' if tool_ok else f"NON (attendu {expect}, eu {result['tools_called']})",
            result['reponse'][:80],
        ))

    header = ('cas', 'latence', 'ok', 'tool_ok', 'reponse')
    widths = [max(len(str(r[i])) for r in ([header] + rows)) for i in range(len(header))]

    def _fmt(row):
        return ' | '.join(str(c).ljust(w) for c, w in zip(row, widths))

    print(_fmt(header))
    print('-+-'.join('-' * w for w in widths))
    for row in rows:
        print(_fmt(row))


if __name__ == '__main__':
    main()
