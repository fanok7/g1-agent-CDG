import math
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, '/home/unitree/unitree_sdk2_python')

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

import robot.hardware as hardware

_ARM_JOINTS = [15, 16, 17, 18, 19, 20, 21,
               22, 23, 24, 25, 26, 27, 28,
               12, 13, 14]
_ENABLE_SLOT = 29
_KP, _KD = 60.0, 1.5
_CTRL_DT = 0.02


def _deg(*vals):
    return [math.radians(v) for v in vals]


CANDIDATES = {
    'A_epaule+80_coude-90': _deg(80, 0, 0, -90,  0, 0, 0,  0, 0, 0, 0,  0, 0, 0,  0, 0, 0),
    'B_epaule-80_coude+90': _deg(-80, 0, 0, 90,  0, 0, 0,  0, 0, 0, 0,  0, 0, 0,  0, 0, 0),
    'C_epaule+80_coude0':   _deg(80, 0, 0, 0,    0, 0, 0,  0, 0, 0, 0,  0, 0, 0,  0, 0, 0),
    'D_epaule+90_coude+90': _deg(90, 0, 0, 90,   0, 0, 0,  0, 0, 0, 0,  0, 0, 0,  0, 0, 0),
}


def main():
    hardware.init()
    pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
    pub.Init()
    low_state = []
    lock = None
    state = {}

    def on_state(msg):
        state['ls'] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(on_state, 10)
    time.sleep(1.0)

    crc = CRC()

    def write(q, w=1.0):
        cmd = unitree_hg_msg_dds__LowCmd_()
        cmd.motor_cmd[_ENABLE_SLOT].q = w
        for i, j in enumerate(_ARM_JOINTS):
            cmd.motor_cmd[j].tau = 0.
            cmd.motor_cmd[j].q = q[i]
            cmd.motor_cmd[j].dq = 0.
            cmd.motor_cmd[j].kp = _KP
            cmd.motor_cmd[j].kd = _KD
        cmd.crc = crc.Crc(cmd)
        pub.Write(cmd)

    def current_q():
        ls = state.get('ls')
        if ls is None:
            return [0.0] * 17
        return [ls.motor_state[j].q for j in _ARM_JOINTS]

    def ramp(to, dur):
        base = current_q()
        t0 = time.time()
        while True:
            r = min((time.time() - t0) / dur, 1.0)
            write([base[i] + r * (to[i] - base[i]) for i in range(17)])
            if r >= 1.0:
                break
            time.sleep(_CTRL_DT)

    def fade():
        t0 = time.time()
        base = current_q()
        while True:
            r = min((time.time() - t0) / 0.8, 1.0)
            write(base, w=1.0 - r)
            if r >= 1.0:
                break
            time.sleep(_CTRL_DT)

    for name, pose in CANDIDATES.items():
        print(f'\n=== {name} ===')
        ramp(pose, 1.5)
        for _ in range(30):
            time.sleep(0.1)
        q = current_q()
        print(f'L_ShPitch={math.degrees(q[0]):+.0f}° L_Elbow={math.degrees(q[3]):+.0f}°')
        time.sleep(1.0)

    print('\n=== retour à zéro ===')
    ramp([0.0] * 17, 1.5)
    fade()
    print('terminé')


if __name__ == '__main__':
    main()