"""Synthetic race generator so the pipeline can be tested without iRacing.

    python -m director fake        # creates output/fake-race with race.json + test clips
"""
from __future__ import annotations

import numpy as np

NAMES = [
    "Max Verhoek", "Lena Fischer", "Tom Okafor", "Adam Kowalski", "Ricardo Alves",
    "Sophie Laurent", "Kenji Sato", "Emma Novak", "Luca Bianchi", "Oscar Lindqvist",
    "Priya Raman", "Dan Mueller",
]


def fake_race(laps: int = 10, ref_lap: float = 90.0, dt: float = 0.27, seed: int = 4) -> dict:
    rng = np.random.default_rng(seed)
    n = len(NAMES)
    skill = np.array([0.010, 0.002, 0.008, 0.006, -0.004, 0.004, -0.002, -0.008, 0.0, -0.010, -0.006, 0.001])
    skill[2] = skill[1] = 0.003  # cars 1 & 2 evenly matched -> long battle
    d = -0.004 * np.arange(n) - 0.002
    t, green_t = 0.0, 40.0
    samples = dict(t=[], frame=[], state=[], flags=[], lap=[], pct=[], surf=[], pit=[], pos=[])
    checkered_t = None
    pit_car, pit_lap, pit_left = 8, 5, 0.0
    spin_car, spin_t = 5, 300.0
    crash_cars, crash_t = (10, 11), 520.0
    frame = 0
    while True:
        if t < green_t:
            state, v = 3, np.zeros(n)
        else:
            state = 4 if checkered_t is None else (5 if t - checkered_t < 30 else 6)
            v = (1.0 / ref_lap) * (1 + skill + rng.normal(0, 0.004, n))
            if checkered_t is not None:
                v *= 0.5
        surf = np.full(n, 3)
        pit = np.zeros(n, dtype=int)
        # spin
        if spin_t <= t < spin_t + 4:
            v[spin_car] = 0.0005
            surf[spin_car] = 0
        # two-car crash
        if crash_t <= t < crash_t + 5:
            for c in crash_cars:
                v[c] = 0.0003
                surf[c] = 0 if t < crash_t + 3 else 3
        # pit stop
        if pit_left > 0:
            pit[pit_car] = 1
            v[pit_car] *= 0.35
            surf[pit_car] = 2
            pit_left -= dt
        elif d[pit_car] >= pit_lap - 0.02 and d[pit_car] < pit_lap and not samples.get("_pitted"):
            pit_left = 30.0
            samples["_pitted"] = True
        d = d + v * dt
        if checkered_t is None and d.max() >= laps:
            checkered_t = t
        order = np.argsort(-d)
        pos = np.empty(n, dtype=int)
        pos[order] = np.arange(1, n + 1)
        samples["t"].append(round(t, 3))
        samples["frame"].append(frame)
        samples["state"].append(state)
        samples["flags"].append(0)
        samples["lap"].append(np.floor(d).astype(int).tolist())
        samples["pct"].append(np.round(np.mod(d, 1.0), 5).tolist())
        samples["surf"].append(surf.tolist())
        samples["pit"].append(pit.tolist())
        samples["pos"].append(pos.tolist())
        t += dt
        frame += 16
        if checkered_t is not None and t > checkered_t + 70:
            break
    samples.pop("_pitted", None)
    # iRacing reports lap 0 on the grid even though pct is ~0.99
    samples["lap"] = [[max(0, l) for l in row] for row in samples["lap"]]
    drivers = [
        dict(idx=i, name=nm, short=nm.split()[-1], number=str(i + 2), car="Mazda MX-5 Cup",
             car_class="MX5", irating=2000 + 50 * i, est_lap=ref_lap)
        for i, nm in enumerate(NAMES)
    ]
    return dict(
        track="Fake Raceway International",
        track_short="Fake Raceway",
        track_config="Grand Prix",
        track_length="4.2 km",
        subsession=0,
        weather=dict(air="24 C", track="31 C", sky="Partly Cloudy"),
        car_names=["Mazda MX-5 Cup"],
        session_num=2,
        session_laps=laps,
        focus_idx=3,
        drivers=drivers,
        cameras=[dict(num=i + 1, name=nm) for i, nm in enumerate(
            ["Nose", "Gearbox", "Roll Bar", "Cockpit", "Chase", "Far Chase", "TV1", "TV2", "TV3", "Chopper", "Blimp"])],
        results=[],
        cars=list(range(n)),
        samples=samples,
        iracing_incidents=[dict(t=spin_t + 0.5, idx=spin_car), dict(t=crash_t + 0.4, idx=crash_cars[0])],
    )
