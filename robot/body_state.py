"""
Proprioception — lecture seule de rt/lowstate (DDS).

Souscripteur INDÉPENDANT de arm_sdk : on ne veut surtout pas instancier le
publisher rt/arm_sdk juste pour lire l'état (ça arme le contrôleur bras).
Plusieurs souscripteurs DDS sur un même topic cohabitent sans problème.

S'initialise paresseusement après hardware.init() (ChannelFactoryInitialize).
"""
import math
import time
import threading

from unitree_sdk2py.core.channel import ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

# Indices moteurs G1 29 DOF (identiques à ceux de arm_sdk._ARM_JOINTS)
JOINTS = {
    'jambe_gauche':  [0, 1, 2, 3, 4, 5],
    'jambe_droite':  [6, 7, 8, 9, 10, 11],
    'taille':        [12, 13, 14],
    'bras_gauche':   [15, 16, 17, 18, 19, 20, 21],
    'bras_droit':    [22, 23, 24, 25, 26, 27, 28],
}
_N_JOINTS = 29

# Indices nommés utilisés pour l'interprétation de la posture des bras
_L_SH_PITCH, _L_SH_ROLL, _L_ELBOW = 15, 16, 18
_R_SH_PITCH, _R_SH_ROLL, _R_ELBOW = 22, 23, 25

# Seuils d'interprétation
_TEMP_CHAUD    = 60     # °C — au-delà : moteur qui chauffe
_TEMP_CRITIQUE = 80     # °C
_TAU_BLOQUE    = 8.0    # N·m sur un joint de BRAS — au repos on mesure < 3
_INCLINAISON   = 0.35   # rad (~20°) — au-delà : le robot penche
_CHUTE         = 1.0    # rad (~57°) — au-delà : probablement au sol
_STALE         = 2.0    # s — au-delà, l'état est périmé

_SUB_TIMEOUT   = 5.0    # s d'attente du premier message au démarrage


class _Reader:
    def __init__(self):
        self._state = None
        self._ts    = 0.0
        self._lock  = threading.Lock()
        self._ready = False
        self._init_lock = threading.Lock()

    def _on_state(self, msg):
        with self._lock:
            self._state = msg
            self._ts    = time.time()

    def _init_once(self):
        with self._init_lock:
            if self._ready:
                return
            sub = ChannelSubscriber("rt/lowstate", LowState_)
            sub.Init(self._on_state, 10)
            self._sub = sub          # garder la référence (sinon GC → plus de callback)
            deadline = time.time() + _SUB_TIMEOUT
            while time.time() < deadline:
                with self._lock:
                    if self._state is not None:
                        break
                time.sleep(0.05)
            self._ready = True

    def snapshot(self):
        """(msg, age_secondes) ou (None, None) si aucun état reçu."""
        self._init_once()
        with self._lock:
            if self._state is None:
                return None, None
            return self._state, time.time() - self._ts


_reader = _Reader()


# ── Interprétation ────────────────────────────────────────────────────────────
def _deg(rad):
    return round(math.degrees(rad))


def _decrire_bras(q, sh_pitch, sh_roll, elbow, cote):
    """Description en langage naturel d'un bras à partir de 3 angles."""
    pitch, roll, elb = q[sh_pitch], q[sh_roll], q[elbow]
    # ShoulderPitch négatif = bras vers l'avant/haut (cf. POSES dans arm_sdk.py)
    if pitch < -0.5:
        pos = "levé vers l'avant"
    elif pitch > 0.8:
        pos = "tiré vers l'arrière"
    elif abs(roll) > 0.5:
        pos = "écarté sur le côté"
    else:
        pos = "le long du corps"
    coude = "plié" if elb > 0.6 else "tendu"
    return "%s : %s, coude %s (épaule %+d°/%+d°, coude %+d°)" % (
        cote, pos, coude, _deg(pitch), _deg(roll), _deg(elb))


def _posture(imu):
    roll, pitch = imu.rpy[0], imu.rpy[1]
    if abs(roll) > _CHUTE or abs(pitch) > _CHUTE:
        etat = "COUCHÉ / AU SOL"
    elif abs(roll) > _INCLINAISON or abs(pitch) > _INCLINAISON:
        etat = "penché"
    else:
        etat = "debout, droit"
    return "%s (roulis %+d°, tangage %+d°, cap %+d°)" % (
        etat, _deg(roll), _deg(pitch), _deg(imu.rpy[2]))


def read_body_state():
    """Retourne un dict structuré de l'état corporel, ou {'erreur': ...}."""
    msg, age = _reader.snapshot()
    if msg is None:
        return {'erreur': "aucune donnée sur rt/lowstate — robot éteint, "
                          "câble eth0 débranché ou hardware.init() non appelé"}
    if age > _STALE:
        return {'erreur': "état périmé (%.1fs) — le robot ne publie plus" % age}

    ms = msg.motor_state
    q  = [ms[j].q for j in range(_N_JOINTS)]

    # Températures : max par groupe (chaque moteur expose 2 sondes)
    temps = {}
    for groupe, idxs in JOINTS.items():
        temps[groupe] = max(max(ms[j].temperature) for j in idxs)
    t_max = max(temps.values())
    j_chauds = [j for j in range(_N_JOINTS)
                if max(ms[j].temperature) >= _TEMP_CHAUD]

    # Effort anormal : uniquement sur les BRAS. Les jambes portent le poids du
    # robot (couple élevé normal au genou) → un seuil unique ferait des faux positifs.
    bras_idx = JOINTS['bras_gauche'] + JOINTS['bras_droit']
    forces = [(j, ms[j].tau_est) for j in bras_idx
              if abs(ms[j].tau_est) > _TAU_BLOQUE]

    erreurs = [j for j in range(_N_JOINTS) if ms[j].motorstate != 0]

    return {
        'posture':      _posture(msg.imu_state),
        'bras': [
            _decrire_bras(q, _L_SH_PITCH, _L_SH_ROLL, _L_ELBOW, "Bras gauche"),
            _decrire_bras(q, _R_SH_PITCH, _R_SH_ROLL, _R_ELBOW, "Bras droit"),
        ],
        'temp_max':     t_max,
        'temp_groupes': temps,
        'joints_chauds': j_chauds,
        'forces':       forces,
        'erreurs':      erreurs,
        'tension':      round(ms[0].vol, 1),
        'age':          round(age, 2),
        'angles_rad':   q,
    }


def arm_angles():
    """Angles bras+taille (17 valeurs, ordre arm_sdk) ou None. Utile pour
    vérifier après coup qu'un geste a bougé les bras."""
    msg, age = _reader.snapshot()
    if msg is None or age > _STALE:
        return None
    order = JOINTS['bras_gauche'] + JOINTS['bras_droit'] + JOINTS['taille']
    return [msg.motor_state[j].q for j in order]
