""""A race in 30": a vertical (9:16) short of one driver's race - a few seconds of
each moment, specific camera angles, punchy captions, no commentary.

    run.bat short                 scan the open replay, pick moments, record, build the video
    run.bat short --reuse         use the last scanned race (no new scan)
    run.bat short --driver 28     whose race it is (car number or name; default: whoever recorded it)
    run.bat short --seconds 60    a longer cut

Output: output/<race>/<race>_race_in_30.mp4 plus short_clips/clean/beat_XX.mp4 (no text) for TikTok's editor.
"""
from __future__ import annotations

from pathlib import Path

PLAN = "plan_short.json"
MANIFEST = "clips_short.json"

DEFAULT_CAMS = {
    "start": ["Chopper", "Blimp"],
    "overtake": ["Chase", "TV1"],
    "lost": ["TV1", "Far Chase"],
    "battle": ["Far Chase", "Chase"],
    "incident": ["TV2", "TV1"],
    "restart": ["Chase"],
    "finish": ["TV1", "Blimp"],
    "custom": ["TV1"],
}
# seconds before / after the key moment that each beat shows
BEAT_SHAPE = {
    "start": (1.0, 3.0), "overtake": (2.3, 1.2), "lost": (2.3, 1.2), "battle": (2.2, 1.0),
    "incident": (1.0, 2.4), "restart": (0.5, 2.8), "finish": (2.6, 0.8), "custom": (1.5, 1.5),
}


def _sc(cfg):
    return cfg.get("short") or {}


def resolve_driver(race: dict, want: str | None):
    from .detect import match_driver

    try:
        return match_driver(race, want if want is not None else "auto")
    except ValueError as e:
        raise SystemExit(f"{e}. Use a car number, e.g. --driver 28, or a name.") from e


def kind_for(ev: dict, me) -> str:
    """Beat kind from the driver's point of view."""
    t = ev["type"]
    if t == "overtake" and me is not None and ev["data"].get("passed") == me:
        return "lost"
    return t if t in BEAT_SHAPE else "custom"


