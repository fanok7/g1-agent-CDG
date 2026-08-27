"""tools/body_state_tool.py — proprioception : ce que le robot sait de son propre corps.

Lit rt/lowstate (DDS) via robot/body_state.py : posture IMU, position réelle des
bras, températures moteurs, effort anormal, flags d'erreur, tension du bus.

Utile pour deux choses très différentes :
  - répondre à « dans quelle position tu es ? », « tu vas bien ? », « tu chauffes ? »
  - VÉRIFIER qu'un geste a réellement abouti (execute_gesture est fire-and-forget :
    sans ce tool, l'agent croit toujours avoir salué, même bras bloqué)
"""

from robot import body_state
from tools.registry import register


def _handler(**_kwargs) -> str:
    etat = body_state.read_body_state()
    if 'erreur' in etat:
        return "Je ne reçois aucune information de mon corps : " + etat['erreur']

    lignes = ["Posture : " + etat['posture']]
    lignes.extend(etat['bras'])

    # Effort anormal = quelqu'un tient/bloque un bras, ou le geste force contre un obstacle
    if etat['forces']:
        detail = ", ".join("joint %d (%.1f N·m)" % (j, t) for j, t in etat['forces'])
        lignes.append("Effort anormal sur un bras — quelque chose le bloque ou le "
                      "retient : " + detail)

    if etat['temp_max'] >= body_state._TEMP_CRITIQUE:
        lignes.append("Températures CRITIQUES : %d°C (joints %s)"
                      % (etat['temp_max'], etat['joints_chauds']))
    elif etat['temp_max'] >= body_state._TEMP_CHAUD:
        lignes.append("Ça chauffe un peu : %d°C max (joints %s)"
                      % (etat['temp_max'], etat['joints_chauds']))
    else:
        lignes.append("Températures normales (%d°C max)" % etat['temp_max'])

    if etat['erreurs']:
        lignes.append("MOTEURS EN ERREUR : %s" % etat['erreurs'])

    lignes.append("Tension du bus : %.1f V" % etat['tension'])
    lignes.append("(mesures datant de %.1fs)" % etat['age'])
    return "\n".join(lignes)


register(
    {
        'name': 'etat_de_mon_corps',
        'description': (
            "Lit les capteurs de TON PROPRE corps (articulations, IMU, températures "
            "moteurs, tension). Appelle ce tool quand on te demande dans quelle "
            "position tu es, où sont tes bras, si tu es debout ou penché, si tu "
            "chauffes, si tu vas bien, si tout fonctionne, ou si quelqu'un te tient "
            "le bras. Appelle-le aussi pour vérifier qu'un geste que tu viens de "
            "faire a bien abouti. "
            "Ne réponds JAMAIS de mémoire sur l'état de ton corps — tu n'as aucune "
            "sensation en dehors de ce tool. "
            "Attention : ce tool décrit TON corps, pas ce que tu vois autour de toi "
            "(pour ça, utilise ce_que_je_vois)."
        ),
        'parameters': {'type': 'object', 'properties': {}, 'required': []},
    },
    _handler,
)
