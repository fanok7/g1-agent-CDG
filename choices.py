"""
choices.py — Libellés canoniques des boutons tactiles, et leurs traductions.

Ces libellés circulent entre QUATRE endroits qui doivent s'accorder au
caractère près :

  1. le prompt système (config.py) qui dit au LLM quoi passer à proposer_choix ;
  2. la description du schéma du tool (tools/tablet_tools.py), que le modèle lit
     aussi bien que le prompt ;
  3. le réflexe code (agent/events.py) qui pousse les boutons quand le modèle a
     oublié d'appeler le tool ;
  4. la table de traduction de la tablette (templates/index.html), dont les CLÉS
     sont ces libellés français.

Ils étaient auparavant recopiés dans les quatre, et avaient divergé : le schéma
du tool proposait « En voiture / En transport en commun / À vélo » là où les
trois autres disaient « Voiture / Transport / Vélo ». Un modèle suivant son
propre schéma produisait donc des clés absentes de la table — et le bouton
restait en français devant un visiteur anglophone.

Le contrat : le robot envoie TOUJOURS le libellé français canonique, la tablette
l'affiche traduit et renvoie le libellé d'origine au robot (qui le comprend).

Ce module ne doit rien importer : il est chargé par config.py, donc très tôt.
"""

# ── Jeux de boutons canoniques ──────────────────────────────────────────────


TRANSPORT = ["À pied", "Voiture", "Transport", "Vélo"]
"""Mode de déplacement, demandé avant tout itinéraire."""

LANGUE = ["Français", "English"]
"""Choix de langue. Volontairement non traduits : ce sont des endonymes."""


# ── Traductions affichées par la tablette ───────────────────────────────────
# Clé = libellé français canonique ci-dessus. Un libellé absent de cette table
# (options libres d'un proposer_choix, déjà dans la bonne langue) est affiché
# tel quel par la tablette.

TRANSLATIONS = {
    "Oui":       {"fr": "Oui",       "en": "Yes",     "es": "Sí",         "de": "Ja",
                  "it": "Sì",        "ar": "نعم",      "zh": "是",          "ja": "はい"},
    "Non":       {"fr": "Non",       "en": "No",      "es": "No",         "de": "Nein",
                  "it": "No",        "ar": "لا",       "zh": "否",          "ja": "いいえ"},
    "À pied":    {"fr": "À pied",    "en": "On foot", "es": "A pie",      "de": "Zu Fuß",
                  "it": "A piedi",   "ar": "سيرًا",     "zh": "步行",        "ja": "徒歩"},
    "Voiture":   {"fr": "Voiture",   "en": "By car",  "es": "En coche",   "de": "Auto",
                  "it": "In auto",   "ar": "سيارة",    "zh": "开车",        "ja": "車"},
    "Transport": {"fr": "Transport", "en": "Transit", "es": "Transporte", "de": "ÖPNV",
                  "it": "Trasporti", "ar": "مواصلات",  "zh": "公共交通",     "ja": "公共交通"},
    "Vélo":      {"fr": "Vélo",      "en": "By bike", "es": "En bici",    "de": "Fahrrad",
                  "it": "In bici",   "ar": "دراجة",    "zh": "自行车",      "ja": "自転車"},
}


def fmt(options):
    """Rend un jeu de boutons tel qu'il doit apparaître dans un prompt ou une
    description de schéma : ['Oui','Non']. Évite de réécrire la liste à la main
    dans du texte, où une divergence ne se verrait pas."""
    return "[" + ",".join("'{}'".format(o) for o in options) + "]"