def plan_short(race: dict, det: dict, cfg, driver=None, seconds: float = 30.0) -> dict:
    from .detect import Race
    from .plan import _resolve_cam

    sc = _sc(cfg)
    R = Race(race)
    me = driver
    cams = race.get("cameras", [])
    beat_len = float(sc.get("beat_seconds", 3.2))
    end_card = float(sc.get("end_card_seconds", 2.2))
    events = [e for e in det["events"] if e["type"] not in ("pit", "caution")]
    start = next((e for e in events if e["type"] == "start"), None)
    finish_all = [e for e in events if e["type"] == "finish"]
    finish = None
    if me is not None:
        finish = next((e for e in finish_all if e["cars"] and e["cars"][0] == me), None)
    finish = finish or (finish_all[0] if finish_all else None)

    def involves(e):
        return me is None or me in e["cars"]

    def worth(e):
        s = e["score"]
        if me is not None and involves(e):
            s *= 3.0  # it's their race
        if e["type"] == "battle":
            s *= 0.75 if e["data"].get("passes") else 0.45  # in 3 seconds a pass beats "they were close"
        if e["type"] == "incident":
            s *= 1.15  # spins and crashes are gold on TikTok
        return s

    fixed = [e for e in (start, finish) if e]
    budget = seconds - end_card - sum(sum(BEAT_SHAPE[kind_for(e, me)]) for e in fixed)
    n_beats = max(1, int(budget // beat_len))
    pool = sorted((e for e in events if e not in fixed and e["score"] >= 2.0), key=worth, reverse=True)
    if me is not None:  # the driver's own moments first, then fill with the race's best
        pool = [e for e in pool if involves(e)] + [e for e in pool if not involves(e)]
    chosen = []
    while pool and len(chosen) < n_beats:
        # variety: each repeat of a kind of moment counts for less (no five battles in a row)
        kinds = [kind_for(c, me) for c in chosen]
        def value(e):
            v = worth(e) * (0.7 ** kinds.count(kind_for(e, me)))
            return v * (2.0 if (me is not None and involves(e)) else 1.0)
        e = max(pool, key=value)
        pool.remove(e)
        if all(abs(e["t"] - c["t"]) > 12 for c in chosen + fixed):  # spread across the race
            chosen.append(e)
    beats = sorted(fixed + chosen, key=lambda e: e["t"])

    rot = {}
    clips = []
    for i, e in enumerate(beats):
        k = kind_for(e, me)
        pre, post = BEAT_SHAPE[k]
        if k not in ("start", "finish"):  # stretch/shrink middle beats to the beat length
            scale = beat_len / (pre + post)
            pre, post = pre * scale, post * scale
        a, b = max(R.t[0], e["t"] - pre), e["t"] + post
        follow = me if (me is not None and me in e["cars"]) else _subject(e)
        if follow is None:
            continue
        names = list(sc.get(f"{k}_cameras") or DEFAULT_CAMS[k])
        cam = _resolve_cam(names[rot.get(k, 0) % len(names)], cams)
        rot[k] = rot.get(k, 0) + 1
        num = next((d["number"] for d in race["drivers"] if d["idx"] == follow), "1")
        clips.append(dict(id=len(clips), start=round(a, 2), end=round(b, 2), events=[e["id"]], primary=e["id"],
                          type=e["type"], kind=k, label=f"{k.upper()}  |  {short_name(race, follow)}",
                          shots=[dict(at=round(a, 2), car=follow, number=num, cam=cam["num"],
                                      cam_name=cam["name"], role=k, ev=e["id"])]))
    return dict(clips=clips, driver=me, seconds=seconds, total_seconds=round(sum(c["end"] - c["start"] for c in clips), 1))


def _subject(e):
    d = e["data"]
    if e["type"] == "overtake":
        return d["passer"]
    if e["type"] == "battle":
        return d["attacker"]
    if e["type"] == "start":
        return d.get("pole")
    return e["cars"][0] if e["cars"] else None


def short_name(race, idx):
    return next((d["short"] for d in race["drivers"] if d["idx"] == idx), "?")


# ------------------------------------------------------------------ captions
def beat_caption(clip: dict, ev: dict | None, race: dict, R, me) -> tuple[str, str]:
    """(tag, main) - short and punchy, from the driver's point of view."""
    if clip.get("caption") is not None and (clip["caption"].get("tag") or clip["caption"].get("main")):
        return clip["caption"].get("tag", ""), clip["caption"].get("main", "")
    nm = lambda i: short_name(race, i).upper()
    k = clip.get("kind") or (kind_for(ev, me) if ev else "custom")
    d = ev["data"] if ev else {}
    if k == "start":
        if me is not None:
            return "LIGHTS OUT", f"STARTING P{R.position(me, ev['t'] - 0.5)}"
        return "LIGHTS OUT", race["track_short"].upper()
    if k == "overtake":
        if me is not None and d.get("passer") == me:
            if d.get("position") == 1:
                return "INTO THE LEAD", "NOW LEADING"
            return "OVERTAKE", f"UP TO P{d['position']}"
        return ("LEAD CHANGE" if d.get("lead_change") else "OVERTAKE"), f"{nm(d['passer'])} GETS {nm(d['passed'])}"
    if k == "lost":
        return "PASSED", f"DOWN TO P{d.get('position', 0) + 1}"
    if k == "battle":
        return "BATTLE", f"FOR P{d['position']}"
    if k == "incident":
        tag = {"crash": "CRASH!", "spin": "SPIN!", "contact": "CONTACT", "offtrack": "OFF!"}[d["severity"]]
        return tag, "  /  ".join(nm(c) for c in ev["cars"][:2])
    if k == "restart":
        return "RESTART", "GREEN GREEN GREEN"
    if k == "finish":
        pos = finish_pos(race, me) if me is not None else 1
        return "CHEQUERED FLAG", (f"P{pos}" if me is not None else f"{nm(ev['cars'][0])} WINS")
    return "HIGHLIGHT", (clip.get("caption") or {}).get("main", "")


def finish_pos(race, me):
    res = sorted((r for r in race.get("results") or [] if r.get("pos", 0) > 0), key=lambda r: r["pos"])
    for r in res:
        if r["idx"] == me:
            return r["pos"]
    return 0


# ------------------------------------------------------------------ graphics
class VG:
    """Vertical graphics (1080x1920 proportions)."""

    def __init__(self, W, H):
        from .graphics import G

        self.g = G(W, H)
        self.W, self.H = W, H
        self.s = W / 1080.0

    def px(self, v):
        return int(round(v * self.s))

    def f(self, w, size):
        from .graphics import font

        return font(w, max(8, self.px(size)))

    def overlay(self, *, title: tuple | None, header: str, pos: int | None, lap: str, tag: str, main: str,
                me_color) -> "object":
        from PIL import Image, ImageDraw

        from .graphics import ACCENT, BG, GOLD, WHITE

        W, H, p = self.W, self.H, self.px
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        tw = lambda t, f: int(d.textlength(t, font=f))
        # header (small) or big title on the first beat
        if title:
            big, track, who = title
            tf = self.f("black", 112)
            d.polygon([(p(60), p(210)), (p(60) + tw(big, tf) + p(70), p(210)), (p(60) + tw(big, tf) + p(40), p(350)),
                       (p(60), p(350))], fill=ACCENT + (255,))
            d.text((p(84), p(222)), big, font=tf, fill=WHITE)
            d.text((p(66), p(370)), track.upper(), font=self.f("xbold", 64), fill=WHITE)
            if who:
                d.text((p(66), p(446)), who.upper(), font=self.f("semi", 46), fill=(220, 226, 234))
        else:
            hf = self.f("bold", 38)
            d.rectangle([p(60), p(200), p(60) + tw(header, hf) + p(40), p(262)], fill=BG + (200,))
            d.rectangle([p(60), p(200), p(68), p(262)], fill=ACCENT + (255,))
            d.text((p(84), p(206)), header, font=hf, fill=WHITE)
        # position badge
        if pos:
            bx, by, bw = W - p(60) - p(190), p(200), p(190)
            d.rectangle([bx, by, bx + bw, by + p(150)], fill=(GOLD if pos == 1 else (16, 19, 25)) + (235,))
            pf = self.f("black", 110)
            t = f"P{pos}"
            d.text((bx + (bw - tw(t, pf)) // 2, by + p(4)), t, font=pf, fill=(15, 15, 15) if pos == 1 else WHITE)
            lf = self.f("semi", 34)
            d.rectangle([bx, by + p(150), bx + bw, by + p(204)], fill=ACCENT + (255,))
            d.text((bx + (bw - tw(lap, lf)) // 2, by + p(156)), lap, font=lf, fill=WHITE)
        # caption under the video
        if tag or main:
            y = p(1395)
            tf2 = self.f("black", 54)
            mf = self.f("black", 104)
            if tag:
                w2 = tw(tag, tf2) + p(56)
                x = (W - w2) // 2
                d.polygon([(x, y), (x + w2 + p(16), y), (x + w2, y + p(76)), (x - p(16), y + p(76))], fill=ACCENT + (255,))
                d.text((x + p(28), y + p(4)), tag, font=tf2, fill=WHITE)
                y += p(90)
            if main:
                size = 104
                while size > 50 and tw(main, self.f("black", size)) > W - p(120):
                    size -= 6
                mf = self.f("black", size)
                x = (W - tw(main, mf)) // 2
                # shadow for readability over bright footage
                d.text((x + p(4), y + p(5)), main, font=mf, fill=(0, 0, 0, 170))
                d.text((x, y), main, font=mf, fill=WHITE)
        return img

    def end_card(self, track: str, who: str, pos: int, podium: list[str], me_color) -> "object":
        from PIL import Image, ImageDraw

        from .graphics import ACCENT, BG, GOLD, WHITE

        W, H, p = self.W, self.H, self.px
        img = Image.new("RGBA", (W, H), BG + (150,))
        d = ImageDraw.Draw(img)
        tw = lambda t, f: int(d.textlength(t, font=f))
        lab = "FINISHED"
        lf = self.f("black", 90)
        d.text(((W - tw(lab, lf)) // 2, p(560)), lab, font=lf, fill=WHITE)
        big = f"P{pos}" if pos else "DONE"
        bf = self.f("black", 380)
        col = GOLD if pos == 1 else WHITE
        d.text(((W - tw(big, bf)) // 2 + p(6), p(660) + p(8)), big, font=bf, fill=(0, 0, 0, 160))
        d.text(((W - tw(big, bf)) // 2, p(660)), big, font=bf, fill=col)
        d.rectangle([p(240), p(1130), W - p(240), p(1138)], fill=ACCENT + (255,))
        sf = self.f("xbold", 54)
        for i, line in enumerate([track.upper(), who.upper()]):
            if line:
                d.text(((W - tw(line, sf)) // 2, p(1170) + i * p(70)), line, font=sf, fill=(225, 230, 236))
        pf = self.f("semi", 40)
        for i, line in enumerate(podium[:3]):
            d.text(((W - tw(line, pf)) // 2, p(1340) + i * p(54)), line, font=pf, fill=(190, 198, 210))
        return img

    def bar(self) -> "object":
        from PIL import Image

        from .graphics import ACCENT

        return Image.new("RGBA", (self.W, self.px(10)), ACCENT + (255,))


# ------------------------------------------------------------------ render
def render_short(proj, race: dict, det: dict, plan: dict, cfg, progress=print) -> Path:
    from .detect import Race
    from .graphics import make_color
    from .render import ensure_pillow, probe, run_ff

    ensure_pillow()
    from .graphics import ensure_fonts

    ensure_fonts()
    sc = _sc(cfg)
    W, H = int(sc.get("width", 1080)), int(sc.get("height", 1920))
    FPS = int(sc.get("fps", 30))
    layout = str(sc.get("layout", "fit")).lower()
    zoom = float(sc.get("zoom", 1.35))
    R = Race(race)
    me = plan.get("driver")
    vg = VG(W, H)
    manifest = proj.load(MANIFEST)
    by_id = {e["id"]: e for e in det["events"]}
    out_dir = proj.path("short")
    gfx = out_dir / "gfx"
    clean_dir = out_dir / "clean"
    for d in (out_dir, gfx, clean_dir):
        d.mkdir(parents=True, exist_ok=True)
    bar_p = gfx / "bar.png"
    vg.bar().save(bar_p)
    total = float(plan.get("seconds", 30))
    me_name = short_name(race, me) if me is not None else ""
    me_num = next((d["number"] for d in race["drivers"] if d["idx"] == me), "")
    header = f"{race['track_short'].upper()}" + (f"  ·  #{me_num} {me_name.upper()}" if me is not None else "")
    total_laps = next((r["laps"] for r in race.get("results") or [] if r.get("pos") == 1), 0)
    me_color = make_color(next((d.get("car", "") for d in race["drivers"] if d["idx"] == me), ""))

    def video_chain(src_label: str, ss: float, L: float, out: str) -> list[str]:
        if layout == "crop":
            return [f"{src_label}trim=start={ss:.3f}:duration={L:.3f},setpts=PTS-STARTPTS,"
                    f"scale=-2:{H},crop={W}:{H},setsar=1,fps={FPS}{out}"]
        fw = int(W * zoom) // 2 * 2
        return [
            f"{src_label}trim=start={ss:.3f}:duration={L:.3f},setpts=PTS-STARTPTS,fps={FPS},split=2[bgs][fgs]",
            f"[bgs]scale=-2:{H},crop={W}:{H},gblur=sigma=28,eq=brightness=-0.18:saturation=0.8[bgv]",
            f"[fgs]scale={fw}:-2,crop={min(fw, W)}:ih[fgv]",
            f"[bgv][fgv]overlay=x=(W-w)/2:y=(H-h)/2-{vg.px(40)},setsar=1{out}",
        ]

    clips = [c for c in plan["clips"] if str(c["id"]) in manifest and proj.path("clips").joinpath(manifest[str(c["id"])]["file"]).exists()]
    if not clips:
        raise SystemExit("No short clips recorded yet. Run:  run.bat short --reuse")
    segs, t_global = [], 0.0
    enc = ["-c:v", "libx264", "-preset", "medium", "-crf", str(sc.get("crf", 20)), "-pix_fmt", "yuv420p",
           "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-video_track_timescale", "90000"]
    for n, c in enumerate(clips):
        man = manifest[str(c["id"])]
        src = proj.path("clips") / man["file"]
        info = probe(src)
        ss = max(0.0, c["start"] - man["session_start"])
        L = max(0.8, min(c["end"] - c["start"], info["duration"] - ss))
        ev = by_id.get(c.get("primary"))
        t_mid = c["start"] + L / 2
        tag, main = beat_caption(c, ev, race, R, me)
        pos = R.position(me, c["end"]) if me is not None else None
        if c.get("kind") == "finish" and me is not None:
            pos = finish_pos(race, me) or pos
        lead = R.order_at(t_mid)
        lap = R.lap_of(lead[0], t_mid) if lead else 0
        lap_txt = f"LAP {lap}/{total_laps}" if total_laps else f"LAP {lap}"
        if R.under_caution(t_mid):
            lap_txt = "CAUTION"
        title = (f"A RACE IN {int(total)}", race["track_short"], f"#{me_num} {me_name}" if me is not None else "") if n == 0 else None
        ov = vg.overlay(title=title, header=header, pos=pos, lap=lap_txt, tag=tag, main=main, me_color=me_color)
        ov_p = gfx / f"beat_{n:02d}.png"
        ov.save(ov_p)
        is_last = n == len(clips) - 1
        hold = float(sc.get("end_card_seconds", 2.2)) if is_last else 0.0
        out_len = L + hold
        args = ["-i", str(src), "-loop", "1", "-t", f"{out_len:.3f}", "-i", str(ov_p),
                "-loop", "1", "-t", f"{out_len:.3f}", "-i", str(bar_p)]
        filters = video_chain("[0:v]", ss, L, "[v0]")
        cur = "[v0]"
        if hold:
            filters.append(f"{cur}tpad=stop_mode=clone:stop_duration={hold:.3f}[v0h]")
            cur = "[v0h]"
        # caption pops in quickly; title stays the whole first beat
        filters.append("[1:v]format=rgba,fade=t=in:st=0.05:d=0.15:alpha=1"
                       + (f",fade=t=out:st={L - 0.2:.2f}:d=0.2:alpha=1" if hold else "") + "[ov]")
        filters.append(f"{cur}[ov]overlay=x=0:y='{vg.px(30)}*pow(max(0,1-t/0.25),2)'[v1]")
        cur = "[v1]"
        if hold:  # end card over the frozen last frame
            fin = finish_pos(race, me) if me is not None else 0
            podium = []
            for r in sorted((r for r in race.get("results") or [] if r.get("pos", 0) > 0), key=lambda r: r["pos"])[:3]:
                podium.append(f"P{r['pos']}  {short_name(race, r['idx']).upper()}")
            ec = vg.end_card(race["track_short"], f"#{me_num} {me_name}" if me is not None else "", fin, podium, me_color)
            ec_p = gfx / "end.png"
            ec.save(ec_p)
            args += ["-loop", "1", "-t", f"{out_len:.3f}", "-i", str(ec_p)]
            filters.append(f"[3:v]format=rgba,fade=t=in:st={L:.2f}:d=0.25:alpha=1[ec]")
            filters.append(f"{cur}[ec]overlay=0:0:enable='gte(t,{L:.2f})'[v2]")
            cur = "[v2]"
        # race progress bar across the top
        filters.append("[2:v]format=rgba[bar]")
        filters.append(f"{cur}[bar]overlay=x='-W+W*min(1,({t_global:.3f}+t)/{total:.3f})':y=0[v3]")
        filters.append("[v3]fade=t=in:st=0:d=0.12:color=white,format=yuv420p[vout]")  # flash on each cut
        if info["audio"]:
            gv = float(sc.get("game_volume", 1.0))
            filters.append(f"[0:a]atrim=start={ss:.3f}:duration={L:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                           f"aformat=channel_layouts=stereo,volume={gv},afade=t=in:d=0.05,apad=whole_dur={out_len:.3f}[a]")
        else:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f"[{4 if hold else 3}:a]atrim=duration={out_len:.3f}[a]")
        out = out_dir / f"seg_{n:02d}.mp4"
        run_ff(args + ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[a]",
                       "-t", f"{out_len:.3f}"] + enc + [str(out)], f"beat {n + 1}")
        segs.append(out)
        # a clean copy (vertical, no text) for TikTok's own editor
        clean = clean_dir / f"beat_{n + 1:02d}_{(c.get('kind') or 'moment')}.mp4"
        cf = video_chain("[0:v]", ss, L, "[cv]") + ["[cv]format=yuv420p[vout]"]
        amap = []
        if info["audio"]:
            cf.append(f"[0:a]atrim=start={ss:.3f}:duration={L:.3f},asetpts=PTS-STARTPTS,aresample=48000[ca]")
            amap = ["-map", "[ca]"]
        run_ff(["-i", str(src), "-filter_complex", ";".join(cf), "-map", "[vout]"] + amap +
               ["-t", f"{L:.3f}"] + enc + [str(clean)], f"clean beat {n + 1}")
        progress(f"  beat {n + 1}/{len(clips)}: {tag} {main}".rstrip())
        t_global += out_len

    lst = out_dir / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segs), encoding="utf-8")
    final = proj.path(f"{proj.dir.name}_race_in_{int(total)}.mp4")
    music = str(sc.get("music", "") or "").strip()
    if music and Path(music).exists():
        mv = float(sc.get("music_volume", 0.35))
        run_ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-stream_loop", "-1", "-i", music,
                "-filter_complex", f"[1:a]volume={mv},afade=t=out:st={max(0, t_global - 1.5):.2f}:d=1.5[m];"
                                   f"[0:a][m]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{t_global:.3f}", "-movflags", "+faststart", str(final)], "music mix")
    else:
        run_ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(final)], "final short")
    if t_global > total + 1:
        progress(f"  note: it came out {t_global:.0f}s (target {total:.0f}s) - fewer beats or a lower beat_seconds will tighten it")
    return final
