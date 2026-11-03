"""agent_local/validator.py — garde-fou post-génération pour le pipeline local.

Un petit modèle local (Qwen3-8B) est plus sensible à l'injection/jailbreak
conversationnel qu'un modèle cloud, et le prompt seul ("jamais de didascalie",
"jamais de jargon" dans config._REGLES_TOOLS) n'est pas une garantie
mécanique. Ce module vérifie, APRÈS génération et AVANT synthèse vocale, que
la réponse texte ne contient rien de hors périmètre.

Pas de schema JSON forcé ici (contrairement au premier essai avec Ollama) :
testé empiriquement, un format JSON forcé empêche vLLM d'émettre des
tool_calls sur ce serveur — les gestes passent par le tool executer_geste
(exécuté en effet de bord dans agent_local/vllm_client.py::run_turn), pas par
un champ de réponse structurée. Ce module valide donc juste le texte parlé.
"""

import re
from typing import Optional

# Didascalie / action entre parenthèses non vide — tout ce que le modèle
# écrit est lu à voix haute (règle _REGLES_TOOLS), une parenthèse d'action
# ("(sourit)", "(fait un geste)"...) ne doit jamais passer.
_DIDASCALIE_RE = re.compile(r'\([^)]+\)')

# Jargon technique que le personnage ne doit jamais prononcer à l'oral
# (_REGLES_TOOLS : "jamais de jargon (API, JSON, caméra...)").
_JARGON_WORDS = (
    'api', 'json', 'caméra', 'camera', 'gpu', 'cuda', 'vllm', 'modèle de langage',
    'llm', 'prompt', 'system prompt', 'jetson', 'modbus', 'websocket', 'token',
    'intelligence artificielle', "en tant qu'ia", 'en tant que modèle',
)

# Marqueurs classiques d'injection/jailbreak conversationnel — si le modèle
# les RÉCITE dans sa réponse (plutôt que de les ignorer), c'est le signe
# qu'il a été détourné par l'entrée utilisateur.
_INJECTION_MARKERS = (
    'ignore les instructions précédentes', 'ignore previous instructions',
    'tu es maintenant', 'you are now', 'nouveau system prompt',
    'system:', '[system]', 'oublie tes instructions', 'disregard previous',
)


def _contains_banned(text: str) -> Optional[str]:
    low = text.lower()
    if _DIDASCALIE_RE.search(text):
        return 'didascalie entre parenthèses'
    for w in _JARGON_WORDS:
        if w in low:
            return f'jargon technique ({w!r})'
    for m in _INJECTION_MARKERS:
        if m in low:
            return f'marqueur d\'injection ({m!r})'
    return None


def validate(raw_content: str) -> Optional[str]:
    """Valide une réponse texte brute du modèle. Retourne le texte nettoyé
    (strip) si valide, sinon None (l'appelant décide du retry / repli)."""
    if not isinstance(raw_content, str):
        return None
    text = raw_content.strip()
    if not text:
        return None
    if _contains_banned(text):
        return None
    return text


# Réponse de repli si même le retry échoue — même philosophie que
# "erreur tool → information indisponible" (_REGLES_TOOLS).
FALLBACK_REPLY = "Désolé, pouvez-vous répéter votre question ?"
