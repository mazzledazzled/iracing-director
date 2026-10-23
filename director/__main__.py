"""iRacing Replay Director

    python -m director scan          open your replay in iRacing first
    python -m director plan
    python -m director capture       OBS must be open
    python -m director commentary    Ollama must be running
    python -m director voice
    python -m director render
    python -m director all           everything above in order
    python -m director remake        everything except the scan (after changing settings)
    python -m director fake          build a test video from a made-up race (no iRacing needed)
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import load_config, new_project, open_project


def step(title):
    print(f"\n== {title} ==")


def cmd_scan(cfg, args):
    from .sim import Sim

    step("Scanning the replay")
    sim = Sim().connect()
    try:
        info = sim.describe()
        name = args.name or f"{info['track_short']}-{info['subsession'] or time.strftime('%Y%m%d-%H%M')}"
        proj = new_project(cfg, name)
        print(f"  {info['track']}  |  {len(info['drivers'])} drivers  |  project: {proj.dir}")
        data = sim.scan(info, speed=int(cfg.scan.speed))
        n = len(data["samples"]["t"])
        if n < 50:
            raise SystemExit("Scan captured almost nothing. Is the replay paused on a menu? Try again.")
        info.update(data)
        if cfg.scan.use_iracing_incidents:
            info["iracing_incidents"] = sim.iracing_incidents(info)
        proj.save("race.json", info)
        print(f"  saved {n} samples")
    finally:
        sim.close()
    return proj


def cmd_plan(cfg, args, proj=None):
    from .detect import detect_all
    from .plan import make_plan

    proj = proj or open_project(cfg, args.name)
    step("Finding highlights")
    race = proj.load("race.json")
    det = detect_all(race, cfg)
    proj.save("events.json", det)
    counts = {}
    for e in det["events"]:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    print("  found: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    plan = make_plan(race, det, cfg)
    proj.save("plan.json", plan)
    print(f"  edit: {len(plan['clips'])} clips, {plan['total_seconds'] / 60:.1f} min")
    for c in plan["clips"]:
        print(f"   {c['id'] + 1:>2}. {fmt_t(c['start'])}-{fmt_t(c['end'])}  {c['label']}")
    return proj


def cmd_review(cfg, args, proj=None):
    from .review import review

    proj = proj or open_project(cfg, args.name)
    check_fresh(proj)
    step("Review the highlights in your browser")
    review(proj, cfg)
    if proj.has("clips.json"):
        print("  next: run.bat capture  (only new or changed clips are recorded), then commentary, voice, render")
    return proj


def maybe_review(cfg, args, proj):
    if cfg.highlights.get("review_before_capture", True) and not getattr(args, "no_review", False):
        cmd_review(cfg, args, proj)


def cmd_capture(cfg, args, proj=None):
    from .capture import capture

    proj = proj or open_project(cfg, args.name)
    check_fresh(proj)
    step("Recording clips with OBS")
    only = [int(x) - 1 for x in args.clips.split(",")] if getattr(args, "clips", None) else None
    capture(proj, proj.load("race.json"), proj.load("plan.json"), cfg, only=only, force=getattr(args, "force", False))
    return proj


def check_fresh(proj):
    """Stop if the highlights were found by an older version of the app."""
    from .detect import DETECT_VERSION

    if proj.load("events.json").get("version") != DETECT_VERSION:
        raise SystemExit(
            "This race's highlights were picked by an older version of the app (that's where the wrong winner came from).\n"
            "Run:  run.bat remake   (re-picks the highlights and re-records the clips; no new scan needed)")


def cmd_commentary(cfg, args, proj=None):
    from .commentary import write_script

    proj = proj or open_project(cfg, args.name)
    check_fresh(proj)
    step("Writing commentary")
    script = write_script(proj.load("race.json"), proj.load("events.json"), proj.load("plan.json"), cfg)
    proj.save("script.json", script)
    print(f"  script saved to {proj.path('script.json')} - edit any line there, then run 'voice' again")
    return proj


def cmd_voice(cfg, args, proj=None):
    from .tts import voice_script

    proj = proj or open_project(cfg, args.name)
    step("Recording the commentator")
    proj.save("voice.json", voice_script(proj, proj.load("script.json"), cfg))
    return proj


def cmd_render(cfg, args, proj=None):
    from .render import render

    proj = proj or open_project(cfg, args.name)
    check_fresh(proj)
    step("Rendering the video")
    out = render(proj, proj.load("race.json"), proj.load("events.json"), proj.load("plan.json"),
                 proj.load("voice.json"), cfg)
    print(f"\n  DONE: {out}")
    return proj


APPS_TO_CLOSE = {"iRacingSim64DX11.exe": "iRacing", "iRacingUI.exe": "iRacing UI", "obs64.exe": "OBS"}


def _running(exe: str) -> bool:
    import subprocess

    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"], capture_output=True, text=True).stdout
    return exe.lower() in out.lower()


def free_gpu(cfg):
    """Close iRacing and OBS after recording so the commentary model and voice get the GPU memory."""
    import subprocess
    import sys
    import time

    print("\n== Recording done ==")
    if sys.platform != "win32":
        return
    if not cfg.capture.get("auto_close_apps", True):
        print("  Close iRacing and OBS now (frees GPU memory for the commentary and voice),")
        try:
            input("  then press Enter to continue... ")
        except EOFError:
            pass
        return
    targets = [exe for exe in APPS_TO_CLOSE if _running(exe)]
    if not targets:
        return
    print("  closing " + ", ".join(APPS_TO_CLOSE[e] for e in targets) + " to free GPU memory...")
    for exe in targets:  # polite close first
        subprocess.run(["taskkill", "/IM", exe], capture_output=True)
    deadline = time.time() + 20
    while time.time() < deadline and any(_running(e) for e in targets):
        time.sleep(1)
    for exe in targets:  # anything still open gets closed for real
        if _running(exe):
            subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True)
    time.sleep(3)  # let the driver release the memory
    still = [APPS_TO_CLOSE[e] for e in targets if _running(e)]
    if still:
        print("  couldn't close " + ", ".join(still) + " - please close it, then press Enter.")
        try:
            input()
        except EOFError:
            pass
    else:
        print("  done")


def cmd_all(cfg, args):
    proj = cmd_scan(cfg, args)
    cmd_plan(cfg, args, proj)
    maybe_review(cfg, args, proj)
    cmd_capture(cfg, args, proj)
    free_gpu(cfg)
    for f in (cmd_commentary, cmd_voice, cmd_render):
        f(cfg, args, proj)


def cmd_remake(cfg, args):
    """Everything after the scan, on the latest (or --name) project. Use after changing settings."""
    proj = open_project(cfg, args.name)
    cmd_plan(cfg, args, proj)
    maybe_review(cfg, args, proj)
    cmd_capture(cfg, args, proj)
    free_gpu(cfg)
    for f in (cmd_commentary, cmd_voice, cmd_render):
        f(cfg, args, proj)


def cmd_short(cfg, args):
    """'A race in 30': vertical short of one driver's race, no commentary."""
    from .detect import DETECT_VERSION, detect_all
    from .short import MANIFEST, PLAN, plan_short, render_short, resolve_driver

    sc = cfg.get("short") or {}
    proj = open_project(cfg, args.name) if args.reuse else cmd_scan(cfg, args)
    race = proj.load("race.json")
    if not proj.has("events.json") or proj.load("events.json").get("version") != DETECT_VERSION:
        step("Finding the action")
        proj.save("events.json", detect_all(race, cfg))
    det = proj.load("events.json")
    driver = resolve_driver(race, args.driver or sc.get("driver", "auto"))
    seconds = float(args.seconds or sc.get("seconds", 30))
    step(f"Picking the moments for a {seconds:.0f}-second short")
    plan = plan_short(race, det, cfg, driver, seconds)
    proj.save(PLAN, plan)
    who = next((f"#{d['number']} {d['name']}" for d in race["drivers"] if d["idx"] == driver), "the whole field")
    print(f"  driver: {who}  |  {len(plan['clips'])} beats")
    for c in plan["clips"]:
        print(f"   {c['id'] + 1:>2}. {fmt_t(c['start'])}  {c['kind']:<9} {c['shots'][0]['cam_name']:<10} {c['end'] - c['start']:.1f}s")
    if sc.get("review", True) and not args.no_review:
        from .review import review

        step("Review the beats in your browser (angles, timing, captions)")
        plan = review(proj, cfg, plan_name=PLAN, manifest_name=MANIFEST)
        plan.setdefault("driver", driver)
        plan.setdefault("seconds", seconds)
        proj.save(PLAN, plan)
    step("Recording the beats with OBS")
    if getattr(args, "fake_capture", False):
        _fake_footage(proj, plan, MANIFEST)
    else:
        from .capture import capture

        capture(proj, race, plan, cfg, manifest_name=MANIFEST, prefix="beat", force=args.force)
    step("Building the vertical video")
    out = render_short(proj, race, det, proj.load(PLAN), cfg)
    print(f"\n  DONE: {out}")
    print(f"  clean vertical clips for TikTok's editor: {proj.path('short') / 'clean'}")


