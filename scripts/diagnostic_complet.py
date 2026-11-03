"""
diagnostic_complet.py — Bilan complet : mains Inspire + 29 moteurs du robot + infra.

Mains : pour CHAQUE actionneur, mesure la course REELLEMENT libre. La consigne
est envoyee a force basse (400 au lieu de 2000) : l'actionneur s'arrete de
lui-meme la ou il bute, sans jamais etre force. On lit alors ou il s'est arrete
et combien il tirait. Aucun matraquage, conformement a l'avertissement du SOP
("adjust motion speed/force to prevent repeated damage to actuators").

Robot : lecture seule de rt/lowstate (aucun ordre envoye, aucun publisher DDS).

    python3.8 scripts/diagnostic_complet.py
"""

import struct
import sys
import time

HAND_IPS = {'gauche': '192.168.123.210', 'droite': '192.168.123.211'}
PORT = 6000
DOIGTS = ['auriculaire', 'annulaire', 'majeur', 'index', 'pouce_flexion', 'pouce_rotation']

R_CLEAR, R_ANGLE_SET, R_FORCE_SET, R_SPEED_SET = 1004, 1486, 1498, 1522
R_POS, R_ANGLE, R_FORCE, R_CURRENT = 1534, 1546, 1582, 1594
R_ERROR, R_STATUS, R_TEMP, R_PALM = 1606, 1612, 1618, 4900

FORCE_TEST, VITESSE_TEST = 400, 250     # doux : l'actionneur ne force pas
SEUIL_EFFORT = 120                      # au-dela : il pousse contre quelque chose
SEUIL_INERTE = 40                       # en dessous : il ne tente rien
TOLERANCE = 50

ERROR_BITS = [(0x01, 'blocage rotor'), (0x02, 'surchauffe'), (0x04, 'surintensite'),
              (0x08, 'anormal'), (0x10, 'erreur comm')]


def err_txt(c):
    return '+'.join(n for b, n in ERROR_BITS if c & b) or ('0x%02X' % c) if c else '-'


class MainInspire:
    def __init__(self, nom, ip):
        from pymodbus.client import ModbusTcpClient
        import inspect
        self.nom, self.ip = nom, ip
        self.c = ModbusTcpClient(ip, port=PORT, timeout=2)
        self.ok = self.c.connect()
        sig = inspect.signature(ModbusTcpClient.read_holding_registers)
        self.kw = {'device_id': 1} if 'device_id' in sig.parameters else {'slave': 1}

    def rd(self, reg, n):
        r = self.c.read_holding_registers(reg, count=n, **self.kw)
        if r.isError():
            raise RuntimeError(reg)
        return list(r.registers)

    def wr(self, reg, v):
        self.c.write_registers(reg, v, **self.kw)

    def o6(self, reg):
        out = []
        for v in self.rd(reg, 3):
            out += [v & 0xFF, (v >> 8) & 0xFF]
        return out

    def s6(self, reg):
        return list(struct.unpack('>6h', struct.pack('>6H', *self.rd(reg, 6))))

    def ouvrir(self, p=2.5):
        self.wr(R_ANGLE_SET, [1000] * 6)
        time.sleep(p)

    def close(self):
        self.c.close()


def course_libre(h, idx):
    """Commande la fermeture complete A FORCE BASSE : l'actionneur s'arrete
    de lui-meme a sa butee. Retourne (angle_atteint, courant, defaut)."""
    h.ouvrir(2.0)
    depart = h.rd(R_ANGLE, 6)[idx]
    cons = [1000] * 6
    cons[idx] = 0
    h.wr(R_ANGLE_SET, cons)

    angle, cur, prec = depart, 0, None
    t0 = time.time()
    while time.time() - t0 < 4.0:
        time.sleep(0.35)
        angle = h.rd(R_ANGLE, 6)[idx]
        cur = h.s6(R_CURRENT)[idx]
        if prec is not None and abs(angle - prec) <= 2:
            break
        prec = angle
    err = h.o6(R_ERROR)[idx]
    h.wr(R_ANGLE_SET, [1000] * 6)
    time.sleep(2.0)
    retour = h.rd(R_ANGLE, 6)[idx]
    try:
        h.wr(R_CLEAR, [1])
    except RuntimeError:
        pass
    return depart, angle, abs(cur), err, retour


def verdict(depart, arret, cur, err, retour):
    course = depart - arret
    if err:
        return 'DEFAUT : ' + err_txt(err)
    if arret <= TOLERANCE:
        v = 'OK — course complete (%d -> %d)' % (depart, arret)
    elif course < TOLERANCE and cur <= SEUIL_INERTE:
        v = 'INERTE — immobile, aucun courant (%d) : actionneur mort ou debranche' % cur
    elif course < TOLERANCE:
        v = 'BLOQUE d\'emblee — ne bouge pas, pousse a %d' % cur
    elif cur >= SEUIL_EFFORT:
        v = 'COURSE LIMITEE — libre jusqu\'a %d, puis bute (courant %d)' % (arret, cur)
    else:
        v = 'PARTIEL — s\'arrete a %d sans forcer (courant %d)' % (arret, cur)
    if abs(retour - 1000) > 120:
        v += ' ; ne revient pas en butee ouverte (%d)' % retour
    return v


