import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, '/home/unitree/unitree_sdk2_python')

import config
import robot.hardware as hardware
import robot.audio as audio
from robot.arm_sdk import execute_direction
from robot.hand_control import HandControl

TEXT = ("In brightest day, in blackest night, no evil shall escape my sight. "
        "Let those who worship evil's might, beware my power, Green Lantern's light!")


def generate_speech(text):
    payload = {"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text}
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.OPENAI_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def decode_pcm24(mp3):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1", "pipe:1"],
        input=mp3, capture_output=True, check=True,
    )
    return proc.stdout


def main():
    hardware.init()
    print('[LANTERN] Génération de la voix...')
    mp3 = generate_speech(TEXT)
    pcm = decode_pcm24(mp3)
    dur = len(pcm) / (24000 * 2)
    print(f'[LANTERN] Voix prête ({dur:.1f}s)')

    hand = HandControl('left')
    if hand.connect():
        hand.set_speed(800)
        hand.set_force_limit(1500)
        hand.close(1000)
        print('[LANTERN] Poing gauche fermé')
    else:
        print('[LANTERN] Main gauche injoignable — poing non garanti')

    thread = threading.Thread(target=execute_direction, args=('devant_gauche',),
                              kwargs={'hold_secs': dur + 2.0}, daemon=True)
    thread.start()
    time.sleep(0.5)

    audio.play_audio(pcm)

    thread.join(timeout=dur + 12)
    hand.open()
    hand.disconnect()
    print('[LANTERN] Terminé')


if __name__ == '__main__':
    main()