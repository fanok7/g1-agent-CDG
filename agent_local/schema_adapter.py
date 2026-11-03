"""agent_local/schema_adapter.py — restreint et reformate les schémas de tools
pour le pipeline local (vLLM, API compatible OpenAI).

Le registry (tools/registry.py) expose déjà tous les tools au format OpenAI
function-calling PLAT ({'name','description','parameters'}) — c'est le même
format que consomme agent/session.py pour le Realtime API. L'API OpenAI
(et donc vLLM, qui l'implémente) attend le même contenu mais IMBRIQUÉ sous
'function' ({'type':'function','function':{...}}). Ce module ne réécrit aucun
tool : il relit tools.registry.get_schemas() et se contente de filtrer +
reformater, exactement comme session.py fait déjà
`tools = [] if TRANSLATE_MODE else schemas` pour le mode interprète.

Portée v1 du pipeline local (squelette) : seulement les 3 tools nécessaires à
l'arbre de décision visiteur + gestes + heure. Élargir cette liste au fur et à
mesure que d'autres tools sont validés sur le pipeline local.
"""

from tools.registry import get_schemas

LOCAL_TOOL_WHITELIST = {
    'executer_geste',
    'relacher_bras',
    'date_heure_actuelle',
}


def get_tool_schemas() -> list:
    """Sous-ensemble whitelisté de tools.registry.get_schemas(), au format
    tool-calling OpenAI/vLLM (nested sous 'function')."""
    out = []
    for schema in get_schemas():
        name = schema.get('name')
        if name not in LOCAL_TOOL_WHITELIST:
            continue
        out.append({
            'type': 'function',
            'function': {
                'name': name,
                'description': schema.get('description', ''),
                'parameters': schema.get('parameters', {}),
            },
        })
    return out
