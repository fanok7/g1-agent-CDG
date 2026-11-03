"""
Garde-fou sur les libellés de boutons tactiles.

Ces libellés circulent entre le prompt système, le schéma du tool
proposer_choix, le réflexe code d'agent/events.py et la table de traduction de
la tablette. Ils avaient divergé une fois (le schéma disait « En voiture » là où
les autres disaient « Voiture ») : le bouton restait alors en français devant un
visiteur étranger, sans erreur ni log. Ces tests échouent si la divergence
revient.

    python3.8 -m pytest tests/test_choices.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import choices  # noqa: E402

# Langues servies par l'interface tablette (cf. definir_langue_ecran).
LANGUES_INTERFACE = ("fr", "en", "es", "de", "it", "ar", "zh", "ja")

# Libellés que le CODE pousse de lui-même (réflexes) : ceux-là doivent
# impérativement être traduisibles, le visiteur ne les a pas choisis.
LIBELLES_REFLEXES = choices.TRANSPORT


def test_libelles_reflexes_tous_traduisibles():
    manquants = [l for l in LIBELLES_REFLEXES if l not in choices.TRANSLATIONS]
    assert not manquants, (
        "libellés poussés par le code mais absents de TRANSLATIONS : %s "
        "— ils s'afficheraient en français quelle que soit la langue" % manquants
    )


def test_toutes_les_langues_couvertes():
    for libelle, traductions in choices.TRANSLATIONS.items():
        manquantes = [lg for lg in LANGUES_INTERFACE if lg not in traductions]
        assert not manquantes, (
            "%r n'est pas traduit en %s" % (libelle, manquantes)
        )


def test_prompt_systeme_cite_les_libelles_canoniques():
    """Le prompt doit citer exactement les libellés que le code pousse, sinon le
    modèle en invente d'autres qui ne seront pas traduits."""
    os.environ.setdefault("ROBOT_MODE", "cdg")
    import config

    for libelle in LIBELLES_REFLEXES:
        assert "'%s'" % libelle in config.SYSTEM_PROMPT, (
            "%r absent du prompt système" % libelle
        )


def test_schema_du_tool_cite_les_libelles_canoniques():
    """La description du schéma est lue par le modèle au même titre que le
    prompt : elle ne doit pas proposer de variantes."""
    import tools.tablet_tools  # noqa: F401
    from tools.registry import get_schemas

    schema = next(s for s in get_schemas() if s["name"] == "proposer_choix")
    description = schema["description"]

    for libelle in LIBELLES_REFLEXES:
        assert "'%s'" % libelle in description, (
            "%r absent de la description de proposer_choix" % libelle
        )