def diag_main(nom, ip):
    print('\n' + '=' * 78)
    print('MAIN %s (%s)' % (nom.upper(), ip))
    print('=' * 78)
    h = MainInspire(nom, ip)
    if not h.ok:
        print('  INJOIGNABLE sur %s:%d' % (ip, PORT))
        return [('connexion', 'ECHEC')]

    ok = sum(1 for _ in range(15) if not h.c.read_holding_registers(R_ANGLE, count=6, **h.kw).isError())
    print('\n[communication] %d/15 lectures reussies' % ok)

    ang, pos = h.rd(R_ANGLE, 6), h.rd(R_POS, 6)
    force, temp = h.s6(R_FORCE), h.o6(R_TEMP)
    err = h.o6(R_ERROR)
    print('\n[etat au repos]')
    print('  %-16s %6s %7s %8s %6s %s' % ('actionneur', 'ANGLE', 'POS', 'force_g', 'T(C)', 'defaut'))
    for i, n in enumerate(DOIGTS):
        hp = '  <-- POS hors plage 0..1000' if not 0 <= pos[i] <= 1000 else ''
        print('  %-16s %6d %7d %8d %6d %s%s' % (n, ang[i], pos[i], force[i], temp[i], err_txt(err[i]), hp))

    try:
        palm = h.rd(R_PALM, 112)
        print('\n[paume tactile] %d/112 points actifs, max=%d' % (len([v for v in palm if v]), max(palm)))
    except RuntimeError:
        print('\n[paume tactile] lecture impossible')

    print('\n[course libre de chaque actionneur — force %d, sans forcage]' % FORCE_TEST)
    h.wr(R_SPEED_SET, [VITESSE_TEST] * 6)
    h.wr(R_FORCE_SET, [FORCE_TEST] * 6)
    res = []
    for i, n in enumerate(DOIGTS):
        d, a, cu, e, r = course_libre(h, i)
        v = verdict(d, a, cu, e, r)
        print('  %-16s %s' % (n, v))
        res.append((n, v))
    h.ouvrir()
    h.close()
    return res


def diag_robot():
    print('\n' + '=' * 78)
    print('ROBOT — 29 moteurs (lecture seule)')
    print('=' * 78)
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        ChannelFactoryInitialize(0, 'eth0')
        box = {}
        ChannelSubscriber('rt/lowstate', LowState_).Init(lambda m: box.__setitem__('m', m), 10)
        time.sleep(2.5)
        m = box.get('m')
    except Exception as e:
        print('  DDS indisponible : %s' % e)
        return []
    if m is None:
        print('  aucune trame rt/lowstate — robot eteint ou ne publie pas')
        return []

    groupes = ['jambe_G'] * 6 + ['jambe_D'] * 6 + ['taille'] * 3 + ['bras_G'] * 7 + ['bras_D'] * 7
    anomalies = []
    for i in range(29):
        s = m.motor_state[i]
        pbs = []
        if s.motorstate:
            pbs.append('defaut=%d' % s.motorstate)
        if s.temperature[1] > 120:
            pbs.append('bobinage=%d' % s.temperature[1])
        if s.temperature[0] > 85:
            pbs.append('carcasse=%d' % s.temperature[0])
        if abs(s.tau_est) > 100:
            pbs.append('couple=%.0f' % s.tau_est)
        if pbs:
            anomalies.append((i, groupes[i], ', '.join(pbs)))
    print('  tension bus : %.1f V' % m.motor_state[0].vol)
    tmax = max(max(m.motor_state[i].temperature[0] for i in range(29)), 0)
    print('  temperature carcasse max : %d C' % tmax)
    if anomalies:
        for i, g, p in anomalies:
            print('  MOTEUR %-2d (%-7s) : %s' % (i, g, p))
    else:
        print('  aucune anomalie sur les 29 moteurs')
    return anomalies


def diag_infra():
    import subprocess
    print('\n' + '=' * 78)
    print('INFRASTRUCTURE')
    print('=' * 78)
    cibles = [('robot (RockChip)', '192.168.123.161'), ('main gauche', '192.168.123.210'),
              ('main droite', '192.168.123.211'), ('Jetson Thor', '192.168.123.166')]
    for nom, ip in cibles:
        r = subprocess.run(['ping', '-c1', '-W1', ip], capture_output=True)
        print('  %-18s %-18s %s' % (nom, ip, 'repond' if r.returncode == 0 else 'silence'))


if __name__ == '__main__':
    bilan = {}
    for nom, ip in HAND_IPS.items():
        bilan[nom] = diag_main(nom, ip)
    anomalies = diag_robot()
    diag_infra()

    print('\n' + '=' * 78)
    print('SYNTHESE')
    print('=' * 78)
    for nom, res in bilan.items():
        pbs = [(n, v) for n, v in res if not v.startswith('OK')]
        print('\nmain %s : %d/%d actionneurs sains' % (nom, len(res) - len(pbs), len(res)))
        for n, v in pbs:
            print('   %-16s %s' % (n, v))
    print('\nrobot : %s' % ('%d moteur(s) en anomalie' % len(anomalies) if anomalies else '29 moteurs sains'))
    for i, g, p in anomalies:
        print('   moteur %-2d (%s) : %s' % (i, g, p))
