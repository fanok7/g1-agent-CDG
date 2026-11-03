# Archives

Documents de **contexte ponctuel** : passations entre sessions, rapports d'incident,
préparations de démo. Ils décrivent un état passé et ne sont pas tenus à jour — ne pas
s'en servir comme source de vérité. La doc vivante reste à la racine
([CLAUDE.md](../CLAUDE.md), [README.md](../README.md), `THOR.md`, `TABLETTE.md`,
`ASSISTANCE.md`, `DASHBOARD.md`, `STATISTIQUES.md`, `IINTERIM.md`, `INFRASTRUCTURE.md`).

## `thor_sonic/` — chantier SONIC / Jetson Thor (sept-oct 2026)
- `sonic_handoff.md` — passation d'installation SONIC (GR00T-WholeBodyControl) sur PC.
- `DEPLOIEMENT_THOR.md` — guide d'install embarquée sur la Thor (copie de référence :
  `~/g1-stack/docs/DEPLOIEMENT_THOR.md` sur la Thor elle-même).
- `THOR_WORK_CONTEXT.md` — contexte d'une session de travail en SSH sur la Thor.
- `INCIDENT_SONIC_REPONSE_ORIN.md` — incident du 2026-10-28 (blessure, pas d'E-stop
  matériel) et vérifications côté Orin NX. **À lire avant toute reprise de SONIC.**
- `WLA_INSPIRE_CONTRACT.md` — contrat du serveur UnifoLM-WLA, variante mains Inspire ;
  référencé par les scripts `scripts/wla_*.py` et `scripts/inspire_hands_diagnostic.py`.

## `sessions/` — préparations et protocoles de test
- `DEMO_CDG.md` — préparation de la démo CDG du 10 août 2026.
- `SIMULATION_VISITEUR.md` — protocole de simulation visiteur ↔ agent `iinterim`
  (résultats du run : `changements/2026-09-24-simulation-iinterim.md`).

## `code_bak/` — sauvegardes horodatées de fichiers source
Copies `*.py.bak.<date>` de `tablet_server/server.py`, `tools/visitor_notify_tool.py` et
`Ecran_de_samy/samy_agenda_server.py`. Conservées parce que les deux derniers ne sont pas
suivis par git : ces copies sont leur seul historique. Ignorées par git.
