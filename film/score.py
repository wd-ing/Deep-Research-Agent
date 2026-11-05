#!/usr/bin/env python3
"""score.py — one-thread film: continuous ambient bed + action SFX in one room.

    ../.venv-film/bin/python score.py            → sfx.wav (mastered −16 LUFS, ≤ −3.5 dBTP)

Bed: slow D-major pad progression, one chord per section, sparse glass motif,
soft sub pulse under retrieval. Action: palette sounds placed at events.json
cues (dumped from window.__events()). Pad gains are per-section constants —
quieter where the action is dense, near-zero in the hold, fullest at resolve.
"""
import json, subprocess, sys, pathlib
import numpy as np

import os
SKILL = os.environ.get("ONETAKE_SCRIPTS") or sys.exit("需要环境变量 ONETAKE_SCRIPTS 指向 onetake 管线的 scripts 目录（含 sfx_palette.py）")
sys.path.insert(0, SKILL)
from sfx_palette import Score, air, glass, wood, sub, bubble, bp, lp, hp, sat, SR, impulse
from scipy import signal

ROOT = pathlib.Path(__file__).parent
DUR = 31.8
EV = json.loads((ROOT / "events.json").read_text())
_rng = np.random.default_rng(11)


def by(k):
    return [e["t"] for e in EV if e["k"] == k]


# ── music: the licensed track — Deep Techno Ambience, 126 BPM ──
# film t = track t - 3.94 (slice starts on track bar 3 downbeat)
def build_bed():
    import wave
    with wave.open(str(ROOT / "music" / "dta.wav"), "rb") as w:
        sr = w.getframerate(); n = w.getnframes(); ch = w.getnchannels()
        x = np.frombuffer(w.readframes(n), np.int16).astype(float) / 32767
    trk = x.reshape(-1, ch)
    trk = signal.resample_poly(trk, SR, sr, axis=0)
    # light-section slice: track bars 1-15, one continuous cut, no splices.
    # 126.05 BPM → bar = 1.9039 s; 15 bars = 28.56 s, ends on the bar-15 line.
    out = np.zeros((int(SR * (DUR + 2)), 2))
    seg = trk[int(0.128 * SR): int(28.7 * SR)]
    out[: len(seg)] = seg
    g = np.ones(len(out))
    # cosine entry: the groove fades in with the typing, not before it
    a = int(1.6 * SR)
    g[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
    # cosine exit: fade lands silent right on the bar-15 line
    i0, i1 = int(26.8 * SR), int(28.6 * SR)
    g[i0:i1] = 0.5 + 0.5 * np.cos(np.pi * np.arange(i1 - i0) / (i1 - i0))
    g[i1:] = 0
    return out * g[:, None]


# ── AAC impact layer: quantized hits on the track grid ──
def build_aacfx():
    import wave
    with wave.open(str(ROOT / "fx.wav"), "rb") as w:
        sr = w.getframerate(); n = w.getnframes(); ch = w.getnchannels()
        x = np.frombuffer(w.readframes(n), np.int16).astype(float) / 32767
    f = x.reshape(-1, ch)
    f = signal.resample_poly(f, SR, sr, axis=0)
    out = np.zeros((int(SR * (DUR + 2)), 2))
    out[: min(len(f), len(out))] = f[: len(out)]
    return np.stack([hp(out[:, c], 220) for c in (0, 1)], 1)   # infra lesson: foley hp 220


# ── action cues ────────────────────────────────────────────────────────────
A4, B4, D5, E5, Fs5, Cs6 = 440.0, 493.9, 587.3, 659.3, 740.0, 1108.7


def build_fx():
    sc = Score(dur=DUR + 2, T60=1.1)
    # typing: ~14 keystrokes across the real typing window, random velocity
    for t in np.linspace(0.6, 2.85, 14):
        sc.place(wood(200 + _rng.uniform(-25, 60), 0.09), t,
                 gain=_rng.uniform(0.16, 0.34), pan=_rng.uniform(-0.15, 0.15), send=0.18)
    sc.place(glass(880, 0.7, 0.7), by("melt")[0], gain=0.22, send=0.5)
    sc.place(air(0.55, 260, 3200, 1.4, 0.35), by("whip")[0], gain=0.5, pan=-0.3, pan_to=0.2, send=0.5)
    sc.place(air(0.45, 2100, 480, 1.2, 0.5), by("gather")[0], gain=0.35, send=0.4)
    # rejects keep a low sub tail under the AAC tom roll
    for t in by("reject"):
        sc.place(sub(66, 0.4), t, gain=0.3, send=0.15)
        sc.place(air(0.3, 900, 170, 1.3, 0.4), t, gain=0.15, send=0.3)
    sc.place(sub(55, 0.4), by("settle")[0], gain=0.35, send=0.2)
    sc.place(air(0.9, 320, 2600, 1.3, 0.45), by("transit")[0], gain=0.42, pan=-0.4, pan_to=0.35, send=0.55)
    sc.place(air(0.3, 3400, 900, 2.2, 0.4), by("strike")[0], gain=0.35, send=0.4)
    sc.place(bubble(330, 0.25), by("pluck")[0], gain=0.3, send=0.35)
    sc.place(sub(50, 0.7), by("seal")[0], gain=0.45, send=0.2)
    sc.place(air(1.6, 180, 1400, 1.1, 0.6), by("lift")[0], gain=0.30, send=0.6)
    sc.place(wood(210, 0.09), by("title")[0], gain=0.2, send=0.3)
    sc.place(air(1.1, 420, 4200, 1.3, 0.4), by("fly")[0], gain=0.5, pan=0.1, pan_to=0, send=0.6)
    sc.place(wood(200, 0.12), by("word")[0], gain=0.3, send=0.3)
    return sc.mix(peak_db=-8.0)


def lufs_tp(path):
    out = subprocess.run(["ffmpeg", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    i_lufs = float(out.split("I:")[-1].split("LUFS")[0].strip())
    tp = float(out.split("Peak:")[-1].split("dBFS")[0].strip())
    return i_lufs, tp


def read_wav(path):
    import wave
    with wave.open(str(path), "rb") as w:
        n = w.getnframes(); ch = w.getnchannels()
        x = np.frombuffer(w.readframes(n), np.int16).astype(float) / 32767
    return x.reshape(-1, ch)


def write_wav(path, m):
    import wave
    m16 = (np.clip(m, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(m16.tobytes())


bed, aacfx, fx = build_bed(), build_aacfx(), build_fx()
n = min(len(bed), len(fx))
mix = bed[:n] * 1.0 + aacfx[:n] * 0.55 + fx[:n] * 0.5
# master fade: the room decays to silence under the held lockup
f0, f1 = int(30.0 * SR), int(30.8 * SR)
mix[f0:f1] *= np.linspace(1, 0, f1 - f0)[:, None]
mix[f1:] = 0
write_wav(ROOT / "mix_raw.wav", mix[: int(SR * (DUR + 0.5))])

i_lufs, tp = lufs_tp(ROOT / "mix_raw.wav")
gain_db = -16.0 - i_lufs
m = mix * 10 ** (gain_db / 20)
peak = np.abs(m).max()
if peak > 10 ** (-3.5 / 20):
    m = sat(m / peak, 1.0) * 10 ** (-3.5 / 20)
write_wav(ROOT / "sfx.wav", m[: int(SR * (DUR + 0.5))])
i2, tp2 = lufs_tp(ROOT / "sfx.wav")
print(f"raw: {i_lufs} LUFS / {tp} dBTP → sfx.wav: {i2} LUFS / {tp2} dBTP  (gain {gain_db:+.1f} dB)")
