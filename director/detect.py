"""Find the interesting moments in a scanned race.

Works purely on race.json (no iRacing needed), so detection can be tuned and
re-run offline.  Produces a list of events:

    {id, type, t, start, end, cars, score, data}

types: start, finish, overtake, battle, incident, pit
"""
from __future__ import annotations

import numpy as np

DETECT_VERSION = 6  # bump when detection changes, so old events.json files get flagged

ON_TRACK = 3
OFF_TRACK = 0
NOT_IN_WORLD = -1


# ---------------------------------------------------------------- helpers
def _runs(mask: np.ndarray, t: np.ndarray, max_gap: float = 0.0):
    """Yield (i_start, i_end) index pairs where mask is True, bridging gaps <= max_gap s."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return
    start = prev = idx[0]
    for i in idx[1:]:
        if t[i] - t[prev] > max_gap + 1e-9 and i != prev + 1:
            yield start, prev
            start = i
        prev = i
    yield start, prev


def race_distance(lap: np.ndarray, pct: np.ndarray, surf: np.ndarray) -> np.ndarray:
    """Continuous race distance in laps for each car (NaN when not in the world).

    Unwraps LapDistPct across the start/finish line instead of trusting
    CarIdxLap, which updates a sample late/early around the line."""
    n, c = pct.shape
    out = np.full((n, c), np.nan)
    valid = (surf != NOT_IN_WORLD) & (pct >= 0) & (lap >= 0)
    for k in range(c):
        v = valid[:, k]
        for a, b in _runs(v, np.arange(n, dtype=float)):
            p = pct[a : b + 1, k].astype(float)
            dp = np.diff(p)
            wraps = np.concatenate([[0], np.cumsum((dp < -0.5).astype(int) - (dp > 0.5).astype(int))])
            unwrapped = p + wraps
            offset = np.round(np.median(lap[a : b + 1, k] + p - unwrapped))
            out[a : b + 1, k] = unwrapped + offset
    return out


class Race:
    """numpy view of race.json plus derived quantities."""

    def __init__(self, race: dict):
        self.raw = race
        s = race["samples"]
        self.t = np.asarray(s["t"], dtype=float)
        self.state = np.asarray(s["state"], dtype=int)
        lap = np.asarray(s["lap"], dtype=float)
        pct = np.asarray(s["pct"], dtype=float)
        self.surf = np.asarray(s["surf"], dtype=int)
        self.pit = np.asarray(s["pit"], dtype=int).astype(bool)
        self.cars = list(race["cars"])  # column -> CarIdx
        self.col = {idx: k for k, idx in enumerate(self.cars)}
        self.d = race_distance(lap, pct, self.surf)
        self.drivers = {d["idx"]: d for d in race["drivers"]}
        self.cls = [self.drivers.get(i, {}).get("car_class", "") for i in self.cars]

        racing = np.flatnonzero(self.state >= 4)
        self.green = float(self.t[racing[0]]) if racing.size else float(self.t[0])
        chk = np.flatnonzero(self.state >= 5)
        self.checkered = float(self.t[chk[0]]) if chk.size else float(self.t[-1])
        self.i_green = int(np.searchsorted(self.t, self.green))

        # last known distance for cars that left the world (towed, disconnected, finished)
        self.dff = self.d.copy()
        for k in range(self.dff.shape[1]):
            col = self.d[:, k]
            valid = ~np.isnan(col)
            if not valid.any():
                continue
            idx = np.where(valid, np.arange(len(col)), 0)
            np.maximum.accumulate(idx, out=idx)
            filled = col[idx]
            filled[: int(np.argmax(valid))] = np.nan
            self.dff[:, k] = filled

        # when each car was last seen; cars gone for good before the flag have retired / disconnected
        self.last_seen = np.array([
            self.t[np.flatnonzero(~np.isnan(self.d[:, k]))[-1]] if (~np.isnan(self.d[:, k])).any() else -1.0
            for k in range(self.d.shape[1])])

        # typical lap time, measured from the data (iRacing's own estimate is often missing or bogus)
        self.ref_lap = self._measured_lap()
        ests = [d.get("est_lap") for d in race["drivers"] if d.get("est_lap")]
        if ests:
            est = float(np.median(ests))
            if 0.6 * self.ref_lap < est < 1.4 * self.ref_lap:
                self.ref_lap = est

        # official finishing order when the replay has it
        res = sorted((x for x in race.get("results") or [] if x.get("idx") in self.col and x.get("pos", 0) > 0),
                     key=lambda x: x["pos"])
        self.official_order = [x["idx"] for x in res]

        # speed in laps/second, lightly smoothed
        dt = np.gradient(self.t)
        dt[dt <= 0] = 1e-3
        sp = np.gradient(np.nan_to_num(self.d, nan=0.0), axis=0) / dt[:, None]
        sp[np.isnan(self.d)] = np.nan
        k = max(1, int(round(1.0 / max(np.median(dt), 1e-3))))  # ~1 s window
        kernel = np.ones(k) / k
        self.speed = np.array(
            [np.convolve(np.nan_to_num(sp[:, c]), kernel, mode="same") for c in range(sp.shape[1])]
        ).T
        self.speed[np.isnan(self.d)] = np.nan
        self._reference_speeds()

    def _reference_speeds(self, bins: int = 200):
        """Normal speed at each spot of the lap, and caution (full-course yellow) periods."""
        import warnings

        n = len(self.t)
        i0, i1 = self.i_green, max(self.i_green + 1, self.idx_at(self.checkered))
        self.spot = np.floor(np.mod(np.nan_to_num(self.d), 1.0) * bins).astype(int).clip(0, bins - 1)
        base = (~self.pit) & (self.surf == ON_TRACK) & ~np.isnan(self.speed)
        base[:i0] = False
        base[i1:] = False
        self.caution = np.zeros(n, dtype=bool)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for _ in range(2):  # second pass excludes the caution laps from "normal"
                use = base & ~self.caution[:, None]
                ref = np.full(bins, np.nan)
                for b in range(bins):
                    v = self.speed[use & (self.spot == b)]
                    if v.size >= 5:
                        ref[b] = np.median(v)
                fallback = np.nanmedian(ref) if not np.isnan(ref).all() else 1.0 / max(self.ref_lap, 1)
                ref = np.where(np.isnan(ref), fallback, ref)
                self.spot_ref = ref[self.spot]
                ratio = np.nanmedian(np.where(base, self.speed / self.spot_ref, np.nan), axis=1)
                ratio = np.nan_to_num(ratio, nan=1.0)
                w = max(1, int(round(10.0 / max(np.median(np.diff(self.t)), 1e-3))))  # ~10 s
                smooth = np.convolve(ratio, np.ones(w) / w, mode="same")
                self.caution = (smooth < 0.8)
                self.caution[:i0] = False
                self.caution[i1:] = False
        self.caution |= self._caution_laps()
        self.caution |= self._pace_car_out()
        # bridge short gaps (catch-up / wave-around bursts inside one caution period)
        if self.caution.any():
            pace = self._pace_car_out()
            for a, b in list(_runs(self.caution, self.t, max_gap=30.0)):
                if self.t[b] - self.t[a] < 20 and not pace[a : b + 1].any():
                    self.caution[a : b + 1] = False  # a brief slow moment (e.g. the start), not a caution
                else:
                    self.caution[a : b + 1] = True
        self.caution[:i0] = False
        self.caution[i1:] = False

    def _pace_car_out(self) -> np.ndarray:
        """Definitive when the scan recorded the pace car: it's on track = caution."""
        ps = self.raw["samples"].get("pace_surf")
        out = np.zeros(len(self.t), dtype=bool)
        if ps and len(ps) == len(self.t):
            arr = np.asarray(ps)
            pit = np.asarray(self.raw["samples"].get("pace_pit") or [0] * len(ps)).astype(bool)
            out = (arr == ON_TRACK) & ~pit
        return out

    def _caution_laps(self) -> np.ndarray:
        """Whole laps run slowly by the field (behind the pace car, including catch-up laps).
        A lap counts as a caution lap when the field's typical time for it is 20% slower
        than normal, ignoring cars that pitted on that lap."""
        n = len(self.t)
        out = np.zeros(n, dtype=bool)
        lead = np.nanmax(np.where(np.isnan(self.dff), -np.inf, self.dff), axis=1)
        lead[np.isinf(lead)] = np.nan
        i0, i1 = self.i_green, self.idx_at(self.checkered)
        if i1 <= i0 + 2:
            return out
        # each car's line-crossing times (lap number -> time), and whether it pitted on that lap
        crossings = []
        for k in range(self.d.shape[1]):
            col = self.d[:, k]
            fl = np.floor(np.nan_to_num(col, nan=-1))
            idx = np.flatnonzero(np.diff(fl) == 1) + 1
            laps = {}
            for j in idx:
                if not np.isnan(col[j - 1]):
                    laps[int(fl[j])] = j
            crossings.append(laps)
        lap_nums = sorted({ln for c in crossings for ln in c})
        for ln in lap_nums:
            times = []
            for k, c in enumerate(crossings):
                if ln in c and ln + 1 in c and c[ln] >= i0:  # only laps started under racing
                    a, b = c[ln], c[ln + 1]
                    if self.pit[a : b + 1, k].any():
                        continue
                    times.append(self.t[b] - self.t[a])
            if len(times) < 3:
                continue
            if np.median(times) > 1.2 * self.ref_lap:
                # mark the leader's lap ln (from the leader crossing ln to crossing ln+1)
                a = np.flatnonzero(lead >= ln)
                b = np.flatnonzero(lead >= ln + 1)
                if a.size:
                    ia = a[0]
                    ib = b[0] if b.size else i1
                    out[ia : ib + 1] = True
        return out

    def under_caution(self, t: float) -> bool:
        return bool(self.caution[self.idx_at(t)])

    def _measured_lap(self) -> float:
        i0 = self.i_green
        i1 = max(i0 + 2, int(np.searchsorted(self.t, self.checkered)))
        laps = []
        for k in range(self.d.shape[1]):
            seg = self.d[i0:i1, k]
            ok = ~np.isnan(seg)
            if ok.sum() < 10:
                continue
            fl = np.floor(np.where(ok, seg, -1))
            cross = np.flatnonzero((np.diff(fl) == 1) & ok[1:] & ok[:-1]) + 1
            if cross.size >= 2:
                laps += list(np.diff(self.t[i0:i1][cross]))
        laps = [x for x in laps if x > 10]
        if laps:
            return float(np.median(laps))
        span = self.t[i1 - 1] - self.t[i0]
        lead = np.nanmax(self.d[i1 - 1]) - np.nanmax(self.d[i0])
        return float(span / lead) if lead > 0.5 else 90.0

    def idx_at(self, t: float) -> int:
        return int(min(len(self.t) - 1, np.searchsorted(self.t, t)))

    def order_at(self, t: float, car_class: str | None = None) -> list[int]:
        """CarIdx list in running order (race distance) at time t."""
        i = self.idx_at(t)
        row = self.dff[i]
        cols = [k for k in range(len(self.cars)) if not np.isnan(row[k])
                and not (t > self.last_seen[k] + 2 and self.last_seen[k] < self.checkered - 5)]
        if car_class is not None:
            cols = [k for k in cols if self.cls[k] == car_class]
        cols.sort(key=lambda k: -row[k])
        return [self.cars[k] for k in cols]

    def position(self, idx: int, t: float) -> int:
        cls = self.cls[self.col[idx]]
        order = self.order_at(t, cls)
        return order.index(idx) + 1 if idx in order else 0

    def lap_of(self, idx: int, t: float) -> int:
        v = self.dff[self.idx_at(t), self.col[idx]]
        return max(1, int(v)) if not np.isnan(v) else 0  # iRacing counts lap 1 from the green flag

    def gap_seconds(self, a: int, b: int, t: float) -> float:
        i = self.idx_at(t)
        return abs(self.d[i, self.col[a]] - self.d[i, self.col[b]]) * self.ref_lap

    def pit_related(self, col: int, t: float) -> bool:
        """True if the car is slowing for, in, or leaving the pit lane around time t
        (pit-lane entry braking starts well before iRacing flags the car as on pit road)."""
        i0, i1 = self.idx_at(t - 15), self.idx_at(t + 30)
        seg_pit = self.pit[i0 : i1 + 1, col]
        seg_surf = self.surf[i0 : i1 + 1, col]
        return bool(seg_pit.any() or np.isin(seg_surf, (1, 2)).any())

    def near_pit(self, col: int, t: float, window: float) -> bool:
        i0, i1 = self.idx_at(t - window), self.idx_at(t + window)
        return bool(self.pit[i0 : i1 + 1, col].any())


