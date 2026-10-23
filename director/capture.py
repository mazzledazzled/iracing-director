"""Record each planned clip: seek the replay, set the camera, record with OBS."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .sim import Sim, SimError


class OBS:
    def __init__(self, cfg):
        try:
            import obsws_python as obs
        except ImportError as e:
            raise SystemExit("obsws-python isn't installed. Run setup.bat first.") from e
        c = cfg.capture
        try:
            self.cl = obs.ReqClient(host=c.obs_host, port=int(c.obs_port), password=c.obs_password or "", timeout=5)
        except Exception as e:
            raise SystemExit(
                "Couldn't reach OBS. Open OBS, then Tools > WebSocket Server Settings: tick "
                "'Enable WebSocket server' and put the port/password into config.toml.\n"
                f"  ({e})"
            ) from e

    def recording(self) -> bool:
        return bool(self.cl.get_record_status().output_active)

    def start(self):
        if self.recording():
            self.cl.stop_record()
            time.sleep(1.0)
        self.cl.start_record()
        for _ in range(50):
            if self.recording():
                return
            time.sleep(0.05)

    def stop(self) -> Path:
        resp = self.cl.stop_record()
        path = getattr(resp, "output_path", None)
        for _ in range(60):
            if not self.recording():
                break
            time.sleep(0.1)
        if not path:
            raise RuntimeError("OBS didn't report where it saved the recording (update OBS to 28+).")
        return Path(path)


def _move_when_free(src: Path, dst: Path, timeout: float = 20.0):
    deadline = time.time() + timeout
    while True:
        try:
            shutil.move(str(src), str(dst))
            return
        except (PermissionError, OSError):
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def clip_sig(clip: dict) -> str:
    """Fingerprint of what a clip records: if it's unchanged, the existing recording is reused."""
    shots = [(round(s["at"], 1), s["car"], s["cam"]) for s in clip.get("shots", [])]
    return json.dumps([round(clip["start"], 1), round(clip["end"], 1), shots])


def reuse_existing(proj, plan: dict, force: bool = False, manifest_name: str = "clips.json") -> tuple[dict, list[int]]:
    """Map plan clips onto recordings that already match; return (new manifest, ids still to record)."""
    old = proj.load(manifest_name) if proj.has(manifest_name) else {}
    by_sig = {m.get("sig"): m for m in old.values() if m.get("sig")}
    manifest, todo = {}, []
    for c in plan["clips"]:
        m = by_sig.get(clip_sig(c))
        if m and not force and proj.path("clips").joinpath(m["file"]).exists():
            manifest[str(c["id"])] = m
        else:
            todo.append(c["id"])
    return manifest, todo


def capture(proj, race: dict, plan: dict, cfg, only: list[int] | None = None, progress=print,
            force: bool = False, manifest_name: str = "clips.json", prefix: str = "clip") -> dict:
    manifest, todo = reuse_existing(proj, plan, force or only is not None, manifest_name)
    if only is not None:
        todo = [c for c in todo if c in only]
        old = proj.load(manifest_name) if proj.has(manifest_name) else {}
        for c in plan["clips"]:  # keep the other clips' recordings as they are
            if c["id"] not in only and str(c["id"]) in old and str(c["id"]) not in manifest:
                manifest[str(c["id"])] = old[str(c["id"])]
    proj.save(manifest_name, manifest)
    if not todo:
        progress("  every clip is already recorded and unchanged - nothing to do")
        return manifest
    if len(todo) < len(plan["clips"]):
        progress(f"  {len(plan['clips']) - len(todo)} clips unchanged (kept), recording {len(todo)}")
    sim = Sim().connect()
    info_now = sim.describe()
    if info_now.get("subsession") != race.get("subsession") or info_now["track"] != race["track"]:
        raise SimError(
            f"The replay open in iRacing ({info_now['track']}) isn't the one this project was scanned from "
            f"({race['track']}). Open the right replay or run scan again."
        )
    obs = OBS(cfg)
    sn = race["session_num"]
    settle = float(cfg.capture.settle_seconds)
    try:
        for clip in plan["clips"]:
            cid = clip["id"]
            if cid not in todo:
                continue
            progress(f"  clip {cid + 1}/{len(plan['clips'])}: {clip['label']}  ({clip['end'] - clip['start']:.0f}s)")
            shots = list(clip["shots"])
            sim.pause()
            lead_in = 1.0
            ok = False
            for _ in range(3):
                if sim.seek(sn, clip["start"] - lead_in):
                    ok = True
                    break
                time.sleep(1.0)
            if not ok or sim["SessionNum"] != sn:
                progress("    couldn't jump to this moment in the race session - skipped (try again with --clips)")
                continue
            if shots:
                sim.camera(shots[0]["number"], shots[0]["cam"])
            if cfg.capture.hide_ui:
                sim.hide_ui()
            time.sleep(settle)
            # let the replay run for the lead-in so the camera is settled, then record
            sim.play(1)
            wait_until = time.time() + lead_in + 10
            while time.time() < wait_until:
                sim.tick()
                if sim["SessionNum"] != sn:
                    break
                if (sim["SessionTime"] or 0) >= clip["start"] - 0.15:
                    break
            if sim["SessionNum"] != sn or (sim["SessionTime"] or 0) < clip["start"] - 1:
                sim.pause()
                progress("    replay isn't at the right moment - skipped (try again with --clips)")
                continue
            obs.start()
            rec_t0 = time.time()
            t_rec = sim["SessionTime"]
            next_shot = 1
            last_t, stall = t_rec, time.time()
            while True:
                sim.tick()
                t = sim["SessionTime"] or 0.0
                while next_shot < len(shots) and t >= shots[next_shot]["at"]:
                    s = shots[next_shot]
                    sim.camera(s["number"], s["cam"])
                    next_shot += 1
                if t >= clip["end"]:
                    break
                if t > last_t:
                    last_t, stall = t, time.time()
                elif time.time() - stall > 5:
                    progress("    replay stopped advancing - ending clip early")
                    break
                if sim["SessionNum"] != sn:
                    break
            sim.pause()
            src = obs.stop()
            dst = proj.path("clips") / f"{prefix}_{cid:02d}_{int(time.time())}{src.suffix}"  # unique: old takes may be reused
            _move_when_free(src, dst)
            manifest[str(cid)] = dict(
                file=dst.name,
                sig=clip_sig(clip),
                session_start=round(float(t_rec), 3),  # race time at the first recorded frame
                wall_seconds=round(time.time() - rec_t0, 2),
            )
            proj.save(manifest_name, manifest)
    finally:
        try:
            if obs.recording():
                obs.cl.stop_record()
        except Exception:
            pass
        sim.pause()
        sim.close()
    return manifest
