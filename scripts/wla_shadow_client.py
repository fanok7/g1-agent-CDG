"""
scripts/wla_shadow_client.py — Client WLA en MODE OMBRE (point 3 du contrat § 8)

Lit l'état réel du robot, interroge le serveur UnifoLM-WLA sur la Thor, journalise les
chunks d'actions reçus. **N'envoie RIEN aux moteurs** — aucun import d'arm_sdk, aucune
écriture Modbus, aucun publisher DDS. La seule écriture est le fichier journal.

Ce qu'il envoie (contrat § 4, variante caméras locales) :
  observation.state.left_ee_6d    (9,)  xyz + rot6d, repère pelvis
  observation.state.right_ee_6d   (9,)
  observation.state.left_fig6d    (6,)  doigts, 1 = ouvert
  observation.state.right_fig6d   (6,)
  observation.state.lower_body   (15,)  jambes G/D + taille (yaw, roll, pitch)
  observation.state.base_rot      (6,)  gravité projetée + vitesse angulaire
  instruction                      str

Les images ne sont envoyées que si le serveur les réclame : on teste la présence des
clés `observation.images.*` dans le `metadata` reçu à la connexion. Avec `--local_cams`
côté Thor, elles en sont absentes et le client n'envoie que l'état.

Pré-requis : lancé depuis /home/unitree/unitree_sdk2_python (SDK Unitree), et
ChannelFactory initialisée sur eth0 — géré par --init-dds.

Usage :
    cd /home/unitree/unitree_sdk2_python
    python3.8 /home/unitree/g1_agent_interim/scripts/wla_shadow_client.py \
        --url ws://192.168.123.166:8600 --requests 10
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from wla_fk_check import G1Kinematics, EE_OFFSET            # noqa: E402
from wla_inspire_check import FIG6D_IDX, HAND_IPS           # noqa: E402

# Indices moteurs G1 29 DOF (cf. robot/body_state.py)
LOWER_BODY = list(range(0, 15))          # jambe G, jambe D, taille (yaw, roll, pitch)
ARM = {'left': list(range(15, 22)), 'right': list(range(22, 29))}
WAIST = [12, 13, 14]

R_ANGLE_ACT = 1546
STALE = 0.5          # s — au-delà, l'état DDS est considéré périmé

DEFAULT_INSTRUCTION = 'Pick up the clothes from the bed and place them into the basket'


# ── État du robot ────────────────────────────────────────────────────────────

class RobotState:
    """Lecture seule : rt/lowstate (DDS) + registres ANGLE des mains (Modbus)."""

    def __init__(self, with_hands=True):
        from robot import body_state
        self._bs = body_state
        self.kin = G1Kinematics()
        self.hands = {}
        if with_hands:
            from pymodbus.client import ModbusTcpClient
            import inspect
            kw = ('device_id' if 'device_id' in
                  inspect.signature(ModbusTcpClient.read_holding_registers).parameters
                  else 'slave')
            for side, ip in HAND_IPS.items():
                c = ModbusTcpClient(ip, port=6000, timeout=1)
                self.hands[side] = (c, {kw: 1}) if c.connect() else None
                if self.hands[side] is None:
                    print('[WLA] main %s injoignable (%s) — fig6d a 1.0' % (side, ip))

    def fig6d(self, side):
        """Doigts dans l'ordre du modèle. Main absente → main ouverte (1.0)."""
        h = self.hands.get(side)
        if h is None:
            return [1.0] * 6
        c, kw = h
        try:
            r = c.read_holding_registers(R_ANGLE_ACT, count=6, **kw)
            if r.isError():
                return [1.0] * 6
            n = [v / 1000.0 for v in r.registers]
            return [n[i] for i in FIG6D_IDX]
        except Exception:
            return [1.0] * 6

    @staticmethod
    def rot6d(R):
        """Deux premières colonnes, colonne par colonne (contrat § 4.1)."""
        return [R[0, 0], R[1, 0], R[2, 0], R[0, 1], R[1, 1], R[2, 1]]

    def observation(self, instruction):
        msg, age = self._bs._reader.snapshot()
        if msg is None:
            raise RuntimeError("aucune donnée sur rt/lowstate — robot éteint ou eth0 ?")
        if age > STALE:
            raise RuntimeError('état DDS périmé (%.2f s)' % age)

        q = [msg.motor_state[j].q for j in range(29)]
        lower = [q[j] for j in LOWER_BODY]
        waist = [q[j] for j in WAIST]

        obs = {'observation.state.lower_body': np.array(lower, dtype=np.float32)}
        for side in ('left', 'right'):
            arm = [q[j] for j in ARM[side]]      # déjà dans l'ordre de la chaîne URDF
            T = self.kin.ee_pose(side, arm, waist, frame='pelvis',
                                 tip='palm_offset', offset=EE_OFFSET[side])
            obs['observation.state.%s_ee_6d' % side] = np.array(
                list(T[:3, 3]) + self.rot6d(T[:3, :3]), dtype=np.float32)
            obs['observation.state.%s_fig6d' % side] = np.array(
                self.fig6d(side), dtype=np.float32)

        imu = msg.imu_state
        # gravité projetée dans le repère bassin + vitesse angulaire
        from scipy.spatial.transform import Rotation
        R_wb = Rotation.from_quat([imu.quaternion[1], imu.quaternion[2],
                                   imu.quaternion[3], imu.quaternion[0]]).as_matrix()
        g = R_wb.T @ np.array([0.0, 0.0, -1.0])
        obs['observation.state.base_rot'] = np.array(
            list(g) + list(imu.gyroscope), dtype=np.float32)
        obs['instruction'] = instruction
        return obs, {'age_dds': age, 'rpy': list(imu.rpy), 'q': q}