def _fake_footage(proj, plan, manifest_name):
    """Test-pattern stand-in for OBS recordings (testing without iRacing)."""
    from .capture import clip_sig
    from .render import run_ff

    man = {}
    for c in plan["clips"]:
        dur = c["end"] - c["start"] + 0.5
        out = proj.path("clips") / f"beat_{c['id']:02d}_test.mp4"
        run_ff(["-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={dur:.2f}",
                "-f", "lavfi", "-i", f"sine=frequency={300 + 40 * c['id']}:d={dur:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)], "test clip")
        man[str(c["id"])] = dict(file=out.name, session_start=c["start"] - 0.5, wall_seconds=dur, sig=clip_sig(c))
    proj.save(manifest_name, man)


def cmd_fake(cfg, args):
    """End-to-end test with a synthetic race and generated footage."""
    from .fake import fake_race
    from .render import run_ff

    proj = new_project(cfg, args.name or "fake-race")
    race = fake_race()
    proj.save("race.json", race)
    cmd_plan(cfg, args, proj)
    step("Generating stand-in footage (instead of OBS)")
    plan = proj.load("plan.json")
    man = {}
    for c in plan["clips"]:
        dur = c["end"] - c["start"] + 0.5
        out = proj.path("clips") / f"clip_{c['id']:02d}.mp4"
        run_ff(["-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={dur:.2f}",
                "-f", "lavfi", "-i", f"sine=frequency={300 + 40 * c['id']}:d={dur:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)], "test clip")
        from .capture import clip_sig

        man[str(c["id"])] = dict(file=out.name, session_start=c["start"] - 0.5, wall_seconds=dur, sig=clip_sig(c))
    proj.save("clips.json", man)
    print(f"  {len(man)} test clips")
    cmd_commentary(cfg, args, proj)
    cmd_voice(cfg, args, proj)
    cmd_render(cfg, args, proj)


def fmt_t(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


class Tee:
    """Mirror everything printed into output/progress.log, so a skill (or you) can follow along."""

    def __init__(self, stream, path):
        self.stream, self.f = stream, open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        try:
            self.stream.write(s)
        except UnicodeEncodeError:
            self.stream.write(s.encode("ascii", "replace").decode())
        self.f.write(s)

    def flush(self):
        self.stream.flush()
        self.f.flush()


def main(argv=None):
    import time

    p = argparse.ArgumentParser(prog="director", description="iRacing replay -> highlights video with AI commentary")
    p.add_argument("command", choices=["scan", "plan", "review", "capture", "commentary", "voice", "render", "all", "remake", "short", "fake", "dashboard"])
    p.add_argument("--name", help="project name (default: latest project)")
    p.add_argument("--clips", help="capture only these clip numbers, e.g. 3,5")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--voice", help="override tts engine (kokoro/piper/sapi/test)")
    p.add_argument("--no-review", dest="no_review", action="store_true", help="skip the review page in all/remake")
    p.add_argument("--force", action="store_true", help="capture: re-record every clip, even unchanged ones")
    p.add_argument("--featured", help='featured driver: "all" (everyone equal), "auto" (who recorded it) or a car number')
    p.add_argument("--length", type=float, help="highlights video: target length in seconds")
    p.add_argument("--driver", help='short: whose race (car number or name; default: whoever recorded the replay)')
    p.add_argument("--seconds", type=float, help="short: target length (default 30)")
    p.add_argument("--reuse", action="store_true", help="short: use the last scanned race instead of scanning again")
    p.add_argument("--no-browser", dest="no_browser", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--fake-capture", dest="fake_capture", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--persona", help='commentary voice, e.g. "gilmore", or "none" for the normal announcer')
    args = p.parse_args(argv)
    if args.command == "dashboard":
        from .dashboard import serve

        return serve(open_browser=not args.no_browser)
    cfg = load_config(args.config)
    if args.voice:
        cfg.tts["engine"] = args.voice
    if args.persona:
        cfg.commentary["persona"] = args.persona
    if args.featured:  # all = every driver equal, auto = whoever recorded the replay, or a car number
        f = args.featured.strip()
        cfg.focus["car_number"] = "" if f.lower() == "all" else f
        if not args.driver:
            args.driver = f
    if args.length:
        cfg.highlights["target_seconds"] = float(args.length)

    from .config import output_root

    log_path = output_root(cfg) / "progress.log"
    if args.command in ("all", "remake", "fake"):
        log_path.write_text("", encoding="utf-8")  # fresh log for a full run
    sys.stdout = Tee(sys.stdout, log_path)
    print(f"\n[{time.strftime('%H:%M:%S')}] START {args.command}" + (f" persona={args.persona}" if args.persona else ""))
    cmds = dict(scan=cmd_scan, plan=cmd_plan, review=cmd_review, capture=cmd_capture, commentary=cmd_commentary,
                voice=cmd_voice, render=cmd_render, all=cmd_all, remake=cmd_remake, short=cmd_short, fake=cmd_fake)
    try:
        cmds[args.command](cfg, args)
        print(f"[{time.strftime('%H:%M:%S')}] FINISHED {args.command}")
    except KeyboardInterrupt:
        print("\nStopped.\nERROR: stopped by user")
        sys.exit(1)
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"\nERROR: {e.code}")
        raise
    except Exception as e:  # clean message on screen, full details in error.log
        import traceback

        log = output_root(cfg) / "error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"\nERROR: {e}\n(full details in {log})")
        sys.exit(1)


if __name__ == "__main__":
    main()