# ---------------------------------------------------------------- detectors
def detect_overtakes(r: Race, start_grace: float = 25.0) -> list[dict]:
    events = []
    n, c = r.d.shape
    on = (r.surf == ON_TRACK) & ~r.pit
    hold = 2.0
    for a in range(c):
        for b in range(a + 1, c):
            if r.cls[a] != r.cls[b]:
                continue
            diff = r.d[:, a] - r.d[:, b]
            ok = on[:, a] & on[:, b] & ~np.isnan(diff) & (np.abs(diff) < 0.5)
            if ok.sum() < 3 or np.nanmin(np.abs(np.where(ok, diff, np.inf))) * r.ref_lap > 1.5:
                continue  # these two were never close
            sign = np.sign(diff)
            okidx = np.flatnonzero(ok & (sign != 0))
            s = sign[okidx]
            ch = np.flatnonzero(s[1:] != s[:-1]) + 1
            flips = [int(okidx[j]) for j in ch if abs(diff[okidx[j]]) * r.ref_lap < 1.5]
            # debounce: keep a flip only if the new order holds for `hold` seconds
            confirmed = []
            for i in flips:
                j = min(n - 1, int(np.searchsorted(r.t, r.t[i] + hold)))
                seg = sign[i : j + 1][ok[i : j + 1]]
                if seg.size and np.all(seg == sign[i]):
                    confirmed.append(i)
            # collapse a pair's flips within 8 s into one moment
            groups = []
            for i in confirmed:
                if groups and r.t[i] - r.t[groups[-1][-1]] < 8.0:
                    groups[-1].append(i)
                else:
                    groups.append([i])
            for g in groups:
                i_first, i_last = g[0], g[-1]
                t = float(r.t[i_last])
                if t < r.green + start_grace or t > r.checkered + 2:
                    continue
                if r.near_pit(a, t, 25) or r.near_pit(b, t, 25):
                    continue
                if r.under_caution(t) or r.under_caution(t - 5):
                    continue
                before = -sign[i_first]  # a flip means the sign changed at i_first
                after = sign[i_last]
                # a "pass" only if the order actually ended up different
                if before == after:
                    # side by side, but the defender held on: credit the attacker anyway
                    front, back = (a, b) if after > 0 else (b, a)
                    passer, passed, swapped = r.cars[back], r.cars[front], False
                else:
                    winner = a if after > 0 else b
                    loser = b if winner == a else a
                    passer, passed, swapped = r.cars[winner], r.cars[loser], True
                pos = r.position(passer, t + 1.0)
                events.append(
                    dict(
                        type="overtake",
                        t=t,
                        cars=[passer, passed],
                        data=dict(
                            passer=passer,
                            passed=passed,
                            position=pos,
                            lap=r.lap_of(passer, t),
                            swaps=len(g),
                            completed=swapped,
                            lead_change=(pos == 1 and swapped),
                        ),
                    )
                )
    return events


