"""
scripts/wla_execute.py — Boucle fermée WLA : serveur → bras + doigts du G1

⚠️ **Par défaut, ce script NE COMMANDE RIEN** : il calcule tout, journalise tout, et
s'arrête avant d'écrire aux moteurs. Il faut `--execute` pour l'autoriser. Lire § Sécurité
avant de l'utiliser.

Chaîne, une fois par chunk :
    état réel (DDS + Modbus) → serveur → 30 poses → on jette les pas périmés →
    filets (enveloppe + vitesse) → cinématique inverse → angles → arm_sdk
                                 → doigts → ordre natif → Modbus

Sécurité :
  - rien n'est envoyé sans `--execute` ;
  - les cibles sont saturées dans l'enveloppe d'entraînement et limitées en vitesse
    (wla_safety, validé sur 181 356 cibles réelles) ;
  - `--speed-scale` ralentit volontairement pour les premiers essais ;
  - **chien de garde** : sans nouveau chunk pendant `--watchdog` secondes, les bras
    tiennent leur dernière position, puis la main est rendue au contrôleur de marche ;
  - la **taille n'est jamais commandée** : elle reste au WBC. Conséquence mesurée : quand
    le modèle suppose un torse penché, la cible peut être hors de portée, jusqu'à 94 mm
    d'écart. C'est le choix sûr — pencher le torse de 30° touche à l'équilibre ;
  - **ne pas lancer `main.py` en même temps** : l'agent commande aussi les bras, et le
    verrou `arm_sdk.is_holding()` ne protège pas contre deux programmes séparés.

Pré-requis : robot debout et alimenté, serveur joignable.

    cd /home/unitree/unitree_sdk2_python
    python3.8 /home/unitree/g1_agent_interim/scripts/wla_execute.py \
        --url ws://192.168.123.166:8600 --chunks 5            # calcul seul
    ... --execute --speed-scale 0.3                            # bouge pour de vrai
"""

import argparse
import asyncio
import json
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from wla_ik import solve_chunk                                   # noqa: E402
from wla_safety import SafetyFilter, clamp_fingers               # noqa: E402
from wla_shadow_client import RobotState, ARM, WAIST             # noqa: E402
from wla_inspire_check import NATIVE_IDX, HAND_IPS               # noqa: E402

CONTROL_HZ = 30.0
R_ANGLE_SET = 1486


class ArmWriter:
    """Écrit les angles des deux bras. La taille est recopiée telle que mesurée."""

    def __init__(self, enabled):
        self.enabled = enabled
        self._ctrl = None
        if enabled:
            from robot import arm_sdk
            self._sdk = arm_sdk
            self._ctrl = arm_sdk._ctrl
            self._ctrl._init_once()

    def write(self, q_left, q_right, waist, weight=1.0):
        if not self.enabled:
            return
        self._ctrl._write(list(q_left) + list(q_right) + list(waist), weight=weight)

    def fade_out(self, waist):
        """Rend progressivement les bras au contrôleur de marche."""
        if not self.enabled:
            return
        base = self._ctrl._current_q()
        t0 = time.time()
        while time.time() - t0 < 1.0:
            self._ctrl._write(base, weight=1.0 - (time.time() - t0))
            time.sleep(0.02)
        self._ctrl._write(base, weight=0.0)


class HandWriter:
    """Écrit les doigts sur le registre ANGLE (1 = ouvert), ordre natif."""

    def __init__(self, enabled, state):
        self.enabled = enabled
        self.hands = state.hands

    def write(self, side, fig6d):
        if not self.enabled:
            return
        h = self.hands.get(side)
        if h is None:
            return
        c, kw = h
        f = clamp_fingers(fig6d, side)
        native = [f[i] for i in NATIVE_IDX]
        try:
            c.write_registers(R_ANGLE_SET,
                              [max(0, min(1000, int(v * 1000))) for v in native], **kw)
        except Exception as e:
            print('[WLA] ecriture main %s echouee : %s' % (side, e))


