"""
scripts/inspire_hands_diagnostic.py — Bilan complet des deux mains Inspire RH56E2

Teste chaque actionneur isolément (les 5 autres doigts restent ouverts) et rapporte :
course réellement atteinte, erreur de suivi, courant, défauts, température, capteurs
de force et paume tactile.

Travaille sur le registre **ANGLE** (0 = fermé, 1000 = ouvert), celui du service DDS
officiel d'Unitree et du dataset WLA — voir archives/thor_sonic/WLA_INSPIRE_CONTRACT.md § 4.2.
⚠️ robot/hand_control.py utilise POS (sens inverse) : ne pas comparer les chiffres.

Sécurité : s'arrête sur le premier défaut d'un doigt, le ramène en butée ouverte, et
efface l'erreur avant de passer au suivant. Ne lance pas ça pendant que main.py tourne
(hand_idle écrit dans les mêmes registres).

Usage :
    python3.8 scripts/inspire_hands_diagnostic.py                 # les deux mains
    python3.8 scripts/inspire_hands_diagnostic.py --side right    # une seule
    python3.8 scripts/inspire_hands_diagnostic.py --no-move       # lecture seule
"""

import argparse
import struct
import time

from pymodbus.client import ModbusTcpClient

HAND_IPS = {'left': '192.168.123.210', 'right': '192.168.123.211'}
PORT = 6000

R_CLEAR_ERROR = 1004
R_CURRENT_LIMIT = 1020
R_ANGLE_SET = 1486
R_FORCE_SET = 1498
R_SPEED_SET = 1522
R_POS_ACT = 1534
R_ANGLE_ACT = 1546
R_FORCE_ACT = 1582
R_CURRENT = 1594
R_ERROR = 1606
R_STATUS = 1612
R_TEMP = 1618
R_PALM = 4900
PALM_COUNT = 112

NATIVE = ['pinky', 'ring', 'middle', 'index', 'thumb_bend', 'thumb_rot']
OPEN = 1000
TARGETS = [1000, 700, 400, 200, 50, 0]

STATUS_NAMES = {0: 'ouverture', 1: 'fermeture', 2: 'cible atteinte', 3: 'force atteinte',
                5: 'protection courant', 6: 'rotor bloque', 7: 'DEFAUT', 255: 'repos'}
ERROR_BITS = [(0x01, 'rotor bloque'), (0x02, 'surchauffe'), (0x04, 'surcourant'),
              (0x08, 'fonctionnement anormal'), (0x10, 'erreur comm')]

# Courant au-delà duquel on considère que le moteur pousse en butée.
STALL_CURRENT = 1000
# Courant en dessous duquel le moteur ne tente visiblement rien.
IDLE_CURRENT = 50


def err_text(code):
    if not code:
        return ''
    return '+'.join(n for b, n in ERROR_BITS if code & b) or ('0x%02X' % code)


class Hand:
    def __init__(self, side):
        self.side = side
        self.ip = HAND_IPS[side]
        self.c = ModbusTcpClient(self.ip, port=PORT, timeout=2)
        if not self.c.connect():
            raise SystemExit('Connexion KO : %s (%s)' % (side, self.ip))
        import inspect
        sig = inspect.signature(ModbusTcpClient.read_holding_registers)
        self.kw = {'device_id': 1} if 'device_id' in sig.parameters else {'slave': 1}

    def close(self):
        self.c.close()

    def rd(self, reg, n):
        r = self.c.read_holding_registers(reg, count=n, **self.kw)
        if r.isError():
            raise RuntimeError('lecture %d : %s' % (reg, r))
        return list(r.registers)

    def wr(self, reg, vals):
        self.c.write_registers(reg, vals, **self.kw)

    def bytes6(self, reg):
        """Les registres ERROR/STATUS/TEMP tiennent 1 octet par doigt, 2 par registre."""
        out = []
        for v in self.rd(reg, 3):
            out += [v & 0xFF, (v >> 8) & 0xFF]
        return out

    def signed6(self, reg):
        regs = self.rd(reg, 6)
        return list(struct.unpack('>' + 'h' * 6, struct.pack('>' + 'H' * 6, *regs)))

    def angles(self):
        return self.rd(R_ANGLE_ACT, 6)

    def clear_error(self):
        self.wr(R_CLEAR_ERROR, [1])
        time.sleep(0.3)

    def open_all(self, settle=2.0):
        self.wr(R_ANGLE_SET, [OPEN] * 6)
        time.sleep(settle)

    def settle(self, idx, target, timeout=2.5):
        """Attend l'immobilisation, retourne (angle, courant, status, erreur)."""
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            time.sleep(0.35)
            a = self.angles()[idx]
            cur = self.rd(R_CURRENT, 6)[idx]
            e = self.bytes6(R_ERROR)[idx]
            s = self.bytes6(R_STATUS)[idx]
            if e or s == 7:
                return a, cur, s, e
            if last is not None and abs(a - last) <= 2 and abs(a - target) <= 3:
                return a, cur, s, e
            last = a
        return a, cur, s, e