def detect_battles(r: Race, gap_s: float, min_s: float) -> list[dict]:
    """Adjacent same-class cars within gap_s seconds for at least min_s seconds."""
    n, c = r.d.shape
    close: dict[tuple, list[int]] = {}
    i0 = r.i_green
    i1 = min(n, r.idx_at(r.checkered) + 1)
    on = ~r.pit & (r.surf != NOT_IN_WORLD) & ~r.caution[:, None]
    classes = sorted(set(r.cls))
    for i in range(i0, i1):
        row = r.d[i]
        for cl in classes:
            cols = [k for k in range(c) if r.cls[k] == cl and not np.isnan(row[k]) and on[i, k]]
            cols.sort(key=lambda k: -row[k])
            for f, bk in zip(cols, cols[1:]):
                if (row[f] - row[bk]) * r.ref_lap <= gap_s:
                    close.setdefault((r.cars[f], r.cars[bk]) if r.cars[f] < r.cars[bk] else (r.cars[bk], r.cars[f]), []).append(i)
    events = []
    for pair, idxs in close.items():
        mask = np.zeros(n, dtype=bool)
        mask[idxs] = True
        for a, b in _runs(mask, r.t, max_gap=2.5):
            dur = r.t[b] - r.t[a]
            if dur < min_s:
                continue
            ta, tb = float(r.t[a]), float(r.t[b])
            mid = (ta + tb) / 2
            ca, cb = r.col[pair[0]], r.col[pair[1]]
            front, back = (pair[0], pair[1]) if r.d[r.idx_at(mid), ca] >= r.d[r.idx_at(mid), cb] else (pair[1], pair[0])
            gaps = [abs(r.d[i, ca] - r.d[i, cb]) * r.ref_lap for i in range(a, b + 1)]
            t_key = float(r.t[a + int(np.nanargmin(gaps))])
            events.append(
                dict(
                    type="battle",
                    t=t_key,
                    start=ta,
                    end=tb,
                    cars=[back, front],
                    data=dict(
                        attacker=back,
                        defender=front,
                        position=r.position(front, mid),
                        duration=round(dur, 1),
                        min_gap=round(float(np.nanmin(gaps)), 2),
                        lap_from=r.lap_of(front, ta),
                        lap_to=r.lap_of(front, tb),
                    ),
                )
            )
    return events