# ── Dialogue avec le serveur ─────────────────────────────────────────────────

async def run(args):
    import websockets
    sys.path.insert(0, args.msgpack_dir)
    import msgpack_numpy as mn

    state = RobotState(with_hands=not args.no_hands)
    log = open(args.log, 'a')
    print('[WLA] connexion a %s' % args.url)

    async with websockets.connect(args.url, max_size=None,
                                  ping_interval=None) as ws:
        raw = await ws.recv()
        if isinstance(raw, str):
            raise SystemExit('erreur serveur : %s' % raw[:500])
        meta = mn.unpackb(raw)
        meta = {(k.decode() if isinstance(k, bytes) else k): v for k, v in meta.items()}
        keys = [k.decode() if isinstance(k, bytes) else k
                for k in meta.get('data_keys', [])]
        want_images = [k for k in keys if k.startswith('observation.images.')]
        chunk = meta.get('action_chunk_size')
        print('[WLA] metadata : chunk=%s  unnorm=%s  images attendues=%s' %
              (chunk, meta.get('default_unnorm_key'),
               want_images if want_images else 'AUCUNE (capture locale)'))
        if want_images:
            print('[WLA] ATTENTION : le serveur reclame des images, '
                  'ce client n en envoie pas (--local_cams absent cote Thor)')

        lat, prep, ages = [], [], []
        for i in range(args.requests):
            ts0 = time.time()
            obs, dbg = state.observation(args.instruction)
            t0 = time.time()
            prep.append(t0 - ts0)
            ages.append(dbg['age_dds'])
            await ws.send(mn.packb({'type': 'get_action', 'obs': obs}))
            rep = await ws.recv()
            t1 = time.time()
            if isinstance(rep, str):
                print('[WLA] erreur serveur :\n%s' % rep[:1500])
                break
            act = mn.unpackb(rep)
            act = {(k.decode() if isinstance(k, bytes) else k): v for k, v in act.items()}
            lat.append(t1 - t0)

            ent = {
                'i': i, 't_envoi': t0, 't_reception': t1, 'latence_s': round(t1 - t0, 4),
                'age_dds_s': round(dbg['age_dds'], 4),
                'preparation_s': round(t0 - ts0, 4),
                'charge_orin': os.getloadavg()[0],
                'etat': {k: (v.tolist() if isinstance(v, np.ndarray) else v)
                         for k, v in obs.items()},
                'actions': {k: np.asarray(v)[0].tolist() for k, v in act.items()
                            if hasattr(v, 'shape')},
            }
            log.write(json.dumps(ent) + '\n')
            log.flush()

            fig = np.asarray(act.get('action.right_fig6d'))[0]
            ee = np.asarray(act.get('action.right_ee_rpy'))[0]
            print('[%2d] latence %5.0f ms | doigts D amplitude %.3f | '
                  'deplacement main D %.1f cm' %
                  (i, (t1 - t0) * 1000, float(fig.max() - fig.min()),
                   float(np.linalg.norm(ee[-1, :3] - ee[0, :3])) * 100))
            if args.period > 0:
                await asyncio.sleep(max(0.0, args.period - (time.time() - t0)))

    log.close()
    if lat:
        a, p, g = np.array(lat), np.array(prep), np.array(ages)
        print('\nlatence aller-retour vue de l Orin : mediane %.0f ms, '
              'min %.0f, max %.0f (%d requetes)' %
              (np.median(a) * 1000, a.min() * 1000, a.max() * 1000, len(a)))
        print('pas perimes a 30 Hz : %.0f sur %s' % (np.median(a) * 30, chunk))
        # Gigue : ce que la Thor veut savoir — l'Orin fait-il attendre l'etat ?
        print('preparation de l etat (Orin) : mediane %.1f ms, max %.1f ms, '
              'gigue p95-p5 %.1f ms' %
              (np.median(p) * 1000, p.max() * 1000,
               (np.percentile(p, 95) - np.percentile(p, 5)) * 1000))
        print('age de l etat DDS : mediane %.1f ms, max %.1f ms' %
              (np.median(g) * 1000, g.max() * 1000))
        print('charge Orin (1 min) : %.2f' % os.getloadavg()[0])
    print('journal : %s' % args.log)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='ws://192.168.123.166:8600')
    p.add_argument('--requests', type=int, default=10)
    p.add_argument('--period', type=float, default=0.0,
                   help='secondes entre deux requetes (0 = enchainer)')
    p.add_argument('--instruction', default=DEFAULT_INSTRUCTION)
    p.add_argument('--log', default='/tmp/wla_shadow.jsonl')
    p.add_argument('--no-hands', action='store_true')
    p.add_argument('--no-init-dds', action='store_true')
    p.add_argument('--iface', default='eth0')
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
