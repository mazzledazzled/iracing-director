"""Assemble the final video with ffmpeg.

Every segment (title, each clip, results) is rendered on its own with:
- broadcast graphics drawn by graphics.py (position tower, lap counter, lower third)
- a slanted-band wipe that covers each cut, plus a whoosh, so the plain concat
  of segments looks like animated transitions
- commentary placed at its cue, with the game audio ducked underneath
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

WIPE = 0.30  # seconds for the band to cross the screen


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    if not exe:
        raise SystemExit("ffmpeg not found. Run setup.bat (it installs one) or install ffmpeg and add it to PATH.")
    return exe


def probe(path: Path) -> dict:
    ff = ffmpeg_exe()
    probe_exe = shutil.which("ffprobe")
    if probe_exe:
        out = subprocess.run([probe_exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                              "-of", "json", str(path)], capture_output=True, text=True)
        j = json.loads(out.stdout or "{}")
        return dict(duration=float(j.get("format", {}).get("duration", 0) or 0),
                    audio=any(s.get("codec_type") == "audio" for s in j.get("streams", [])))
    out = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    dur = 0.0
    for tok in out.split("Duration:")[1:2]:
        h, m, s = tok.split(",")[0].strip().split(":")
        dur = int(h) * 3600 + int(m) * 60 + float(s)
    return dict(duration=dur, audio="Audio:" in out)


def run_ff(args: list[str], what: str):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while rendering {what}:\n{res.stderr[-2500:]}")


def ensure_pillow():
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("  installing Pillow (one time)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"], check=True)


def make_whoosh(path: Path, rate: int = 48000):
    """Synthesised swoosh: noise swept through a resonant filter, with a quick swell."""
    import numpy as np

    n = int(0.55 * rate)
    t = np.arange(n) / rate
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    y1 = y2 = 0.0
    for i in range(n):  # sweeping two-pole band-pass
        f = 300 + 3200 * (t[i] / t[-1]) ** 1.5
        r = 0.985
        c = 2 * r * math.cos(2 * math.pi * f / rate)
        y = noise[i] * (1 - r) + c * y1 - r * r * y2
        y2, y1 = y1, y
        out[i] = y
    env = np.sin(np.pi * np.clip(t / t[-1], 0, 1)) ** 2
    out = out * env
    out = 0.6 * out / (np.abs(out).max() + 1e-9)
    st = np.stack([out, out], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def caption_for(ev: dict, race: dict, R, t_mid: float):
    """Default lower-third text for an event: (tag, main, sub, [car idx for colour chips])."""
    from .commentary import ordinal

    drv = {d["idx"]: d for d in race["drivers"]}
    nm = lambda i: drv.get(i, {}).get("short", "?")
    d, typ = ev["data"], ev["type"]
    if typ == "overtake":
        tag = "LEAD CHANGE" if d.get("lead_change") else "OVERTAKE"
        verb = "PASSES" if d.get("completed") else "ATTACKS"
        return (tag, f"{nm(d['passer'])} {verb} {nm(d['passed'])}",
                f"FOR {ordinal(d['position']).upper()}  ·  LAP {d['lap']}", [d["passer"], d["passed"]])
    if typ == "battle":
        lap = R.lap_of(d["defender"], t_mid)
        return ("BATTLE", f"{nm(d['attacker'])}  v  {nm(d['defender'])}",
                f"FOR {ordinal(d['position']).upper()}  ·  LAP {lap}", [d["attacker"], d["defender"]])
    if typ == "incident":
        tag = {"crash": "CRASH", "spin": "SPIN", "contact": "INCIDENT", "offtrack": "OFF TRACK"}[d["severity"]]
        return (tag, "  /  ".join(nm(c) for c in ev["cars"][:3]), f"LAP {d['lap']}", ev["cars"][:3])
    if typ == "start":
        pole = d.get("pole")
        sub = f"{len(race['drivers'])} DRIVERS" + (f"  ·  POLE: {nm(pole)}" if pole is not None else "")
        return ("RACE START", race["track_short"].upper(), sub, [])
    if typ == "restart":
        ld = d.get("leader")
        return ("RESTART", f"{nm(ld)} LEADS" if ld is not None else "GREEN FLAG", f"LAP {d.get('lap', '')}",
                [ld] if ld is not None else [])
    if typ == "finish":
        c = ev["cars"][0] if ev["cars"] else None
        if c is None:
            return ("CHEQUERED FLAG", "RACE OVER", "", [])
        car = drv.get(c, {}).get("car", "")
        if d.get("focus_finish"):
            return ("FINISH", f"{nm(c)} FINISHES {ordinal(d.get('position')).upper()}", car.upper(), [c])
        return ("CHEQUERED FLAG", f"{nm(c)} WINS", car.upper(), [c])
    if typ == "custom":
        return ("HIGHLIGHT", "  /  ".join(nm(c) for c in ev["cars"][:3]), f"LAP {d.get('lap', '')}", ev["cars"][:3])
    return None


class Renderer:
    def __init__(self, proj, cfg, race, det):
        ensure_pillow()
        from .detect import Race
        from .graphics import G

        self.proj, self.cfg, self.race, self.det = proj, cfg, race, det
        self.r = cfg.render
        self.W, self.H, self.FPS = int(self.r.width), int(self.r.height), int(self.r.fps)
        from .graphics import ensure_fonts

        ensure_fonts()
        self.g = G(self.W, self.H)
        self.R = Race(race)
        self.rd = proj.path("render")
        self.gfx = self.rd / "gfx"
        self.gfx.mkdir(parents=True, exist_ok=True)
        self.drv = {d["idx"]: d for d in race["drivers"]}
        st, self.S = self.g.stinger()
        self.stinger = self.gfx / "stinger.png"
        st.save(self.stinger)
        self.whoosh = self.gfx / "whoosh.wav"
        if not self.whoosh.exists():
            make_whoosh(self.whoosh)
        self.total_laps = self._total_laps()

    # ---------- helpers
    def px(self, v):
        return self.g.px(v)

    def enc(self) -> list[str]:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(self.r.crf), "-pix_fmt", "yuv420p",
                "-r", str(self.FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-video_track_timescale", "90000"]

    def _total_laps(self) -> int:
        res = self.race.get("results") or []
        laps = [r["laps"] for r in res if r.get("pos") == 1]
        if laps and laps[0] > 0:
            return int(laps[0])
        try:
            return int(self.race.get("session_laps"))
        except (TypeError, ValueError):
            return 0

    def color(self, idx):
        from .graphics import make_color

        return make_color(self.drv.get(idx, {}).get("car", ""))

    def short(self, idx):
        return self.drv.get(idx, {}).get("short", "?")

    def base_video(self, label="base") -> str:
        W, H = self.W, self.H
        return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={self.FPS}")

    def wipe(self, src: str, stinger_in: str, out_len: float, wipe_in: bool, wipe_out: bool, out: str) -> list[str]:
        """Overlay the slanted band: leaving the screen at the start, covering it at the end."""
        if not (wipe_in or wipe_out):
            return [f"{src}null{out}"]
        W, S, D = self.W, self.S, WIPE
        e1 = f"(t/{D})*(t/{D})*(3-2*t/{D})"
        p2 = f"((t-{out_len - D:.3f})/{D})"
        e2 = f"{p2}*{p2}*(3-2*{p2})"
        x_in = f"{-S}+{W + S}*{e1}"
        x_out = f"{-(W + 2 * S)}+{W + S}*{e2}"
        parts = []
        if wipe_in and wipe_out:
            x = f"if(lt(t,{D}),{x_in},{x_out})"
            en = f"lt(t,{D})+gte(t,{out_len - D:.3f})"
        elif wipe_in:
            x, en = x_in, f"lt(t,{D})"
        else:
            x, en = x_out, f"gte(t,{out_len - D:.3f})"
        parts.append(f"{stinger_in}format=rgba[stg]")
        parts.append(f"{src}[stg]overlay=x='{x}':y=0:enable='{en}'{out}")
        return parts

    def audio_mix(self, game: str, voices: list[tuple[Path, float]], first_input: int, out_len: float,
                  whoosh_input: int | None, whoosh_at: float | None) -> list[str]:
        f, labels = [], []
        cv = float(self.r.commentary_volume)
        for k, (_, at) in enumerate(voices):
            ms = int(at * 1000)
            fx = ""
            if self.r.get("voice_polish", True):
                # broadcast-style polish: trim rumble, gentle compression, a touch of presence and room
                fx = ("highpass=f=90,acompressor=threshold=0.1:ratio=3:attack=8:release=120:makeup=1.5,"
                      "equalizer=f=3000:t=q:w=1.2:g=2,aecho=0.85:0.5:18|32:0.12|0.07,")
            f.append(f"[{first_input + k}:a]aresample=48000,{fx}aformat=channel_layouts=stereo,volume={cv},"
                     f"adelay={ms}|{ms}[v{k}]")
            labels.append(f"[v{k}]")
        mixins = []
        if labels:
            if len(labels) == 1:
                f.append(f"{labels[0]}apad=whole_dur={out_len:.3f}[voice]")
            else:
                f.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
                         f"apad=whole_dur={out_len:.3f}[voice]")
            f.append("[voice]asplit=2[vsc][vmix]")
            f.append(f"{game}[vsc]sidechaincompress=threshold=0.015:ratio={self.r.duck_ratio}:attack=15:release=450[gd]")
            mixins = ["[gd]", "[vmix]"]
        else:
            mixins = [game]
        if whoosh_input is not None and whoosh_at is not None:
            ms = int(max(0, whoosh_at) * 1000)
            wv = float(self.r.get("whoosh_volume", 0.12))
            f.append(f"[{whoosh_input}:a]aresample=48000,volume={wv},adelay={ms}|{ms},apad=whole_dur={out_len:.3f}[wh]")
            mixins.append("[wh]")
        if len(mixins) == 1:
            f.append(f"{mixins[0]}alimiter=limit=0.95[a]")
        else:
            f.append(f"{''.join(mixins)}amix=inputs={len(mixins)}:normalize=0:duration=first,alimiter=limit=0.95[a]")
        return f

    # ---------- position tower
    def tower_stream(self, name: str, t0: float, length: float, hot: set) -> Path | None:
        """Write a concat list of tower images (one per change) covering [t0, t0+length]."""
        R, focus = self.R, self.det.get("focus")
        frames, last_key = [], None
        step = 1.0
        n = max(1, int(math.ceil(length / step)))
        for k in range(n + 1):
            t = t0 + k * step
            i = R.idx_at(t)
            order = R.order_at(t)
            if not order:
                continue
            lead = order[0]
            dl = R.dff[i, R.col[lead]]
            lap = R.lap_of(lead, t)
            caution = R.under_caution(t)
            if t >= R.checkered:
                header = "FINISH"
            elif caution:
                header = "CAUTION"
            elif self.total_laps and lap >= self.total_laps:
                header = "FINAL LAP"
            else:
                header = f"LAP {lap}" + (f" / {self.total_laps}" if self.total_laps else "")
            rows = []
            shown = order[:10]
            for pos, c in enumerate(order, start=1):
                if c not in shown and c != focus:
                    continue
                gap_l = dl - R.dff[i, R.col[c]]
                if pos == 1:
                    gap = "LEADER"
                elif gap_l >= 1.0:
                    nl = int(gap_l)
                    gap = f"+{nl} LAP" + ("S" if nl > 1 else "")
                else:
                    gap = f"+{gap_l * R.ref_lap:.1f}"
                rows.append(dict(pos=pos, name=self.short(c), color=self.color(c), gap=gap,
                                 focus=(c == focus), hot=(c in hot), sep=(c not in shown)))
            key = (header, tuple((r["pos"], r["name"], r["gap"]) for r in rows))
            if key == last_key:
                frames[-1][1] += step
                continue
            last_key = key
            img = self.g.tower(header, rows, caution=(header == "CAUTION"))
            p = self.gfx / f"{name}_tower_{len(frames):03d}.png"
            img.save(p)
            frames.append([p, step])
        if not frames:
            return None
        lst = self.gfx / f"{name}_tower.txt"
        body = "".join(f"file '{p.name}'\nduration {d:.3f}\n" for p, d in frames)
        body += f"file '{frames[-1][0].name}'\n"
        lst.write_text(body, encoding="utf-8")
        return lst

    # ---------- lower third content
    def lower_third_for(self, ev: dict, t_mid: float):
        c = caption_for(ev, self.race, self.R, t_mid)
        if not c:
            return None
        tag, main, sub, cars = c
        return tag, main, sub, [self.color(x) for x in cars]

    # ---------- segments
    def title(self, intro: dict | None, footage: tuple[Path, float] | None, makes: list[str], wipe_out=True) -> Path:
        dur = max(float(self.r.title_card_seconds), (intro["seconds"] + 1.6) if intro else 0)
        info = []
        if self.total_laps:
            info.append(f"{self.total_laps} laps")
        info.append(f"{len(self.race['drivers'])} drivers")
        img = self.g.title(self.race["track"], self.race.get("track_config", ""), makes, "  ·  ".join(info))
        tp = self.gfx / "title.png"
        img.save(tp)
        args, filters = [], []
        if footage:  # slow-motion, blurred footage of the start behind the title
            src, ss = footage
            args += ["-i", str(src)]
            filters.append(f"[0:v]trim=start={ss:.3f}:duration={dur / 2 + 0.2:.3f},setpts=2*(PTS-STARTPTS),"
                           f"{self.base_video()},gblur=sigma={self.px(6)},eq=brightness=-0.06:saturation=0.85,"
                           f"trim=duration={dur:.3f}[bg]")
        else:
            args += ["-f", "lavfi", "-i", f"color=c=0x0c0f14:s={self.W}x{self.H}:r={self.FPS}:d={dur:.3f}"]
            filters.append("[0:v]null[bg]")
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(tp),
                 "-loop", "1", "-t", f"{dur:.3f}", "-i", str(self.stinger),
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        slide = self.px(80)
        filters.append("[1:v]format=rgba,fade=t=in:st=0.2:d=0.5:alpha=1[tt]")
        filters.append(f"[bg][tt]overlay=x='-{slide}*pow(max(0,1-(t-0.2)/0.6),3)':y=0[v1]")
        filters += self.wipe("[v1]", "[2:v]", dur, False, wipe_out, "[v2]")
        filters.append("[v2]fade=t=in:st=0:d=0.5,format=yuv420p[vout]")
        filters.append(f"[3:a]atrim=duration={dur:.3f}[game]")
        voices = [(self.proj.path("audio") / intro["file"], 0.7)] if intro else []
        for p, _ in voices:
            args += ["-i", str(p)]
        args += ["-i", str(self.whoosh)]
        wh_idx = 4 + len(voices)
        filters += self.audio_mix("[game]", voices, 4, dur, wh_idx, dur - 0.4 if wipe_out else None)
        out = self.rd / "title.mp4"
        run_ff(args + ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[a]",
                       "-t", f"{dur:.3f}"] + self.enc() + [str(out)], "title")
        return out

    def clip(self, clip: dict, man: dict, lines: list[dict], primary: dict, progress=print) -> Path:
        src = self.proj.path("clips") / man["file"]
        info = probe(src)
        ss = max(0.0, clip["start"] - man["session_start"])
        L = max(1.0, min(clip["end"] - clip["start"], info["duration"] - ss))
        placed, prev_end = [], 0.0
        for ln in sorted(lines, key=lambda x: x["at"]):
            at = max(ln["at"] - clip["start"], prev_end + 0.25, 0.35)
            if at > L - 1.2:
                progress(f"    dropped a line that didn't fit: {ln['text'][:60]}")
                continue
            placed.append((self.proj.path("audio") / ln["file"], at))
            prev_end = at + ln["seconds"]
        hold = min(3.0, max(0.0, prev_end + 0.4 - L))
        out_len = L + hold
        name = f"seg_{clip['id']:02d}"
        t_start = man["session_start"] + ss  # race time of the first frame used
        hot = set(primary["cars"])
        tower = self.tower_stream(name, t_start, out_len, hot)
        if clip.get("caption") is not None:  # edited on the review page
            cap = clip["caption"]
            cars = list(dict.fromkeys(s["car"] for s in clip.get("shots", [])))[:2]
            lt = (cap.get("tag", ""), cap.get("main", ""), cap.get("sub", ""), [self.color(c) for c in cars]) \
                if (cap.get("tag") or cap.get("main")) else None
        else:
            lt = self.lower_third_for(primary, t_start + L / 2)
        lt_path = None
        if lt and self.r.show_lower_thirds:
            lt_path = self.gfx / f"{name}_lt.png"
            self.g.lower_third(*lt).save(lt_path)

        args = ["-i", str(src)]
        filters = [f"[0:v]trim=start={ss:.3f}:duration={L:.3f},setpts=PTS-STARTPTS,{self.base_video()}"
                   + (f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold > 0 else "") + "[v0]"]
        nxt, cur = 1, "[v0]"
        m = self.px(36)
        if tower:
            args += ["-f", "concat", "-safe", "0", "-i", str(tower)]
            filters.append(f"[{nxt}:v]fps={self.FPS},format=rgba,fade=t=in:st=0.25:d=0.4:alpha=1[tw]")
            filters.append(f"{cur}[tw]overlay=x={m}:y={m}:eof_action=repeat[vt]")
            cur, nxt = "[vt]", nxt + 1
        if lt_path:
            t_in, t_out = 0.7, max(1.5, min(6.5, out_len - 1.0))
            args += ["-loop", "1", "-t", f"{out_len:.3f}", "-i", str(lt_path)]
            filters.append(f"[{nxt}:v]format=rgba,fade=t=in:st={t_in}:d=0.2:alpha=1,"
                           f"fade=t=out:st={t_out:.2f}:d=0.35:alpha=1[lt]")
            x0 = self.px(60)
            filters.append(f"{cur}[lt]overlay=x='{x0}-({x0}+w)*pow(max(0,1-(t-{t_in})/0.4),3)':"
                           f"y=H-h-{self.px(60)}:enable='between(t,{t_in},{t_out + 0.4:.2f})'[vl]")
            cur, nxt = "[vl]", nxt + 1
        args += ["-loop", "1", "-t", f"{out_len:.3f}", "-i", str(self.stinger)]
        filters += self.wipe(cur, f"[{nxt}:v]", out_len, True, True, "[vw]")
        nxt += 1
        filters.append("[vw]format=yuv420p[vout]")
        gv = float(self.r.game_volume)
        if info["audio"]:
            filters.append(f"[0:a]atrim=start={ss:.3f}:duration={L:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                           f"aformat=channel_layouts=stereo,volume={gv},apad=whole_dur={out_len:.3f}[game]")
        else:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f"[{nxt}:a]atrim=duration={out_len:.3f}[game]")
            nxt += 1
        first_voice = nxt
        for p, _ in placed:
            args += ["-i", str(p)]
        args += ["-i", str(self.whoosh)]
        wh = first_voice + len(placed)
        filters += self.audio_mix("[game]", placed, first_voice, out_len, wh, out_len - 0.4)
        out = self.rd / f"{name}.mp4"
        run_ff(args + ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[a]",
                       "-t", f"{out_len:.3f}"] + self.enc() + [str(out)], f"clip {clip['id'] + 1}")
        return out

    def results(self, outro: dict | None, footage: Path | None) -> Path:
        dur = max(float(self.r.results_card_seconds), (outro["seconds"] + 1.6) if outro else 0)
        rows, fastest = self.results_rows()
        img = self.g.results(self.race["track"], rows, fastest)
        rp = self.gfx / "results.png"
        img.save(rp)
        args, filters = [], []
        if footage:
            args += ["-sseof", "-0.5", "-i", str(footage)]
            filters.append(f"[0:v]trim=duration=0.1,setpts=PTS-STARTPTS,{self.base_video()},"
                           f"tpad=stop_mode=clone:stop_duration={dur:.3f},gblur=sigma={self.px(14)},trim=duration={dur:.3f}[bg]")
        else:
            args += ["-f", "lavfi", "-i", f"color=c=0x0c0f14:s={self.W}x{self.H}:r={self.FPS}:d={dur:.3f}"]
            filters.append("[0:v]null[bg]")
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(rp),
                 "-loop", "1", "-t", f"{dur:.3f}", "-i", str(self.stinger),
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append("[1:v]format=rgba,fade=t=in:st=0.15:d=0.45:alpha=1[rt]")
        filters.append(f"[bg][rt]overlay=x=0:y='{self.px(40)}*pow(max(0,1-(t-0.15)/0.5),3)'[v1]")
        filters += self.wipe("[v1]", "[2:v]", dur, True, False, "[v2]")
        filters.append(f"[v2]fade=t=out:st={dur - 0.6:.2f}:d=0.6,format=yuv420p[vout]")
        filters.append(f"[3:a]atrim=duration={dur:.3f}[game]")
        voices = [(self.proj.path("audio") / outro["file"], 0.6)] if outro else []
        for p, _ in voices:
            args += ["-i", str(p)]
        filters += self.audio_mix("[game]", voices, 4, dur, None, None)
        out = self.rd / "results.mp4"
        run_ff(args + ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[a]",
                       "-t", f"{dur:.3f}"] + self.enc() + [str(out)], "results")
        return out

    def results_rows(self, n: int = 10):
        drv, focus = self.drv, self.det.get("focus")
        res = sorted((r for r in self.race.get("results") or [] if r["idx"] in drv and r.get("pos", 0) > 0),
                     key=lambda r: r["pos"])
        if not res:
            order = next((e["data"].get("order", []) for e in self.det["events"]
                          if e["type"] == "finish" and "order" in e["data"]), [])
            res = [dict(pos=i + 1, idx=c, fastest=0, reason="Running") for i, c in enumerate(order)]

        def lap(tm):
            return f"{int(tm // 60)}:{tm % 60:06.3f}" if tm and tm > 0 else "-"

        rows = []
        for r in res[:n]:
            d = drv[r["idx"]]
            st = r.get("reason", "Running")
            rows.append(dict(pos=r["pos"], name=d["name"], car=d.get("car", ""), color=self.color(r["idx"]),
                             best=lap(r.get("fastest")), status="" if st in ("Running", "") else
                             ("DNF" if st.lower().startswith(("disc", "retired", "dnf")) else st.upper()[:10]),
                             focus=(r["idx"] == focus)))
        fast = [r for r in res if r.get("fastest", 0) and r["fastest"] > 0]
        fastest = None
        if fast:
            b = min(fast, key=lambda r: r["fastest"])
            fastest = f"{drv[b['idx']]['short']}  {lap(b['fastest'])}"
        return rows, fastest


def render(proj, race: dict, det: dict, plan: dict, voice: dict, cfg, progress=print) -> Path:
    rr = Renderer(proj, cfg, race, det)
    manifest = proj.load("clips.json")
    by_id = {e["id"]: e for e in det["events"]}
    lines = voice["lines"]
    intro = next((l for l in lines if l["kind"] == "intro"), None)
    outro = next((l for l in lines if l["kind"] == "outro"), None)

    from .commentary import Facts

    facts = Facts(race, det)
    makes = []
    for d in race["drivers"]:
        m = facts.car(d["idx"])
        if m and m not in makes:
            makes.append(m)

    clips = [c for c in plan["clips"] if str(c["id"]) in manifest
             and (proj.path("clips") / manifest[str(c["id"])]["file"]).exists()]
    for c in clips:
        if abs(manifest[str(c["id"])]["session_start"] - c["start"]) > 5:
            raise SystemExit(f"Clip {c['id'] + 1} on disk was recorded for an older edit. Run:  run.bat capture")
    segs = []
    progress("  title")
    first = clips[0] if clips else None
    footage = None
    if first:
        m0 = manifest[str(first["id"])]
        footage = (proj.path("clips") / m0["file"], max(0.0, first["start"] - m0["session_start"]))
    segs.append(rr.title(intro, footage, makes))
    for c in clips:
        progress(f"  clip {c['id'] + 1}/{len(plan['clips'])}: {c['label']}")
        cl = [l for l in lines if l["kind"] == "clip" and l["clip"] == c["id"]]
        segs.append(rr.clip(c, manifest[str(c["id"])], cl, by_id[c["primary"]], progress))
    progress("  results")
    last = proj.path("clips") / manifest[str(clips[-1]["id"])]["file"] if clips else None
    segs.append(rr.results(outro, last))

    lst = rr.rd / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segs), encoding="utf-8")
    out = proj.path(f"{proj.dir.name}_highlights.mp4")
    run_ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)], "final video")
    return out