def detect_incidents(r: Race, iracing_marks: list[dict] | None = None) -> list[dict]:
    n, c = r.d.shape
    i0, i1 = r.i_green, min(n, r.idx_at(r.checkered) + 1)  # nothing after the flag (cool-down lap)
    green_flag = ~r.caution
    spot_ref = r.spot_ref
    raw = []
    for k in range(c):
        idx = r.cars[k]
        # off-track excursions
        off = (r.surf[:, k] == OFF_TRACK) & ~r.pit[:, k]
        off[:i0] = False
        off[i1:] = False
        for a, b in _runs(off, r.t, max_gap=0.6):
            dur = r.t[b] - r.t[a]
            if dur < 0.8:
                continue
            if r.pit_related(k, float(r.t[a])):  # cutting across the pit-entry line isn't a moment
                continue
            raw.append(dict(t=float(r.t[a]), car=idx, kind="offtrack", dur=float(dur)))
        # big slowdowns (spins, crashes) under green
        slow = (r.speed[:, k] < 0.45 * spot_ref[:, k]) & green_flag & ~r.pit[:, k] & ~np.isnan(r.d[:, k])
        slow[:i0] = False
        slow[i1:] = False
        for a, b in _runs(slow, r.t, max_gap=1.0):
            if r.t[b] - r.t[a] < 1.0:
                continue
            if r.pit_related(k, float(r.t[a])):
                continue
            raw.append(dict(t=float(r.t[a]), car=idx, kind="spin", dur=float(r.t[b] - r.t[a])))
    marks = [m for m in iracing_marks or [] if m.get("idx") in r.col and r.green <= m["t"] <= r.checkered]
    # ignore markers on cars that aren't on track, and junk runs (the search sometimes just steps along one car)
    marks = [m for m in marks if r.surf[r.idx_at(m["t"]), r.col[m["idx"]]] != NOT_IN_WORLD]
    per_car: dict = {}
    for m in marks:
        per_car[m["idx"]] = per_car.get(m["idx"], 0) + 1
    marks = [m for m in marks if per_car[m["idx"]] <= 15 and not r.pit_related(r.col[m["idx"]], m["t"])]
    for m in marks:
        raw.append(dict(t=float(m["t"]), car=m["idx"], kind="marker", dur=0.0))
    raw.sort(key=lambda e: e["t"])

    # cluster: same car within 6 s, or several cars within 4 s at a similar track spot
    events: list[dict] = []
    for e in raw:
        k = r.col[e["car"]]
        spot = r.d[r.idx_at(e["t"]), k] % 1.0
        joined = None
        for ev in events[-6:]:
            if e["t"] - ev["data"]["t_last"] > 6:
                continue
            same_car = e["car"] in ev["cars"]
            ds = abs(((spot - ev["data"]["spot"]) + 0.5) % 1.0 - 0.5)
            if same_car or (e["t"] - ev["data"]["t_last"] < 4 and ds < 0.03):
                joined = ev
                break
        if joined:
            if e["car"] not in joined["cars"]:
                joined["cars"].append(e["car"])
            joined["data"]["kinds"].add(e["kind"])
            joined["data"]["t_last"] = e["t"]
            joined["data"]["dur"] = max(joined["data"]["dur"], e["dur"])
        else:
            events.append(
                dict(
                    type="incident",
                    t=e["t"],
                    cars=[e["car"]],
                    data=dict(kinds={e["kind"]}, t_last=e["t"], spot=float(spot), dur=e["dur"]),
                )
            )
    out = []
    for ev in events:
        kinds = ev["data"].pop("kinds")
        ev["data"].pop("t_last")
        severity = "crash" if len(ev["cars"]) > 1 and ("spin" in kinds or "marker" in kinds) else (
            "spin" if "spin" in kinds else ("offtrack" if "offtrack" in kinds else "contact")
        )
        ev["data"]["severity"] = severity
        ev["data"]["iracing_flagged"] = "marker" in kinds
        ev["data"]["positions"] = {str(c): r.position(c, ev["t"] - 1) for c in ev["cars"]}
        ev["data"]["lap"] = r.lap_of(ev["cars"][0], ev["t"])
        out.append(ev)
    return out


