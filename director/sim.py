"""Talking to iRacing through its SDK: reading session info, scanning the
replay at high speed, seeking, and switching cameras.

Only this file (and capture.py) need iRacing running. Everything downstream
works from the race.json this produces.
"""
from __future__ import annotations

import re
import time

try:
    import irsdk  # pyirsdk
except ImportError:  # allows offline steps / tests on machines without it
    irsdk = None


class SimError(RuntimeError):
    pass


SESSION_STATE_RACING = 4
SESSION_STATE_CHECKERED = 5
SESSION_STATE_COOLDOWN = 6


def clean_name(name: str) -> str:
    # iRacing appends digits to duplicate names ("John Smith2")
    return re.sub(r"\d+$", "", (name or "").strip()) or "Unknown"


def short_name(name: str) -> str:
    parts = clean_name(name).split()
    return parts[-1] if parts else name


class Sim:
    def __init__(self):
        if irsdk is None:
            raise SimError("pyirsdk isn't installed. Run setup.bat first.")
        self.ir = irsdk.IRSDK()

    # ---------- connection ----------
    def connect(self):
        if not self.ir.startup():
            raise SimError(
                "Couldn't connect to iRacing. Start iRacing and open your replay "
                "(iRacing UI > Replays > your race > Watch), then try again."
            )
        for _ in range(100):
            if self.ir.is_connected and self.ir["SessionInfo"]:
                return self
            time.sleep(0.1)
        raise SimError("Connected to iRacing but no session data yet. Is the replay open?")

    def close(self):
        try:
            self.ir.shutdown()
        except Exception:
            pass

    def tick(self):
        """Wait for the next telemetry update (~60 Hz) and freeze it for reading."""
        self.ir.freeze_var_buffer_latest()

    def __getitem__(self, key):
        return self.ir[key]

    # ---------- session info ----------
    def race_session_num(self) -> int:
        sessions = self.ir["SessionInfo"]["Sessions"]
        races = [s for s in sessions if str(s.get("SessionType", "")).lower() == "race"]
        if not races:
            raise SimError("This replay doesn't contain a race session.")
        # heat races: take the last (the feature)
        return int(races[-1]["SessionNum"])

    def describe(self) -> dict:
        ir = self.ir
        wk = ir["WeekendInfo"] or {}
        di = ir["DriverInfo"] or {}
        race_num = self.race_session_num()
        drivers = []
        for d in di.get("Drivers", []):
            if d.get("IsSpectator") or d.get("CarIsPaceCar") or d.get("CarIdx", -1) < 0:
                continue
            if str(d.get("UserName", "")).lower() == "pace car":
                continue
            drivers.append(
                dict(
                    idx=int(d["CarIdx"]),
                    name=clean_name(d.get("UserName", "")),
                    short=short_name(d.get("UserName", "")),
                    number=str(d.get("CarNumber", d["CarIdx"])),
                    car=d.get("CarScreenNameShort") or d.get("CarScreenName", ""),
                    car_class=d.get("CarClassShortName") or "",
                    irating=d.get("IRating", 0),
                    est_lap=float(d.get("CarClassEstLapTime") or 0) or None,
                )
            )
        cams = [
            dict(num=int(g["GroupNum"]), name=str(g["GroupName"]))
            for g in (ir["CameraInfo"] or {}).get("Groups", [])
        ]
        session = next(
            s for s in ir["SessionInfo"]["Sessions"] if int(s["SessionNum"]) == race_num
        )
        results = []
        for r in session.get("ResultsPositions") or []:
            results.append(
                dict(
                    pos=int(r.get("Position", 0)),
                    class_pos=int(r.get("ClassPosition", 0)) + 1,
                    idx=int(r.get("CarIdx", -1)),
                    laps=int(r.get("LapsComplete", 0)),
                    fastest=float(r.get("FastestTime", 0) or 0),
                    reason=str(r.get("ReasonOutStr", "")),
                    incidents=int(r.get("Incidents", 0)),
                )
            )
        car_names = sorted({d["car"] for d in drivers if d["car"]})
        return dict(
            track=wk.get("TrackDisplayName", "Unknown track"),
            track_short=wk.get("TrackDisplayShortName", wk.get("TrackDisplayName", "")),
            track_config=wk.get("TrackConfigName", ""),
            track_length=wk.get("TrackLength", ""),
            subsession=wk.get("SubSessionID", 0),
            weather=dict(
                air=wk.get("TrackAirTemp", ""),
                track=wk.get("TrackSurfaceTemp", ""),
                sky=wk.get("TrackSkies", ""),
            ),
            car_names=car_names,
            session_num=race_num,
            session_laps=session.get("SessionLaps", ""),
            focus_idx=int(di.get("DriverCarIdx", -1)),
            pace_idx=next((int(d["CarIdx"]) for d in di.get("Drivers", []) if d.get("CarIsPaceCar")), -1),
            drivers=drivers,
            cameras=cams,
            results=results,
        )

    # ---------- replay control ----------
    def pause(self):
        self.ir.replay_set_play_speed(0, False)

    def play(self, speed: int = 1):
        self.ir.replay_set_play_speed(int(speed), False)

    def hide_ui(self):
        self.ir.cam_set_state(irsdk.CameraState.ui_hidden)

    def seek(self, session_num: int, session_time: float, timeout: float = 15.0) -> bool:
        """Jump to a session time (seconds). Returns True once the replay is there."""
        self.ir.replay_search_session_time(int(session_num), int(max(0, session_time) * 1000))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.tick()
            if self.ir["SessionNum"] == session_num and abs(
                (self.ir["SessionTime"] or 0) - session_time
            ) < 1.5:
                return True
            time.sleep(0.05)
        return False

    def live_cameras(self) -> dict:
        """Camera groups loaded in iRacing right now (includes custom groups you made/loaded
        in the camera tool): lower-case name -> group number."""
        out = {}
        for g in (self.ir["CameraInfo"] or {}).get("Groups", []):
            out[str(g["GroupName"]).strip().lower()] = int(g["GroupNum"])
        return out

    def camera(self, car_number: str, group_num: int):
        self.ir.cam_switch_num(str(car_number), int(group_num), 0)

    # ---------- the fast analysis pass ----------
    def scan(self, info: dict, speed: int = 16, progress=print) -> dict:
        ir = self.ir
        sn = info["session_num"]
        idxs = [d["idx"] for d in info["drivers"]]
        self.pause()
        if not self.seek(sn, 0.0):
            progress("  (couldn't confirm seek to race start - scanning from where the replay is)")
        samples = dict(t=[], frame=[], state=[], flags=[], lap=[], pct=[], surf=[], pit=[], pos=[])
        pace = info.get("pace_idx", -1)
        if pace >= 0:  # the pace car on track means a caution
            samples["pace_surf"], samples["pace_pit"] = [], []
        self.play(speed)
        last_t, stalled_since, t0 = -1.0, None, time.time()
        last_print = 0.0
        while True:
            self.tick()
            if ir["SessionNum"] != sn:
                break
            t = ir["SessionTime"] or 0.0
            if t <= last_t + 1e-3:
                stalled_since = stalled_since or time.time()
                if time.time() - stalled_since > 3.0:  # end of replay
                    break
                continue
            stalled_since = None
            last_t = t
            lap, pct = ir["CarIdxLap"], ir["CarIdxLapDistPct"]
            surf, pit, pos = ir["CarIdxTrackSurface"], ir["CarIdxOnPitRoad"], ir["CarIdxPosition"]
            samples["t"].append(round(t, 3))
            samples["frame"].append(ir["ReplayFrameNum"])
            samples["state"].append(ir["SessionState"])
            samples["flags"].append(ir["SessionFlags"])
            samples["lap"].append([lap[i] for i in idxs])
            samples["pct"].append([round(pct[i], 5) for i in idxs])
            samples["surf"].append([surf[i] for i in idxs])
            samples["pit"].append([1 if pit[i] else 0 for i in idxs])
            samples["pos"].append([pos[i] for i in idxs])
            if pace >= 0:
                samples["pace_surf"].append(surf[pace])
                samples["pace_pit"].append(1 if pit[pace] else 0)
            if time.time() - last_print > 2:
                last_print = time.time()
                progress(f"  scanning... race time {int(t // 60)}:{int(t % 60):02d}")
            if time.time() - t0 > 3 * 3600:
                break
        self.pause()
        return dict(cars=idxs, samples=samples)

    def iracing_incidents(self, info: dict, progress=print, limit: int = 400) -> list[dict]:
        """Step through iRacing's own incident markers in the race session."""
        ir = self.ir
        sn = info["session_num"]
        out = []
        self.pause()
        self.seek(sn, 0.0)
        last_frame = ir["ReplayFrameNum"]
        misses = 0
        for _ in range(limit):
            ir.replay_search(irsdk.RpySrchMode.next_incident)
            moved = False
            for _ in range(40):
                time.sleep(0.05)
                self.tick()
                if ir["ReplayFrameNum"] != last_frame:
                    moved = True
                    break
            if not moved:
                misses += 1
                if misses >= 2:
                    break
                continue
            time.sleep(0.25)
            self.tick()
            last_frame = ir["ReplayFrameNum"]
            if ir["SessionNum"] != sn:
                break
            out.append(dict(t=round(ir["SessionTime"], 2), idx=int(ir["CamCarIdx"])))
            # junk detection: the search sometimes just steps along one car at a fixed interval
            if len(out) >= 8:
                last = out[-8:]
                gaps = [b["t"] - a["t"] for a, b in zip(last, last[1:])]
                if len({m["idx"] for m in last}) == 1 and max(gaps) - min(gaps) < 0.6:
                    progress("  iRacing's incident list looks unreliable for this replay - ignoring it")
                    return []
        progress(f"  iRacing flagged {len(out)} incident markers")
        return out
