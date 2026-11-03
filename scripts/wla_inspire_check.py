"""
scripts/wla_inspire_check.py — Correspondance mains Inspire ↔ fig6d du modèle UnifoLM-WLA
(point 2 de archives/thor_sonic/WLA_INSPIRE_CONTRACT.md § 8). Hors boucle agent.

Convention du dataset (= service DDS officiel Unitree, dfx_inspire_service/include/inspire.h) :
registre ANGLE (consigne 1486, mesure 1546) / 1000, 0 = fermé, 1 = ouvert,
ordre natif par main [pinky, ring, middle, index, thumb_bend, thumb_rot].
⚠️ ≠ registre POS (1474/1534) utilisé par robot/hand_control.py : sens inverse, 0 = ouvert.

Usage :
    python3.8 scripts/wla_inspire_check.py                        # lecture seule (ne bouge rien)
    python3.8 scripts/wla_inspire_check.py --test index --side left
        → ouvre la main, ferme UN doigt à 0.2, affiche quel canal fig6d bouge, rouvre.
    python3.8 scripts/wla_inspire_check.py --test thumb_rot --value 0 --side left
        → posture pouce rot = 0 (seule valeur vue à l'entraînement, contrat § 4.2).
"""

import argparse
import time

from pymodbus.client import ModbusTcpClient

HAND_IPS = {'left': '192.168.123.210', 'right': '192.168.123.211'}  # = robot/hand_control.py
PORT = 6000
REG_ANGLE_SET = 1486
REG_ANGLE_ACT = 1546
NATIVE = ['pinky', 'ring', 'middle', 'index', 'thumb_bend', 'thumb_rot']
FIG6D = ['thumb_oc', 'thumb_lat', 'index', 'middle', 'ring', 'little']
# Contrat § 4.2 (corrigé 2026-10-01), par main : lecture fig6d = n[[4,5,3,2,1,0]],
# commande n = fig6d[[5,4,3,2,0,1]]. Vérifié physiquement : index natif (3) → fig6d index (2).
FIG6D_IDX = [4, 5, 3, 2, 1, 0]
NATIVE_IDX = [5, 4, 3, 2, 0, 1]

# Butée basse de thumb_rot mesurée le 2026-10-02 (registre ANGLE, 0 = fermé) —
# voir scripts/inspire_hands_diagnostic.py pour le relevé complet.
# Main DROITE : course complète jusqu'à 0, courant nul.
# Main GAUCHE : butée mécanique dure à 222, reproductible à 4 essais et insensible à
# la vitesse. Sous 250 le courant passe à 1620 (> limite 1400) → LOCKED_ROTOR +
# OVER_CURRENT, et le moteur reste **verrouillé** jusqu'à un CLEAR_ERROR explicite.
# Course libre (courant nul) au-dessus de 400 ; frottement ~400 mA entre 350 et 250.
# On sature donc à 0.40 : le modèle commande thumb_lat ≈ 0 en permanence, et 0.25
# tiendrait mais en tirant du courant en continu.
THUMB_ROT_MIN = {'left': 0.40, 'right': 0.0}

# Flexion du pouce DROIT : actionneur mort (voir diagnostic). Aucune commande n'a
# d'effet, sur ANGLE comme sur POS, et le courant reste à 8 au lieu de ~1600 en butée.
DEAD_ACTUATORS = {'right': ['thumb_bend']}


def _kw():
    # pymodbus 3.6 (python3.8) : slave= ; pymodbus ≥ 3.10 (miniconda) : device_id=
    import inspect
    sig = inspect.signature(ModbusTcpClient.read_holding_registers)
    return {'device_id': 1} if 'device_id' in sig.parameters else {'slave': 1}


def read_native(client):
    r = client.read_holding_registers(REG_ANGLE_ACT, count=6, **_kw())
    if r.isError():
        raise RuntimeError(r)
    return [v / 1000.0 for v in r.registers]


def write_native(client, q):
    client.write_registers(REG_ANGLE_SET, [max(0, min(1000, int(v * 1000))) for v in q], **_kw())


def to_fig6d(native):
    """Lecture main → modèle."""
    return [native[i] for i in FIG6D_IDX]


def to_native(fig6d, side=None):
    """Modèle → commande main. Sature thumb_rot à la butée mesurée si `side` est donné."""
    n = [fig6d[i] for i in NATIVE_IDX]
    if side is not None:
        n[5] = max(THUMB_ROT_MIN[side], n[5])
    return n


def fmt(names, vals):
    return '  '.join('%s=%.3f' % (n, v) for n, v in zip(names, vals))


def connect(side):
    c = ModbusTcpClient(HAND_IPS[side], port=PORT, timeout=2)
    if not c.connect():
        raise SystemExit('Connexion KO : main %s (%s)' % (side, HAND_IPS[side]))
    return c


def show(side, client):
    nat = read_native(client)
    print('[%s %s] natif  : %s' % (side, HAND_IPS[side], fmt(NATIVE, nat)))
    print('[%s %s] fig6d  : %s' % (side, HAND_IPS[side], fmt(FIG6D, to_fig6d(nat))))
    return nat


def test_finger(side, finger, value, hold):
    c = connect(side)
    try:
        print('Ouverture complète de la main %s…' % side)
        write_native(c, [1.0] * 6)
        time.sleep(2.0)
        before = to_fig6d(show(side, c))
        q = [1.0] * 6
        q[NATIVE.index(finger)] = value
        print('%s seul → natif=%.2f, maintenu %.0f s…' % (finger, value, hold))
        write_native(c, q)
        time.sleep(hold)
        after = to_fig6d(show(side, c))
        moved = [(n, b - a) for n, b, a in zip(FIG6D, before, after) if abs(b - a) > 0.1]
        print('Canaux fig6d qui ont bougé :', moved or 'aucun')
        print("→ Regarde le robot : c'est bien la main %s, et c'est bien le doigt %s ?" % (side, finger))
    finally:
        write_native(c, [1.0] * 6)
        c.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--test', choices=NATIVE, help='ferme ce doigt seul (fait BOUGER la main)')
    p.add_argument('--side', choices=list(HAND_IPS), default='left')
    p.add_argument('--value', type=float, default=0.2, help='cible natif du doigt testé (0 = fermé)')
    p.add_argument('--hold', type=float, default=4.0, help='durée de maintien (s), le temps de regarder')
    a = p.parse_args()
    if a.test:
        test_finger(a.side, a.test, a.value, a.hold)
        return
    for side in ('left', 'right'):
        c = connect(side)
        try:
            show(side, c)
        finally:
            c.close()


if __name__ == '__main__':
    main()
