"""
scripts/wla_fk_check.py — Point 1 du contrat WLA : valider la cinématique directe

Vérifie que notre FK reproduit `observation.state.<side>_ee_pose_gripper_base` du dataset
Unitree à partir des angles (`<side>_arm`, 7 valeurs) et de la taille (`lower_body[12:15]`).
Une erreur de repère ici fausse toutes les actions sans aucun message d'erreur
(archives/thor_sonic/WLA_INSPIRE_CONTRACT.md § 4.1).

Le contrat laisse deux inconnues, qu'on tranche en les essayant toutes et en gardant la
combinaison qui minimise l'erreur :
  - **repère** : pelvis (base) ou torso ;
  - **point de l'effecteur** : wrist_yaw, palm, ou palm + offset ;
  - **ordre des 3 derniers axes du bras** : le dataset nomme
    `wrist_yaw, wrist_roll, wrist_pitch`, mais la chaîne URDF est roll → pitch → yaw.

Aucun accès au robot : lecture de fichiers uniquement.

Usage :
    python3.8 scripts/wla_fk_check.py --parquet ep.parquet [--episode 0] [--frames 400]
"""

import argparse
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

URDF = ('/home/unitree/unitree_lerobot/unitree_lerobot/eval_robot/assets/g1/'
        'g1_body29_hand14.urdf')

# Noms du dataset : shoulder_pitch, shoulder_roll, shoulder_yaw, elbow, puis 3 axes de poignet.
DATASET_ARM = ['shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow',
               'wrist_yaw', 'wrist_roll', 'wrist_pitch']
# Ordre réel de la chaîne URDF, du torse vers la main.
CHAIN_ARM = ['shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow',
             'wrist_roll', 'wrist_pitch', 'wrist_yaw']
WAIST = ['waist_yaw', 'waist_roll', 'waist_pitch']

# Point d'effecteur du dataset, mesuré le 2026-10-02 : décalage constant depuis
# `<side>_hand_palm_link`, exprimé dans le repère de la main. Écart-type 0,4 mm sur
# 300 frames. L'URDF disponible porte des mains Dex3, pas Inspire — ce décalage absorbe
# la différence de géométrie et place le point au centre de préhension.
EE_OFFSET = {'left': (0.12339, -0.00622, 0.0),
             'right': (0.12405, 0.00509, 0.0)}


def load_urdf(path=URDF):
    """Retourne {nom_joint: (parent, enfant, xyz, rpy, axis)}."""
    root = ET.parse(path).getroot()
    joints = {}
    for j in root.findall('joint'):
        o, a = j.find('origin'), j.find('axis')
        xyz = np.array([float(v) for v in (o.get('xyz') or '0 0 0').split()]) if o is not None \
            else np.zeros(3)
        rpy = np.array([float(v) for v in (o.get('rpy') or '0 0 0').split()]) if o is not None \
            else np.zeros(3)
        axis = np.array([float(v) for v in a.get('xyz').split()]) if a is not None else None
        joints[j.get('name')] = (j.find('parent').get('link'), j.find('child').get('link'),
                                 xyz, rpy, axis, j.get('type'))
    return joints


def homog(R, t):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def joint_T(joints, name, q):
    """Transformée parent→enfant d'une articulation à l'angle q."""
    _, _, xyz, rpy, axis, jtype = joints[name]
    T = homog(Rotation.from_euler('xyz', rpy).as_matrix(), xyz)
    if jtype != 'fixed' and axis is not None:
        T = T @ homog(Rotation.from_rotvec(np.asarray(axis, float) * q).as_matrix(), np.zeros(3))
    return T


class G1Kinematics:
    """FK du bassin (pelvis) jusqu'à la main, taille incluse."""

    def __init__(self, urdf=URDF):
        self.j = load_urdf(urdf)

    def T_pelvis_torso(self, waist_q):
        """waist_q = [yaw, roll, pitch], l'ordre du dataset ET de la chaîne."""
        T = np.eye(4)
        for name, q in zip(WAIST, waist_q):
            T = T @ joint_T(self.j, name + '_joint', q)
        return T

    def T_torso_ee(self, side, arm_q, tip='palm', offset=(0., 0., 0.)):
        """arm_q dans l'ordre de la CHAINE (CHAIN_ARM)."""
        T = np.eye(4)
        for name, q in zip(CHAIN_ARM, arm_q):
            T = T @ joint_T(self.j, '%s_%s_joint' % (side, name), q)
        if tip in ('palm', 'palm_offset'):
            T = T @ joint_T(self.j, '%s_hand_palm_joint' % side, 0.0)
        if tip == 'palm_offset':
            T = T @ homog(np.eye(3), np.asarray(offset, float))
        return T

    def ee_pose(self, side, arm_q, waist_q, frame='pelvis', tip='palm', offset=(0., 0., 0.)):
        T = self.T_torso_ee(side, arm_q, tip, offset)
        if frame == 'pelvis':
            T = self.T_pelvis_torso(waist_q) @ T
        return T