def detect_pits(r: Race) -> list[dict]:
    events = []
    for k, idx in enumerate(r.cars):
        p = r.pit[:, k].copy()
        p[: r.i_green] = False
        for a, b in _runs(p, r.t, max_gap=1.0):
            if r.t[b] - r.t[a] < 5:
                continue
            t = float(r.t[a])
            events.append(
                dict(
                    type="pit",
                    t=t,
                    cars=[idx],
                    data=dict(
                        duration=round(float(r.t[b] - r.t[a]), 1),
                        position_before=r.position(idx, t - 3),
                        lap=r.lap_of(idx, t),
                    ),
                )
            )
    return events


def detect_cautions(r: Race) -> list[dict]:
    """Full-course cautions (whole field slowed) and the restarts after them."""
    events = []
    for a, b in _runs(r.caution, r.t, max_gap=30.0):
        ta, tb = float(r.t[a]), float(r.t[b])
        if tb - ta < 20:
            continue
        leader = r.order_at(tb)[0] if r.order_at(tb) else None
        lap = r.lap_of(leader, ta) if leader is not None else 0
        events.append(dict(type="caution", t=ta, cars=[leader] if leader is not None else [],
                           data=dict(lap=lap, duration=round(tb - ta, 1))))
        if tb < r.checkered - 10:
            events.append(dict(type="restart", t=tb, cars=r.order_at(tb)[:3],
                               data=dict(lap=r.lap_of(leader, tb) if leader is not None else 0,
                                         leader=leader, order=r.order_at(tb)[:5])))
    return events


