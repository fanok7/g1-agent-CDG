"""
inspire_sop_check.py — Diagnostic des mains Inspire calqué sur le "RH56DFTP-E2 SOP".

Suit la logique du document, dans son ordre :
  §1 Communication            (SOP I.3 / II.3)
  §2 Données temps réel       (SOP I.1 — angle, courant, force, température, statut)
  §3 Codes de défaut          (SOP I.4 — blocage rotor / surintensité / surchauffe)
  §4 Décalage de position     (SOP I.2 — "Finger Offset" → calibration capteur)
  §5 Paume tactile            (SOP I.1 — Tactile Data)
  §6 Test d'actionneur        (SOP II.4 — "Fingers Unable to Move")

Sécurité, conformément à l'avertissement du SOP ("adjust motion speed/force to
prevent repeated damage to actuators") : vitesse et force réduites, et surtout
ABANDON IMMÉDIAT d'un doigt dès qu'il force sans bouger. Un actionneur bloqué
n'est jamais sollicité en boucle.

Ne pas lancer pendant que main.py tourne (hand_idle écrit dans les mêmes registres).

    python3.8 scripts/inspire_sop_check.py                # les deux mains
    python3.8 scripts/inspire_sop_check.py --side left    # une seule
    python3.8 scripts/inspire_sop_check.py --no-move      # §1 à §5 seulement
"""

import argparse
import struct
import time

from pymodbus.client import ModbusTcpClient

HAND_IPS = {'left': '192.168.123.210', 'right': '192.168.123.211'}
PORT = 6000

# Registres (identiques sur RH56DFTP et RH56E2 — le SOP le précise en préambule)
R_CLEAR_ERROR = 1004
R_FORCE_LIMIT = 1020
R_ANGLE_SET   = 1486
R_FORCE_SET   = 1498
R_SPEED_SET   = 1522
R_POS_ACT     = 1534
R_ANGLE_ACT   = 1546
R_FORCE_ACT   = 1582
R_CURRENT     = 1594
R_ERROR       = 1606
R_STATUS      = 1612
R_TEMP        = 1618
R_PALM        = 4900
PALM_COUNT    = 112

DOIGTS = ['auriculaire', 'annulaire', 'majeur', 'index', 'pouce_flex', 'pouce_rot']

# SOP I.4 : "stall, overcurrent, or overheating"
ERROR_BITS = [(0x01, 'blocage rotor'), (0x02, 'surchauffe'), (0x04, 'surintensite'),
              (0x08, 'fonctionnement anormal'), (0x10, 'erreur communication')]
STATUS_NAMES = {0: 'ouverture', 1: 'fermeture', 2: 'cible atteinte', 3: 'force atteinte',
                5: 'protection courant', 6: 'rotor bloque', 7: 'DEFAUT', 255: 'repos'}

OUVERT = 1000
CIBLES = [1000, 600, 300, 0]
PLAGE_NOMINALE = 1000      # ANGLE et POS doivent rester dans 0..1000
COURANT_BLOCAGE = 900      # au-dela : le moteur pousse fort
COURANT_INERTE = 50        # en dessous : le moteur ne tente rien
TOLERANCE = 60             # ecart angle/consigne considere comme atteint


def err_txt(code):
    if not code:
        return '-'
    return '+'.join(n for b, n in ERROR_BITS if code & b) or ('0x%02X' % code)


class Main:
    def __init__(self, side):
        self.side = side
        self.ip = HAND_IPS[side]
        self.c = ModbusTcpClient(self.ip, port=PORT, timeout=2)
        self.ok = self.c.connect()
        import inspect
        sig = inspect.signature(ModbusTcpClient.read_holding_registers)
        self.kw = {'device_id': 1} if 'device_id' in sig.parameters else {'slave': 1}
        self.echecs = 0

    def close(self):
        self.c.close()

    def rd(self, reg, n):
        r = self.c.read_holding_registers(reg, count=n, **self.kw)
        if r.isError():
            self.echecs += 1
            raise RuntimeError('lecture %d impossible' % reg)
        return list(r.registers)

    def wr(self, reg, vals):
        self.c.write_registers(reg, vals, **self.kw)

    def octets6(self, reg):
        """ERROR / STATUS / TEMP : 1 octet par doigt, 2 doigts par registre."""
        out = []
        for v in self.rd(reg, 3):
            out += [v & 0xFF, (v >> 8) & 0xFF]
        return out

    def signes6(self, reg):
        regs = self.rd(reg, 6)
        return list(struct.unpack('>' + 'h' * 6, struct.pack('>' + 'H' * 6, *regs)))

    def courants(self):
        """Le registre de courant est SIGNE : 65534 vaut -2, pas 65534. Sans cette
        conversion, un actionneur qui ne tire rien passe pour un actionneur qui
        force a fond — et le diagnostic s'inverse (blocage mecanique vs moteur mort)."""
        return self.signes6(R_CURRENT)

    def angles(self):
        return self.rd(R_ANGLE_ACT, 6)

    def effacer_defaut(self):
        self.wr(R_CLEAR_ERROR, [1])
        time.sleep(0.4)

    def ouvrir(self, pause=2.0):
        self.wr(R_ANGLE_SET, [OUVERT] * 6)
        time.sleep(pause)