def read_only_report(h):
    print('\n=== main %s (%s) — etat courant ===' % (h.side, h.ip))
    ang, pos = h.angles(), h.rd(R_POS_ACT, 6)
    cur, force = h.rd(R_CURRENT, 6), h.signed6(R_FORCE_ACT)
    err, sta, temp = h.bytes6(R_ERROR), h.bytes6(R_STATUS), h.bytes6(R_TEMP)
    print('%-11s %6s %6s %8s %8s %6s %-18s %s' %
          ('doigt', 'ANGLE', 'POS', 'courant', 'force_g', 'T(C)', 'status', 'erreur'))
    for i, n in enumerate(NATIVE):
        print('%-11s %6d %6d %8d %8d %6d %-18s %s' %
              (n, ang[i], pos[i], cur[i], force[i], temp[i],
               STATUS_NAMES.get(sta[i], sta[i]), err_text(err[i]) or '-'))
    lim = h.rd(R_CURRENT_LIMIT, 6)
    print('limites : force=%s  vitesse=%s  courant=%s' %
          (h.rd(R_FORCE_SET, 6), h.rd(R_SPEED_SET, 6), lim))
    try:
        palm = h.rd(R_PALM, PALM_COUNT)
        nz = [v for v in palm if v > 0]
        print('paume tactile : %d/%d points actifs, total=%d, max=%d' %
              (len(nz), PALM_COUNT, sum(palm), max(palm)))
    except RuntimeError as e:
        print('paume tactile : illisible (%s)' % e)


def sweep_finger(h, idx):
    """Balaye un doigt seul, les autres ouverts. Retourne le verdict."""
    name = NATIVE[idx]
    h.clear_error()
    h.open_all()
    rows, reached, faulted = [], [], None
    for target in TARGETS:
        q = [OPEN] * 6
        q[idx] = target
        h.wr(R_ANGLE_SET, q)
        a, cur, s, e = h.settle(idx, target)
        rows.append((target, a, a - target, cur, s, e))
        if e or s == 7:
            faulted = target
            break
        if abs(a - target) <= 15:
            reached.append(target)
    h.wr(R_ANGLE_SET, [OPEN] * 6)
    time.sleep(1.5)
    back = h.angles()[idx]
    h.clear_error()

    print('\n--- %s / %s' % (h.side, name))
    print('  %8s %8s %8s %8s  %-18s %s' % ('consigne', 'angle', 'ecart', 'courant', 'status', 'erreur'))
    for target, a, d, cur, s, e in rows:
        print('  %8d %8d %+8d %8d  %-18s %s' %
              (target, a, d, cur, STATUS_NAMES.get(s, s), err_text(e) or '-'))
    print('  retour ouvert : %d' % back)

    moved = max(abs(r[1] - rows[0][1]) for r in rows) if rows else 0
    maxcur = max(r[3] for r in rows) if rows else 0
    if moved < 20 and maxcur < IDLE_CURRENT:
        verdict = 'MORT — ne bouge pas, aucun courant'
    elif faulted is not None:
        verdict = 'butee a %d (defaut sous cette consigne)' % faulted
    elif reached and min(reached) == 0:
        verdict = 'OK — course complete 0..1000'
    elif reached:
        verdict = 'partiel — mini atteint %d' % min(reached)
    else:
        verdict = 'ne suit pas la consigne'
    if back < OPEN - 30:
        verdict += ' ; ne revient pas en butee ouverte (%d)' % back
    print('  VERDICT : %s' % verdict)
    return name, verdict, maxcur


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--side', choices=list(HAND_IPS) + ['both'], default='both')
    p.add_argument('--no-move', action='store_true', help='lecture seule')
    a = p.parse_args()
    sides = list(HAND_IPS) if a.side == 'both' else [a.side]

    summary = {}
    for side in sides:
        h = Hand(side)
        try:
            read_only_report(h)
            if a.no_move:
                continue
            t0 = h.bytes6(R_TEMP)
            res = [sweep_finger(h, i) for i in range(6)]
            summary[side] = res
            print('\n  temperature %s : avant=%s apres=%s' % (side, t0, h.bytes6(R_TEMP)))
            h.open_all()
            h.clear_error()
        finally:
            h.close()

    if summary:
        print('\n' + '=' * 72)
        print('BILAN')
        for side, res in summary.items():
            print('\nmain %s :' % side)
            for name, verdict, maxcur in res:
                print('  %-11s %-52s courant max %d' % (name, verdict, maxcur))


if __name__ == '__main__':
    main()
