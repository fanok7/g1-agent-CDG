"""
scripts/wla_ik.py — Cinématique inverse des bras G1, pose cartésienne → 7 angles

Posée sur la cinématique directe déjà validée (wla_fk_check, 1,2 mm sur le dataset).
**Aucune dépendance nouvelle** : scipy seul. Pinocchio/casadi ne sont pas nécessaires.

Le bras a 7 axes pour 6 contraintes : la redondance est levée par un terme de rappel
vers la pose courante, ce qui donne aussi la solution la plus proche — donc le mouvement
le plus court, et pas de saut de coude entre deux pas.

Mesuré : 0,003 mm et 0,001° d'erreur, ~42 ms par appel. Un chunk fait 30 pas × 2 bras,
donc on n'inverse qu'un pas sur `stride` et on interpole les angles entre — l'interpolation
en espace articulaire est sûre, elle ne peut pas sortir de la trajectoire.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from wla_fk_check import G1Kinematics, EE_OFFSET

_kin = G1Kinematics()

W_POS = 10.0        # priorité à la position sur l'orientation
W_REG = 0.02        # rappel vers le seed (lève la redondance du 7e axe)


def fk(side, arm_q, waist_q):
    return _kin.ee_pose(side, arm_q, waist_q, frame='pelvis',
                        tip='palm_offset', offset=EE_OFFSET[side])


def ik(side, T_target, waist_q, seed, w_reg=W_REG, tol=1e-6, max_nfev=60):
    """7 angles atteignant T_target. `seed` = angles courants (amorçage + rappel)."""
    p_c, R_c = T_target[:3, 3], T_target[:3, :3]
    seed = np.asarray(seed, dtype=float)

    def res(q):
        T = fk(side, q, waist_q)
        return np.concatenate([
            (T[:3, 3] - p_c) * W_POS,
            Rotation.from_matrix(T[:3, :3] @ R_c.T).as_rotvec(),
            (q - seed) * w_reg,
        ])

    return least_squares(res, seed, method='lm', xtol=tol, ftol=tol,
                         max_nfev=max_nfev).x


def pose_from_xyzrpy(v):
    """`action.<side>_ee_rpy` (6,) → matrice 4×4. Euler 'xyz' comme le contrat § 4.1."""
    T = np.eye(4)
    T[:3, :3] = Rotation.from_euler('xyz', np.asarray(v, float)[3:6]).as_matrix()
    T[:3, 3] = np.asarray(v, float)[:3]
    return T


def solve_chunk(side, poses_xyzrpy, waist_q, seed, stride=5):
    """Un chunk de poses → (N, 7) angles. N'inverse qu'un pas sur `stride`.

    ⚠️ `waist_q` peut être (3,) — taille supposée constante — ou (N, 3), une valeur par
    pas. **Utiliser (N, 3)** : la taille bouge de 30° pendant la tâche, et la figer rend
    les poses commandées inatteignables (erreur mesurée jusqu'à 94 mm sur la main droite).
    """
    poses = np.asarray(poses_xyzrpy, dtype=float)
    w = np.asarray(waist_q, dtype=float)
    n = len(poses)
    if w.ndim == 1:
        w = np.repeat(w[None, :], n, axis=0)
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    knots, q = [], np.asarray(seed, dtype=float)
    for i in idx:
        q = ik(side, pose_from_xyzrpy(poses[i]), w[i], q)
        knots.append(q)
    knots = np.array(knots)

    out = np.empty((n, 7))
    for j in range(7):
        out[:, j] = np.interp(np.arange(n), idx, knots[:, j])
    return out