def section_communication(h):
    """SOP I.3 / II.3 — qualite de la liaison Modbus TCP."""
    print('\n§1 COMMUNICATION (SOP I.3)')
    if not h.ok:
        print('   ECHEC : connexion TCP impossible sur %s:%d' % (h.ip, PORT))
        return False
    t0 = time.time()
    ok = 0
    for _ in range(20):
        try:
            h.angles()
            ok += 1
        except RuntimeError:
            pass
    ms = (time.time() - t0) / 20 * 1000
    print('   %s:%d — %d/20 lectures reussies, %.1f ms par lecture' % (h.ip, PORT, ok, ms))
    if ok < 20:
        print('   ANOMALIE : %d lecture(s) perdue(s) — voir SOP chapitre communication' % (20 - ok))
    return ok > 0


def section_etat(h):
    """SOP I.1 + I.4 — donnees temps reel et codes de defaut."""
    print('\n§2 DONNEES TEMPS REEL ET DEFAUTS (SOP I.1 / I.4)')
    ang = h.angles()
    pos = h.rd(R_POS_ACT, 6)
    cur = h.courants()
    force = h.signes6(R_FORCE_ACT)
    err, sta, temp = h.octets6(R_ERROR), h.octets6(R_STATUS), h.octets6(R_TEMP)
    print('   %-12s %6s %7s %8s %8s %6s %-16s %s'
          % ('doigt', 'ANGLE', 'POS', 'courant', 'force_g', 'T(C)', 'statut', 'defaut'))
    for i, n in enumerate(DOIGTS):
        print('   %-12s %6d %7d %8d %8d %6d %-16s %s'
              % (n, ang[i], pos[i], cur[i], force[i], temp[i],
                 STATUS_NAMES.get(sta[i], sta[i]), err_txt(err[i])))
    defauts = [(DOIGTS[i], err_txt(err[i])) for i in range(6) if err[i]]
    if defauts:
        print('   DEFAUTS ACTIFS : ' + ', '.join('%s=%s' % d for d in defauts))
    else:
        print('   Aucun code de defaut actif (coherent avec une LED verte, SOP I.4)')
    return ang, pos, cur, force, err


def section_limites(h):
    print('\n§3 PARAMETRES (SOP I.1 — Parameter Settings)')
    print('   force  : %s' % h.rd(R_FORCE_SET, 6))
    print('   vitesse: %s' % h.rd(R_SPEED_SET, 6))
    try:
        print('   courant: %s' % h.rd(R_FORCE_LIMIT, 6))
    except RuntimeError:
        pass


def section_offset(pos, ang):
    """SOP I.2 — Finger Offset : une position hors plage appelle une calibration."""
    print('\n§4 DECALAGE DE POSITION (SOP I.2 — Finger Offset)')
    hs = [(DOIGTS[i], pos[i]) for i in range(6) if not 0 <= pos[i] <= PLAGE_NOMINALE]
    if hs:
        for n, v in hs:
            print('   HORS PLAGE : %-12s POS=%d (attendu 0..%d)' % (n, v, PLAGE_NOMINALE))
        print('   -> Le SOP prescrit une CALIBRATION CAPTEUR via le logiciel constructeur.')
    else:
        print('   Toutes les positions sont dans la plage nominale 0..%d' % PLAGE_NOMINALE)
    return [n for n, _ in hs]


def section_tactile(h):
    """SOP I.1 — Tactile Data (absent sur les E2 sans module tactile)."""
    print('\n§5 PAUME TACTILE (SOP I.1 — Tactile Data)')
    try:
        p = h.rd(R_PALM, PALM_COUNT)
    except RuntimeError:
        print('   lecture impossible')
        return
    actifs = [v for v in p if v]
    print('   %d/%d points actifs, total=%d, max=%d'
          % (len(actifs), PALM_COUNT, sum(p), max(p) if p else 0))
    if not actifs:
        print('   0 point : soit rien ne touche la paume, soit le modele n\'a pas de')
        print('   module tactile — le SOP precise que le E2 peut en etre depourvu.')


