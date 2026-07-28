"""scripts/test_micro_snr.py — Mesure la qualité du micro du robot.

Sert à décider si un appel d'assistance peut passer par le micro du robot plutôt
que par celui de la tablette : on veut le rapport signal/bruit RÉEL, voix contre
souffle des ventilateurs, à la distance où se tient un visiteur.

Usage (main.py doit être ARRÊTÉ — il occupe le micro) :
    python3.8 scripts/test_micro_snr.py
"""

import sys
import time
import wave

import numpy as np
import sounddevice as sd

SR = 48000
SEC = 8
OUT = "/tmp/mic_robot_voix.wav"


def trouver_micro():
    """Même logique que robot/audio.py : le Cubilux s'annonce comme 'MIC',
    on retombe sur n'importe quel périphérique USB en secours."""
    devs = sd.query_devices()
    for pref in ("MIC", "USB"):
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0 and pref in d["name"].upper():
                return i, d["name"]
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            return i, d["name"]
    return None, None


def db(v):
    return 20 * np.log10(max(float(v), 1e-9) / 32768.0)


def main():
    dev, nom = trouver_micro()
    if dev is None:
        print("Aucun micro trouvé.")
        return 1
    print("Micro : [{}] {}\n".format(dev, nom))
    print("Place-toi devant le robot, à la distance d'un visiteur (~1 m).")
    print("Tu vas parler normalement pendant {} secondes.\n".format(SEC))
    for n in (3, 2, 1):
        print("  {}...".format(n))
        time.sleep(1)
    print("\n>>> PARLE MAINTENANT <<<\n")

    a = sd.rec(int(SR * SEC), samplerate=SR, channels=1, dtype="int16", device=dev)
    for r in range(SEC, 0, -1):
        print("  {} s restantes".format(r))
        time.sleep(1)
    sd.wait()
    print("\nTerminé.\n")

    x = a.flatten().astype(np.float32)
    # Tranches de 50 ms : les plus fortes portent la voix, les plus faibles le
    # bruit de fond entre les mots. Le rapport des deux est le SNR utile — bien
    # plus parlant qu'un RMS global, qui mélange les deux.
    n = int(SR * 0.05)
    tr = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(tr ** 2, axis=1))
    rms.sort()
    bruit = np.mean(rms[: max(1, len(rms) // 10)])     # 10 % les plus calmes
    voix = np.mean(rms[-max(1, len(rms) // 5):])       # 20 % les plus forts
    snr = db(voix) - db(bruit)
    crete = float(np.max(np.abs(x)))

    print("bruit de fond : {:6.1f} dBFS".format(db(bruit)))
    print("voix          : {:6.1f} dBFS".format(db(voix)))
    print("SNR           : {:6.1f} dB".format(snr))
    print("crête         : {:.0f}/32768 {}".format(
        crete, "— SATURATION" if crete > 32000 else "— pas de saturation"))

    if db(voix) < -45:
        print("\n⚠ Voix trop faible : tu n'as probablement pas parlé, ou tu es "
              "trop loin. Recommence en parlant pendant tout le décompte.")
    else:
        verdict = ("excellent" if snr > 30 else "bon" if snr > 20
                   else "moyen" if snr > 12 else "insuffisant")
        print("\nVERDICT : {} (>20 dB = exploitable pour un appel)".format(verdict))

    with wave.open(OUT, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(a.tobytes())
    print("Enregistrement : {}".format(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