def start_and_finish(r: Race, focus: int | None) -> list[dict]:
    grid = r.order_at(r.green - 0.5)
    ev = [
        dict(
            type="start",
            t=r.green,
            cars=grid[:3],
            data=dict(grid=grid, pole=grid[0] if grid else None),
        )
    ]
    final_order = r.official_order or r.order_at(min(r.t[-1], r.checkered + 60))
    winner = final_order[0] if final_order else None
    ev.append(
        dict(
            type="finish",
            t=r.checkered,
            cars=[winner] if winner is not None else [],
            data=dict(order=final_order),
        )
    )
    # the focus driver's own finish, if it's well after the winner's
    if focus is not None and focus in r.col and focus != winner:
        k = r.col[focus]
        i0 = r.idx_at(r.checkered)
        crossed = np.flatnonzero(np.diff(np.floor(np.nan_to_num(r.d[i0:, k]))) > 0)
        if crossed.size:
            tf = float(r.t[i0 + crossed[0] + 1])
            if tf - r.checkered > 8:
                ev.append(
                    dict(
                        type="finish",
                        t=tf,
                        cars=[focus],
                        data=dict(focus_finish=True, position=(final_order.index(focus) + 1) if focus in final_order else r.position(focus, tf + 1)),
                    )
                )
    return ev