def tester_doigt(h, idx):
    """SOP II.4 — Actuator Inspection, avec abandon immediat sur blocage."""
    nom = DOIGTS[idx]
    h.ouvrir()
    depart = h.angles()[idx]
    lignes, courant_max, atteint_min = [], 0, depart
    verdict = None

    for cible in CIBLES:
        consignes = [OUVERT] * 6
        consignes[idx] = cible
        h.wr(R_ANGLE_SET, consignes)

        angle, cur, sta, err = depart, 0, 0, 0
        precedent = None
        t0 = time.time()
        while time.time() - t0 < 2.5:
            time.sleep(0.35)
            angle = h.angles()[idx]
            cur = h.courants()[idx]
            err = h.octets6(R_ERROR)[idx]
            sta = h.octets6(R_STATUS)[idx]
            if err or sta == 7:
                break
            if precedent is not None and abs(angle - precedent) <= 2:
                break
            precedent = angle

        courant_max = max(courant_max, abs(cur))
        atteint_min = min(atteint_min, angle)
        lignes.append((cible, angle, cur, STATUS_NAMES.get(sta, sta), err_txt(err)))

        immobile = abs(angle - depart) < TOLERANCE and abs(angle - cible) > TOLERANCE
        cur_abs = abs(cur)
        if err or sta == 7:
            verdict = 'DEFAUT remonte par l\'actionneur : %s' % err_txt(err)
            break
        if immobile and cur_abs >= COURANT_BLOCAGE:
            verdict = ('BLOQUE — force sans bouger (courant %d), arret du test pour '
                       'ne pas l\'abimer (SOP II.4)' % cur_abs)
            break
        if immobile and cur_abs <= COURANT_INERTE:
            verdict = ('INERTE — ne bouge pas et ne tire aucun courant (%d) : '
                       'actionneur mort ou debranche (SOP II.4)' % cur_abs)
            break

    h.wr(R_ANGLE_SET, [OUVERT] * 6)
    time.sleep(2.0)
    retour = h.angles()[idx]
    if verdict is None:
        course = depart - atteint_min
        if atteint_min <= TOLERANCE:
            verdict = 'OK — course complete'
        elif course < TOLERANCE:
            verdict = 'IMMOBILE — aucune course utile'
        else:
            verdict = 'PARTIEL — descend seulement jusqu\'a %d' % atteint_min
    if abs(retour - OUVERT) > 100:
        verdict += ' ; ne revient pas en butee ouverte (%d)' % retour

    print('\n   --- %s ---' % nom)
    print('   %8s %8s %9s %-16s %s' % ('consigne', 'angle', 'courant', 'statut', 'defaut'))
    for c, a, cu, s, e in lignes:
        print('   %8d %8d %9d %-16s %s' % (c, a, cu, s, e))
    print('   VERDICT : %s' % verdict)

    try:
        h.effacer_defaut()
    except RuntimeError:
        pass
    return nom, verdict, courant_max


def section_mouvement(h):
    print('\n§6 TEST D\'ACTIONNEUR (SOP II.4 — Fingers Unable to Move)')
    avant = h.octets6(R_TEMP)
    # Force et vitesse moderees : le SOP demande de les reduire pour ne pas
    # repeter de dommages sur un actionneur deja en difficulte.
    h.wr(R_SPEED_SET, [500] * 6)
    h.wr(R_FORCE_SET, [1000] * 6)
    resultats = [tester_doigt(h, i) for i in range(6)]
    h.ouvrir()
    apres = h.octets6(R_TEMP)
    print('\n   temperatures avant : %s' % avant)
    print('   temperatures apres : %s' % apres)
    return resultats


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--side', choices=list(HAND_IPS) + ['both'], default='both')
    p.add_argument('--no-move', action='store_true')
    a = p.parse_args()
    cotes = list(HAND_IPS) if a.side == 'both' else [a.side]

    bilan = {}
    for side in cotes:
        print('\n' + '=' * 74)
        print('MAIN %s (%s) — diagnostic SOP RH56DFTP-E2' % (side.upper(), HAND_IPS[side]))
        print('=' * 74)
        h = Main(side)
        try:
            if not section_communication(h):
                bilan[side] = [('communication', 'ECHEC', 0)]
                continue
            ang, pos, cur, force, err = section_etat(h)
            section_limites(h)
            hors_plage = section_offset(pos, ang)
            section_tactile(h)
            if a.no_move:
                bilan[side] = [('(lecture seule)', 'non teste', 0)]
            else:
                bilan[side] = section_mouvement(h)
            if hors_plage:
                bilan[side].append(('offset capteur', 'calibration requise : ' + ', '.join(hors_plage), 0))
        finally:
            h.close()

    print('\n' + '=' * 74)
    print('BILAN')
    print('=' * 74)
    for side, res in bilan.items():
        print('\nmain %s :' % side)
        for nom, verdict, cmax in res:
            print('  %-14s %s' % (nom, verdict))


if __name__ == '__main__':
    main()
