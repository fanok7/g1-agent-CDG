import json
import os
import subprocess
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, '/home/unitree/unitree_sdk2_python')

import config
import robot.hardware as hardware
import robot.audio as audio

TEXT = "Juliette, moi je suis chaud chaud t'as vue, champagne "


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
    print('[PAROLE] Génération de la voix...')
    mp3 = generate_speech(TEXT)
    pcm = decode_pcm24(mp3)
    print(f'[PAROLE] Voix prête ({len(pcm)} octets PCM 24kHz)')
    audio.play_audio(pcm)
    print('[PAROLE] Terminé')


if __name__ == '__main__':
    main()