# ---------------------------------------------------------------- scoring
def score(ev: dict, focus: int | None, focus_mult: float) -> float:
    d = ev["data"]
    typ = ev["type"]
    if typ == "start":
        s = 1000.0
    elif typ == "finish":
        s = 900.0 if not d.get("focus_finish") else 500.0
    elif typ == "overtake":
        pos = d["position"] or 20
        s = 4.0 + max(0.0, 4.0 - 0.4 * (pos - 1))
        if d.get("lead_change"):
            s += 6
        if not d.get("completed"):
            s -= 1.5
        s += min(d.get("swaps", 1) - 1, 3) * 0.7  # back-and-forth is exciting
    elif typ == "battle":
        pos = d["position"] or 20
        s = 1.0 + min(d["duration"] / 40.0, 2.5) + max(0.0, 3.0 - 0.3 * (pos - 1))
        s += max(0.0, 0.5 - d["min_gap"]) * 2
    elif typ == "incident":
        s = {"crash": 9.0, "spin": 6.0, "contact": 3.0, "offtrack": 2.5}[d["severity"]]
        s += 1.0 * (len(ev["cars"]) - 1)
        if d.get("iracing_flagged"):
            s += 1.0
        best_pos = min([p for p in d["positions"].values() if p] or [20])
        s += max(0.0, 2.0 - 0.2 * (best_pos - 1))
    elif typ == "pit":
        s = 1.0
    elif typ == "caution":
        s = 1.5
    elif typ == "restart":
        s = 8.0
    else:
        s = 1.0
    if focus is not None and focus in ev["cars"] and typ not in ("start", "finish"):
        s *= focus_mult
    return round(s, 2)


def link_battles(events: list[dict]):
    """Note how many passes happened within each battle (adds drama + commentary context)."""
    passes = [e for e in events if e["type"] == "overtake"]
    for b in (e for e in events if e["type"] == "battle"):
        pair = set(b["cars"])
        times = [p["t"] for p in passes if set(p["cars"]) == pair and b["start"] - 3 <= p["t"] <= b["end"] + 3]
        b["data"]["passes"] = len(times)
        if times:  # the key moment of a battle is its last pass
            b["t"] = max(times)
            last = next(p for p in passes if p["t"] == b["t"] and set(p["cars"]) == pair)
            b["data"]["position"] = last["data"]["position"]
            b["data"]["attacker"], b["data"]["defender"] = last["data"]["passer"], last["data"]["passed"]
            b["cars"] = [last["data"]["passer"], last["data"]["passed"]]


def detect_all(race: dict, cfg) -> dict:
    r = Race(race)
    focus = resolve_focus(race, cfg)
    h = cfg.highlights
    events = []
    events += start_and_finish(r, focus)
    events += detect_overtakes(r)
    events += detect_battles(r, h.battle_gap, h.min_battle_seconds)
    events += detect_incidents(r, race.get("iracing_incidents"))
    events += detect_pits(r)
    events += detect_cautions(r)
    link_battles(events)
    for e in events:
        e.setdefault("start", e["t"])
        e.setdefault("end", e["t"])
        e["score"] = score(e, focus, cfg.focus.score_multiplier)
        if e["type"] == "battle":
            e["score"] = round(e["score"] + 1.2 * e["data"].get("passes", 0), 2)
    events.sort(key=lambda e: e["t"])
    for i, e in enumerate(events):
        e["id"] = i
    return dict(
        version=DETECT_VERSION,
        green=r.green,
        checkered=r.checkered,
        ref_lap=round(r.ref_lap, 3),
        focus=focus,
        events=events,
    )


def match_driver(race: dict, text) -> int | None:
    """Find a driver from what someone typed or picked: "all"/"" -> None (everyone equal),
    "auto" -> whoever recorded the replay, a car number ("28" or "#28"), or a name (full or part)."""
    want = str(text or "").strip()
    if not want or want.lower() in ("all", "none", "everyone"):
        return None
    ids = {d["idx"] for d in race["drivers"]}
    if want.lower() == "auto":
        f = race.get("focus_idx", -1)
        return f if f in ids else None
    num = want.lstrip("#").strip()
    for d in race["drivers"]:
        if d["number"] == num:
            return d["idx"]
    low = want.lower()
    for key in ("name", "short"):
        for d in race["drivers"]:
            if d[key].lower() == low:
                return d["idx"]
    hits = [d for d in race["drivers"] if len(low) >= 3 and low in d["name"].lower()]
    if len(hits) == 1:
        return hits[0]["idx"]
    raise ValueError(f"No driver matching '{want}' in this race" +
                     (f" ({len(hits)} match - be more specific)" if hits else ""))


def resolve_focus(race: dict, cfg) -> int | None:
    try:
        return match_driver(race, cfg.focus.car_number)
    except ValueError:
        return None
