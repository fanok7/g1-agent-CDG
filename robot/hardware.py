import os
import sys
sys.path.insert(0, '/home/unitree/unitree_sdk2_python')

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
from config import ROBOT_INTERFACE, ROBOT_NETWORK_ID, ROBOT_VOLUME

_audio_client = None
_arm_client   = None


def init():
    global _audio_client, _arm_client
    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print(f'[HARDWARE] Init réseau {ROBOT_INTERFACE}...')
    ChannelFactoryInitialize(ROBOT_NETWORK_ID, ROBOT_INTERFACE)

    _audio_client = AudioClient()
    _audio_client.SetTimeout(10.0)
    _audio_client.Init()
    _audio_client.SetVolume(ROBOT_VOLUME)

    _arm_client = G1ArmActionClient()
    _arm_client.SetTimeout(10.0)
    _arm_client.Init()

    if os.environ.get('G1_VERBOSE_LOGS', '0') == '1':
        print('[HARDWARE] OK')


def get_audio_client():
    return _audio_client


def get_arm_client():
    return _arm_client
