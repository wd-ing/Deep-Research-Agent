#!/usr/bin/env python3
"""compose.py — FX score only. Music is the licensed track (music/dta.wav);
this score renders the film's impact sounds as instruments, quantized to the
track's measured grid (126.05 BPM, beat k at 0.128+k·0.476 s, film t = track
t − 3.94) and tuned to its C-family centre.

    ../.venv-film/bin/python compose.py   → fx_score.json (render via aac)

FX map: gate stamps → fm_percussion rim roll; card rain → pentatonic taps;
framework ticks → high accents; citation chips ×7 → fm_bell climbing C-major
pentatonic; click → clap+snare accent; seal → kick_909; rejects → tom roll;
settle → tom; strike → snare rim; mark zigzag → bell sparks.
"""
import json, pathlib
from audio_as_code import Song, Track, Note

ROOT = pathlib.Path(__file__).parent
BPM = 96                    # score-internal grid; FX placed in seconds/0.625
BEATS = 54
EV = json.loads((ROOT / "events.json").read_text())
def evs(k):
    return [e["t"] for e in EV if e["k"] == k]
def qb(t):
    """film seconds → nearest 16th on the track grid → score beats."""
    j = round(t / 0.119)
    return j * 0.119 / 0.625

def note(p, s, d, v):
    return Note(pitch=p, start=round(s, 4), duration=d, velocity=min(v, 1.0))

fx_perc, fx_bell, fx_tom, fx_k909, fx_snr, clap = [], [], [], [], [], []

# gate stamps → rimshot roll on the grid, alternating accents/pitch
for i, t in enumerate(evs("stamp")):
    fx_perc.append(note("C4" if i % 2 == 0 else "G3", qb(t), 0.15,
                        0.5 if i % 2 == 0 else 0.34))
# card rain → conga-ish pentatonic taps
PENT = ["A4", "E4", "C5", "G4", "D4", "G4", "A4", "C5", "E4", "G4"]
for i, t in enumerate(evs("rain")):
    fx_perc.append(note(PENT[i % len(PENT)], qb(t), 0.2, 0.34))
# framework cascade → high tick accents
for i in range(8):
    fx_perc.append(note("G5", qb(11.9 + i * 0.16), 0.1, 0.28))
# citation chips → fm_bell climbing C-major pentatonic
CHIP_SCALE = ["C5", "D5", "E5", "G5", "A5", "C6", "D6"]
for i, t in enumerate(evs("chip")):
    fx_bell.append(note(CHIP_SCALE[i % len(CHIP_SCALE)], qb(t), 0.8, 0.44))
# rejects → low tom roll into the next beat
for t in evs("reject"):
    fx_tom.append(note("C2", qb(t), 0.35, 0.5))
    fx_tom.append(note("G1", qb(t) + 0.25, 0.4, 0.42))
# settle → tom hit
fx_tom.append(note("D2", qb(evs("settle")[0]), 0.4, 0.55))
# click → clap + snare accent on the grid
clap.append(note("D2", qb(evs("click")[0]), 0.25, 0.62))
fx_snr.append(note("E2", qb(evs("click")[0]), 0.2, 0.45))
# seal → 909 thud; strike → snare rim
fx_k909.append(note("C1", qb(evs("seal")[0]), 0.5, 0.7))
fx_snr.append(note("E2", qb(evs("strike")[0]), 0.2, 0.5))
# mark zigzag → bell sparks, C pentatonic
bell = [note(p, s, 1.2, 0.30) for p, s in
        [("E5", 43.0), ("G5", 43.6), ("A5", 44.2), ("C6", 44.8), ("E6", 45.4)]]

song = Song(
    bpm=BPM, beats=BEATS, seed=7, master_gain=0.9,
    tracks=[
        Track(name="clap",    instrument="clap",             gain=0.30, pan=-0.1,  notes=clap),
        Track(name="fx_perc", instrument="fm_percussion",    gain=0.40, pan=0.0,   notes=fx_perc),
        Track(name="fx_bell", instrument="fm_bell",          gain=0.34, pan=0.15,  release_seconds=1.5, notes=fx_bell),
        Track(name="fx_tom",  instrument="toms",             gain=0.42, pan=-0.15, notes=fx_tom),
        Track(name="fx_k909", instrument="kick_909",         gain=0.55, pan=0.0,   notes=fx_k909),
        Track(name="fx_snr",  instrument="electronic_snare", gain=0.40, pan=-0.05, notes=fx_snr),
        Track(name="bell",    instrument="fm_bell",          gain=0.28, pan=0.3,   release_seconds=2.0, notes=bell),
    ],
)
(ROOT / "fx_score.json").write_text(song.model_dump_json(indent=1))
print(f"fx_score.json: {sum(len(t.notes) for t in song.tracks)} FX notes, "
      f"quantized to the 126 BPM track grid")