async def run(args):
    import websockets
    sys.path.insert(0, args.msgpack_dir)
    import msgpack_numpy as mn

    state = RobotState()
    arms = ArmWriter(args.execute)
    hands = HandWriter(args.execute, state)
    filters = {s: SafetyFilter(s, speed_scale=args.speed_scale) for s in ('left', 'right')}
    log = open(args.log, 'a')

    if args.execute:
        print('\n*** MODE REEL — les bras et les doigts vont BOUGER ***')
        print('*** arret : Ctrl+C, les bras sont rendus au controleur de marche ***\n')
    else:
        print('[WLA] calcul seul — rien ne sera envoye aux moteurs (--execute pour bouger)')

    async with websockets.connect(args.url, max_size=None, ping_interval=None) as ws:
        raw = await ws.recv()
        if isinstance(raw, str):
            raise SystemExit('erreur serveur : %s' % raw[:500])
        meta = {(k.decode() if isinstance(k, bytes) else k): v
                for k, v in mn.unpackb(raw).items()}
        print('[WLA] chunk=%s unnorm=%s' % (meta.get('action_chunk_size'),
                                            meta.get('default_unnorm_key')))

        for side in filters:
            obs0, _ = state.observation(args.instruction)
            filters[side].reset(obs0['observation.state.%s_ee_6d' % side][:3])

        last_chunk_t = time.time()
        try:
            for k in range(args.chunks):
                obs, dbg = state.observation(args.instruction)
                t0 = time.time()
                await ws.send(mn.packb({'type': 'get_action', 'obs': obs}))
                rep = await ws.recv()
                t1 = time.time()
                if isinstance(rep, str):
                    print('[WLA] erreur serveur :\n%s' % rep[:1200])
                    break
                act = {(key.decode() if isinstance(key, bytes) else key): v
                       for key, v in mn.unpackb(rep).items()}
                last_chunk_t = t1
                latence = t1 - t0

                # Les premiers pas du chunk sont deja perimes a l'arrivee.
                skip = int(np.ceil(latence * CONTROL_HZ))
                q_now = dbg['q']
                waist = [q_now[j] for j in WAIST]

                plan = {}
                for side in ('left', 'right'):
                    poses = np.asarray(act['action.%s_ee_rpy' % side])[0][skip:]
                    if not len(poses):
                        continue
                    poses = poses.copy()
                    for i in range(len(poses)):          # filets AVANT la cinematique
                        poses[i, :3] = filters[side].step(poses[i, :3])
                    seed = [q_now[j] for j in ARM[side]]
                    plan[side] = (solve_chunk(side, poses, waist, seed, stride=args.stride),
                                  np.asarray(act['action.%s_fig6d' % side])[0][skip:])

                if len(plan) == 2:
                    n = min(len(plan['left'][0]), len(plan['right'][0]))
                    t_start = time.time()
                    for i in range(n):
                        if time.time() - last_chunk_t > args.watchdog:
                            print('[WLA] CHIEN DE GARDE : plus de chunk, arret')
                            raise KeyboardInterrupt
                        arms.write(plan['left'][0][i], plan['right'][0][i], waist)
                        if i % 5 == 0:
                            for side in ('left', 'right'):
                                hands.write(side, plan[side][1][i])
                        dt = (i + 1) / CONTROL_HZ - (time.time() - t_start)
                        if dt > 0:
                            await asyncio.sleep(dt)
                        if time.time() - t_start > args.apply_max:
                            break

                log.write(json.dumps({
                    'k': k, 'latence_s': round(latence, 4), 'pas_jetes': skip,
                    'pas_appliques': len(plan.get('left', [[]])[0]),
                    'age_dds_s': round(dbg['age_dds'], 4),
                    'filtre_G': filters['left'].report(),
                    'filtre_D': filters['right'].report(),
                }) + '\n')
                log.flush()
                print('[%2d] latence %4.0f ms | %2d pas jetes | %2d appliques | %s' %
                      (k, latence * 1000, skip, len(plan.get('left', [[]])[0]),
                       'ENVOYE' if args.execute else 'calcul seul'))
        except KeyboardInterrupt:
            print('\n[WLA] arret demande')
        finally:
            msg, _ = state._bs._reader.snapshot()
            if msg is not None:
                arms.fade_out([msg.motor_state[j].q for j in WAIST])
            log.close()

    for f in filters.values():
        print('  ' + f.report())
    print('journal : %s' % args.log)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='ws://192.168.123.166:8600')
    p.add_argument('--chunks', type=int, default=5)
    p.add_argument('--execute', action='store_true',
                   help='AUTORISE l ecriture aux moteurs (sinon calcul seul)')
    p.add_argument('--speed-scale', type=float, default=0.3,
                   help='ralentissement pour les premiers essais (1.0 = pleine vitesse)')
    p.add_argument('--stride', type=int, default=10, help='1 cinematique inverse sur N pas')
    p.add_argument('--watchdog', type=float, default=1.5,
                   help='secondes sans chunk avant arret')
    p.add_argument('--apply-max', type=float, default=1.0,
                   help='duree max d application d un chunk (s)')
    p.add_argument('--instruction',
                   default='Pick up the clothes from the bed and place them into the basket')
    p.add_argument('--log', default='/tmp/wla_execute.jsonl')
    p.add_argument('--iface', default='eth0')
    p.add_argument('--no-init-dds', action='store_true')
    p.add_argument('--msgpack-dir',
                   default='/tmp/claude-1000/-home-unitree-g1-agent-interim/'
                           'fd9a9f39-ec63-446c-b2cb-d38c989d328d/scratchpad')
    a = p.parse_args()

    if not a.no_init_dds:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        ChannelFactoryInitialize(0, a.iface)
    asyncio.get_event_loop().run_until_complete(run(a))


if __name__ == '__main__':
    main()