def reorder(arm_dataset, order):
    """Passe des 7 valeurs du dataset à l'ordre de la chaîne URDF."""
    if order == 'as_named':
        # on fait confiance aux noms du dataset
        idx = [DATASET_ARM.index(n) for n in CHAIN_ARM]
    elif order == 'as_listed':
        # les 3 derniers sont déjà dans l'ordre de la chaîne, les noms sont faux
        idx = [0, 1, 2, 3, 4, 5, 6]
    else:
        raise ValueError(order)
    return [arm_dataset[i] for i in idx]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--parquet', required=True)
    p.add_argument('--episode', type=int, default=0)
    p.add_argument('--frames', type=int, default=400)
    a = p.parse_args()

    import pyarrow.parquet as pq
    cols = ['episode_index', 'observation.state.left_arm', 'observation.state.right_arm',
            'observation.state.lower_body',
            'observation.state.left_ee_pose_gripper_base',
            'observation.state.right_ee_pose_gripper_base',
            'observation.state.left_ee_pose_gripper_torso',
            'observation.state.right_ee_pose_gripper_torso']
    t = pq.read_table(a.parquet, columns=cols)
    df = t.to_pandas()
    df = df[df['episode_index'] == a.episode].head(a.frames).reset_index(drop=True)
    print('episode %d : %d frames' % (a.episode, len(df)))

    kin = G1Kinematics()
    arr = lambda c, i: np.asarray(df[c][i], dtype=float)

    variants = []
    for order in ('as_named', 'as_listed'):
        for frame, ref in (('pelvis', 'base'), ('torso', 'torso')):
            for tip in ('wrist', 'palm'):
                variants.append((order, frame, ref, tip))

    print('\n%-10s %-7s %-6s %-7s %10s %10s %10s %10s' %
          ('ordre', 'repere', 'ref', 'point', 'pos_G(cm)', 'pos_D(cm)', 'rot_G(deg)', 'rot_D(deg)'))
    best = None
    for order, frame, ref, tip in variants:
        stats = {}
        for side, S in (('left', 'G'), ('right', 'D')):
            dp, dr = [], []
            for i in range(len(df)):
                arm = reorder(arr('observation.state.%s_arm' % side, i), order)
                waist = arr('observation.state.lower_body', i)[12:15]
                T = kin.ee_pose(side, arm, waist, frame=frame, tip=tip)
                ref_pose = arr('observation.state.%s_ee_pose_gripper_%s' % (side, ref), i)
                dp.append(np.linalg.norm(T[:3, 3] - ref_pose[:3]))
                Rr = Rotation.from_euler('xyz', ref_pose[3:6]).as_matrix()
                ang = Rotation.from_matrix(T[:3, :3] @ Rr.T).magnitude()
                dr.append(np.degrees(ang))
            stats[S] = (np.mean(dp) * 100, np.mean(dr))
        print('%-10s %-7s %-6s %-7s %10.2f %10.2f %10.2f %10.2f' %
              (order, frame, ref, tip, stats['G'][0], stats['D'][0], stats['G'][1], stats['D'][1]))
        score = stats['G'][0] + stats['D'][0]
        if best is None or score < best[0]:
            best = (score, order, frame, ref, tip, stats)

    score, order, frame, ref, tip, stats = best
    print('\nMEILLEURE COMBINAISON')
    print('  ordre des axes du bras : %s' % order)
    print('  repere                 : %s (champ ..._gripper_%s)' % (frame, ref))
    print('  point de l\'effecteur   : %s' % tip)
    print('  erreur position  : gauche %.2f cm, droite %.2f cm' % (stats['G'][0], stats['D'][0]))
    print('  erreur rotation  : gauche %.2f deg, droite %.2f deg' % (stats['G'][1], stats['D'][1]))
    print('\nCritere du contrat : quelques mm. %s' %
          ('ATTEINT' if max(stats['G'][0], stats['D'][0]) < 1.0 else
           'NON ATTEINT — il reste un offset ou un repere a trouver'))


if __name__ == '__main__':
    main()
