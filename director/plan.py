"""Turn scored events into an edit list: which stretches of the replay to
record, which car to follow and which cameras to use."""
from __future__ import annotations

MIN_SCORE = 2.0
MERGE_GAP = 2.5  # seconds between windows that get merged into one clip
MIN_SHOT = 6.0  # never cut cameras faster than this
LONG_SHOT = 15.0  # shots longer than this get a mid-shot angle change


def _window(ev: dict, h, race_end: float) -> tuple[float, float]:
    t, typ = ev["t"], ev["type"]
    if typ == "start":
        a, b = t - 4, t + 30
    elif typ == "finish":
        a, b = (t - 10, t + 4) if ev["data"].get("focus_finish") else (t - 14, t + 5)
    elif typ == "battle":
        a, b = max(ev["start"] - 2, t - 10), min(ev["end"] + 4, t + 6)
        if b - a < 12:
            b = a + 12
    elif typ == "restart":
        a, b = t - 6, t + 12
    elif typ == "incident":
        a, b = t - h.pre_roll, t + h.post_roll + min(ev["data"].get("dur", 0), 4)
    else:
        a, b = t - h.pre_roll, t + h.post_roll
    return max(0.0, a), min(race_end, b)


def _subject(ev: dict, focus):
    typ, d = ev["type"], ev["data"]
    if typ == "overtake":
        return d["passer"]
    if typ == "battle":
        return d["attacker"]
    if typ == "start":
        return focus if focus is not None else d.get("pole")
    return ev["cars"][0] if ev["cars"] else focus


def _union_len(windows):
    total, cur = 0.0, None
    for a, b in sorted(windows):
        if cur is None or a > cur[1] + MERGE_GAP:
            if cur:
                total += cur[1] - cur[0]
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        total += cur[1] - cur[0]
    return total


def _resolve_cam(name: str, cams: list[dict]) -> dict:
    if not cams:
        return dict(num=1, name=name)
    low = name.lower()
    for c in cams:
        if c["name"].lower() == low:
            return c
    for c in cams:
        if low in c["name"].lower():
            return c
    return cams[0]


def label_for(ev: dict, race: dict) -> str:
    drv = {d["idx"]: d for d in race["drivers"]}
    nm = lambda i: drv.get(i, {}).get("short", "?").upper()
    d, typ = ev["data"], ev["type"]
    if typ == "start":
        return f"RACE START  |  {race['track_short'].upper()}"
    if typ == "finish":
        if d.get("focus_finish"):
            return f"FINISH  |  {nm(ev['cars'][0])}  P{d.get('position', '?')}"
        return f"CHEQUERED FLAG  |  {nm(ev['cars'][0])} WINS" if ev["cars"] else "CHEQUERED FLAG"
    if typ == "overtake":
        verb = "PASSES" if d.get("completed") else "ATTACKS"
        tag = "LEAD CHANGE  |  " if d.get("lead_change") else ""
        return f"{tag}LAP {d['lap']}  |  {nm(d['passer'])} {verb} {nm(d['passed'])} FOR P{d['position']}"
    if typ == "battle":
        laps = f"LAPS {d['lap_from']}-{d['lap_to']}" if d["lap_to"] > d["lap_from"] else f"LAP {d['lap_from']}"
        return f"{laps}  |  BATTLE FOR P{d['position']}  |  {nm(d['defender'])} v {nm(d['attacker'])}"
    if typ == "custom":
        return "CUSTOM"
    if typ == "restart":
        return f"LAP {d['lap']}  |  RESTART  |  {nm(d['leader'])} LEADS" if d.get("leader") is not None else "RESTART"
    if typ == "incident":
        what = {"crash": "CRASH", "spin": "SPIN", "contact": "INCIDENT", "offtrack": "OFF TRACK"}[d["severity"]]
        who = ", ".join(nm(c) for c in ev["cars"][:3])
        return f"LAP {d['lap']}  |  {what}  |  {who}"
    return ""


class CameraDirector:
    """Picks camera angles like a TV director:
    - battles: a wide chase camera on the attacker (both cars in frame), then trackside
    - passes: chase camera building up, cut to a trackside camera for the move itself
    - incidents: trackside cameras
    - start: helicopter/blimp, then trackside; finish: trackside at the line
    - your own car: an occasional onboard shot in long moments
    """

    ROLES = dict(start="start_cameras", finish="finish_cameras", chase="chase_cameras",
                 trackside="trackside_cameras", incident="incident_cameras", onboard="onboard_cameras")
    DEFAULTS = dict(start_cameras=["Chopper", "Blimp"], finish_cameras=["TV1", "Blimp"],
                    chase_cameras=["Far Chase", "Chase"], trackside_cameras=["TV1", "TV2", "TV3"],
                    incident_cameras=["TV2", "TV1", "TV3"], onboard_cameras=["Cockpit", "Roll Bar"])

    def __init__(self, cap, cams):
        self.cams = cams
        self.lists = {r: list(cap.get(key) or self.DEFAULTS[key]) for r, key in self.ROLES.items()}
        self.rot = {r: 0 for r in self.ROLES}
        self.onboard = bool(cap.get("onboard_for_focus", True))
        self.onboard_count = 0

    def pick(self, role: str, avoid=None) -> dict:
        names = self.lists[role]
        for _ in range(len(names)):
            cam = _resolve_cam(names[self.rot[role] % len(names)], self.cams)
            self.rot[role] += 1
            if cam["num"] != avoid or len(names) == 1:
                return cam
        return cam

    def coverage(self, e: dict, s0: float, s1: float, car, focus):
        """List of (time, role, car) shots covering one segment."""
        typ, d, t = e["type"], e["data"], e["t"]
        length = s1 - s0
        mine = focus is not None and focus in e["cars"]
        if typ == "start":
            out = [(s0, "start", car)]
            if length > 18:
                out.append((s0 + 14, "trackside", car))
            if length > 34:
                out.append((s0 + 26, "chase", car))
            return out
        if typ == "finish":
            out = [(s0, "chase", car)]
            if t - 4 > s0 + 3:
                out.append((t - 4, "finish", car))  # trackside at the line for the flag
            return out
        if typ == "incident":
            return [(s0, "incident", car)]
        if typ == "restart":
            return [(s0, "chase", car), (max(s0 + MIN_SHOT, t - 1), "trackside", car)] if length > 12 else [(s0, "chase", car)]
        if typ == "overtake":
            cut = t - 2.5
            if cut - s0 >= 4 and s1 - cut >= 4:
                return [(s0, "chase", car), (cut, "trackside", car)]
            return [(s0, "trackside", car)]
        if typ == "battle":
            out = [(s0, "chase", d["attacker"])]
            if length > LONG_SHOT:
                mid = s0 + length / 2
                if mine and self.onboard and self.onboard_count % 2 == 0:
                    out.append((mid, "onboard", focus))  # see it from your own cockpit
                else:
                    out.append((mid, "trackside", d["defender"]))
                self.onboard_count += 1 if mine else 0
            return out
        return [(s0, "trackside", car)]


def make_plan(race: dict, det: dict, cfg) -> dict:
    h, cap = cfg.highlights, cfg.capture
    events = det["events"]
    focus = det["focus"]
    race_end = race["samples"]["t"][-1]
    by_id = {e["id"]: e for e in events}

    must = [
        e for e in events
        if (e["type"] == "start" and h.include_start) or (e["type"] == "finish" and h.include_finish)
    ]
    chosen = list(must)
    windows = [_window(e, h, race_end) for e in chosen]
    budget = float(h.target_seconds)  # start/finish count toward the target too
    pool = [e for e in events if e not in must and e["type"] not in ("pit", "caution") and e["score"] >= MIN_SCORE]
    # spread the screen time around the field: each time a driver is already in the edit,
    # their next moments count for less, so everyone's best moments get a look-in
    seen: dict = {}
    spread = float(cfg.highlights.get("spread", 0.35))

    def value(e):
        reps = sum(seen.get(c, 0) for c in e["cars"]) / max(1, len(e["cars"]))
        return e["score"] / (1 + spread * reps)

    while pool:
        e = max(pool, key=value)
        pool.remove(e)
        w = _window(e, h, race_end)
        if _union_len(windows + [w]) <= budget:
            chosen.append(e)
            windows.append(w)
            for c in e["cars"]:
                seen[c] = seen.get(c, 0) + 1

    # merge overlapping windows into clips
    items = sorted(zip(windows, chosen), key=lambda x: x[0][0])
    clips = []
    for (a, b), e in items:
        if clips and a <= clips[-1]["end"] + MERGE_GAP:
            c = clips[-1]
            c["end"] = max(c["end"], b)
            c["events"].append(e["id"])
        else:
            clips.append(dict(start=a, end=b, events=[e["id"]]))

    cams = race.get("cameras", [])
    drv = {d["idx"]: d for d in race["drivers"]}
    director = CameraDirector(cap, cams)
    for n, c in enumerate(clips):
        evs = sorted((by_id[i] for i in c["events"]), key=lambda e: e["t"])
        primary = max(evs, key=lambda e: e["score"])
        c["id"] = n
        c["primary"] = primary["id"]
        c["type"] = primary["type"]
        c["label"] = label_for(primary, race)
        c["start"] = round(c["start"], 2)
        c["end"] = round(c["end"], 2)

        # who to watch: at each moment, the best-scoring event whose window covers it
        wins = [(e, _window(e, h, race_end)) for e in evs]
        segs, t = [], c["start"]
        while t < c["end"]:
            cover = [e for e, (a, b) in wins if a <= t <= b and _subject(e, focus) is not None] or [primary]
            best = max(cover, key=lambda e: e["score"])
            if segs and segs[-1][2] is best:
                segs[-1][1] = t + 0.5
            else:
                segs.append([t, t + 0.5, best])
            t += 0.5
        # no shot shorter than MIN_SHOT: fold it into the stronger neighbour
        while len(segs) > 1:
            short = [i for i, s in enumerate(segs) if s[1] - s[0] < MIN_SHOT]
            if not short:
                break
            i = short[0]
            if i == 0:
                j = 1
            elif i == len(segs) - 1:
                j = i - 1
            else:
                j = i - 1 if segs[i - 1][2]["score"] >= segs[i + 1][2]["score"] else i + 1
            lo, hi = min(i, j), max(i, j)
            segs[lo : hi + 1] = [[segs[lo][0], segs[hi][1], segs[j][2]]]

        shots = []
        for s0, s1, e in segs:
            car = _subject(e, focus)
            if car is None:
                continue
            for at, cam_role, who in director.coverage(e, s0, s1, car, focus):
                if shots and shots[-1]["car"] == who and shots[-1]["role"] == cam_role:
                    continue
                cam = director.pick(cam_role, avoid=shots[-1]["cam"] if shots else None)
                shots.append(dict(at=round(at, 2), car=who, ev=e["id"], role=cam_role,
                                  number=drv.get(who, {}).get("number", "1"), cam=cam["num"], cam_name=cam["name"]))
        c["shots"] = shots
    total = sum(c["end"] - c["start"] for c in clips)
    return dict(clips=clips, total_seconds=round(total, 1), focus=focus)
